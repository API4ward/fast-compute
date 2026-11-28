// Shared helpers for /ui, /admin-ui and /welcome. Token lives in sessionStorage only.
const TOKEN_KEY = "dockerserver_token";
const LANG_KEY = "fastcompute_lang";   // shared with the homepage (home.js) and welcome.js

function getToken() { try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; } }
function setToken(tok) { try { tok ? sessionStorage.setItem(TOKEN_KEY, tok) : sessionStorage.removeItem(TOKEN_KEY); } catch {} }

// ----- i18n: t(key, vars) over per-page dictionaries (addI18n); data-i18n* attributes for static markup -----
const LANG = (() => {
  try { const l = localStorage.getItem(LANG_KEY); if (l === "en" || l === "zh") return l; } catch {}
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
})();
const I18N = { en: {}, zh: {} };
function addI18n(dicts) { for (const l of Object.keys(I18N)) Object.assign(I18N[l], dicts[l] || {}); }
function hasT(key) { return I18N.en[key] != null; }
function t(key, vars) {
  let s = I18N[LANG][key];
  if (s == null) s = I18N.en[key];
  if (s == null) return key;
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] == null ? m : String(vars[k]))) : s;
}
// `code` and **bold** inside dictionary strings become DOM nodes (never innerHTML).
function rich(text) {
  const frag = document.createDocumentFragment();
  for (const part of String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/)) {
    if (!part) continue;
    if (part[0] === "`") frag.append(el("code", {}, part.slice(1, -1)));
    else if (part.startsWith("**")) frag.append(el("b", {}, part.slice(2, -2)));
    else frag.append(part);
  }
  return frag;
}
function tr(key, vars) { return rich(t(key, vars)); }
function setLang(lang) {
  try { localStorage.setItem(LANG_KEY, lang); } catch {}
  location.reload();
}
function applyI18n(root = document) {
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  root.querySelectorAll("[data-i18n]").forEach(n => { n.textContent = t(n.dataset.i18n); });
  root.querySelectorAll("[data-i18n-rich]").forEach(n => { clear(n).append(tr(n.dataset.i18nRich)); });
  root.querySelectorAll("[data-i18n-ph]").forEach(n => { n.placeholder = t(n.dataset.i18nPh); });
  root.querySelectorAll("[data-i18n-title]").forEach(n => { n.title = t(n.dataset.i18nTitle); });
  root.querySelectorAll("[data-i18n-aria]").forEach(n => { n.setAttribute("aria-label", t(n.dataset.i18nAria)); });
  root.querySelectorAll("[data-lang-switch]").forEach(n => {
    n.textContent = LANG === "zh" ? "EN" : "中文";
    n.title = t("lang.switch");
    n.setAttribute("aria-label", t("lang.switch"));
    n.onclick = () => setLang(LANG === "zh" ? "en" : "zh");
  });
}

addI18n({
  en: {
    "lang.switch": "切换到中文 (Switch to Chinese)",
    "brand.admin": "Admin",
    "login.token": "Access token",
    "login.submit": "Sign in",
    "login.session": "The token is kept in this tab's session storage only.",
    "login.invalid": "Invalid or revoked token.",
    "login.notAdmin": "This token is not an admin token.",
    "login.home": "← Back to the homepage",
    "btn.refresh": "Refresh", "btn.signout": "Sign out", "btn.save": "Save", "btn.delete": "Delete",
    "btn.cancel": "Cancel", "btn.close": "Close", "btn.edit": "Edit", "btn.copy": "Copy", "btn.copied": "Copied",
    "btn.apply": "Apply", "btn.create": "Create", "btn.run": "Run", "btn.open": "Open",
    "role.admin": "admin", "role.user": "user",
    "yes": "yes", "no": "no", "none": "none", "loading": "loading…", "never": "never",
    "status.queued": "queued", "status.preparing": "preparing", "status.running": "running",
    "status.succeeded": "succeeded", "status.failed": "failed", "status.timeout": "timeout", "status.stopped": "stopped",
    "status.out_of_credits": "out of credits", "status.killed_by_admin": "killed by admin", "status.lost": "lost",
    "status.error": "error",
    "unit.s": "{v}s", "unit.m": "{v}m", "unit.h": "{v}h",
    "col.id": "ID", "col.user": "User", "col.status": "Status", "col.image": "Image", "col.command": "Command",
    "col.cpu": "CPU", "col.mb": "MB", "col.started": "Started", "col.run": "Run", "col.cost": "Cost h",
    "col.cpuPct": "CPU% avg/peak", "col.memPeak": "Mem MB peak", "col.node": "Node",
    "jobs.none": "No jobs.",
    "job.title": "Job {id}",
    "job.command": "Command:",
    "job.meta": "image {image} · {cpus} CPU · {mem} MB · timeout {timeout}s · exit {exit}",
    "job.git": "git {url}",
    "job.error": "error: {error}",
    "job.refreshLogs": "Refresh logs",
    "job.stop": "Stop job",
    "job.logs": "Logs (last 500 lines)",
    "job.empty": "(empty)",
    "err.missing": "Not available on this server yet ({msg}). The server may need an update.",
    "err.network": "Could not reach the server: {msg}",
    "err.prefix": "Error:",
  },
  zh: {
    "lang.switch": "Switch to English（切换到英文）",
    "brand.admin": "管理后台",
    "login.token": "访问令牌",
    "login.submit": "登录",
    "login.session": "令牌只保存在当前标签页的会话存储中，关闭标签页即失效。",
    "login.invalid": "令牌无效或已被吊销。",
    "login.notAdmin": "这不是管理员令牌。",
    "login.home": "← 返回首页",
    "btn.refresh": "刷新", "btn.signout": "退出登录", "btn.save": "保存", "btn.delete": "删除",
    "btn.cancel": "取消", "btn.close": "关闭", "btn.edit": "编辑", "btn.copy": "复制", "btn.copied": "已复制",
    "btn.apply": "应用", "btn.create": "创建", "btn.run": "查询", "btn.open": "打开",
    "role.admin": "管理员", "role.user": "用户",
    "yes": "是", "no": "否", "none": "无", "loading": "加载中…", "never": "从未",
    "status.queued": "排队中", "status.preparing": "准备中", "status.running": "运行中",
    "status.succeeded": "成功", "status.failed": "失败", "status.timeout": "超时", "status.stopped": "已停止",
    "status.out_of_credits": "额度用尽", "status.killed_by_admin": "被管理员终止", "status.lost": "丢失",
    "status.error": "出错",
    "unit.s": "{v} 秒", "unit.m": "{v} 分钟", "unit.h": "{v} 小时",
    "col.id": "ID", "col.user": "用户", "col.status": "状态", "col.image": "镜像", "col.command": "命令",
    "col.cpu": "CPU", "col.mb": "内存 MB", "col.started": "开始时间", "col.run": "运行时长", "col.cost": "费用（时）",
    "col.cpuPct": "CPU% 平均/峰值", "col.memPeak": "内存峰值 MB", "col.node": "节点",
    "jobs.none": "还没有任务。",
    "job.title": "任务 {id}",
    "job.command": "命令：",
    "job.meta": "镜像 {image} · {cpus} CPU · {mem} MB · 超时 {timeout} 秒 · 退出码 {exit}",
    "job.git": "git {url}",
    "job.error": "错误：{error}",
    "job.refreshLogs": "刷新日志",
    "job.stop": "停止任务",
    "job.logs": "日志（最后 500 行）",
    "job.empty": "（空）",
    "err.missing": "服务器暂不支持此功能（{msg}），可能需要升级服务器。",
    "err.network": "无法连接服务器：{msg}",
    "err.prefix": "出错：",
  },
});

// ----- REST calls -----
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
    let detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : text;
    if (typeof detail === "string" && detail.length > 400) detail = detail.slice(0, 400) + "…";
    const e = new Error(`${r.status}: ${detail}`);
    e.status = r.status;
    e.detail = detail;
    throw e;
  }
  return data;
}

// A readable message for a failed api() call: a missing endpoint (older server) or no connection
// gets a translated explanation; anything else is the server's own message.
function errText(e) {
  if (!e) return "";
  if ((e.status === 404 && e.detail === "Not Found") || e.status === 405) return t("err.missing", { msg: e.message });
  if (e.status == null) return t("err.network", { msg: e.message || String(e) });
  return e.message;
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
function fmtTime(ts) { return ts ? ts.replace("T", " ").slice(0, 19) : "–"; }
function fmtDur(s) {
  if (s == null) return "–";
  if (s < 60) return t("unit.s", { v: s.toFixed(1) });
  if (s < 3600) return t("unit.m", { v: (s / 60).toFixed(1) });
  return t("unit.h", { v: (s / 3600).toFixed(2) });
}
// Status pill: translated label, the raw value as tooltip.
function statusBadge(s) { return el("span", { class: "badge s-" + s, title: s }, hasT("status." + s) ? t("status." + s) : s); }
function roleLabel(r) { return hasT("role." + r) ? t("role." + r) : r; }

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map(h => el("th", { class: h.num ? "num" : "" }, h.label != null ? h.label : h)))),
    el("tbody", {}, rows)));
}
function td(v, cls) { return el("td", { class: cls || "" }, v); }

function showMsg(container, text, ok = false) {
  const m = el("div", { class: "msg " + (ok ? "ok" : "err"), role: ok ? "status" : "alert" }, text);
  container.prepend(m);
  setTimeout(() => m.remove(), ok ? 4000 : 8000);
}

// A copyable code block (deploy commands and the like).
function codeBox(text) {
  const pre = el("pre", { class: "logs" }, text);
  const btn = el("button", { class: "btn copy", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(pre.textContent); btn.textContent = t("btn.copied"); setTimeout(() => btn.textContent = t("btn.copy"), 1500); }
    catch { /* clipboard unavailable (http or denied): the text is still selectable */ }
  } }, t("btn.copy"));
  return el("div", { class: "codebox" }, pre, btn);
}

// Login flow: verifies the token with /me; `requireAdmin` rejects non-admin tokens.
async function initLogin({ requireAdmin, onReady }) {
  applyI18n();
  const login = $("login"), app = $("app");
  async function tryStart() {
    try {
      const me = await api("/me");
      if (requireAdmin && me.role !== "admin") { const e = new Error(t("login.notAdmin")); e.notAdmin = true; throw e; }
      login.classList.add("hidden"); app.classList.remove("hidden");
      $("who").textContent = `${me.name} · ${roleLabel(me.role)}`;
      const av = $("who-avatar");
      if (av) av.textContent = (me.name || "?").slice(0, 1);
      onReady(me);
    } catch (e) {
      setToken(null);
      login.classList.remove("hidden"); app.classList.add("hidden");
      $("login-err").textContent = e.status === 401 ? t("login.invalid") : e.notAdmin ? e.message : errText(e);
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
    document.querySelectorAll("nav button[data-tab]").forEach(x => {
      x.classList.toggle("active", x === b);
      if (x === b) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
    });
    document.querySelectorAll("section[data-tab]").forEach(s => s.classList.toggle("hidden", s.dataset.tab !== b.dataset.tab));
    onShow(b.dataset.tab);
  }));
}

function jobsTable(jobs, { showUser = false, userNames = {}, onSelect } = {}) {
  const headers = [t("col.id")].concat(showUser ? [t("col.user")] : []).concat([t("col.status"), t("col.image"),
    t("col.command"), t("col.cpu"), t("col.mb"), t("col.started"), { label: t("col.run"), num: true },
    { label: t("col.cost"), num: true }, { label: t("col.cpuPct"), num: true }, { label: t("col.memPeak"), num: true }]);
  const rows = jobs.map(j => el("tr", { class: "clickable", tabindex: "0", onclick: () => onSelect && onSelect(j),
    onkeydown: ev => { if (ev.key === "Enter" && onSelect) onSelect(j); } },
    td(j.id),
    showUser ? td(userNames[j.user_id] || j.user_id) : null,
    td(statusBadge(j.status)),
    td(j.image),
    td(j.command.length > 60 ? j.command.slice(0, 60) + "…" : j.command, "wrap"),
    td(j.cpus), td(j.mem_mb),
    td(fmtTime(j.started_at)),
    td(fmtDur(j.duration_seconds), "num"),
    td(fmtH(j.cost_hours), "num"),
    td(j.cpu_avg_pct == null ? "–" : `${j.cpu_avg_pct.toFixed(0)} / ${j.cpu_peak_pct.toFixed(0)}`, "num"),
    td(j.mem_peak_mb == null ? "–" : j.mem_peak_mb.toFixed(0), "num")));
  return jobs.length ? table(headers, rows) : el("p", { class: "empty" }, t("jobs.none"));
}

async function showJobDetail(container, job, { canStop = true, onChange } = {}) {
  clear(container).classList.remove("hidden");
  const logs = el("pre", { class: "logs" }, t("loading"));
  const stopBtn = el("button", { class: "btn danger", onclick: async () => {
    stopBtn.disabled = true;
    try { await api(`/jobs/${job.id}/stop`, { method: "POST" }); onChange && onChange(); }
    catch (e) { showMsg(container, errText(e)); }
  } }, t("job.stop"));
  const active = ["queued", "preparing", "running"].includes(job.status);
  const meta = [t("job.meta", { image: job.image, cpus: job.cpus, mem: job.mem_mb, timeout: job.timeout_seconds, exit: job.exit_code ?? "–" })]
    .concat(job.git_url ? [t("job.git", { url: job.git_url })] : []).concat(job.error ? [t("job.error", { error: job.error })] : []);
  container.append(
    el("div", { class: "card-head" }, el("h2", {}, t("job.title", { id: job.id })), statusBadge(job.status),
      el("div", { class: "end" },
        el("button", { class: "btn sm", onclick: () => showJobDetail(container, job, { canStop, onChange }) }, t("job.refreshLogs")),
        canStop && active ? stopBtn : null,
        el("button", { class: "btn sm ghost", onclick: () => container.classList.add("hidden") }, t("btn.close")))),
    el("p", {}, el("span", { class: "muted" }, t("job.command") + " "), el("code", {}, job.command)),
    el("p", { class: "muted" }, meta.join(" · ")),
    el("h3", {}, t("job.logs")), logs);
  try { logs.textContent = (await api(`/jobs/${job.id}/logs?tail=500`, { raw: true })) || t("job.empty"); }
  catch (e) { logs.textContent = errText(e); }
}
