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

目前线上网关运行在 `/home/ubuntu/projects/dockerserver`（代码与 `server/` 相同）；
`deploy/dockerserver.service` 和 `deploy/Caddyfile` 里写的是这个路径，如果从其他目录部署需要修改。

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env        # 然后编辑 .env
.venv/bin/python -m dockerserver.cli create-admin admin   # 打印第一个管理员令牌
sudo sh deploy/install-service.sh             # systemd 服务（只有 unit 文件放到 /etc）
```

只有使用本机执行时，服务用户才需要访问 Docker（`docker` 组）。

### 配置：`server/.env`

所有配置都在项目里的 `.env`（不进 git，权限 600）。应用启动时自己读取（可用
`DOCKERSERVER_ENV_FILE` 指定其他路径），其中的值优先于继承的环境变量，日志会显示
`config file: …/.env (loaded)`。完整模板：`server/.env.example`。

| 配置项 | 默认值 | |
|---|---|---|
| `DOCKERSERVER_DATA_DIR` | `./data` | SQLite 数据库和任务日志 |
| `DOCKERSERVER_ALLOWED_IMAGES` / `DEFAULT_IMAGE` | `python:3.12-slim` | 镜像白名单 / 默认镜像 |
| `DOCKERSERVER_MAX_UPLOAD_BYTES` | 50 MB | 上传 / 工作目录大小上限 |
| `DOCKERSERVER_DEFAULT_CREDIT_HOURS` | 1000 | 新用户初始额度 |
| `DOCKERSERVER_MONITOR_INTERVAL` | 10 | 采样用量 / 检查额度的间隔（秒） |
| `DOCKERSERVER_MIN_START_SECONDS` | 60 | 启动任务所需的最少余额（按任务规格的秒数） |
| `DOCKERSERVER_DEFAULT_MAX_CPUS` / `_MAX_MEM_MB` / `_MAX_RUN_SECONDS` / `_MAX_CONCURRENT_JOBS` | 1 / 256 / 600 / 2 | 新用户的限额 |
| `DOCKERSERVER_ALLOWED_HOSTS` | `127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com` | `/mcp`、`/mcp-admin` 接受的 Host |
| `DOCKERSERVER_AUTH_FAIL_LIMIT` / `_WINDOW` | 20 / 300 | 每个 IP 在窗口内的错误令牌次数，超过返回 429 |
| `DOCKERSERVER_NODES` | — | 计算节点（见下） |
| `DOCKERSERVER_LOCAL_EXECUTION` | 自动 | 是否也用本机 Docker（默认：没有可用节点时才用） |
| `DOCKERSERVER_NODE_LOST_GRACE` | 120 | 节点失联多少秒后任务标记为 `lost` |
| `DOCKERSERVER_QUEUE_RETRY` / `_QUEUE_TIMEOUT` | 5 / 3600 | 排队重试间隔 / 最长等待 |
| `DOCKERSERVER_NODE_INFO_TTL` / `_NODE_POLL_INTERVAL` | 5 / 2 | 容量缓存 / 状态轮询（秒） |

修改后执行：`sudo systemctl restart dockerserver`。

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
- 修改后：`sudo systemctl reload caddy`。
- DNS：域名经过 Cloudflare 代理。Caddyfile 信任 Cloudflare 的 IP 段，从 `CF-Connecting-IP` 取
  访客真实 IP（按 IP 限制认证失败需要用到）。如果关闭 Cloudflare 代理，请把 A 记录指向网关公网
  IP，并删掉这几行。
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
费用。SQLite 数据库是 `server/data/dockerserver.db`，请定期备份。
