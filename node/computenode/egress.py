"""Outbound path for network=true jobs. The job has no route out; traffic goes like this:

  job container (internal per-job network) -> egress proxy container -> public internet

  * job network: Docker "internal" with no host address (inhibit_ipv4), so the job cannot reach
    the Docker host, the LAN, the tailnet or other jobs. Its only other member is the proxy.
  * proxy: fixed code, HTTP forward + CONNECT on allowed ports only. It resolves the name itself,
    refuses the request if ANY resolved address is not public IPv4, and connects to the address
    it checked, so a DNS answer cannot change between the check and the connection.
"""

EGRESS_PROXY_PORT = 3128

# Runs inside the proxy container. Binds while the job network is its only network, so the
# listener exists only there. Prints "ready" once listening.
EGRESS_CODE = r"""
import ipaddress, os, socket, threading
from urllib.parse import urlsplit

MAX_HEAD = 16384
MAX_CONNS = 64
HOP_BY_HOP = {"connection", "proxy-connection", "proxy-authorization", "keep-alive", "te",
              "trailer", "upgrade"}


class Denied(Exception):
    pass


def public_ipv4(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_global and not ip.is_multicast


def resolve(host, port, ports):
    if port not in ports:
        raise Denied("port not allowed")
    if not host or len(host) > 253 or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in host):
        raise Denied("bad host")
    # Resolvers disagree on octal/hex/short numeric hosts, so only a canonical dotted quad passes.
    last = host.rstrip(".").rsplit(".", 1)[-1].lower()
    if last.isdigit() or last.startswith("0x"):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise Denied("bad host")
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise Denied("name did not resolve")
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    if not addrs or not all(public_ipv4(a) for a in addrs):
        raise Denied("destination not allowed")
    return addrs[0]


def parse_head(head):
    # Returns (method, host, port, first_bytes_for_upstream or None for CONNECT).
    try:
        text = head.decode("latin-1")
        lines = text[:-4].split("\r\n")
        method, target, version = lines[0].split(" ")
    except ValueError:
        raise Denied("bad request")
    if "\n" in "".join(lines) or "\r" in "".join(lines) or not version.startswith("HTTP/1."):
        raise Denied("bad request")
    if method == "CONNECT":
        host, sep, port = target.rpartition(":")
        if not sep or not port.isascii() or not port.isdigit():
            raise Denied("bad target")
        return method, host, int(port), None
    try:
        u = urlsplit(target)
        host, port = u.hostname, u.port or 80
    except ValueError:
        raise Denied("bad target")
    if u.scheme != "http" or not host:
        raise Denied("only absolute http:// URLs or CONNECT")
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    kept = [l for l in lines[1:] if l.split(":", 1)[0].strip().lower() not in HOP_BY_HOP]
    out = [f"{method} {path} {version}"] + kept + ["Connection: close", "", ""]
    return method, host, port, "\r\n".join(out).encode("latin-1")


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


def serve(c, ports, slots):
    u = None
    try:
        c.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(4096)
            if not d or len(buf) + len(d) > MAX_HEAD:
                return
            buf += d
        end = buf.index(b"\r\n\r\n") + 4
        head, rest = buf[:end], buf[end:]
        try:
            method, host, port, first = parse_head(head)
            addr = resolve(host, port, ports)
        except Denied as e:
            body = (str(e) + "\n").encode()
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Type: text/plain\r\n"
                      b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            return
        try:
            u = socket.create_connection((addr, port), timeout=10)
        except OSError:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            return
        u.settimeout(None)
        c.settimeout(None)
        if method == "CONNECT":
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            u.sendall(first)
        if rest:
            u.sendall(rest)
        t = threading.Thread(target=pump, args=(u, c), daemon=True)
        t.start()
        pump(c, u)
        t.join()
    except OSError:
        pass
    finally:
        slots.release()
        c.close()
        if u is not None:
            u.close()


def main():
    ports = {int(p) for p in os.environ["EGRESS_PORTS"].split(",")}
    slots = threading.BoundedSemaphore(MAX_CONNS)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((socket.gethostbyname(socket.gethostname()), int(os.environ["EGRESS_PORT"])))
    s.listen(128)
    print("ready", flush=True)
    while True:
        c, _ = s.accept()
        if not slots.acquire(blocking=False):
            c.close()
            continue
        threading.Thread(target=serve, args=(c, ports, slots), daemon=True).start()


if __name__ == "__main__":
    main()
"""
