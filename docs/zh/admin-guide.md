# 管理员指南

[English](../en/admin-guide.md) · [用户指南](user-guide.md) · [部署](deployment.md)

管理员负责用户、限额、额度和令牌。按设计，**管理员不能打开、停止或查看其他用户单个任务的日志**，
无论通过网页、REST 还是 MCP。管理员只能看到任务的*元数据*（状态、节点、规格、时间、费用、
CPU/内存）。影响用户正在运行任务的唯一方式是**暂停**该用户。管理员自己的任务和普通用户一样
（通过 `/mcp` 或 `/jobs`）。

## 令牌

- 令牌是随机生成的（`ds_…`），签发时**只显示一次**，服务器只保存 SHA-256 哈希，无法找回，只能重新签发。
- 一个用户可以有多个令牌（例如笔记本、CI），可以单独吊销。
- 禁用用户后，他的所有令牌都会失效。
- 第一个管理员在服务器上用 CLI 创建（见下文）。

## 网页：https://compute.llmfreedomforge.com/admin-ui

用管理员令牌登录（令牌只保存在当前标签页）。

- **Users（用户）**：创建用户（令牌只显示一次）；打开用户可以修改限额和角色、签发/吊销令牌、
  **禁用账号** / **启用账号**、**暂停（杀掉所有任务）**。
- **额度**（在用户详情里）：按小时充值或调整（负数为扣减），可带备注；查看该用户的额度流水
  （充值、调整、任务扣费，以及每笔之后的余额）。
- **Jobs（任务）**：所有用户任务的只读列表（可按状态筛选）。
- **Usage（用量）**：按日期范围统计每个用户的任务数、运行时间、CPU 小时、GB 小时和费用。

## 禁用与暂停

| 操作 | 令牌 | 新任务 | 正在运行的任务 |
|---|---|---|---|
| 禁用（`set_user_status active=false`，`POST /admin/users/{id}/disable`） | 拒绝（401） | 不能提交 | **继续运行** |
| 暂停（`suspend_user`，`POST /admin/users/{id}/suspend`） | 拒绝（401） | 不能提交 | **被杀** → `killed_by_admin`，按已运行时间计费 |
| 启用（`set_user_status active=true`，`POST /admin/users/{id}/enable`） | 恢复可用 | 可以提交 | — |

管理员不能禁用或暂停自己。

## 管理 MCP：`/mcp-admin`（只接受管理员令牌，其他令牌返回 403）

```bash
claude mcp add --transport http dockerserver-admin https://compute.llmfreedomforge.com/mcp-admin \
  --header "Authorization: Bearer ds_管理员令牌"
```

| 工具 | 用途 |
|---|---|
| `list_users` | 所有用户的角色、限额、启用状态、余额 |
| `create_user` | `name`、`role`（user/admin），可选 `max_cpus`、`max_mem_mb`、`max_run_seconds`、`max_concurrent_jobs`、`allow_network`，返回中含令牌 |
| `set_user_status` | `user_id`、`active` |
| `set_user_limits` | `user_id` 加上任意限额字段、`role` |
| `suspend_user` | `user_id` → 账号被禁用，返回被杀任务的 id |
| `list_user_jobs` | `user_id?`、`status?`、`limit`，只有元数据 |
| `add_credits` | `user_id`、`hours`（负数为扣减）、`reason`（`topup`/`adjust`）、`note` |
| `get_ledger` | `user_id`、`limit` |
| `usage_report` | `start?`、`end?`（UTC） |
| `issue_token` | `user_id`、`name` → 新令牌（只显示一次） |
| `revoke_token` | `token_id` |

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

```bash
export DS=https://compute.llmfreedomforge.com ADMIN=ds_管理员令牌
# 创建一个限额更大的用户（返回里有令牌，把它交给用户）
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"name":"alice","max_cpus":4,"max_mem_mb":8192,"max_run_seconds":7200,"max_concurrent_jobs":3}' \
  $DS/admin/users
# 充值 50 额度小时
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"hours":50,"reason":"topup","note":"11月"}' $DS/admin/users/2/credits
# 11 月用量
curl -s -H "Authorization: Bearer $ADMIN" "$DS/admin/usage?start=2026-11-01T00:00:00&end=2026-12-01T00:00:00"
```

## 限额与默认值

- 新用户获得 `DOCKERSERVER_DEFAULT_CREDIT_HOURS`（1000）额度，以及 `server/.env` 中的默认限额：
  `DOCKERSERVER_DEFAULT_MAX_CPUS`（1）、`_MAX_MEM_MB`（256）、`_MAX_RUN_SECONDS`（600）、
  `_MAX_CONCURRENT_JOBS`（2）。修改默认值不影响已有用户。
- 即使用户限额允许，比所有计算节点都大的任务也会被拒绝。
- 联网需要用户的 `allow_network`，同时节点上设置 `COMPUTENODE_ALLOW_NETWORK=true`。

## CLI（在服务器上的 `server/` 目录中执行）

```bash
.venv/bin/python -m dockerserver.cli create-admin [名字]      # 创建第一个管理员，打印令牌
.venv/bin/python -m dockerserver.cli issue-token <名字>       # 给已有用户签发新令牌
.venv/bin/python -m dockerserver.cli list-users               # 用户、限额、余额
.venv/bin/python -m dockerserver.cli credits <名字> <小时> [--note 备注]   # 负数为扣减
```
