// Shared helpers for /ui and /admin-ui. Token lives in sessionStorage only.
const TOKEN_KEY = "dockerserver_token";

function getToken() { try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; } }
function setToken(t) { try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); } catch {} }

async function api(path, opts = {}) {
  const headers = Object.assign({ Authorization: "Bearer " + getToken() }, opts.headers || {});
  let body = opts.body;
  if (body && typeof body === "object" && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const r = await fetch(path, { method: opts.method || "GET", headers, body });
  const text = await r.text();
  let data = text;
  if (!opts.raw || !r.ok) { try { data = JSON.parse(text); } catch {} }
  if (!r.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : text;
    const e = new Error(`${r.status}: ${detail}`);
    e.status = r.status;
    throw e;
  }
  return data;
}

// Build DOM safely (text only, never innerHTML with data).
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) n.append(c instanceof Node ? c : String(c));
  return n;
}
const $ = (id) => document.getElementById(id);
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

function fmtH(h, d = 4) { return h == null ? "–" : Number(h).toFixed(d); }
function fmtTime(t) { return t ? t.replace("T", " ").slice(0, 19) : "–"; }
function fmtDur(s) {
  if (s == null) return "–";
  if (s < 60) return s.toFixed(1) + "s";
  if (s < 3600) return (s / 60).toFixed(1) + "m";
  return (s / 3600).toFixed(2) + "h";
}
function statusBadge(s) { return el("span", { class: "badge s-" + s }, s); }

function table(headers, rows) {
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map(h => el("th", { class: h.num ? "num" : "" }, h.label || h)))),
    el("tbody", {}, rows));
}
function td(v, cls) { return el("td", { class: cls || "" }, v); }

function showMsg(container, text, ok = false) {
  const m = el("div", { class: "msg " + (ok ? "ok" : "err") }, text);
  container.prepend(m);
  setTimeout(() => m.remove(), ok ? 4000 : 8000);
}

// Login flow: verifies the token with /me; `requireAdmin` rejects non-admin tokens.
async function initLogin({ requireAdmin, onReady }) {
  const login = $("login"), app = $("app");
  async function tryStart() {
    try {
      const me = await api("/me");
      if (requireAdmin && me.role !== "admin") throw new Error("this token is not an admin token");
      login.classList.add("hidden"); app.classList.remove("hidden");
      $("who").textContent = `${me.name} (${me.role})`;
      onReady(me);
    } catch (e) {
      setToken(null);
      login.classList.remove("hidden"); app.classList.add("hidden");
      $("login-err").textContent = e.status === 401 ? "Invalid or revoked token." : (e.message || "");
    }
  }
  $("login-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    setToken($("token-input").value.trim());
    $("token-input").value = "";
    tryStart();
  });
  $("logout").addEventListener("click", () => { setToken(null); location.reload(); });
  if (getToken()) {
    tryStart();
  } else {
    login.classList.remove("hidden");   // no token yet: show the sign-in form
    $("token-input").focus();
  }
}

function setupTabs(onShow) {
  document.querySelectorAll("nav button[data-tab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("nav button[data-tab]").forEach(x => x.classList.toggle("active", x === b));
    document.querySelectorAll("section[data-tab]").forEach(s => s.classList.toggle("hidden", s.dataset.tab !== b.dataset.tab));
    onShow(b.dataset.tab);
  }));
}

function jobsTable(jobs, { showUser = false, userNames = {}, onSelect } = {}) {
  const headers = ["ID"].concat(showUser ? ["User"] : []).concat(["Status", "Command", "CPU", "MB",
    "Started", { label: "Run", num: true }, { label: "Cost h", num: true }, { label: "CPU% avg/peak", num: true }, { label: "Mem MB peak", num: true }]);
  const rows = jobs.map(j => el("tr", { class: "clickable", onclick: () => onSelect && onSelect(j) },
    td(j.id),
    showUser ? td(userNames[j.user_id] || j.user_id) : null,
    td(statusBadge(j.status)),
    td(j.command.length > 60 ? j.command.slice(0, 60) + "…" : j.command, "wrap"),
    td(j.cpus), td(j.mem_mb),
    td(fmtTime(j.started_at)),
    td(fmtDur(j.duration_seconds), "num"),
    td(fmtH(j.cost_hours), "num"),
    td(j.cpu_avg_pct == null ? "–" : `${j.cpu_avg_pct.toFixed(0)} / ${j.cpu_peak_pct.toFixed(0)}`, "num"),
    td(j.mem_peak_mb == null ? "–" : j.mem_peak_mb.toFixed(0), "num")));
  return jobs.length ? table(headers, rows) : el("p", { class: "muted" }, "No jobs.");
}

async function showJobDetail(container, job, { canStop = true, onChange } = {}) {
  clear(container).classList.remove("hidden");
  const logs = el("pre", { class: "logs" }, "loading…");
  const stopBtn = el("button", { class: "btn danger", onclick: async () => {
    stopBtn.disabled = true;
    try { await api(`/jobs/${job.id}/stop`, { method: "POST" }); onChange && onChange(); }
    catch (e) { showMsg(container, e.message); }
  } }, "Stop job");
  const active = ["queued", "preparing", "running"].includes(job.status);
  container.append(
    el("h2", {}, `Job ${job.id} `, statusBadge(job.status)),
    el("p", {}, el("span", { class: "muted" }, "Command: "), el("code", {}, job.command)),
    el("p", { class: "muted" }, `image ${job.image} · ${job.cpus} CPU · ${job.mem_mb} MB · timeout ${job.timeout_seconds}s · exit ${job.exit_code ?? "–"}`
      + (job.git_url ? ` · git ${job.git_url}` : "") + (job.error ? ` · error: ${job.error}` : "")),
    el("div", { class: "row" },
      el("button", { class: "btn", onclick: () => showJobDetail(container, job, { canStop, onChange }) }, "Refresh logs"),
      canStop && active ? stopBtn : null),
    el("h3", {}, "Logs (last 500 lines)"), logs);
  try { logs.textContent = (await api(`/jobs/${job.id}/logs?tail=500`, { raw: true })) || "(empty)"; }
  catch (e) { logs.textContent = e.message; }
}
