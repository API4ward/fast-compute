// fast-compute public homepage: texts (zh/en), hero terminal demo, cost calculator,
// access-request form and the product advisor chat (POST /sales/chat).
"use strict";

const LANG_KEY = "fastcompute_lang";   // shared with /welcome
const CHAT_KEY = "fastcompute_sales_chat";

const ICONS = {
  box: '<path d="M12 2 3 7v10l9 5 9-5V7z M3 7l9 5 9-5 M12 12v10"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  nodes: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M17.5 14v7M14 17.5h7"/>',
  meter: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="M12 18l4-6"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
};

const T = {
  en: {
    "nav.features": "Features", "nav.arch": "Architecture", "nav.sites": "Websites", "nav.pricing": "Pricing",
    "nav.faq": "FAQ", "nav.dashboard": "Dashboard", "nav.access": "Request access",
    "hero.eyebrow": "MCP-native · extensible compute platform",
    "hero.title": 'Say it to Claude.<br><span class="grad">Run it on a cluster.</span>',
    "hero.sub": "fast-compute runs your code in isolated containers across a growing pool of compute nodes, and publishes small websites in one sentence, straight from Claude, any MCP client or a REST API.",
    "hero.cta1": "Request access", "hero.cta2": "Ask the product advisor",
    "hero.t1": "No servers to manage", "hero.t2": "Isolated by default", "hero.t3": "Pay per credit-hour",
    "s.v1": "10+ cores", "s.k1": "in the compute pool today",
    "s.v2": "< 1 min", "s.k2": "from a sentence to a running job",
    "s.v3": "~1 s", "s.k3": "for a sleeping website to wake",
    "s.v4": "3 node types", "s.k4": "macOS · Windows · AWS Lambda (soon)",
    "f.kicker": "Features", "f.title": "Everything you need to hand compute to an AI assistant",
    "a.kicker": "Architecture", "a.title": "One front door, an extensible pool of nodes",
    "a.body": "A public bridge handles accounts, tokens, billing and HTTPS. It sends each job to the node with the most free capacity over a private Tailscale network, and queues jobs when every node is busy. Adding capacity means starting a small node agent on another machine.",
    "a.live": "Live", "a.joining": "Joining", "a.soon": "Coming soon",
    "a.l1": "Mac mini node: Docker, about 10 cores and 24 GB for jobs.",
    "a.l2": "Windows node through WSL and Docker is being connected.",
    "a.l3": "AWS Lambda as a serverless node type, for bursty short jobs.",
    "d.clients": "You & your AI", "d.visitors": "Website visitors", "d.bridge": "fast-compute bridge",
    "d.bridge2": "auth · billing · scheduler · HTTPS", "d.tailnet": "private tailnet", "d.yours": "your machine",
    "w.kicker": "Small websites", "w.title": "Ship a website in one sentence",
    "w.body": "Landing pages, docs, portfolios, browser games: ask Claude to publish the files and get a public HTTPS link back. Re-upload a file and the live site updates at once.",
    "w.ask": "Publish my portfolio (index.html, style.css, app.js, avatar.png) as a site and send me the link.",
    "w.answer": 'Done. Your site is live at <a>https://sites…/sites/you/portfolio/</a>. Edit any file and I will push the update.',
    "h.kicker": "How it works", "h.title": "Three steps from zero to running",
    "p.kicker": "Pricing", "p.title": "Simple credits, billed per job",
    "p.body": "<b>1 credit-hour = 1 CPU core + 256 MB RAM for one hour.</b> A job costs run time × max(cpus, memory / 256 MB), charged for the size you reserve.",
    "p.calc": "Estimate a job", "p.cpus": "CPUs", "p.mem": "Memory", "p.mins": "Run time", "p.cost": "Cost",
    "p.fine": "Accounts are invite-only for now; credits are topped up by the administrator. Ask about quotas through Request access.",
    "q.kicker": "FAQ", "q.title": "Common questions",
    "r.title": "Request access", "r.body": "fast-compute is in an invite-only early stage. Tell us who you are and what you want to run, and we will get back to you with an account and a token.",
    "r.name": "Name", "r.contact": "Email or other contact *", "r.msg": "What do you want to run or host?",
    "r.send": "Send request", "r.ok": "Thanks! Your request was sent; we will be in touch.", "r.need": "Please leave a contact so we can reply.",
    "r.contactLine": "Or reach us directly: ",
    "ft.guide": "Getting started",
    "c.fab": "Ask us anything", "c.title": "Product advisor", "c.sub": "Answers about fast-compute",
    "c.reset": "New conversation", "c.ph": "Ask about pricing, security, websites…", "c.send": "Send",
    "c.hello": "Hi! I can answer questions about fast-compute: what it runs, how billing works, hosting websites, using it from Claude, or the upcoming AWS Lambda nodes. What are you planning to build?",
    "c.err": "Sorry, I could not answer just now. Please try again in a moment.",
    "c.calc": (c) => `${c} credit-hours`,
  },
  zh: {
    "nav.features": "功能", "nav.arch": "架构", "nav.sites": "网站托管", "nav.pricing": "计费",
    "nav.faq": "常见问题", "nav.dashboard": "控制台", "nav.access": "申请试用",
    "hero.eyebrow": "MCP 原生 · 可扩展的算力平台",
    "hero.title": '对 Claude 说一句，<br><span class="grad">代码在集群上运行。</span>',
    "hero.sub": "fast-compute 在不断扩展的算力节点池上，用隔离容器运行你的代码；一句话就能发布小型网站。Claude、任意 MCP 客户端或 REST API 都可以直接调用。",
    "hero.cta1": "申请试用", "hero.cta2": "咨询产品顾问",
    "hero.t1": "无需管理服务器", "hero.t2": "默认隔离", "hero.t3": "按算力时计费",
    "s.v1": "10+ 核", "s.k1": "当前算力池规模",
    "s.v2": "< 1 分钟", "s.k2": "从一句话到任务运行",
    "s.v3": "约 1 秒", "s.k3": "休眠网站被唤醒",
    "s.v4": "3 类节点", "s.k4": "macOS · Windows · AWS Lambda（即将推出）",
    "f.kicker": "功能", "f.title": "把算力交给 AI 助手，所需的一切都在这里",
    "a.kicker": "架构", "a.title": "一个入口，可扩展的节点池",
    "a.body": "公网桥接服务负责账户、令牌、计费和 HTTPS，通过私有 Tailscale 网络把每个任务派发到空闲资源最多的节点，节点全忙时自动排队。扩容只需在新机器上启动一个轻量节点程序。",
    "a.live": "已上线", "a.joining": "接入中", "a.soon": "即将推出",
    "a.l1": "Mac mini 节点：Docker，约 10 核、24 GB 供任务使用。",
    "a.l2": "Windows 节点（WSL + Docker）正在接入。",
    "a.l3": "AWS Lambda 无服务器节点，适合突发的短任务。",
    "d.clients": "你和你的 AI", "d.visitors": "网站访客", "d.bridge": "fast-compute 桥接服务",
    "d.bridge2": "认证 · 计费 · 调度 · HTTPS", "d.tailnet": "私有 tailnet", "d.yours": "你的机器",
    "w.kicker": "小型网站", "w.title": "一句话，网站上线",
    "w.body": "落地页、文档、作品集、网页游戏：让 Claude 发布文件，就能拿到公开的 HTTPS 链接。重新上传文件，线上立即更新。",
    "w.ask": "把我的作品集（index.html、style.css、app.js、avatar.png）发布成网站，把链接发给我。",
    "w.answer": '已发布：<a>https://sites…/sites/you/portfolio/</a>。需要修改任何文件，告诉我就会更新。',
    "h.kicker": "使用流程", "h.title": "三步开始运行",
    "p.kicker": "计费", "p.title": "简单的算力时，按任务结算",
    "p.body": "<b>1 算力时 = 1 个 CPU 核 + 256 MB 内存运行 1 小时。</b>任务费用 = 运行时间 × max(cpus, 内存 / 256 MB)，按申请的规格计费。",
    "p.calc": "估算一个任务", "p.cpus": "CPU", "p.mem": "内存", "p.mins": "运行时间", "p.cost": "费用",
    "p.fine": "目前为邀请制，额度由管理员充值。需要更大配额请通过「申请试用」联系我们。",
    "q.kicker": "常见问题", "q.title": "你可能想问",
    "r.title": "申请试用", "r.body": "fast-compute 目前处于邀请制早期阶段。告诉我们你是谁、想运行什么，我们会为你开通账户并发送令牌。",
    "r.name": "称呼", "r.contact": "邮箱或其他联系方式 *", "r.msg": "你想运行或托管什么？",
    "r.send": "提交申请", "r.ok": "谢谢！申请已提交，我们会尽快联系你。", "r.need": "请留下联系方式，方便我们回复。",
    "r.contactLine": "也可以直接联系：",
    "ft.guide": "快速上手",
    "c.fab": "咨询产品顾问", "c.title": "产品顾问", "c.sub": "解答关于 fast-compute 的问题",
    "c.reset": "新对话", "c.ph": "问问价格、安全、网站托管……", "c.send": "发送",
    "c.hello": "你好！我可以解答关于 fast-compute 的问题：能运行什么、怎么计费、网站托管、如何在 Claude 里使用，以及即将推出的 AWS Lambda 节点。你打算做什么？",
    "c.err": "抱歉，暂时无法回答，请稍后再试。",
    "c.calc": (c) => `${c} 算力时`,
  },
};

const FEATURES = {
  en: [
    ["spark", "AI-native via MCP", "Add one connector URL to claude.ai, Claude Desktop or Claude Code and just ask: “run this with 4 CPUs”."],
    ["box", "Isolated containers", "Every job gets a fresh container: read-only system, unprivileged user, no host access, network off by default."],
    ["globe", "Small websites", "Publish HTML, JS, CSS and images to a public HTTPS URL on a separate origin. Sites sleep when idle."],
    ["nodes", "Extensible node pool", "Jobs go to the least-busy node and queue when full. AWS Lambda nodes <span class=\"tag\">soon</span>"],
    ["meter", "Transparent billing", "Credit-hours per job, CPU and memory sampled while it runs, usage reports for you and the admin."],
    ["lock", "Encrypted secrets", "Save API keys once as environment variables; they are encrypted, injected into your jobs, hidden from admins."],
  ],
  zh: [
    ["spark", "MCP 原生，AI 直接调用", "在 claude.ai、Claude Desktop 或 Claude Code 里添加一个连接器地址，然后直接说：“用 4 个 CPU 跑这个”。"],
    ["box", "隔离容器", "每个任务一个全新容器：系统只读、非特权用户、无法访问宿主机，默认不联网。"],
    ["globe", "小型网站托管", "把 HTML、JS、CSS 和图片发布到独立域名下的公开 HTTPS 地址，闲置自动休眠。"],
    ["nodes", "可扩展节点池", "任务派发到最空闲的节点，满载时排队。AWS Lambda 节点 <span class=\"tag\">即将推出</span>"],
    ["meter", "透明计费", "按任务结算算力时，运行中采样 CPU 和内存，用户和管理员都能看到用量报表。"],
    ["lock", "加密的密钥", "API Key 等存为环境变量，加密保存、自动注入任务，管理员也看不到。"],
  ],
};

const HOW = {
  en: [["Get a token", "Request access; the admin sends you a personal ds_ token and starting credits."],
       ["Connect Claude", "Add the MCP connector URL in claude.ai / Desktop, or one command in Claude Code."],
       ["Just ask", "Claude uploads your code, picks CPU and memory, runs it and reads back the output."]],
  zh: [["获取令牌", "申请试用后，管理员会发给你专属的 ds_ 令牌和初始额度。"],
       ["连接 Claude", "在 claude.ai / Desktop 添加 MCP 连接器地址，或在 Claude Code 里运行一条命令。"],
       ["直接提要求", "Claude 会上传代码、选择 CPU 和内存、运行并读回输出。"]],
};

const SITE_STEPS = {
  en: ["<b>Upload</b> files one by one with <code>put_site_file</code> (up to 50 MB per site).",
       "<b>Publish</b> with <code>publish_site</code> and get a public HTTPS URL.",
       "<b>Iterate</b>: change a file and the live site updates at once. Sites are free for now."],
  zh: ["用 <code>put_site_file</code> 逐个<b>上传</b>文件（单站点 50 MB 以内）。",
       "用 <code>publish_site</code> <b>发布</b>，得到公开的 HTTPS 地址。",
       "<b>迭代</b>：修改文件，线上立即生效。网站托管目前免费。"],
};

const PRICE_LIST = {
  en: ["2 CPU + 256 MB for 30 minutes = 1 credit-hour", "New accounts start with a credit grant", "Websites are currently free", "Usage and balance visible anytime in the dashboard"],
  zh: ["2 CPU + 256 MB 运行 30 分钟 = 1 算力时", "新账户自带初始额度", "网站托管目前免费", "随时在控制台查看用量和余额"],
};

const FAQ = {
  en: [
    ["What can I run?", "Batch-style jobs: scripts, data processing, tests, builds, simulations. Each job runs in a container (default python:3.12-slim) with your files in /workspace and ends when the command finishes. Long-running servers with inbound ports and GPUs are not offered yet."],
    ["How is my code isolated?", "Fresh container per job, read-only root filesystem, unprivileged user, no access to the host, Docker or other jobs, small /tmp, and network off unless your account allows outbound HTTP/HTTPS."],
    ["Which AI tools work with it?", "Anything that speaks MCP: claude.ai and Claude Desktop custom connectors (with OAuth sign-in), Claude Code, and other MCP clients over Streamable HTTP. There is also a REST API."],
    ["What about AWS Lambda?", "Lambda support as a serverless node type is in development. It will let short, bursty jobs scale out without a machine of our own; we will announce it here when it is live."],
    ["Can I host a website with a backend?", "Not yet. Sites serve static files only (HTML, JS, CSS, images, games). Front-end apps that call external APIs work fine."],
    ["How do I get an account?", "Use Request access below. Accounts are invite-only while we grow the node pool."],
  ],
  zh: [
    ["可以运行什么？", "批处理类任务：脚本、数据处理、测试、构建、仿真等。每个任务在容器中运行（默认 python:3.12-slim），文件位于 /workspace，命令结束任务即结束。暂不提供需要入站端口的常驻服务和 GPU。"],
    ["代码如何隔离？", "每个任务一个全新容器：根文件系统只读、非特权用户、无法访问宿主机、Docker 和其他任务，/tmp 很小；除非账户开通，否则不能联网（开通后可访问公网 HTTP/HTTPS）。"],
    ["支持哪些 AI 工具？", "所有支持 MCP 的客户端：claude.ai 和 Claude Desktop 的自定义连接器（支持 OAuth 登录）、Claude Code，以及其他 Streamable HTTP MCP 客户端。也提供 REST API。"],
    ["AWS Lambda 什么时候支持？", "Lambda 无服务器节点正在开发中，可以让短时突发任务弹性扩展、无需自有机器。上线后会在这里公布。"],
    ["能托管带后端的网站吗？", "暂时不行。网站只提供静态文件（HTML、JS、CSS、图片、游戏），调用外部 API 的前端应用可以正常使用。"],
    ["怎么开通账户？", "请使用下方的「申请试用」。在扩充节点池期间，账户采用邀请制。"],
  ],
};

const CHIPS = {
  en: ["How much does it cost?", "Can I host a website?", "How do I use it with Claude?", "Is AWS Lambda supported?"],
  zh: ["怎么收费？", "可以托管网站吗？", "怎么在 Claude 里用？", "支持 AWS Lambda 吗？"],
};

// ---------- helpers ----------
let lang = (() => {
  try { const v = localStorage.getItem(LANG_KEY); if (v === "zh" || v === "en") return v; } catch (e) { /* ignore */ }
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
})();
const t = (k) => T[lang][k] ?? T.en[k] ?? k;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Tiny, safe Markdown: escape first, then bold, inline code, links to http(s) URLs and lists.
function md(text) {
  const lines = esc(text).split(/\n/);
  let html = "", list = null;
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  for (const raw of lines) {
    const m = raw.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/);
    if (m) {
      const kind = m[1] ? "ol" : "ul";
      if (list !== kind) { if (list) html += `</${list}>`; html += `<${kind}>`; list = kind; }
      html += `<li>${inline(m[2])}</li>`;
      continue;
    }
    if (list) { html += `</${list}>`; list = null; }
    if (raw.trim()) html += `<p>${inline(raw)}</p>`;
  }
  if (list) html += `</${list}>`;
  return html;
}

// ---------- static texts ----------
function applyTexts() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  $("#lang-btn").textContent = lang === "zh" ? "EN" : "中文";
  document.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n); });
  document.querySelectorAll("[data-i18n-html]").forEach((n) => { n.innerHTML = t(n.dataset.i18nHtml); });
  document.querySelectorAll("[data-i18n-ph]").forEach((n) => { n.placeholder = t(n.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-title]").forEach((n) => { n.title = t(n.dataset.i18nTitle); });

  $("#feature-grid").innerHTML = FEATURES[lang].map(([ic, h, p]) =>
    `<div class="card feat"><div class="ic"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[ic]}</svg></div><h3>${h}</h3><p>${p}</p></div>`).join("");
  $("#how-grid").innerHTML = HOW[lang].map(([h, p], i) =>
    `<div class="card"><div class="n">0${i + 1}</div><h3>${h}</h3><p>${p}</p></div>`).join("");
  $("#site-steps").innerHTML = SITE_STEPS[lang].map((s) => `<li><span>${s}</span></li>`).join("");
  $("#price-list").innerHTML = PRICE_LIST[lang].map((s) => `<li>${esc(s)}</li>`).join("");
  $("#faq-list").innerHTML = FAQ[lang].map(([q, a]) => `<details><summary>${esc(q)}</summary><p>${esc(a)}</p></details>`).join("");
  renderChips();
  calc();
  if (chatLog().length === 0) renderChat();
}

// ---------- hero terminal ----------
const DEMO = {
  en: [
    ["u", "Run train.py on fast-compute with 4 CPUs and 2 GB, then show me the accuracy."],
    ["c", "● fast-compute · submit_job"],
    ["t", '{ "files": ["train.py", "data.csv"], "command": "python train.py",\n  "cpus": 4, "mem_mb": 2048, "wait": 60 }'],
    ["o", "→ job 7f3a2c  queued → running on node mini\n→ epoch 5/5  loss 0.142\n→ succeeded in 41 s · 0.36 credit-hours"],
    ["ai", "Training finished in 41 s. Final accuracy is 94.8% (loss 0.142). Want me to try a larger model next?"],
  ],
  zh: [
    ["u", "用 fast-compute 以 4 个 CPU、2 GB 内存运行 train.py，然后告诉我准确率。"],
    ["c", "● fast-compute · submit_job"],
    ["t", '{ "files": ["train.py", "data.csv"], "command": "python train.py",\n  "cpus": 4, "mem_mb": 2048, "wait": 60 }'],
    ["o", "→ 任务 7f3a2c  排队 → 在节点 mini 上运行\n→ epoch 5/5  loss 0.142\n→ 41 秒完成 · 0.36 算力时"],
    ["ai", "训练用时 41 秒，最终准确率 94.8%（loss 0.142）。要不要接着试一个更大的模型？"],
  ],
};
let termRun = 0;
async function playTerm() {
  const run = ++termRun, el = $("#term");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  while (run === termRun) {
    el.innerHTML = "";
    for (const [cls, text] of DEMO[lang]) {
      if (run !== termRun) return;
      const line = document.createElement("div");
      line.className = cls;
      el.appendChild(line);
      if (cls === "u" && !reduce) {
        const caret = document.createElement("span"); caret.className = "caret";
        for (let i = 1; i <= text.length; i++) {
          if (run !== termRun) return;
          line.textContent = text.slice(0, i); line.appendChild(caret);
          await sleep(28);
        }
        caret.remove();
        await sleep(500);
      } else {
        line.textContent = text;
        await sleep(reduce ? 0 : 900);
      }
      line.after(document.createElement("br"));
    }
    if (reduce) return;
    await sleep(6000);
  }
}

// ---------- calculator ----------
function calc() {
  const cpu = Number($("#c-cpu").value), mem = Number($("#c-mem").value), mins = Number($("#c-min").value);
  $("#o-cpu").textContent = cpu;
  $("#o-mem").textContent = mem >= 1024 ? `${(mem / 1024).toFixed(mem % 1024 ? 2 : 0)} GB` : `${mem} MB`;
  $("#o-min").textContent = mins >= 60 ? `${Math.floor(mins / 60)} h ${mins % 60 ? (mins % 60) + " min" : ""}`.trim() : `${mins} min`;
  const cost = (mins / 60) * Math.max(cpu, mem / 256);
  $("#o-cost").textContent = T[lang]["c.calc"](cost < 10 ? cost.toFixed(2) : cost.toFixed(1));
}

// ---------- access request ----------
async function sendLead(ev) {
  ev.preventDefault();
  const f = ev.target, msg = $("#lead-msg"), btn = f.querySelector("button");
  const data = Object.fromEntries(new FormData(f).entries());
  msg.className = "form-msg";
  if (!String(data.contact || "").trim()) { msg.textContent = t("r.need"); msg.classList.add("err"); return; }
  data.lang = lang;
  btn.disabled = true;
  try {
    const r = await fetch("/sales/lead", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    f.reset();
    msg.textContent = t("r.ok"); msg.classList.add("ok");
  } catch (e) {
    msg.textContent = e.message || t("c.err"); msg.classList.add("err");
  } finally { btn.disabled = false; }
}

// ---------- chat ----------
function chatLog() {
  try { return JSON.parse(sessionStorage.getItem(CHAT_KEY) || "[]"); } catch (e) { return []; }
}
function saveLog(log) {
  try { sessionStorage.setItem(CHAT_KEY, JSON.stringify(log.slice(-30))); } catch (e) { /* ignore */ }
}
function bubble(role, text, extra) {
  const d = document.createElement("div");
  d.className = `msg ${role === "user" ? "user" : "bot"}${extra ? " " + extra : ""}`;
  if (role === "user") d.textContent = text; else d.innerHTML = md(text);
  return d;
}
function renderChat() {
  const box = $("#chat-log");
  box.innerHTML = "";
  box.appendChild(bubble("assistant", t("c.hello")));
  for (const m of chatLog()) box.appendChild(bubble(m.role, m.content));
  box.scrollTop = box.scrollHeight;
  renderChips();
}
function renderChips() {
  const chips = $("#chat-chips");
  chips.innerHTML = "";
  if (chatLog().length) return;
  for (const q of CHIPS[lang]) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = q;
    b.onclick = () => ask(q);
    chips.appendChild(b);
  }
}
let busy = false;
async function ask(text) {
  text = text.trim();
  if (!text || busy) return;
  busy = true;
  const log = chatLog();
  log.push({ role: "user", content: text });
  saveLog(log);
  const box = $("#chat-log");
  $("#chat-chips").innerHTML = "";
  box.appendChild(bubble("user", text));
  const typing = document.createElement("div");
  typing.className = "msg bot typing"; typing.innerHTML = "<span></span><span></span><span></span>";
  box.appendChild(typing); box.scrollTop = box.scrollHeight;
  $("#chat-form button").disabled = true;
  try {
    const r = await fetch("/sales/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: log.slice(-16), lang }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || t("c.err"));
    log.push({ role: "assistant", content: body.reply });
    saveLog(log);
    typing.replaceWith(bubble("assistant", body.reply));
  } catch (e) {
    log.pop(); saveLog(log);   // let the visitor retry the same question
    typing.replaceWith(bubble("assistant", e.message || t("c.err"), "err"));
  } finally {
    busy = false;
    $("#chat-form button").disabled = false;
    box.scrollTop = box.scrollHeight;
  }
}
function openChat() {
  $("#chat").classList.remove("hidden");
  $("#chat-fab").classList.add("hidden");
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  setTimeout(() => $("#chat-text").focus(), 50);
}
function closeChat() {
  $("#chat").classList.add("hidden");
  $("#chat-fab").classList.remove("hidden");
}

// ---------- wiring ----------
document.addEventListener("DOMContentLoaded", () => {
  applyTexts();
  playTerm();
  $("#lang-btn").onclick = () => {
    lang = lang === "zh" ? "en" : "zh";
    try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* ignore */ }
    applyTexts(); renderChat(); playTerm();
  };
  ["#c-cpu", "#c-mem", "#c-min"].forEach((s) => $(s).addEventListener("input", calc));
  $("#lead-form").addEventListener("submit", sendLead);
  document.querySelectorAll("[data-open-chat]").forEach((b) => b.addEventListener("click", openChat));
  $("#chat-close").onclick = closeChat;
  $("#chat-reset").onclick = () => { saveLog([]); renderChat(); };
  const ta = $("#chat-text");
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); const v = ta.value; ta.value = ""; ta.style.height = ""; ask(v); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
  });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 120) + "px"; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChat(); });
  renderChat();
  fetch("/sales/info").then((r) => r.json()).then((info) => {
    if (info.contact) {
      const p = $("#contact-line");
      p.textContent = t("r.contactLine") + info.contact;
      p.classList.remove("hidden");
    }
  }).catch(() => {});
});
