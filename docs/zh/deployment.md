# 部署

[English](../en/deployment.md) · [用户指南](user-guide.md) · [管理员指南](admin-guide.md)

## 架构

```
用户 / AI 助手 ──HTTPS──▶ Cloudflare ──▶ Caddy :443 ──▶ DockerServer 127.0.0.1:8800
                                          （aws-london 网关：API、MCP、网页、计费）
                                                   │  HTTP + Bearer 令牌，仅 tailnet
                                                   ▼
                               计算节点 agent :8900（例如 mini，100.126.20.127）
                               └─ 每个任务一个严格隔离的 Docker 容器
```

- **网关**（`server/`，运行在 aws-london）：用户、令牌、额度、调度、MCP、网页。只监听
  127.0.0.1:8800，由 Caddy 提供 HTTPS。8800 端口不能对公网开放。
- **计算节点**（`node/`）：执行任务。只监听自己的 Tailscale IP，只接受网关的 IP 并校验每个节点
  各自的令牌。容器隔离由节点自己强制执行。
- 没有可用节点时，网关用本机 Docker 运行任务（兜底 / 开发用）。

## 网关（`server/`）

线上网关运行在 `/home/ubuntu/projects/fast-compute/server`。部署脚本使用它们所在的目录
（`install-service.sh` 会把路径填入 unit 文件；`install-caddy.sh` 引入该目录下的
`deploy/Caddyfile`），相对路径的 `DOCKERSERVER_DATA_DIR` 以 `server/` 为基准。

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env        # 然后编辑 .env
.venv/bin/python -m dockerserver.cli create-admin admin   # 打印第一个管理员令牌
sudo sh deploy/install-service.sh             # systemd 服务（只有 unit 文件放到 /etc）
```

服务用户需要访问本机 Docker（`docker` 组）：静态站点的容器运行在网关上；使用本机执行任务时也需要。

### 配置：`server/.env`

所有配置都在项目里的 `.env`（不进 git，权限 600）。应用启动时自己读取（可用
`DOCKERSERVER_ENV_FILE` 指定其他路径），其中的值优先于继承的环境变量，日志会显示
`config file: …/.env (loaded)`。完整模板：`server/.env.example`。

| 配置项 | 默认值 | |
|---|---|---|
| `DOCKERSERVER_DATA_DIR` | `data`（相对 `server/`） | SQLite 数据库、任务日志、管理后台的设置（`settings.json`） |
| `DOCKERSERVER_ALLOWED_IMAGES` / `DEFAULT_IMAGE` | `python:3.12-slim` | 镜像白名单 / 默认镜像 |
| `DOCKERSERVER_MAX_UPLOAD_BYTES` | 50 MB | 上传 / 工作目录大小上限 |
| `DOCKERSERVER_DEFAULT_CREDIT_HOURS` | 1000 | 新用户初始额度 |
| `DOCKERSERVER_MONITOR_INTERVAL` | 10 | 采样用量 / 检查额度的间隔（秒） |
| `DOCKERSERVER_MIN_START_SECONDS` | 60 | 启动任务所需的最少余额（按任务规格的秒数） |
| `DOCKERSERVER_DEFAULT_MAX_CPUS` / `_MAX_MEM_MB` / `_MAX_RUN_SECONDS` / `_MAX_CONCURRENT_JOBS` | 1 / 256 / 600 / 2 | 新用户的限额 |
| `DOCKERSERVER_ALLOWED_HOSTS` | `127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com` | `/mcp`、`/mcp-admin` 接受的 Host |
| `DOCKERSERVER_AUTH_FAIL_LIMIT` / `_WINDOW` | 20 / 300 | 每个 IP 在窗口内的错误令牌次数，超过返回 429 |
| `DOCKERSERVER_PUBLIC_URL` | `https://compute.llmfreedomforge.com` | OAuth issuer / 资源 URL |
| `DOCKERSERVER_OAUTH` | `true` | 为 MCP 自定义连接器（claude.ai / Desktop）启用 OAuth |
| `DOCKERSERVER_OAUTH_REDIRECT_HOSTS` | `claude.ai,claude.com` | 客户端注册时允许的 https 回调域名（另外允许 `http://localhost`） |
| `DOCKERSERVER_OAUTH_ACCESS_TTL` / `_REFRESH_TTL` | 3600 / 2592000 | OAuth 令牌有效期（秒） |
| `DOCKERSERVER_SECRET_KEY` | 首次启动时生成 | Fernet 密钥，用于加密用户的环境变量，以及在 `/admin-ui` → 设置 中保存的密钥（`data/settings.json` 中的 AWS 秘密访问密钥、产品顾问 API 密钥），缺失时自动写入 `.env`。**请随 `.env` 一起备份；丢失后这些内容都无法解密**（用户要重新设置变量，管理员要重新填写密钥）。 |
| `DOCKERSERVER_NODES` | — | 计算节点（见下） |
| `DOCKERSERVER_LOCAL_EXECUTION` | 自动 | 是否也用本机 Docker（默认：没有可用节点时才用） |
| `DOCKERSERVER_NODE_LOST_GRACE` | 120 | 节点失联多少秒后任务标记为 `lost` |
| `DOCKERSERVER_QUEUE_RETRY` / `_QUEUE_TIMEOUT` | 5 / 3600 | 排队重试间隔 / 最长等待 |
| `DOCKERSERVER_NODE_INFO_TTL` / `_NODE_POLL_INTERVAL` | 5 / 2 | 容量缓存 / 状态轮询（秒） |
| `DOCKERSERVER_NODE_HEARTBEAT` | 30 | 管理后台“节点”页的节点健康探测间隔（秒，0 = 关闭） |

修改后执行：`sudo systemctl restart dockerserver`。管理员在 `/admin-ui` → **设置** 中保存的内容（AWS 凭证、
Lambda 节点、首页产品顾问）无需重启即可生效，保存在 `server/data/settings.json`（权限 600）中。

### 静态站点

站点文件保存在网关本机 `server/data/sites/u<用户id>/<站点>/`，每个站点在网关的 Docker 上由一个
`nginx:alpine` 小容器提供（`fc-site-<id>`：只读根文件系统、uid 101、无任何 capability、32 MB、0.25 CPU、
站点目录只读挂载）。容器在内部网络 `fc-sites` 上（`internal`、禁止容器互访），没有外网，只有网关进程能
连到它。访问 `/sites/...` 时网关按需启动容器并转发；空闲 1 小时后停止。因此服务用户需要能访问网关本机的
Docker（`docker` 组）。第一次使用会拉取 `nginx:alpine` 镜像。

用户的页面必须和 `/ui`、`/admin-ui` 不同源（否则页面里的 JS 能读到同源的令牌），所以站点用单独的域名：
`DOCKERSERVER_SITES_URL`。该域名只提供 `/sites/...`，其他域名上的 `/sites/...` 会 307 跳转过去。

| 配置项 | 默认值 | |
|---|---|---|
| `DOCKERSERVER_SITES_URL` | 空（= `PUBLIC_URL`，不推荐） | 站点的公开根地址，例如 `https://sites-18-171-232-21.sslip.io` |
| `DOCKERSERVER_SITE_IDLE_SECONDS` | 3600 | 多少秒无访问后停止容器 |
| `DOCKERSERVER_SITE_MAX_AWAKE` | 20 | 同时唤醒的容器上限（超出时停掉最久未访问的） |
| `DOCKERSERVER_SITE_MAX_FILE_BYTES` / `_MAX_BYTES` / `_MAX_FILES` | 5 MB / 50 MB / 1000 | 单文件 / 单站点 / 文件数上限 |
| `DOCKERSERVER_DEFAULT_MAX_SITES` | 3 | 新用户的站点数上限 |
| `DOCKERSERVER_SITE_CREDIT_PER_HOUR` | 0 | 容器每唤醒 1 小时扣多少算力时（0 = 不收费，仍记录用量） |
| `DOCKERSERVER_SITE_IMAGE` / `_NETWORK` | `nginx:alpine` / `fc-sites` | 镜像 / Docker 网络 |

### 在网关上配置计算节点

```sh
DOCKERSERVER_NODES='[{"name":"mini","url":"http://100.126.20.127:8900","token_env":"DOCKERSERVER_NODE_TOKEN_MINI"}]'
DOCKERSERVER_NODE_TOKEN_MINI=<节点的 COMPUTENODE_TOKEN>
```

JSON 外面要保留单引号。令牌变量为空的节点会被跳过（日志里有警告）。在网关上检查：
`sh deploy/check-node.sh`（打印 `/v1/info`，不会打印令牌）。重启后日志会显示
`execution: compute nodes=['mini@http://…'], local docker=off`。

调度规则：在放得下的节点中优先选空闲 CPU（其次内存）最多的；节点返回 409 就换下一个；都放不下
则 `queued` 并重试；比所有节点都大则提交时直接拒绝。按节点测得的运行时间计费，扣费后在节点上
删除该任务。

#### AWS Lambda 节点

Lambda 节点把任务发给 AWS Lambda 函数（每个任务一次调用），而不是节点 agent。`lambda-node/deploy.sh`
会创建函数、存储桶、相关角色以及供服务器使用的 IAM 用户（加上 `LOGS=1` 时函数还能写 CloudWatch 日志）。
最方便的接入方式是 `/admin-ui` → **设置**：保存 AWS 访问密钥、添加节点，再运行诊断和测试任务；节点立即开始
接收任务，无需重启（见[管理员指南](admin-guide.md#设置)）。也可以写在 `server/.env` 里：在 `DOCKERSERVER_NODES`
中加一条 `"type": "lambda"` 的条目（在设置页中只读显示），例如
`{"name":"aws-lambda","type":"lambda","function":"fast-compute-job","bucket":"…","region":"eu-west-2","max_concurrency":10,"network":"none"}`，
并配置 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`（或用 `"profile"`）。设置页中保存的密钥优先于 `AWS_*`
变量，节点自己的 `profile` 又优先于两者。服务端需要 `boto3`（已在 `requirements.txt`）。只有 Docker 计算节点都
没有空位时才会把任务派给 Lambda（本机 Docker 若也运行任务，则排在所有节点之后，包括 Lambda）；任务必须放得进一次调用（函数内存，每 1769 MB 对应 1 个 vCPU），超时不超过函数超时
− 30 秒（最多 870 秒），镜像为 `python:3.12-slim`，网络设置要与函数一致（`none` = 无公网的隔离 VPC，
`internet` = 有公网）。部署脚本、设计、限制和排错见 [`lambda-node/README.md`](../../lambda-node/README.md)。

### MCP 连接器的 OAuth

claude.ai 和 Claude Desktop 的自定义连接器不支持自定义 header，而是使用 MCP OAuth 2.1。服务端用 MCP SDK
的认证组件实现：`/.well-known/oauth-protected-resource/mcp`（以及 `/mcp-admin`）、
`/.well-known/oauth-authorization-server`、`/register`（动态注册，回调地址限制在 `OAUTH_REDIRECT_HOSTS`）、
`/authorize`（PKCE S256）→ `/oauth/consent`（用户粘贴 `ds_` 令牌，有 CSRF 防护和限流）、`/token`、`/revoke`。
`/mcp` 返回 401 时带 `WWW-Authenticate: Bearer resource_metadata=…`。OAuth 令牌（`dsa_` 访问令牌 1 小时，
`dsr_` 刷新令牌 30 天，每次刷新轮换）只存哈希，绑定用户、批准时用的 `ds_` 令牌、客户端和资源；用户被停用/
封禁或该 `ds_` 令牌被吊销时随即失效。OAuth 令牌只能用于签发它的 MCP 地址，不能用于 REST API。
`Bearer ds_…` 方式不受影响。

带环境变量的任务只会派给 `/v1/info` 中声明了 `"features": ["env"]` 的节点（支持环境变量的新版 agent）；
如果没有节点支持，这类任务会被拒绝并给出明确的错误。请更新所有节点的 agent（在 `node/` 里 `git pull`，然后重启 `./start.sh`）。

## 计算节点（`node/`）

需要：Docker、Python 3、Tailscale（节点绑定在自己的 tailnet IP 上）。

```bash
cd node
./start.sh          # 首次运行：创建 .venv 和 config.env（含新生成的令牌，权限 600）
.venv/bin/python -m computenode token    # 打印令牌 → 填到网关的 .env 里
```

`node/config.env`（不进 git；全部配置项见 `node/computenode/config.py`）：

| 配置项 | 默认（模板） | |
|---|---|---|
| `COMPUTENODE_TOKEN` | 自动生成 | 网关使用的 Bearer 令牌 |
| `COMPUTENODE_LISTEN` / `_PORT` | `tailscale` / 8900 | 绑定 tailnet IP |
| `COMPUTENODE_ALLOWED_CLIENTS` | `100.82.2.33` | 允许的来源 IP（网关） |
| `COMPUTENODE_ALLOWED_IMAGES` | `python:3.12-slim` | |
| `COMPUTENODE_ALLOW_NETWORK` | `false` | 是否允许任务联网 |
| `COMPUTENODE_RESERVE_CPUS` / `_RESERVE_MEM_MB` | 2 / 8192 | 给机器自身保留的资源 |
| `COMPUTENODE_MAX_CPUS` / `_MAX_MEM_MB` | 0 = Docker 总量 − 保留 | 对外提供的容量 |
| `COMPUTENODE_MAX_TIMEOUT_SECONDS` | 86400 | |
| `COMPUTENODE_RETAIN_SECONDS` | 3600 | 结束后无人取走的任务在此时间后删除 |

`start.sh` 优先使用已设置的 `DOCKER_HOST`（在 mini 上默认用桌面用户的 Docker Desktop socket）。
节点 API 见 `node/README.md`。

## HTTPS：Caddy

- 站点配置在项目里：`server/deploy/Caddyfile`。`/etc/caddy/Caddyfile` 只有一行
  `import <路径>/deploy/Caddyfile`；由 `sudo sh deploy/install-caddy.sh` 设置（需要时从官方 apt 源
  安装 Caddy，并把 `caddy` 用户加入属主所在组以便读取）。
- 在 `/admin-ui` → **Caddy** 里编辑（检查、应用、备份；通过 Caddy 本机管理接口 `127.0.0.1:2019`，无需 sudo）。
  手工修改文件后，用 `sudo systemctl reload caddy` 或管理页面的 **把文件加载到 Caddy** 生效。
  配置项：`DOCKERSERVER_CADDYFILE`（默认 `deploy/Caddyfile`）、`DOCKERSERVER_CADDY_ADMIN`
  （默认 `http://127.0.0.1:2019`，只能是本机地址）、`DOCKERSERVER_CADDY_MANAGE=false` 关闭此功能。
  在页面上做的修改会让 git 工作区里的 `deploy/Caddyfile` 变为已修改，`git pull` 前请先提交（或 `git stash`）。
- DNS：域名经过 Cloudflare 代理。Caddyfile 信任 Cloudflare 的 IP 段，从 `CF-Connecting-IP` 取
  访客真实 IP（按 IP 限制认证失败需要用到）。如果关闭 Cloudflare 代理，请把 A 记录指向网关公网
  IP，并删掉这几行。
- 站点域名（`DOCKERSERVER_SITES_URL`）在 Caddyfile 中有单独的一段，只转发 `/sites/*`；Caddy 自动申请证书。
  默认用 sslip.io（`sites-<IP 用横线>.sslip.io`，无需配置 DNS）；公网 IP 变化后要同时改 Caddyfile 和 `.env`。
  想换成自己的域名（如 `sites.llmfreedomforge.com`）：添加 A 记录（可走 Cloudflare 代理），改 Caddyfile 的站点名和
  `DOCKERSERVER_SITES_URL`，然后重载 Caddy、重启服务。
- AWS 安全组：入站只开放 TCP 80 和 443。

## 运维

| 任务 | 命令 |
|---|---|
| 网关状态 / 日志 | `systemctl status dockerserver`，`journalctl -u dockerserver -f` |
| Caddy 日志 | `journalctl -u caddy -f` |
| 节点是否可达 | `sh deploy/check-node.sh` |
| 健康检查 | `curl https://compute.llmfreedomforge.com/health` |
| 更新网关 | `git pull`，`.venv/bin/pip install -r requirements.txt`，`sudo systemctl restart dockerserver` |
| 更新节点 | `git pull`，重新运行 `./start.sh`（该节点上正在运行的任务会被杀） |
| 测试 | `cd server && .venv/bin/python -m pytest -q`（没有 Docker 权限时跳过 Docker 测试） |

网关重启时，会结束之前仍处于活动状态的任务（在节点上杀掉并标记为 `error`），并补扣所有未扣的
费用。请定期备份 `server/data/`（SQLite 数据库 `dockerserver.db`、`settings.json` 等），并同时备份
`server/.env`：其中的 `DOCKERSERVER_SECRET_KEY` 用于解密保存的密钥。
