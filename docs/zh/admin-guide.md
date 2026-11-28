# 管理员指南

[English](../en/admin-guide.md) · [用户指南](user-guide.md) · [部署](deployment.md)

管理员负责用户、限额、额度和令牌，以及服务器的各项设置（AWS Lambda 计算节点、首页产品顾问）。
按设计，**管理员不能打开、停止或查看其他用户单个任务的日志**，
无论通过网页、REST 还是 MCP。管理员只能看到任务的*元数据*（状态、节点、规格、时间、费用、
CPU/内存）。影响用户正在运行任务的唯一方式是**封禁**该用户。管理员自己的任务和普通用户一样
（通过 `/mcp` 或 `/jobs`）。

## 令牌

- 令牌是随机生成的（`ds_…`），签发时**只显示一次**，服务器只保存 SHA-256 哈希，无法找回，只能重新签发。
- 一个用户可以有多个令牌（例如笔记本、CI），可以单独吊销。
- 停用用户后，他的所有令牌都会失效。
- 第一个管理员在服务器上用 CLI 创建（见下文）。

## 网页：https://compute.llmfreedomforge.com/admin-ui

用管理员令牌登录（令牌只保存在当前标签页）。宽屏时功能导航在左侧边栏，手机上变成顶部可横向滑动的标签栏。

**界面语言**：页面支持中文和英文。点登录框上或“退出登录”旁边的 **中文 / EN** 按钮即可切换，页面会刷新，
但不用重新登录。这个选择保存在浏览器里，与产品主页和用户页面 `/ui` 共用；第一次打开时跟随浏览器语言。
服务器返回的消息（错误信息、诊断详情和提示）仍是英文。

- **用户**（Users）：创建用户（令牌只显示一次）；打开用户可以修改限额和角色、签发/吊销令牌、
  **停用账户** / **启用账户**、**封禁（终止全部任务）**。
- **额度**（在用户详情里）：按小时充值或调整（负数为扣减），可带备注；查看该用户的额度流水
  （充值、调整、任务扣费，以及每笔之后的余额）。
- **任务**（Jobs）：所有用户任务的只读列表（可按状态筛选）。
- **节点**（Nodes）：所有计算节点，包括 `DOCKERSERVER_NODES` 中配置的和在 **设置** 里添加的（卡片上的小标签
  显示 `.env` 或“后台添加”；AWS Lambda 节点带“AWS Lambda”标记，显示并发槽位、单任务规格、函数、网络和
  最长运行时间，而不是 CPU/内存，并有一个 **设置** 按钮直接打开它的设置）：在线 / 离线 / 排空中、
  最后一次联系时间和延迟、节点自己报告的 CPU 和内存用量 / 容量（`/v1/info`）、功能、允许的镜像、
  本服务器在该节点上正在运行的任务（只有元数据）以及 24 小时统计（成功 / 失败 / 丢失、CPU 小时）。
  打开页面时会检查所有节点，页面显示期间每 20 秒刷新一次；服务器还会在后台每 30 秒检查一次
  （`DOCKERSERVER_NODE_HEARTBEAT`），所以没人看的时候“无法连接的起始时间”也是准的。节点自己报告的任务数与
  本服务器记录的不一致时会给出提示（例如有残留容器）。**排空** 让节点不再接新任务，正在运行的任务
  正常结束（重启或升级节点前使用）；**恢复接收任务** 让它重新接任务。排空状态保存在 `server/data/nodes.json`。
  管理员 MCP：`list_nodes`、`set_node_draining`。
- **用量**（Usage）：按日期范围统计每个用户的任务数、运行时间、CPU 小时、GB 小时和费用。
- **网站**（Sites）：每个用户的站点容器汇总（站点数、唤醒中的容器、存储、访问次数、流量、唤醒时长、
  费用），以及所有站点的明细（容器名 `fc-site-<id>`、唤醒/休眠、文件数、最后访问）。只读，看不到文件内容。
  用户详情里也有该用户的站点列表，以及 **删除用户** 按钮。
- **Caddy**：HTTPS 入口。显示 Caddy 管理接口是否在线、正在运行的配置是否与 `server/deploy/Caddyfile` 一致，
  以及正在运行的站点（域名、上游；⚠ 表示该域名不在 `DOCKERSERVER_ALLOWED_HOSTS` 中，连接器登录在该域名上不可用）。
  可以直接编辑 Caddyfile：**校验** 只做语法检查；**应用** 先把新配置加载进 Caddy（失败时 Caddy 保持旧配置），
  成功后才写入文件，并把旧文件保存在 **备份** 中（最近 30 份，位于 `server/data/caddy/backups/`）。
  回滚：在备份上点 **载入编辑器**，再点应用。如果在服务器上手工改了文件，点 **把文件加载到 Caddy**
  即可生效，无需 sudo。修改主域名时仍需同步修改 `.env` 中的 `DOCKERSERVER_ALLOWED_HOSTS` / `PUBLIC_URL` 并重启服务。
  校验和应用始终基于 Caddy 的完整根配置（`/etc/caddy/Caddyfile`，配置项 `DOCKERSERVER_CADDY_ROOT`）：只替换其中
  引入本项目文件的 `import` 行，其他项目引入的 Caddyfile 中的站点保持不变。如果根配置没有引入本项目文件，应用会被拒绝。
- **设置**（Settings）：AWS 账户、Lambda 节点（含诊断、测试任务和函数日志）、部署助手、首页产品顾问，见下一节。

## 设置

在这里配置和调试 AWS Lambda 计算节点以及首页产品顾问，不用改 `server/.env`，也不用重启：所有修改立即生效，
卡片上会显示最近一次是谁、在什么时候修改的。保存的内容在 `server/data/settings.json`（权限 600）中；
AWS 秘密访问密钥和产品顾问的 API 密钥用 `DOCKERSERVER_SECRET_KEY`（`server/.env` 里的这个密钥同时用于
加密用户的环境变量）加密保存，之后不会再显示，接口不会返回，也不会写进日志。请把 `.env` 和 `data/`
一起备份：丢了这个加密密钥，这里保存的 AWS 密钥和 API 密钥就只能重新填写。`server/.env` 中的配置依然有效；两边都配置时以这里为准
（Lambda 节点自己的 `profile` 除外，见下文）。

### AWS 账户

本服务器访问所有 Lambda 节点时使用的访问密钥：调用函数、在 S3 中存取任务文件、读取函数日志。
`lambda-node/deploy.sh` 会为服务器创建一个只有这些权限的 IAM 用户（`fast-compute-server`），
用 `aws iam create-access-key --user-name fast-compute-server` 为它创建访问密钥。

- 卡片显示凭证来源：**已保存在本服务器**、**server/.env** 或 **未配置**，密钥 ID 会部分遮盖（`AKIA…WXYZ`），
  并显示服务器上是否安装了 boto3（没有安装时：在 `server/` 目录下运行 `.venv/bin/pip install -r requirements.txt`，
  然后重启）。
- 表单：**访问密钥 ID**、**秘密访问密钥**（不会预先填入；两项要一起填，只改区域时两项都留空）、**默认区域**
  （添加节点时默认填入）。填好后点 **保存**。点 **删除已保存的凭证**（会先确认）后，改用 `server/.env`
  或服务器默认的 AWS 凭证。
- **检查凭证** 向 AWS 确认这组凭证属于谁（STS GetCallerIdentity，不需要任何权限）：显示账户和 ARN，
  或者显示错误原因和处理建议（例如密钥 ID 不存在、秘密密钥不对、服务器时钟不准）。如果用的是账户的
  root 密钥，会给出警告。
- 每个 Lambda 节点使用凭证的优先级：节点自己的 `profile`（仅限 `.env` 中的节点）> 这里保存的密钥 >
  boto3 默认凭证链（`server/.env` 中的 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`、实例角色等）。

### Lambda 节点

每个 Lambda 节点一张卡片：函数、存储桶、区域、网络、最大并发、镜像、超时设置、活动任务数和是否排空中。
来自 `DOCKERSERVER_NODES` 的节点标有 **server/.env · 只读**（请在 `.env` 中修改并重启）；在这里添加的节点
可以 **编辑** 和 **删除**。

- **添加 Lambda 节点**：名称（字母、数字和 `. _ -`，在所有节点中唯一）、函数（函数名或 ARN）、S3 存储桶和
  区域（填 `deploy.sh` 最后打印的值）、网络（`none`：函数在隔离的 VPC 中，节点只接收**不联网**的任务；
  `internet`：不在 VPC 中，只接收**联网**的任务。必须与函数部署时的设置一致）、最大并发（即函数的预留并发）、
  镜像（逗号分隔，默认 `python:3.12-slim`）。**高级选项**：任务文件前缀（必须以 `fast-compute/` 开头，
  IAM 策略和 2 天自动清理只覆盖这个前缀）、启动超时（30–900 秒，默认 300）、失联判定（20–600 秒，默认 60）、
  每个任务使用全新沙箱（默认开启）。**测试设置** 用尚未保存的值跑一遍诊断；**添加节点** 保存后，节点立即开始接收任务。
- **编辑**：最大并发、镜像、网络、超时和全新沙箱可以在任务运行时修改（对新任务生效）。函数、存储桶、区域和
  前缀只能在节点空闲时修改：先在 **节点** 页 **排空** 它，等任务结束后再保存。名称不能修改（删除后重新添加）。
  有任务在运行时不能 **删除**；删除节点不会动 AWS 中的函数和存储桶。
- 同一个函数只添加一次（无论在这里还是在 `.env` 中）：每个节点各自计算自己的并发槽位。

调试工具（每个 Lambda 节点都有，包括 `.env` 中的节点）：

- **诊断**：以清单形式逐项检查，✓ 通过、**!** 警告、✕ 失败、– 跳过（前面的步骤失败了），每项都有详情、
  提示和耗时。检查项：**boto3 库**；**AWS 凭证**（STS：账户、ARN、凭证来源）；**Lambda 函数**（状态、内存 →
  vCPU 数、超时 → 任务最长运行时间、架构、VPC；VPC 与节点的网络设置不符时给出警告）；**预留并发**（已设置，
  且不低于最大并发）；**异步重试**（必须为 0，否则同一个任务可能运行两次）；**S3 存储桶读写**（在
  `<前缀>_diagnostics/` 下写入、读取并删除一个探测对象，并确认读取不存在的文件时返回“不存在”而不是“无权访问”）；
  **HTTPS 预签名 URL**（模拟函数的实际操作）。如果服务器的 IAM 策略还是旧版 `deploy.sh` 生成的，就没有读取
  预留并发和重试设置的权限，读取尚未写入的任务文件时 S3 也会返回 403：这几项会被跳过或给出警告，并提示重新
  运行 `deploy.sh`。
- **运行测试任务**：在函数上按真实任务的流程运行一段固定的诊断脚本（不会执行管理员输入的命令）；不计费，
  不记为用户任务，运行期间占用一个并发槽位，最多 120 秒。进度每 1.5 秒刷新一次；结束后显示状态、退出码、
  耗时（调用耗时、冷启动 ≈ 收到首次状态的时间、总耗时）和输出：Python 版本、平台、vCPU 数、内存上限、
  `/tmp` 剩余空间，以及能否连上公网（`1.1.1.1:443`）和 S3，并与节点的网络设置对照。节点离线或槽位已满时无法启动。
- **函数日志**：函数在 CloudWatch Logs 中最近 15 分钟 / 1 小时 / 6 小时 / 24 小时的事件（最新的在最后，最多 300 条）：
  Lambda 自己记录的 `START` / `END` / `REPORT` 行（耗时、最大内存用量）、超时和崩溃信息。这里永远不会出现任务的
  输出，任务输出只有任务的主人能看到。函数用 `LOGS=1` 部署时才会写这些日志（`NETWORK=none` 时总会写）；
  出现 “no log group …” 说明它从未写过日志，出现 `AccessDenied` 说明服务器的 IAM 策略还是旧版 `deploy.sh` 生成的（重新运行即可）。
  开启全新沙箱时，每次调用最后都会有一行 `Runtime exited …`，这是正常现象。

在这里修改后，**节点** 页的数据也会同步刷新。更多细节、权限说明和排错表见
[`lambda-node/README.md`](../../lambda-node/README.md)（英文）。

### 部署助手

一个表单（区域、函数名、内存 MB、超时、网络、并发数、是否记录函数日志），生成可以直接 **复制** 的命令。
在装有 aws CLI v2（以 AWS 管理员身份登录）和支持 buildx 的 Docker 的机器上，在本仓库目录中运行：

```sh
REGION=eu-west-2 FUNCTION=fast-compute-job MEMORY_MB=2048 TIMEOUT=900 NETWORK=none CONCURRENCY=10 LOGS=0 sh lambda-node/deploy.sh
```

它会创建或更新：存放任务文件的 S3 存储桶、ECR 镜像、函数的执行角色（本身没有任何权限）、`NETWORK=none`
时的隔离 VPC、函数（预留并发，异步重试次数为 0），以及供本服务器使用的 IAM 用户，最后打印创建访问密钥的命令。
接下来：在 **AWS 账户** 中保存密钥，在 **Lambda 节点** 中添加节点（点 **用这些值添加节点** 会自动填好表单），
然后运行 **诊断** 和一次测试任务。`LOGS=1` 允许 Lambda 写入函数自己的日志组；代价是：任务代码能读到函数
角色的凭证，因此任务也能往这个日志组里写内容（只能写这一个日志组）。

### 首页产品顾问

即 `/` 上的产品顾问聊天（见[下文](#产品主页与产品顾问)）。卡片显示配置来源（**已保存在本服务器** /
**server/.env** / **未配置**）和当前模式（大模型回答，或仅内置 FAQ）。字段：**接口地址（Base URL）**（任意
OpenAI 兼容接口）、**模型**（留空则使用 `.env` 中的值；两处都没有模型时只用 FAQ 回答）、**每次回答的最大
token 数**（16–32000）、**API 密钥**（不会显示；留空则保留当前的密钥，也可以删除这里保存的密钥）。这里保存
的值会逐项覆盖 `DOCKERSERVER_SALES_BASE_URL`、`_MODEL`、`_MAX_TOKENS` 和 `_API_KEY`，并立即生效（与 `.env`
相同的值不会另外保存，所以之后修改 `.env` 仍然有效）；**恢复为 server/.env** 会删掉这些值。**测试** 用当前设置
提一个问题（不受访客频率限制），显示回答、模式、耗时，出错时显示服务商返回的错误。

## 停用与封禁

| 操作 | 令牌 | 新任务 | 正在运行的任务 |
|---|---|---|---|
| 停用（`set_user_status active=false`，`POST /admin/users/{id}/disable`） | 拒绝（401） | 不能提交 | **继续运行** |
| 封禁（`suspend_user`，`POST /admin/users/{id}/suspend`） | 拒绝（401） | 不能提交 | **被终止** → `killed_by_admin`，按已运行时间计费 |
| 启用（`set_user_status active=true`，`POST /admin/users/{id}/enable`） | 恢复可用 | 可以提交 | — |

管理员不能停用或封禁自己。

**删除用户**（`delete_user`，`DELETE /admin/users/{id}`，网页需要再输入一次用户名确认）：终止该用户
正在运行的任务，停止并删除他的所有站点容器和站点文件，删除保存的环境变量，吊销所有令牌。任务和额度
流水保留用于统计；用户从列表中隐藏，用户名可以重新使用。**不可撤销。** 管理员不能删除自己。

## 管理 MCP：`/mcp-admin`（只接受管理员令牌，其他令牌返回 403）

```bash
claude mcp add --transport http dockerserver-admin https://compute.llmfreedomforge.com/mcp-admin \
  --header "Authorization: Bearer ds_管理员令牌"
```

作为 claude.ai / Claude Desktop 自定义连接器：URL 填 `https://compute.llmfreedomforge.com/mcp-admin`，
在授权页面粘贴**管理员**令牌（普通用户令牌会被拒绝）。该连接器拿到的 OAuth 令牌只能用于 `/mcp-admin`。

| 工具 | 用途 |
|---|---|
| `list_users` | 所有用户的角色、限额、启用状态、余额 |
| `create_user` | `name`、`role`（user/admin），可选 `max_cpus`、`max_mem_mb`、`max_run_seconds`、`max_concurrent_jobs`、`allow_network`，返回中含令牌 |
| `set_user_status` | `user_id`、`active` |
| `set_user_limits` | `user_id` 加上任意限额字段、`role` |
| `suspend_user` | `user_id` → 账号被停用，返回被终止任务的 id |
| `list_user_jobs` | `user_id?`、`status?`、`limit`，只有元数据 |
| `add_credits` | `user_id`、`hours`（负数为扣减）、`reason`（`topup`/`adjust`）、`note` |
| `get_ledger` | `user_id`、`limit` |
| `usage_report` | `start?`、`end?`（UTC） |
| `issue_token` | `user_id`、`name` → 新令牌（只显示一次） |
| `revoke_token` | `token_id` |
| `delete_user` | `user_id` → 被终止的任务、被删除的站点 |
| `list_sites` | `user_id?` → 每个站点的容器状态和用量（不含文件内容） |
| `list_nodes` | 计算节点：健康状况、容量、其上的任务（元数据）、24 小时统计 |
| `set_node_draining` | `name`、`draining` |
| `lambda_settings` | 只读：当前使用的 AWS 凭证（不含密钥）、Lambda 节点、产品顾问设置 |
| `diagnose_lambda_node` | `name` → 诊断清单 |
| `lambda_function_logs` | `name`、`minutes`（默认 60，最多 1440）→ 函数的 CloudWatch 日志 |

设置只能通过网页或 REST API 修改，MCP 只能查看。

## 管理 REST API

| 方法 | 路径 | |
|---|---|---|
| POST | `/admin/users` | `{name, role?, max_cpus?, max_mem_mb?, max_run_seconds?, max_concurrent_jobs?, allow_network?}` → 用户信息 + `token` |
| GET | `/admin/users`、`/admin/users/{id}` | 列表 / 单个用户 |
| PATCH | `/admin/users/{id}` | 限额、`role`、`active` |
| POST | `/admin/users/{id}/disable` · `/enable` · `/suspend` | 账号状态 |
| GET / POST | `/admin/users/{id}/tokens` | 查看 / 签发（`{name}`） |
| DELETE | `/admin/tokens/{id}` | 吊销 |
| POST | `/admin/users/{id}/credits` | `{hours, reason: "topup"|"adjust", note}` |
| GET | `/admin/users/{id}/ledger?limit=` | 额度流水 |
| GET | `/admin/jobs?user_id=&status=&limit=` | 任务元数据（不含命令、代码、日志） |
| GET | `/admin/usage?start=&end=` | 用量报表 |
| GET | `/admin/sites?user_id=` | 站点、容器状态和用量 |
| DELETE | `/admin/users/{id}` | 删除用户及其站点、容器 |
| GET | `/admin/nodes` | 计算节点（`source`：`env` 或 `admin`） |
| POST | `/admin/nodes/{name}/drain` · `/undrain` | 排空 / 恢复节点 |
| GET | `/admin/settings` | 当前使用的 AWS 凭证（不含密钥）、Lambda 节点、默认值、产品顾问 |
| PUT / DELETE | `/admin/settings/aws` | 保存 `{access_key_id, secret_access_key, region}`（或只传 `{region}`）/ 删除 |
| POST | `/admin/settings/aws/check` | `{ok, source, account, arn, ms, error, hint}` |
| POST | `/admin/settings/lambda/nodes` | 添加 Lambda 节点（201） |
| PUT / DELETE | `/admin/settings/lambda/nodes/{name}` | 修改 / 删除（`.env` 中的节点或有任务运行时返回 409） |
| POST | `/admin/settings/lambda/validate` | 诊断尚未保存的节点设置 |
| POST | `/admin/settings/lambda/nodes/{name}/diagnose` | 诊断节点 |
| POST | `/admin/settings/lambda/nodes/{name}/test-job` | 启动测试任务（202） |
| GET | `/admin/settings/lambda/tests/{id}` | 测试任务状态 |
| GET | `/admin/settings/lambda/nodes/{name}/logs?minutes=60` | 函数日志 |
| PUT / DELETE | `/admin/settings/advisor` | 保存 `{base_url, model, max_tokens, api_key}` / 恢复为 `.env` |
| POST | `/admin/settings/advisor/test` | `{question}` → `{ok, reply, mode, ms, error}` |
| GET | `/admin/leads` | 首页提交的试用申请 |

```bash
export DS=https://compute.llmfreedomforge.com ADMIN=ds_管理员令牌
# 创建一个限额更大的用户（返回里有令牌，把它交给用户）
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"name":"alice","max_cpus":4,"max_mem_mb":8192,"max_run_seconds":7200,"max_concurrent_jobs":3}' \
  $DS/admin/users
# 充值 50 算力时
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"hours":50,"reason":"topup","note":"11月"}' $DS/admin/users/2/credits
# 11 月用量
curl -s -H "Authorization: Bearer $ADMIN" "$DS/admin/usage?start=2026-11-01T00:00:00&end=2026-12-01T00:00:00"
# 诊断 Lambda 节点 aws-lambda
curl -s -X POST -H "Authorization: Bearer $ADMIN" $DS/admin/settings/lambda/nodes/aws-lambda/diagnose
```

## 产品主页与产品顾问

`/` 是公开的产品主页（中英文）。右下角的聊天窗口是产品顾问：

- 未配置大模型时，按内置 FAQ 回答。要接入大模型，可以在 **设置 → 首页产品顾问** 中配置任意 OpenAI 兼容接口
  （立即生效，还能当场测试），也可以写在 `server/.env` 中再重启：`DOCKERSERVER_SALES_BASE_URL`（默认
  `https://api.openai.com/v1`，例如 `https://api.deepseek.com/v1`、`https://api.x.ai/v1`）、
  `DOCKERSERVER_SALES_MODEL` 和 `DOCKERSERVER_SALES_API_KEY`（`DOCKERSERVER_SALES_MAX_TOKENS` 限制回答长度）。
  设置页中保存的值优先于 `.env`。模型和 API 密钥都配置好后，`GET /sales/info` 返回 `"mode": "llm"`。
- 模型只能看到 `server/dockerserver/sales.py` 中的产品介绍（`KNOWLEDGE`），产品变化时请同步修改；它看不到任何用户数据。
- 限制：每个访客 IP 每小时最多 `DOCKERSERVER_SALES_RATE_LIMIT` 个问题（30），全站每天最多
  `DOCKERSERVER_SALES_DAILY_LIMIT` 次大模型回答（500），超出后改用 FAQ 回答。设置页中的测试不计入访客限制。
- 「申请试用」表单保存在 `server/data/leads.jsonl`，用管理员令牌调用 `GET /admin/leads` 查看。
  可选的 `DOCKERSERVER_SALES_CONTACT` 会在主页显示联系方式。

## 限额与默认值

- 新用户获得 `DOCKERSERVER_DEFAULT_CREDIT_HOURS`（1000）额度，以及 `server/.env` 中的默认限额：
  `DOCKERSERVER_DEFAULT_MAX_CPUS`（1）、`_MAX_MEM_MB`（256）、`_MAX_RUN_SECONDS`（600）、
  `_MAX_CONCURRENT_JOBS`（2）。修改默认值不影响已有用户。
- 即使用户限额允许，比所有计算节点都大的任务也会被拒绝。
- 联网需要用户的 `allow_network`，同时节点上设置 `COMPUTENODE_ALLOW_NETWORK=true`。
- 站点：每个用户最多 `max_sites` 个（新用户默认 `DOCKERSERVER_DEFAULT_MAX_SITES` = 3，0 表示不能建站），
  在用户详情或 `set_user_limits` 中修改。其余站点参数见部署文档。

## CLI（在服务器上的 `server/` 目录中执行）

```bash
.venv/bin/python -m dockerserver.cli create-admin [名字]      # 创建第一个管理员，打印令牌
.venv/bin/python -m dockerserver.cli issue-token <名字>       # 给已有用户签发新令牌
.venv/bin/python -m dockerserver.cli list-users               # 用户、限额、余额
.venv/bin/python -m dockerserver.cli credits <名字> <小时> [--note 备注]   # 负数为扣减
```
