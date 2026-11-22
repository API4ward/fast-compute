"""Inbound path to a service job. The app has no route out; requests reach it like this:

  bridge (tailnet IP) -> Forwarder port on this node -> 127.0.0.1:<published relay port>
    -> relay container (edge network) -> app container (app network)

  * app network: Docker "internal" with no host address (inhibit_ipv4), so the app cannot reach
    the Docker host through a gateway. Its only other member is the relay.
  * edge network: holds only the relay, so no other container can reach the relay's listener.
  * relay: trusted fixed code, listens on its edge address only and forwards to one ip:port.
  * Forwarder: one port per app, accepts only the configured bridge addresses. One port per app
    means a pooled upstream connection in the bridge's proxy can never cross between apps.
"""
import asyncio
import logging
import threading

log = logging.getLogger("computenode.ingress")
RELAY_PORT = 9000

# Runs inside the relay container. Binds before the app network is attached, so the listener
# exists only on the edge address. Prints "ready" once listening. Every connection must open
# with the per-service secret: Docker runtimes differ in whether other bridges can reach a
# published container port (and may NAT them to the gateway), so the network is not the check.
RELAY_CODE = r"""
import hmac, os, socket, threading
target = (os.environ["TARGET_HOST"], int(os.environ["TARGET_PORT"]))
secret = os.environ.pop("RELAY_SECRET").encode()
def authorized(c):
    c.settimeout(5)
    got = b""
    try:
        while len(got) < len(secret):
            d = c.recv(len(secret) - len(got))
            if not d:
                return False
            got += d
    except OSError:
        return False
    c.settimeout(None)
    return hmac.compare_digest(got, secret)
def pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass
def serve(c):
    if not authorized(c):
        c.close()
        return
    try:
        u = socket.create_connection(target, timeout=5)
        u.settimeout(None)
    except OSError:
        c.close()
        return
    t = threading.Thread(target=pump, args=(u, c), daemon=True)
    t.start()
    pump(c, u)
    t.join()
    c.close()
    u.close()
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((socket.gethostbyname(socket.gethostname()), %d))
s.listen(128)
print("ready", flush=True)
while True:
    c, _ = s.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
""" % RELAY_PORT


class Forwarder:
    """TCP forwarders on one background event loop (no thread per connection)."""

    def __init__(self, allowed_clients: list[str], max_conns: int = 64):
        self.allowed = set(allowed_clients)
        self.max_conns = max_conns
        self._loop = asyncio.new_event_loop()
        self._servers: dict[int, asyncio.base_events.Server] = {}
        self._conns: dict[int, set[asyncio.Task]] = {}
        threading.Thread(target=self._loop.run_forever, name="ingress", daemon=True).start()

    def _call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def open(self, host: str, port: int, target_port: int, preamble: bytes = b""):
        """Forward host:port to 127.0.0.1:target_port, sending preamble first on each connection."""
        self._call(self._open(host, port, target_port, preamble))

    def close(self, port: int):
        self._call(self._close(port))

    def active(self, port: int) -> int:
        return len(self._conns.get(port, ()))

    def shutdown(self):
        for port in list(self._servers):
            try:
                self.close(port)
            except Exception:  # noqa: BLE001
                log.exception("closing ingress port %s", port)
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _open(self, host, port, target_port, preamble):
        conns: set[asyncio.Task] = set()

        async def handle(reader, writer):
            task = asyncio.current_task()
            peer = (writer.get_extra_info("peername") or ("", 0))[0]
            if peer not in self.allowed or len(conns) >= self.max_conns:
                writer.transport.abort()
                return
            conns.add(task)
            up = None
            try:
                up_r, up = await asyncio.open_connection("127.0.0.1", target_port)
                if preamble:
                    up.write(preamble)
                    await up.drain()
                await asyncio.gather(_pump(reader, up), _pump(up_r, writer))
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                conns.discard(task)
                for w in (writer, up):
                    if w is not None:
                        w.transport.abort()

        self._conns[port] = conns
        self._servers[port] = await asyncio.start_server(handle, host, port)

    async def _close(self, port):
        srv = self._servers.pop(port, None)
        if srv is not None:
            srv.close()
        conns = self._conns.pop(port, set())
        for t in list(conns):
            t.cancel()
        if conns:
            await asyncio.gather(*conns, return_exceptions=True)


async def _pump(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, RuntimeError):
        writer.transport.abort()
