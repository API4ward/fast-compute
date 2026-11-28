// Admin "Settings" tab (admin.html): AWS account, Lambda nodes (diagnose, test job, function logs,
// add / edit / delete), a deploy helper for lambda-node/deploy.sh and the homepage advisor.
// Talks only to /admin/settings/*; every server or AWS value goes into the page as text (el()).
// Needs common.js and admin-i18n.js; uses selectTab() and loadNodes() from admin.html.

// The server's validation rules (admin_settings.py), for early feedback only: the server decides.
// HTML pattern attributes are compiled with the "v" flag, so "-", "(", ")" and "/" are escaped.
const SET_PAT = {
  name: "[A-Za-z0-9][A-Za-z0-9_.\\-]{0,39}",
  fn: "[A-Za-z0-9_\\-]{1,64}|arn:aws[a-z\\-]*:lambda:[a-z0-9\\-]+:[0-9]{12}:function:[A-Za-z0-9_\\-]{1,64}(:[A-Za-z0-9$_\\-]+)?",
  deployFn: "[a-z0-9]+([_\\-][a-z0-9]+)*",
  bucket: "[a-z0-9][a-z0-9.\\-]{1,61}[a-z0-9]",
  region: "[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-[0-9]",
  prefix: "fast-compute\\/[A-Za-z0-9!_.*'\\(\\)\\/\\-]{0,187}",
};
const REGION_RE = /^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$/;
// deploy.sh also names the ECR repository (lowercase letters and digits joined by single separators) and
// the IAM role "<name>-exec" (at most 64 characters) after the function: stricter than Lambda's own rule.
const DEPLOY_FN_RE = /^(?=.{2,59}$)[a-z0-9]+(?:[_-][a-z0-9]+)*$/;
const IMAGE_RE = /^[a-z0-9][a-z0-9._\/:@-]{0,199}$/;
const AWS_REGIONS = ["eu-west-2", "eu-west-1", "eu-west-3", "eu-central-1", "eu-central-2", "eu-north-1", "eu-south-1",
  "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "sa-east-1", "ap-southeast-1", "ap-southeast-2",
  "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-south-1", "ap-east-1", "me-south-1", "af-south-1"];

const SET = {
  data: null,          // last GET /admin/settings
  outs: new Map(),     // node name -> { diag, test, logs } result areas, kept across re-renders
  polls: new Map(),    // node name -> { id, timer } of a running test job
  form: null,          // { node: row | null, prefill: {}, adv: bool } while the add / edit form is open;
                       // prefill holds what was typed, so a re-render shows it again
  edits: {},           // "aws" / "advisor" -> { field name: value } typed there but not saved
  account: null,       // AWS account id from the last successful credentials check
  focus: null,         // node card to scroll to after the next render (Nodes tab -> Settings)
};
const EDIT_CARDS = { aws: "set-aws", advisor: "set-advisor" };

// ---------- small builders ----------
// Element.append() would print null / undefined as text: these skip them (like el()).
function addKids(node, ...kids) { for (const c of kids.flat()) if (c != null) node.append(c); return node; }
function setKids(node, ...kids) { return addKids(clear(node), ...kids); }
function kvRow(k, v) { return el("div", {}, el("div", { class: "k" }, k), el("div", {}, v == null || v === "" ? "–" : v)); }
function field(label, input, help, cls) {
  return el("label", { class: "field" + (cls ? " " + cls : "") }, el("span", {}, label), input, help ? el("span", { class: "help" }, help) : null);
}
function checkField(input, label, help, cls) {
  return el("label", { class: "check" + (cls ? " " + cls : "") }, input,
    el("span", {}, label, help ? el("span", { class: "help", style: "display:block" }, help) : null));
}
function alertBox(kind, text, hint) {
  return el("div", { class: "alert " + kind, role: kind === "err" ? "alert" : null }, text, hint ? el("div", { class: "hint" }, hint) : null);
}
function busyLine(text) { return el("p", { class: "summary-line muted" }, el("span", { class: "spinner" }), text); }
function srcLabel(src) { return hasT("set.src." + src) ? t("set.src." + src) : (src || "–"); }
function sourceBadge(src) {
  const tone = { admin: "tone-ok", env: "tone-info", none: "tone-warn" }[src] || "";
  return el("span", { class: "badge " + tone, title: src || "" }, srcLabel(src));
}
function netLabel(n) { return n === "internet" ? t("set.net.internet") : t("set.net.none"); }
function outHead(title, slot, extra, onClose) {
  return el("div", { class: "out-head" }, el("h4", {}, title),
    el("div", { class: "end" }, extra || null,
      el("button", { class: "btn sm ghost", type: "button", onclick: () => { if (onClose) onClose(); clear(slot); } }, t("btn.close"))));
}
function nodeOut(name) {
  let o = SET.outs.get(name);
  if (!o) SET.outs.set(name, o = { diag: el("div", { class: "node-out" }), test: el("div", { class: "node-out" }), logs: el("div", { class: "node-out" }) });
  return o;
}
function refreshNodesTab() { if (typeof loadNodes === "function") loadNodes({ probe: false }); }
function enc(name) { return encodeURIComponent(name); }

// A skipped step's detail from the server ('skipped: "<title>" did not pass') in the page's language.
function skipDetail(s, all) {
  const m = s.status === "skip" && typeof s.detail === "string" ? s.detail.match(/^skipped: "(.+)" did not pass$/) : null;
  const dep = m && (all || []).find(x => x && x.title === m[1]);
  return dep && hasT("set.step." + dep.id) ? t("set.diag.skipped", { step: t("set.step." + dep.id) }) : s.detail;
}

// One diagnostic step: ✓ ok / ! warn / ✕ fail / – skip, title, detail, hint, ms.
// (steps.map(stepRow) passes the whole list as the third argument.)
function stepRow(s, _i, all) {
  const st = ["ok", "warn", "fail", "skip"].includes(s.status) ? s.status : "skip";
  const known = s.id && hasT("set.step." + s.id);
  const detail = skipDetail(s, all);
  return el("li", { class: "st-" + st },
    el("span", { class: "step-ic", role: "img", title: t("set.st." + st), "aria-label": t("set.st." + st) },
      { ok: "✓", warn: "!", fail: "✕", skip: "–" }[st]),
    el("div", {},
      el("div", { class: "step-title" }, el("b", { title: known && s.title ? s.title : null }, known ? t("set.step." + s.id) : (s.title || s.id || "")),
        s.ms != null ? el("span", { class: "muted small" }, `${Math.round(s.ms)} ms`) : null),
      detail ? el("div", { class: "step-detail" }, detail) : null,
      s.hint ? el("div", { class: "step-hint" }, s.hint) : null));
}
function diagView(r) {
  const steps = Array.isArray(r.steps) ? r.steps : [];
  const count = st => steps.filter(x => x.status === st).length;
  const fails = count("fail"), warns = count("warn");
  const [tone, text] = !r.ok || fails ? ["tone-bad", fails ? t("set.diag.fail", { n: fails }) : t("set.diag.notOk")]
    : warns ? ["tone-warn", t("set.diag.warn", { n: warns })] : ["tone-ok", t("set.diag.ok")];
  return [el("div", { class: "summary-line" }, el("span", { class: "badge " + tone }, text),
      r.checked_at ? el("span", { class: "muted small" }, t("set.diag.at", { time: fmtTime(r.checked_at) })) : null),
    steps.length ? el("ol", { class: "checklist" }, steps.map(stepRow)) : null];
}

// ---------- load / render ----------
// Every form keeps what was typed across a reload (tab switch, Refresh, a save in another card);
// `fresh` ("aws" | "advisor") is the card whose own save or remove caused it: it shows the saved values.
async function loadSettings({ fresh } = {}) {
  renderDeploy();
  const err = clear($("settings-error"));
  let d;
  try {
    d = await api("/admin/settings");
  } catch (e) {
    takeEdits(fresh);
    SET.data = null;
    addKids(err, alertBox("err", t("set.loadFailed", { msg: errText(e) })));
    for (const [id, key] of [["set-aws", "set.aws.title"], ["set-lambda", "set.l.title"], ["set-advisor", "set.adv.title"]])
      setKids($(id), el("h2", {}, t(key)), el("p", { class: "empty" }, t("set.unavailable")));
    syncDeploy();
    return;
  }
  takeEdits(fresh);
  SET.data = d;
  addKids(err, importNotice(d.imported));
  renderAws(d.aws || {});
  renderLambda();
  renderAdvisor(d.advisor || {});
  putEdits();
  syncDeploy();
  if (SET.focus) {
    const card = $("lnode-" + SET.focus);
    SET.focus = null;
    if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// The AWS and advisor fields whose value differs from the one the server sent (el() sets it as the
// default value), so a re-render can put them back. The card whose own save or remove caused it keeps
// only the advisor's test question; a card without a form (the last load failed) keeps what was taken.
function takeEdits(fresh) {
  for (const [card, id] of Object.entries(EDIT_CARDS)) {
    if (!$(id).querySelector("form")) continue;
    SET.edits[card] = Object.fromEntries(
      [...$(id).querySelectorAll(card === fresh ? "textarea[name=question]" : "input[name], textarea[name]")]
        .filter(x => (x.type === "checkbox" ? x.checked !== x.defaultChecked : x.value !== x.defaultValue))
        .map(x => [x.name, x.type === "checkbox" ? x.checked : x.value]));
  }
}
function putEdits() {
  for (const [card, id] of Object.entries(EDIT_CARDS))
    for (const [name, v] of Object.entries(SET.edits[card] || {})) {
      const x = $(id).querySelector(`[name="${name}"]`);
      if (x) x[x.type === "checkbox" ? "checked" : "value"] = v;
    }
}

// What was carried over from the interim "AWS Lambda" page's lambda.json (shown for 30 days).
function importNotice(r) {
  if (!r) return null;
  const items = [];
  if (r.aws_key) items.push(t("set.imp.key", { key: r.aws_key }));
  if (r.nodes && r.nodes.length) items.push(t("set.imp.nodes", { names: r.nodes.join(", ") }));
  if (r.aws_key_skipped) items.push(t("set.imp.keySkipped", { msg: r.aws_key_skipped }));
  for (const s of r.skipped || []) items.push(t("set.imp.skipped", { name: s.name, msg: s.reason }));
  if (r.error) items.push(r.error);
  if (!items.length) items.push(t("set.imp.none"));
  const partial = r.aws_key_skipped || (r.skipped && r.skipped.length) || r.error;
  return el("div", { class: "alert " + (partial ? "warn" : "info") }, t("set.imp.title", { when: fmtTime(r.at) }),
    el("ul", { class: "plain" }, items.map(x => el("li", {}, x))));
}

// Nodes tab -> "Settings" on a Lambda node card.
function openLambdaSettings(name) {
  SET.focus = name;
  selectTab("settings");
}

// ---------- 1. AWS account ----------
function renderAws(a) {
  const box = clear($("set-aws"));
  const boto = a.boto3 || {};
  const keyId = el("input", { name: "access_key_id", autocomplete: "off", spellcheck: "false", maxlength: "128", placeholder: "AKIA…" });
  const secret = el("input", { name: "secret_access_key", type: "password", autocomplete: "new-password", maxlength: "256" });
  const region = el("input", { name: "region", value: a.region || "", spellcheck: "false", autocomplete: "off", list: "aws-regions",
    pattern: SET_PAT.region, placeholder: "eu-west-2" });
  const out = el("div", { style: "margin-top:12px" });
  const save = el("button", { class: "btn primary", type: "submit" }, t("btn.save"));
  const check = el("button", { class: "btn", type: "button", onclick: () => checkAws(check, out) }, t("set.aws.check"));
  const remove = a.access_key_id ? el("button", { class: "btn danger", type: "button", onclick: async () => {
    if (!confirm(t("set.aws.confirmRemove"))) return;
    remove.disabled = true;
    try {
      await api("/admin/settings/aws", { method: "DELETE" });
      await loadSettings({ fresh: "aws" });
      showMsg($("set-aws"), t("set.aws.removed"), true);
      refreshNodesTab();
    } catch (e) { remove.disabled = false; showMsg(out, errText(e)); }
  } }, t("set.aws.remove")) : null;
  const form = el("form", { novalidate: true },
    el("div", { class: "form-grid" },
      field(t("set.aws.keyId"), keyId, a.access_key_id ? t("set.aws.keyIdHelpSaved") : t("set.aws.keyIdHelp")),
      field(t("set.aws.secret"), secret, t("set.aws.secretHelp")),
      field(t("set.aws.region"), region, t("set.aws.regionHelp"))),
    el("div", { class: "form-actions" }, save, check, remove));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    if (!form.reportValidity()) return;
    const kid = keyId.value.trim(), sec = secret.value.trim(), reg = region.value.trim() || null;
    if (!!kid !== !!sec) { showMsg(out, t("set.aws.both")); return; }
    save.disabled = true;
    try {
      await api("/admin/settings/aws", { method: "PUT", body: kid ? { access_key_id: kid, secret_access_key: sec, region: reg } : { region: reg } });
      secret.value = "";
      await loadSettings({ fresh: "aws" });
      showMsg($("set-aws"), kid ? t("set.aws.savedMsg") : t("set.aws.regionMsg"), true);
      if (kid) refreshNodesTab();
    } catch (e) { showMsg(out, errText(e)); }
    finally { save.disabled = false; }
  });
  const inUse = a.source === "admin" ? t("set.aws.useAdmin", { key: a.access_key_id || "–" })
    : a.source === "env" ? (a.env_access_key_id ? t("set.aws.useEnvKey", { key: a.env_access_key_id }) : t("set.aws.useEnvChain"))
    : t("set.aws.useNone");
  addKids(box,
    el("div", { class: "card-head" }, el("h2", {}, t("set.aws.title")), sourceBadge(a.source)),
    el("p", { class: "card-desc" }, t("set.aws.desc")),
    boto.installed === false ? alertBox("warn", tr("set.aws.noBoto")) : null,
    a.error ? alertBox("err", a.error) : null,
    a.source === "none" ? alertBox("info", t("set.aws.noneHint")) : null,
    el("div", { class: "kv", style: "margin:0 0 20px" },
      kvRow(t("set.aws.inUse"), inUse),
      kvRow(t("set.aws.savedKey"), a.access_key_id || t("set.aws.notSaved")),
      kvRow(t("set.aws.envKey"), a.env_access_key_id || "–"),
      kvRow(t("set.aws.defRegion"), a.region || "–"),
      kvRow(t("set.updated"), a.updated_at ? t("set.updatedBy", { time: fmtTime(a.updated_at), name: a.updated_by || "?" }) : "–"),
      kvRow("boto3", boto.installed ? (boto.version || t("set.aws.installed")) : boto.installed === false ? t("set.aws.notInstalled") : "–")),
    form, out);
}

async function checkAws(btn, out) {
  btn.disabled = true;
  setKids(out, busyLine(t("set.aws.checking")));
  try {
    const r = await api("/admin/settings/aws/check", { method: "POST" });
    if (r.ok && r.account) SET.account = r.account;
    setKids(out, el("ol", { class: "checklist" }, stepRow({
      status: r.ok ? "ok" : "fail", title: r.ok ? t("set.aws.checkOk") : t("set.aws.checkFail"), ms: r.ms, hint: r.hint,
      detail: r.ok ? [t("set.aws.account", { account: r.account || "–" }), r.arn ? "ARN " + r.arn : null, t("set.aws.via", { src: srcLabel(r.source) })]
        .filter(Boolean).join(" · ") : (r.error || t("set.aws.checkFail")) })));
  } catch (e) { setKids(out, alertBox("err", errText(e))); }
  finally { btn.disabled = false; }
}

// ---------- 2. Lambda nodes ----------
function renderLambda() {
  const box = clear($("set-lambda"));
  const nodes = (SET.data && SET.data.lambda_nodes) || [];
  // an open edit form starts from the node as the server has it now (what was typed stays on top)
  if (SET.form && SET.form.node) SET.form.node = nodes.find(n => n.name === SET.form.node.name) || SET.form.node;
  addKids(box,
    el("div", { class: "card-head" }, el("h2", {}, t("set.l.title")),
      el("div", { class: "end" }, el("button", { class: "btn primary", type: "button", onclick: () => openNodeForm(null) }, t("set.l.add")))),
    el("p", { class: "card-desc" }, t("set.l.desc")),
    SET.form ? nodeForm() : null,
    nodes.length ? el("div", { class: "lnodes" }, nodes.map(lambdaCard)) : el("div", { class: "empty" }, t("set.l.empty")));
}

function lambdaCard(n) {
  const o = nodeOut(n.name);
  const editable = n.source === "admin" && n.editable !== false;
  const btn = (label, fn, cls) => { const b = el("button", { class: "btn sm" + (cls ? " " + cls : ""), type: "button" }, label); b.onclick = () => fn(b); return b; };
  return el("div", { class: "node-card", id: "lnode-" + n.name },
    el("div", { class: "node-head" },
      el("h3", {}, n.name),
      el("span", { class: "tag" + (n.source === "admin" ? " accent" : ""), title: t(n.source === "env" ? "node.src.envTip" : "node.src.adminTip") },
        n.source === "env" ? t("set.l.envTag") : t("set.l.adminTag")),
      typeof n.online === "boolean" ? el("span", { class: "badge s-" + (n.online ? "online" : "offline"), title: n.online ? "online" : "offline" },
        t(n.online ? "node.online" : "node.offline")) : null,
      n.draining ? el("span", { class: "badge s-draining", title: "draining" }, t("node.draining")) : null,
      el("span", { class: "badge" + (n.active_jobs ? " s-running" : ""), title: "active_jobs" }, t("set.l.activeJobs", { n: n.active_jobs || 0 }))),
    n.error ? alertBox("err", t("set.l.notLoaded", { msg: n.error })) : null,
    !n.error && n.last_error ? alertBox("warn", t("set.l.lastError", { msg: n.last_error })) : null,
    el("div", { class: "kv" },
      kvRow(t("set.l.function"), n.function), kvRow(t("set.l.bucket"), n.bucket), kvRow(t("set.l.region"), n.region),
      kvRow(t("set.l.network"), netLabel(n.network)), kvRow(t("set.l.concurrency"), n.max_concurrency),
      kvRow(t("set.l.images"), (n.images || []).join(", ")), kvRow(t("set.l.prefix"), n.prefix),
      kvRow(t("set.l.timeouts"), t("set.l.timeoutsV", { start: n.start_timeout ?? "–", stale: n.stale_after ?? "–" })),
      kvRow(t("set.l.fresh"), n.fresh_sandbox === false ? t("no") : t("yes")),
      n.credentials ? kvRow(t("set.l.creds"), srcLabel(n.credentials)) : null,
      n.updated_at ? kvRow(t("set.updated"), t("set.updatedBy", { time: fmtTime(n.updated_at), name: n.updated_by || "?" })) : null),
    el("div", { class: "btn-row", style: "margin-top:14px" },
      btn(t("set.l.diagnose"), b => diagnoseNode(n.name, b)),
      btn(t("set.l.test"), b => testNode(n.name, b)),
      btn(t("set.l.logs"), () => showLogs(n.name, 60)),
      editable ? btn(t("btn.edit"), () => openNodeForm(n)) : null,
      editable ? btn(t("btn.delete"), b => deleteNode(n, b), "danger") : null,
      !editable ? el("span", { class: "muted small" }, t("set.l.envNote")) : null),
    o.diag, o.test, o.logs);
}

async function diagnoseNode(name, btn) {
  const slot = nodeOut(name).diag;
  btn.disabled = true;
  setKids(slot, outHead(t("set.diag.title"), slot), busyLine(t("set.diag.running")));
  try {
    const r = await api(`/admin/settings/lambda/nodes/${enc(name)}/diagnose`, { method: "POST" });
    setKids(slot, outHead(t("set.diag.title"), slot), ...diagView(r));
  } catch (e) { setKids(slot, outHead(t("set.diag.title"), slot), alertBox("err", errText(e))); }
  finally { btn.disabled = false; }
}

// ----- test job: POST, then poll GET .../tests/{id} every 1.5 s until finished -----
function stopPoll(name) {
  const p = SET.polls.get(name);
  if (p) { clearTimeout(p.timer); SET.polls.delete(name); }
}
function testDone(st) { return st.phase === "finished" || st.phase === "error"; }
async function testNode(name, btn) {
  if (SET.polls.has(name)) return;
  const slot = nodeOut(name).test;
  btn.disabled = true;
  setKids(slot, outHead(t("set.test.title"), slot), busyLine(t("set.test.starting")));
  try {
    const st = await api(`/admin/settings/lambda/nodes/${enc(name)}/test-job`, { method: "POST" });
    renderTest(name, st);
    if (!testDone(st) && st.id != null) pollTest(name, st.id, 0, st);
  } catch (e) { setKids(slot, outHead(t("set.test.title"), slot), alertBox("err", errText(e))); }
  finally { btn.disabled = false; }
}
// `last`: the latest state the server sent, shown as ended if the page stops checking.
function pollTest(name, id, fails, last) {
  const p = { id, timer: null };
  p.timer = setTimeout(async () => {
    let st;
    try {
      st = await api(`/admin/settings/lambda/tests/${enc(id)}`);
    } catch (e) {
      if (SET.polls.get(name) !== p) return;
      if (e.status !== 404 && fails < 3) return pollTest(name, id, fails + 1, last);   // a network blip: try again
      SET.polls.delete(name);   // 404: the server restarted (tests are kept in memory), or it kept failing
      renderTest(name, { ...last, phase: "error", status: null, error: t("set.test.gaveUp", { msg: errText(e) }) });
      return;
    }
    if (SET.polls.get(name) !== p) return;    // closed or replaced meanwhile
    renderTest(name, st);
    if (testDone(st)) SET.polls.delete(name); else pollTest(name, id, 0, st);
  }, 1500);
  SET.polls.set(name, p);
}
function renderTest(name, st) {
  const slot = nodeOut(name).test, done = testDone(st), tm = st.timings || {};
  const secs = v => (v == null ? "–" : t("set.test.sec", { v: Number(v).toFixed(1) }));
  const phaseTone = st.phase === "error" ? " tone-bad" : done ? "" : " tone-info";
  const hasTimes = tm.invoke_ms != null || tm.first_status_s != null || tm.total_s != null;
  setKids(slot, outHead(t("set.test.title"), slot, null, () => stopPoll(name)),
    el("div", { class: "summary-line" },
      done ? null : el("span", { class: "spinner" }),
      el("span", { class: "badge" + phaseTone, title: st.phase || "" }, hasT("set.phase." + st.phase) ? t("set.phase." + st.phase) : (st.phase || "–")),
      st.status ? statusBadge(st.status) : null,
      st.exit_code != null ? el("span", { class: "muted small" }, t("set.test.exit", { code: st.exit_code })) : null),
    Array.isArray(st.steps) && st.steps.length ? el("ul", { class: "progress-lines" }, st.steps.map(s => el("li", {}, String(s)))) : null,
    done || hasTimes ? el("div", { class: "kv" },
      kvRow(t("set.test.invoke"), tm.invoke_ms != null ? `${Math.round(tm.invoke_ms)} ms` : "–"),
      kvRow(t("set.test.cold"), tm.first_status_s != null ? "≈ " + secs(tm.first_status_s) : "–"),
      kvRow(t("set.test.total"), secs(tm.total_s)),
      kvRow(t("set.test.started"), st.started_at ? fmtTime(st.started_at) : "–")) : null,
    st.error ? alertBox("err", st.error) : null,
    // "No output." only for a test the server reports as ended (not when the page stopped checking)
    st.logs ? el("pre", { class: "logs" }, st.logs) : done && st.finished_at ? el("p", { class: "muted" }, t("set.test.noOutput")) : null);
}

// ----- function logs (CloudWatch) -----
function fmtTs(ts) {
  if (ts == null || ts === "") return "–";
  if (typeof ts === "number") return new Date(ts < 1e12 ? ts * 1000 : ts).toISOString().replace("T", " ").slice(0, 19);
  return fmtTime(String(ts));
}
function showLogs(name, minutes) {
  const slot = nodeOut(name).logs;
  const sel = el("select", { "aria-label": t("set.logs.range") },
    [15, 60, 360, 1440].map(m => el("option", { value: String(m), selected: m === minutes }, t("set.logs.m" + m))));
  const body = el("div");
  const load = async () => {
    setKids(body, busyLine(t("loading")));
    try {
      const r = await api(`/admin/settings/lambda/nodes/${enc(name)}/logs?minutes=${sel.value}`);
      const evs = Array.isArray(r.events) ? r.events : [];
      const lines = [];
      let stream = null;
      for (const ev of evs) {
        if (ev.stream && ev.stream !== stream) { stream = ev.stream; lines.push(`── ${stream}`); }
        lines.push(`${fmtTs(ev.ts)}  ${String(ev.message ?? "").replace(/\s+$/, "")}`);
      }
      setKids(body,
        el("p", { class: "muted small", style: "margin:0 0 6px" }, t("set.logs.group", { group: r.log_group || "–", n: evs.length })),
        r.error ? alertBox("err", r.error, r.hint) : r.hint ? alertBox("info", r.hint) : null,
        lines.length ? el("pre", { class: "logs" }, lines.join("\n")) : !r.error ? el("p", { class: "empty" }, t("set.logs.none")) : null);
    } catch (e) { setKids(body, alertBox("err", errText(e))); }
  };
  sel.addEventListener("change", load);
  setKids(slot, outHead(t("set.logs.title"), slot, [sel, el("button", { class: "btn sm", type: "button", onclick: load }, t("btn.refresh"))]), body);
  load();
}

// ----- add / edit / delete -----
function openNodeForm(node, prefill) {
  if (!SET.data) return;
  SET.form = { node: node || null, prefill: prefill || {} };
  renderLambda();
  const f = $("lnode-form");
  if (f) {
    f.scrollIntoView({ behavior: "smooth", block: "start" });
    const first = f.querySelector("input:not([disabled])");
    if (first) first.focus({ preventScroll: true });
  }
}
function closeNodeForm() { SET.form = null; renderLambda(); }

function nodeForm() {
  const st = SET.form, { node, prefill } = st;
  const defs = (SET.data && SET.data.lambda_defaults) || {};
  const v = Object.assign({ prefix: "fast-compute/jobs/", images: ["python:3.12-slim"], max_concurrency: 10, network: "none",
    start_timeout: 300, stale_after: 60, fresh_sandbox: true }, defs, node || {}, prefill || {});
  const inp = (name, attrs) => el("input", Object.assign({ name, spellcheck: "false", autocomplete: "off" }, attrs));
  const f = {
    name: inp("name", { value: v.name || "", required: true, pattern: SET_PAT.name, maxlength: "40", disabled: !!node, placeholder: "aws-lambda" }),
    function: inp("function", { value: v.function || "", required: true, pattern: SET_PAT.fn, maxlength: "200", placeholder: "fast-compute-job" }),
    bucket: inp("bucket", { value: v.bucket || "", required: true, pattern: SET_PAT.bucket, maxlength: "63", placeholder: "fast-compute-jobs-<account>-<region>" }),
    region: inp("region", { value: v.region || "", required: true, pattern: SET_PAT.region, list: "aws-regions", placeholder: "eu-west-2" }),
    network: el("select", { name: "network" },
      el("option", { value: "none", selected: v.network !== "internet" }, t("set.net.none")),
      el("option", { value: "internet", selected: v.network === "internet" }, t("set.net.internet"))),
    max_concurrency: inp("max_concurrency", { type: "number", min: "1", max: "1000", step: "1", required: true, value: v.max_concurrency }),
    images: inp("images", { value: Array.isArray(v.images) ? v.images.join(", ") : v.images || "", placeholder: "python:3.12-slim" }),
    prefix: inp("prefix", { value: v.prefix || "", pattern: SET_PAT.prefix, maxlength: "200", placeholder: "fast-compute/jobs/" }),
    start_timeout: inp("start_timeout", { type: "number", min: "30", max: "900", step: "1", value: v.start_timeout }),
    stale_after: inp("stale_after", { type: "number", min: "20", max: "600", step: "1", value: v.stale_after }),
    fresh_sandbox: el("input", { type: "checkbox", name: "fresh_sandbox", checked: v.fresh_sandbox !== false }),
  };
  const netHelp = el("span", { class: "help" });
  const setNetHelp = () => { netHelp.textContent = f.network.value === "internet" ? t("set.net.internetHelp") : t("set.net.noneHelp"); };
  f.network.addEventListener("change", setNetHelp);
  setNetHelp();
  const adv = el("details", { class: "adv", open: !!st.adv }, el("summary", {}, t("set.l.advanced")),
    el("div", { class: "form-grid" },
      field(t("set.l.prefix"), f.prefix, t("set.l.prefixHelp")),
      field(t("set.l.startTimeout"), f.start_timeout, t("set.l.startTimeoutHelp")),
      field(t("set.l.staleAfter"), f.stale_after, t("set.l.staleAfterHelp")),
      checkField(f.fresh_sandbox, t("set.l.fresh"), t("set.l.freshHelp"), "full")));
  adv.addEventListener("toggle", () => { st.adv = adv.open; });
  const result = el("div", { class: "node-out" });
  const save = el("button", { class: "btn primary", type: "submit" }, node ? t("btn.save") : t("set.l.addSave"));
  const test = el("button", { class: "btn", type: "button" }, t("set.l.testSettings"));
  const form = el("form", { class: "form-card", id: "lnode-form", novalidate: true },
    el("h3", {}, node ? t("set.l.editTitle", { name: node.name }) : t("set.l.addTitle")),
    node && node.active_jobs ? alertBox("warn", t("set.l.editBusy", { n: node.active_jobs })) : null,
    el("div", { class: "form-grid" },
      field(t("set.l.name"), f.name, node ? t("set.l.nameFixed") : t("set.l.nameHelp")),
      field(t("set.l.function"), f.function, t("set.l.functionHelp")),
      field(t("set.l.bucket"), f.bucket, t("set.l.bucketHelp")),
      field(t("set.l.region"), f.region, t("set.l.regionHelp")),
      el("label", { class: "field" }, el("span", {}, t("set.l.network")), f.network, netHelp),
      field(t("set.l.concurrency"), f.max_concurrency, t("set.l.concurrencyHelp")),
      field(t("set.l.images"), f.images, t("set.l.imagesHelp"), "full")),
    adv,
    el("div", { class: "form-actions" }, save, test, el("button", { class: "btn ghost", type: "button", onclick: closeNodeForm }, t("btn.cancel"))),
    result);
  // What is typed goes into prefill (applied last above), so a re-render of the card shows it again.
  const keep = () => {
    st.prefill = Object.fromEntries(Object.entries(f).map(([k, x]) => [k, x.type === "checkbox" ? x.checked : x.value]));
  };
  form.addEventListener("input", keep);
  form.addEventListener("change", keep);
  const images = () => f.images.value.split(/[\s,]+/).filter(Boolean);
  const validate = () => {
    const imgs = images();
    f.images.setCustomValidity(imgs.length > 10 || imgs.some(i => !IMAGE_RE.test(i)) ? t("set.l.imagesBad") : "");
    if (adv.querySelector(":invalid")) adv.open = true;
    return form.reportValidity();
  };
  // An empty images / prefix / timeout field means the default, on Add and on Edit alike:
  // the server fills in [], "" and null from its defaults.
  const optNum = x => (x.value === "" ? null : Number(x.value));
  const body = () => ({ name: f.name.value.trim(), function: f.function.value.trim(), bucket: f.bucket.value.trim(),
    region: f.region.value.trim(), network: f.network.value, max_concurrency: Number(f.max_concurrency.value),
    images: images(), prefix: f.prefix.value.trim(), start_timeout: optNum(f.start_timeout), stale_after: optNum(f.stale_after),
    fresh_sandbox: f.fresh_sandbox.checked });
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    if (!validate()) return;
    const b = body();
    save.disabled = true;
    try {
      if (node) await api(`/admin/settings/lambda/nodes/${enc(node.name)}`, { method: "PUT", body: b });
      else await api("/admin/settings/lambda/nodes", { method: "POST", body: b });
      SET.form = null;
      await loadSettings();
      showMsg($("set-lambda"), t(node ? "set.l.updated" : "set.l.added", { name: b.name }), true);
      refreshNodesTab();
    } catch (e) { setKids(result, alertBox("err", errText(e))); }
    finally { save.disabled = false; }
  });
  test.addEventListener("click", async () => {
    if (!validate()) return;
    test.disabled = true;
    setKids(result, outHead(t("set.l.testTitle"), result), busyLine(t("set.diag.running")));
    try {
      const r = await api("/admin/settings/lambda/validate", { method: "POST", body: body() });
      setKids(result, outHead(t("set.l.testTitle"), result), ...diagView(r));
    } catch (e) { setKids(result, outHead(t("set.l.testTitle"), result), alertBox("err", errText(e))); }
    finally { test.disabled = false; }
  });
  return form;
}

async function deleteNode(n, btn) {
  if (!confirm(t("set.l.confirmDelete", { name: n.name }))) return;
  btn.disabled = true;
  try {
    await api(`/admin/settings/lambda/nodes/${enc(n.name)}`, { method: "DELETE" });
    stopPoll(n.name);
    SET.outs.delete(n.name);
    if (SET.form && SET.form.node && SET.form.node.name === n.name) SET.form = null;
    await loadSettings();
    showMsg($("set-lambda"), t("set.l.deleted", { name: n.name }), true);
    refreshNodesTab();
  } catch (e) {
    btn.disabled = false;
    showMsg(btn.closest(".node-card") || $("set-lambda"), errText(e));
  }
}

// ---------- 3. Deploy helper (no server call: builds the deploy.sh command) ----------
function renderDeploy() {
  const box = $("set-deploy");
  if (box.dataset.ready) return;
  box.dataset.ready = "1";
  const inp = (name, attrs) => el("input", Object.assign({ name, spellcheck: "false", autocomplete: "off" }, attrs));
  const f = {
    region: inp("region", { value: "eu-west-2", list: "aws-regions", pattern: SET_PAT.region }),
    fn: inp("function", { value: "fast-compute-job", pattern: SET_PAT.deployFn, minlength: "2", maxlength: "59" }),
    mem: inp("memory", { type: "number", min: "128", max: "10240", step: "1", value: "2048" }),
    timeout: inp("timeout", { type: "number", min: "60", max: "900", step: "1", value: "900" }),
    network: el("select", { name: "network" },
      el("option", { value: "none" }, t("set.net.none")), el("option", { value: "internet" }, t("set.net.internet"))),
    conc: inp("concurrency", { type: "number", min: "1", max: "1000", step: "1", value: "10" }),
    logs: el("input", { type: "checkbox", name: "logs" }),
  };
  f.region.addEventListener("input", () => { f.region.dataset.touched = "1"; });
  const cmd = codeBox("");
  const pre = cmd.querySelector("pre"), copy = cmd.querySelector(".copy");
  const facts = el("p", { class: "muted small", style: "margin:6px 0 0" });
  const use = el("button", { class: "btn", type: "button" }, t("set.dep.use"));
  const update = () => {
    const region = f.region.value.trim(), fn = f.fn.value.trim();
    const mem = Number(f.mem.value), to = Number(f.timeout.value), conc = Number(f.conc.value);
    const checks = [[f.region, REGION_RE.test(region)], [f.fn, DEPLOY_FN_RE.test(fn)],
      [f.mem, Number.isInteger(mem) && mem >= 128 && mem <= 10240], [f.timeout, Number.isInteger(to) && to >= 60 && to <= 900],
      [f.conc, Number.isInteger(conc) && conc >= 1 && conc <= 1000]];
    for (const [input, ok] of checks) input.setAttribute("aria-invalid", ok ? "false" : "true");
    const ok = checks.every(c => c[1]);
    copy.disabled = !ok;
    use.disabled = !ok || !SET.data;
    if (!ok) { pre.textContent = t("set.dep.fix"); facts.textContent = ""; return; }
    pre.textContent = `REGION=${region} FUNCTION=${fn} MEMORY_MB=${mem} TIMEOUT=${to} NETWORK=${f.network.value} `
      + `CONCURRENCY=${conc} LOGS=${f.logs.checked ? 1 : 0} sh lambda-node/deploy.sh`;
    facts.textContent = t("set.dep.facts", { vcpu: Math.min(mem / 1769, 6).toFixed(2), longest: to - 30, conc });
  };
  for (const x of Object.values(f)) x.addEventListener(x.tagName === "SELECT" || x.type === "checkbox" ? "change" : "input", update);
  use.addEventListener("click", () => {
    const region = f.region.value.trim();
    openNodeForm(null, { function: f.fn.value.trim(), region, network: f.network.value, max_concurrency: Number(f.conc.value),
      bucket: SET.account ? `fast-compute-jobs-${SET.account}-${region}` : "" });
  });
  box.syncDeploy = (awsRegion) => {
    if (awsRegion && !f.region.dataset.touched) f.region.value = awsRegion;
    update();
  };
  setKids(box,
    el("div", { class: "card-head" }, el("h2", {}, t("set.dep.title"))),
    el("p", { class: "card-desc" }, tr("set.dep.desc")),
    el("div", { class: "form-grid" },
      field(t("set.dep.region"), f.region),
      field(t("set.dep.function"), f.fn, t("set.dep.functionHelp")),
      field(t("set.dep.memory"), f.mem, t("set.dep.memoryHelp")),
      field(t("set.dep.timeout"), f.timeout, t("set.dep.timeoutHelp")),
      el("label", { class: "field" }, el("span", {}, t("set.l.network")), f.network, el("span", { class: "help" }, t("set.dep.networkHelp"))),
      field(t("set.dep.concurrency"), f.conc, t("set.dep.concurrencyHelp")),
      checkField(f.logs, t("set.dep.logs"), t("set.dep.logsHelp"), "full")),
    el("h3", {}, t("set.dep.command")),
    cmd, facts,
    el("h3", {}, t("set.dep.creates")),
    el("ul", { class: "plain" }, ["set.dep.c1", "set.dep.c2", "set.dep.c3", "set.dep.c4", "set.dep.c5", "set.dep.c6"].map(k => el("li", {}, tr(k)))),
    el("h3", {}, t("set.dep.then")),
    el("ol", { class: "plain" }, ["set.dep.n1", "set.dep.n2", "set.dep.n3"].map(k => el("li", {}, tr(k)))),
    el("div", { class: "form-actions" }, use));
  update();
}
function syncDeploy() {
  const box = $("set-deploy");
  if (box.syncDeploy) box.syncDeploy(SET.data && SET.data.aws ? SET.data.aws.region : null);
}

// ---------- 4. Homepage advisor ----------
function renderAdvisor(a) {
  const box = clear($("set-advisor"));
  const base = el("input", { name: "base_url", type: "url", value: a.base_url || "", spellcheck: "false", autocomplete: "off",
    placeholder: "https://api.openai.com/v1", required: true });
  const model = el("input", { name: "model", value: a.model || "", spellcheck: "false", autocomplete: "off", placeholder: "gpt-4o-mini" });
  const maxTok = el("input", { name: "max_tokens", type: "number", min: "16", max: "32000", step: "1", value: a.max_tokens || 800 });
  const key = el("input", { name: "api_key", type: "password", autocomplete: "new-password",
    placeholder: a.has_api_key ? t("set.adv.keyKeep") : t("set.adv.keyNone") });
  // only a key saved here can be removed here (has_api_key is also true for a server/.env key)
  const dropKey = (a.saved || []).includes("api_key") ? el("input", { type: "checkbox", name: "drop_key" }) : null;
  const out = el("div");
  const save = el("button", { class: "btn primary", type: "submit" }, t("btn.save"));
  const reset = a.source === "admin" ? el("button", { class: "btn danger", type: "button", onclick: async () => {
    if (!confirm(t("set.adv.confirmReset"))) return;
    reset.disabled = true;
    try {
      await api("/admin/settings/advisor", { method: "DELETE" });
      await loadSettings({ fresh: "advisor" });
      showMsg($("set-advisor"), t("set.adv.resetDone"), true);
    } catch (e) { reset.disabled = false; showMsg(out, errText(e)); }
  } }, t("set.adv.reset")) : null;
  const form = el("form", { novalidate: true },
    el("div", { class: "form-grid" },
      field(t("set.adv.base"), base, t("set.adv.baseHelp")),
      field(t("set.adv.model"), model, t("set.adv.modelHelp")),
      field(t("set.adv.maxTokens"), maxTok, t("set.adv.maxTokensHelp")),
      field(t("set.adv.key"), key, a.has_api_key ? t("set.adv.keyHelpSet") : t("set.adv.keyHelp")),
      dropKey ? checkField(dropKey, t("set.adv.dropKey"), null, "full") : null),
    el("div", { class: "form-actions" }, save, reset), out);
  // A value equal to the server/.env one is sent as null (= use .env): only real overrides are
  // saved here, so a later change in server/.env still applies.
  const env = a.env && typeof a.env === "object" ? a.env : null;
  const noSlash = s => String(s || "").replace(/\/+$/, "");
  const override = (v, envV) => (env && v === envV ? null : v);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    if (!form.reportValidity()) return;
    const k = key.value.trim();
    save.disabled = true;
    try {
      await api("/admin/settings/advisor", { method: "PUT", body: {
        base_url: override(noSlash(base.value.trim()), env && noSlash(env.base_url || "https://api.openai.com/v1")),
        model: override(model.value.trim(), env && (env.model || "")),
        max_tokens: maxTok.value.trim() === "" ? null : override(Number(maxTok.value), env && env.max_tokens),
        api_key: k ? k : dropKey && dropKey.checked ? "" : null } });
      key.value = "";
      await loadSettings({ fresh: "advisor" });
      showMsg($("set-advisor"), t("set.adv.saved"), true);
    } catch (e) { showMsg(out, errText(e)); }
    finally { save.disabled = false; }
  });
  // Test box: one question through the configured model, without the visitor rate limits.
  const q = el("textarea", { name: "question", rows: "2", maxlength: "500", style: "width:100%;resize:vertical", placeholder: t("set.adv.testPh") });
  const ask = el("button", { class: "btn", type: "submit" }, t("set.adv.ask"));
  const answer = el("div");
  const testForm = el("form", {}, field(t("set.adv.question"), q, null, "full"), el("div", { class: "form-actions", style: "margin-top:10px" }, ask), answer);
  testForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    const question = q.value.trim();
    if (!question) { q.focus(); return; }
    ask.disabled = true;
    setKids(answer, busyLine(t("set.adv.asking")));
    try {
      const r = await api("/admin/settings/advisor/test", { method: "POST", body: { question } });
      setKids(answer,
        el("div", { class: "summary-line", style: "margin-top:12px" },
          el("span", { class: "badge " + (r.ok ? "tone-ok" : "tone-bad") }, r.ok ? t("set.adv.ok") : t("set.adv.failed")),
          r.mode ? el("span", { class: "badge", title: r.mode }, modeLabel(r.mode)) : null,
          r.ms != null ? el("span", { class: "muted small" }, `${Math.round(r.ms)} ms`) : null),
        r.reply ? el("div", { class: "reply" }, r.reply) : null,
        r.error ? alertBox("err", r.error) : null);
    } catch (e) { setKids(answer, alertBox("err", errText(e))); }
    finally { ask.disabled = false; }
  });
  addKids(box,
    el("div", { class: "card-head" }, el("h2", {}, t("set.adv.title")), sourceBadge(a.source),
      a.mode ? el("span", { class: "badge " + (a.mode === "llm" ? "tone-info" : ""), title: a.mode }, modeLabel(a.mode)) : null),
    el("p", { class: "card-desc" }, t("set.adv.desc")),
    a.error ? alertBox("err", a.error) : null,
    el("div", { class: "kv", style: "margin:0 0 18px" },
      kvRow(t("set.adv.apiKey"), a.has_api_key ? t("set.adv.keySet") : t("set.adv.keyMissing")),
      kvRow(t("set.updated"), a.updated_at ? t("set.updatedBy", { time: fmtTime(a.updated_at), name: a.updated_by || "?" }) : "–")),
    form,
    el("h3", {}, t("set.adv.testTitle")),
    el("p", { class: "muted small", style: "margin:0 0 8px" }, t("set.adv.testDesc")),
    testForm);
}
function modeLabel(m) { return hasT("set.mode." + m) ? t("set.mode." + m) : m; }

// ---------- wiring ----------
document.body.append(el("datalist", { id: "aws-regions" }, AWS_REGIONS.map(r => el("option", { value: r }))));
document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => {
  const target = $(b.dataset.goto);
  if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}));
$("settings-refresh").addEventListener("click", () => loadSettings());
