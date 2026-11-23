// Welcome / getting-started guide, shown on /welcome (no sign-in) and in the "Welcome" tab of /ui.
// Every URL is built from location.origin, so the page is right on whichever host serves it.
// Needs common.js (el, clear, api).

const WELCOME_LANG_KEY = "fastcompute_lang";

const WELCOME_TEXT = {
  en: {
    title: "Welcome to fast-compute",
    intro: "fast-compute runs your code in an isolated, throw-away Docker container on a compute node and "
      + "charges credit-hours for the time it runs. The easiest way to use it is from an AI assistant "
      + "(Claude) through MCP; you can also call the REST API directly.",
    yourAccount: "Your account",
    limitsLine: (m) => `Up to ${m.max_cpus} CPU · ${m.max_mem_mb} MB · ${m.max_run_seconds}s per job · `
      + `${m.max_concurrent_jobs} jobs at once · network ${m.allow_network ? "allowed" : "not allowed"} · `
      + `balance ${Number(m.balance_hours).toFixed(2)} credit-hours`,
    step1: "1. Get an access token",
    step1Body: "You need a token that starts with ds_. Ask the administrator for one. It is shown only once "
      + "when issued. Keep it secret: whoever has it can run jobs and spend your credits. "
      + "Sign in to this dashboard with the same token.",
    step2: "2. Connect Claude",
    webTitle: "claude.ai or Claude Desktop (custom connector, recommended)",
    webSteps: [
      "Open Settings → Connectors → Add custom connector.",
      "Name it fast-compute and paste this URL (leave the OAuth client ID and secret empty):",
      "Click Connect. A fast-compute page opens: paste your ds_ token and click Approve.",
      "The app gets its own short-lived token that renews automatically; your ds_ token is not shared with it.",
    ],
    ccTitle: "Claude Code",
    ccBody: "Run this once in a terminal (replace ds_YOUR_TOKEN):",
    otherTitle: "Other MCP clients",
    otherBody: "Use Streamable HTTP at the URL below with the header Authorization: Bearer ds_YOUR_TOKEN. "
      + "Clients that only speak stdio can use the mcp-remote bridge (needs Node.js):",
    step3: "3. Run your first job",
    askBody: "Just ask Claude, for example:",
    askExample: "Use fast-compute to run this Python script with 1 CPU and show me the output.",
    toolsBody: "Claude uses these tools: submit_job, list_jobs, get_job, get_job_logs, stop_job, "
      + "get_balance, get_usage, list_env, set_env, delete_env (and the site tools in step 6). "
      + "A submit_job call looks like:",
    restTitle: "Or with the REST API (curl)",
    restBody: "Upload a file or an archive (.zip, .tar, .tar.gz); it is unpacked into /workspace. "
      + "Pass command with --form-string so that ; and other characters are kept.",
    restDocs: "Interactive API docs: ",
    step4: "4. What a job can do",
    envRows: [
      ["Image", (i) => i.images.join(", ") || "python:3.12-slim"],
      ["Files", () => "Your uploaded files (or git clone) land in /workspace, which is also the working directory. It is deleted when the job ends; print results to stdout or keep them small."],
      ["Network", () => "Off by default. Pass network=true (if your account allows it) to reach public websites over HTTP/HTTPS (ports 80 and 443) through a proxy; HTTP_PROXY / HTTPS_PROXY are set, so pip install and requests work. Private, LAN and host addresses stay blocked."],
      ["System", () => "Read-only system, runs as user nobody, no apt. /tmp is small (64 MB). No access to the host, other jobs or Docker."],
      ["Env vars", () => "Variables you save (Dashboard → Environment variables, or set_env) are injected into every job. They are stored encrypted and admins cannot see them. A job's own env overrides them."],
      ["Logs", () => "stdout and stderr are the job's logs: get_job_logs, GET /jobs/{id}/logs, or click a job on the Dashboard."],
    ],
    step5: "5. Credits and billing",
    billing: [
      "1 credit-hour = 1 CPU core + 256 MB RAM for 1 hour.",
      "A job costs run time × max(cpus, mem_mb / 256), charged once when it ends. You pay for the size you reserve, not what you use.",
      "Example: 2 CPU + 256 MB for 30 minutes = 1 credit-hour; 1 CPU + 1024 MB for 1 hour = 4.",
      "A job is refused if your credit cannot cover 60 s, and stopped (out_of_credits) if credit runs out while it runs. Ask the administrator for a top-up.",
    ],
    step6: "6. Host a static web page or browser game",
    sitesIntro: "A site is a set of files (HTML, JS, CSS, images) kept on the server and served read-only at "
      + "a public HTTPS address. Nothing runs on the server, so it is for pure front-end pages. Ask Claude, e.g.:",
    sitesExample: "Publish these four files as a fast-compute site called gun-fu and give me the link.",
    sitesSteps: [
      "put_site_file(site, path, content): upload one file per call (text; or base64 with encoding=\"base64\" for images). Max 5 MB per file, 50 MB per site.",
      "publish_site(site) → { url }: the address is <sites host>/sites/<your name>/<site>/ and serves index.html. Link files with relative paths (game.js, img/a.png).",
      "list_sites, list_site_files, delete_site_file, unpublish_site, delete_site: manage them. Re-uploading a file updates the live site at once.",
      "A site sleeps after an hour without visitors and wakes on the next visit (the first page load takes about a second longer). External fonts and CDNs work.",
    ],
    help: "Full guide: docs/en/user-guide.md in the fast-compute repository.",
    copy: "Copy", copied: "Copied",
    signIn: "Open the dashboard",
  },
  zh: {
    title: "欢迎使用 fast-compute",
    intro: "fast-compute 在计算节点上为你的代码启动一个隔离的、用完即删的 Docker 容器，按运行时间扣除算力时（credit-hour）。"
      + "最简单的用法是通过 MCP 让 AI 助手（Claude）提交任务；也可以直接调用 REST API。",
    yourAccount: "我的账户",
    limitsLine: (m) => `每个任务最多 ${m.max_cpus} CPU · ${m.max_mem_mb} MB · ${m.max_run_seconds} 秒 · `
      + `同时 ${m.max_concurrent_jobs} 个任务 · 网络${m.allow_network ? "可用" : "未开通"} · `
      + `余额 ${Number(m.balance_hours).toFixed(2)} 算力时`,
    step1: "1. 获取访问令牌",
    step1Body: "你需要一个以 ds_ 开头的令牌，请向管理员申请。令牌只在签发时显示一次，请妥善保管："
      + "拿到令牌的人可以运行任务并消耗你的额度。登录本控制台用的也是这个令牌。",
    step2: "2. 连接 Claude",
    webTitle: "claude.ai 或 Claude Desktop（自定义连接器，推荐）",
    webSteps: [
      "打开 设置 → 连接器（Connectors）→ 添加自定义连接器（Add custom connector）。",
      "名称填 fast-compute，URL 粘贴下面这个地址（OAuth Client ID 和 Secret 留空）：",
      "点击连接（Connect），会打开 fast-compute 页面：粘贴你的 ds_ 令牌并点击 Approve。",
      "应用会拿到它自己的短期令牌并自动续期，你的 ds_ 令牌不会交给它。",
    ],
    ccTitle: "Claude Code",
    ccBody: "在终端里运行一次（把 ds_YOUR_TOKEN 换成你的令牌）：",
    otherTitle: "其他 MCP 客户端",
    otherBody: "使用下面的 URL（Streamable HTTP），并带上请求头 Authorization: Bearer ds_YOUR_TOKEN。"
      + "只支持 stdio 的客户端可以用 mcp-remote 桥接（需要 Node.js）：",
    step3: "3. 运行第一个任务",
    askBody: "直接对 Claude 说，例如：",
    askExample: "用 fast-compute 以 1 个 CPU 运行这个 Python 脚本，并把输出给我看。",
    toolsBody: "Claude 会用到这些工具：submit_job、list_jobs、get_job、get_job_logs、stop_job、"
      + "get_balance、get_usage、list_env、set_env、delete_env（以及第 6 步的站点工具）。submit_job 的参数示例：",
    restTitle: "或使用 REST API（curl）",
    restBody: "上传单个文件或压缩包（.zip、.tar、.tar.gz），会解压到 /workspace。"
      + "command 请用 --form-string 传递，避免 ; 等字符被 curl 截断。",
    restDocs: "交互式 API 文档：",
    step4: "4. 任务运行环境",
    envRows: [
      ["镜像", (i) => i.images.join(", ") || "python:3.12-slim"],
      ["文件", () => "上传的文件（或 git clone 的仓库）放在 /workspace，也是工作目录。任务结束后删除；结果请打印到 stdout，或保持较小。"],
      ["网络", () => "默认关闭。传 network=true（需账户已开通）即可通过代理访问公网 HTTP/HTTPS（80 和 443 端口）；已设置 HTTP_PROXY / HTTPS_PROXY，pip install 和 requests 可直接使用。内网、局域网和宿主机地址始终不可访问。"],
      ["系统", () => "系统只读，以 nobody 用户运行，不能 apt。/tmp 很小（64 MB）。无法访问宿主机、其他任务或 Docker。"],
      ["环境变量", () => "你保存的变量（控制台 → 环境变量，或 set_env）会注入到你的每个任务。加密存储，管理员看不到。任务自己的 env 优先。"],
      ["日志", () => "stdout 和 stderr 就是任务日志：get_job_logs、GET /jobs/{id}/logs，或在控制台点击任务查看。"],
    ],
    step5: "5. 额度与计费",
    billing: [
      "1 算力时 = 1 个 CPU 核 + 256 MB 内存运行 1 小时。",
      "任务费用 = 运行时间 × max(cpus, mem_mb / 256)，在任务结束时一次性扣除。按申请的规格计费，而不是实际用量。",
      "例如：2 CPU + 256 MB 运行 30 分钟 = 1 算力时；1 CPU + 1024 MB 运行 1 小时 = 4。",
      "余额不足 60 秒时任务会被拒绝；运行中额度耗尽会被停止（out_of_credits）。需要充值请联系管理员。",
    ],
    step6: "6. 托管静态网页或网页游戏",
    sitesIntro: "站点是一组保存在服务器上的文件（HTML、JS、CSS、图片），以只读方式通过公开的 HTTPS 地址提供。"
      + "服务器端不执行任何代码，适合纯前端页面。直接对 Claude 说，例如：",
    sitesExample: "把这四个文件发布成 fast-compute 站点 gun-fu，并把网址给我。",
    sitesSteps: [
      "put_site_file(site, path, content)：每次上传一个文件（文本；图片等二进制用 base64 并传 encoding=\"base64\"）。单文件最大 5 MB，单站点 50 MB。",
      "publish_site(site) → { url }：网址为 <站点域名>/sites/<你的用户名>/<站点名>/，根路径返回 index.html。文件之间请用相对路径引用（game.js、img/a.png）。",
      "list_sites、list_site_files、delete_site_file、unpublish_site、delete_site：查看和管理。重新上传文件后线上立即生效。",
      "站点 1 小时无人访问会自动休眠，下次访问时自动唤醒（首次加载多约 1 秒）。可以使用外部字体和 CDN。",
    ],
    help: "完整说明：fast-compute 仓库中的 docs/zh/user-guide.md。",
    copy: "复制", copied: "已复制",
    signIn: "打开控制台",
  },
};

function welcomeLang() {
  try { const l = localStorage.getItem(WELCOME_LANG_KEY); if (l === "en" || l === "zh") return l; } catch {}
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}

function codeBlock(text, t) {
  const btn = el("button", { class: "btn copy", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = t.copied; setTimeout(() => btn.textContent = t.copy, 1500); }
    catch { /* clipboard unavailable (http or denied): the text is still selectable */ }
  } }, t.copy);
  return el("div", { class: "codebox" }, el("pre", { class: "logs" }, text), btn);
}

// container: element to fill. opts.me: the signed-in user (optional), opts.dashboardLink: show a link to /ui.
async function renderWelcome(container, opts = {}) {
  let info = { images: ["python:3.12-slim"] };
  try {
    const r = await fetch("/info");
    if (r.ok) { const j = await r.json(); info = { images: j.allowed_images || info.images }; }
  } catch {}
  const draw = (lang) => {
    const t = WELCOME_TEXT[lang];
    const base = location.origin, mcp = base + "/mcp";
    const langBtn = (code, label) => el("button", { class: "btn" + (code === lang ? " primary" : ""), type: "button",
      onclick: () => { try { localStorage.setItem(WELCOME_LANG_KEY, code); } catch {} draw(code); } }, label);
    const firstJob = JSON.stringify({ command: "python main.py",
      files: { "main.py": "import platform\nprint('hello from', platform.node())\nprint(sum(range(10**7)))" },
      cpus: 1, mem_mb: 256, wait: 30 }, null, 1).replace(/\n\s*/g, " ");
    clear(container).append(
      el("div", { class: "panel welcome" },
        el("div", { class: "row", style: "justify-content:space-between;align-items:center" },
          el("h2", { style: "margin:0" }, t.title),
          el("div", { class: "row" }, langBtn("zh", "中文"), langBtn("en", "English"))),
        el("p", {}, t.intro),
        opts.me ? el("p", { class: "muted" }, el("strong", {}, t.yourAccount + ": "), t.limitsLine(opts.me)) : null,
        opts.dashboardLink ? el("p", {}, el("a", { href: "/ui" }, t.signIn + " →")) : null),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step1), el("p", {}, t.step1Body)),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step2),
        el("h3", {}, t.webTitle),
        el("ol", {}, el("li", {}, t.webSteps[0]), el("li", {}, t.webSteps[1], codeBlock(mcp, t)),
          el("li", {}, t.webSteps[2]), el("li", {}, t.webSteps[3])),
        el("h3", {}, t.ccTitle), el("p", {}, t.ccBody),
        codeBlock(`claude mcp add --transport http fast-compute ${mcp} \\\n  --header "Authorization: Bearer ds_YOUR_TOKEN"`, t),
        el("h3", {}, t.otherTitle), el("p", {}, t.otherBody),
        codeBlock(JSON.stringify({ mcpServers: { "fast-compute": { command: "npx",
          args: ["-y", "mcp-remote", mcp, "--header", "Authorization: Bearer ${FC_TOKEN}"],
          env: { FC_TOKEN: "ds_YOUR_TOKEN" } } } }, null, 2), t)),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step3),
        el("p", {}, t.askBody), el("blockquote", {}, t.askExample),
        el("p", {}, t.toolsBody), codeBlock(firstJob, t),
        el("h3", {}, t.restTitle), el("p", {}, t.restBody),
        codeBlock(`export FC=${base} TOKEN=ds_YOUR_TOKEN\n`
          + `curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python main.py" \\\n`
          + `  -F file=@main.py -F cpus=1 -F mem_mb=256 $FC/jobs\n`
          + `curl -s -H "Authorization: Bearer $TOKEN" $FC/jobs/JOB_ID\n`
          + `curl -s -H "Authorization: Bearer $TOKEN" "$FC/jobs/JOB_ID/logs?tail=200"`, t),
        el("p", { class: "muted" }, t.restDocs, el("a", { href: base + "/docs" }, base + "/docs"))),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step4),
        el("table", {}, el("tbody", {}, t.envRows.map(([k, f]) =>
          el("tr", {}, el("th", { scope: "row" }, k), el("td", { class: "wrap" }, f(info))))))),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step5),
        el("ul", {}, t.billing.map(b => el("li", {}, b)))),
      el("div", { class: "panel welcome" },
        el("h2", {}, t.step6),
        el("p", {}, t.sitesIntro), el("blockquote", {}, t.sitesExample),
        el("ol", {}, t.sitesSteps.map(b => el("li", {}, b))),
        codeBlock(JSON.stringify({ site: "gun-fu", path: "index.html", content: "<!doctype html>…" }), t)),
      el("div", { class: "panel welcome" },
        el("p", { class: "muted" }, t.help)));
  };
  draw(welcomeLang());
}
