# 用户指南

[English](../en/user-guide.md) · [管理员指南](admin-guide.md) · [部署](deployment.md)

fast-compute 会在计算节点上用一个隔离的、用完即删的 Docker 容器运行你的代码，并按运行时间扣除
额度。可以让 AI 助手通过 **MCP** 使用，也可以用 **REST API**，在**网页**上查看自己的任务。

- 地址：**https://compute.llmfreedomforge.com**
- 需要一个**访问令牌**（以 `ds_` 开头），向管理员申请。请保密：拿到令牌的人可以用你的额度跑任务。
  令牌只在签发时显示一次。

## 1. 基本概念

| 术语 | 含义 |
|---|---|
| 任务 | 在一个新容器里执行的一条 shell 命令，例如 `python main.py`。 |
| 工作目录 | 你的文件在 `/workspace`（也是当前目录），可写，任务结束后删除。 |
| 镜像 | 目前只允许 `python:3.12-slim`。 |
| 规格 | `cpus`（可以是小数，如 `0.5`）和 `mem_mb`。默认 **1 CPU、256 MB**。 |
| 超时 | `timeout_seconds`，到时间任务被杀。默认等于你的最长运行时间。 |
| 额度小时 | 计费单位：**1 个 CPU 核 + 256 MB 内存运行 1 小时**。 |

### 计费

```
费用（额度小时）= 运行秒数 / 3600 × max(cpus, mem_mb / 256)
```

按**申请的规格** × 实际经过时间计费，不按实际用量。

| 规格 | 运行时间 | 费用 |
|---|---|---|
| 1 CPU，256 MB | 1 小时 | 1 |
| 2 CPU，256 MB | 30 分钟 | 1 |
| 1 CPU，1024 MB | 1 小时 | 4（内存是较大的那一维） |
| 0.5 CPU，128 MB | 10 分钟 | 约 0.083 |

- 新账号默认有 **1000 额度小时**（管理员可另行配置）。
- 可用额度不够按申请规格运行 60 秒时，任务会被**拒绝（HTTP 402）**。
- 运行**过程中**额度用完，任务会被停止，状态为 `out_of_credits`。
- 每个任务在结束时扣一次费，金额显示在任务的 `cost_hours` 字段。

### 限额

每个账号有管理员设置的限额：最多 CPU、最多内存、单个任务最长运行时间、同时运行的任务数、是否
允许联网。新账号默认 1 CPU、256 MB、600 秒、同时 2 个任务、不联网。超出限额会被拒绝
（HTTP 400/403/429）。可以用 `get_balance`（MCP）、`GET /me` 或在网页上查看。

### 隔离：任务能做什么、不能做什么

- ✅ 读写 `/workspace`；使用 `/tmp`（64 MB，不能执行程序）；输出到 stdout/stderr（即日志）。
- ❌ **默认不能联网**：需要你的账号允许、提交时传 `network=true`，**并且**计算节点也开放网络。
  所以一般不能 `pip install`，请只用标准库，或把纯 Python 依赖和代码一起上传（例如放在
  `vendor/` 目录并加入 `sys.path`）。
- ❌ 系统只读：不能 `apt`，不能写 `/workspace` 和 `/tmp` 以外的地方。
- ❌ 看不到宿主机、其他任务或 Docker socket。以 `nobody` 用户运行，最多 256 个进程，内存限制
  不含 swap（超出会被杀，提示 out of memory）。

### 任务状态

| 状态 | 含义 |
|---|---|
| `queued` | 排队中，等待计算节点有空闲资源（会自动重试）。 |
| `preparing` | 正在复制文件 / 克隆 git 仓库。 |
| `running` | 运行中。 |
| `succeeded` | 退出码 0。 |
| `failed` | 退出码非 0，看 `exit_code` 和日志。 |
| `timeout` | 到达超时被杀。 |
| `stopped` | 你自己停止的。 |
| `out_of_credits` | 额度用完（或不够启动）。 |
| `killed_by_admin` | 你的账号被管理员暂停。 |
| `lost` | 计算节点失联，按最后一次联系上的时间计费。 |
| `error` | 看 `error` 字段（例如 git 地址错误、克隆失败）。 |

## 2. 连接 AI 助手（MCP）——推荐

**Claude Code：**

```bash
claude mcp add --transport http dockerserver https://compute.llmfreedomforge.com/mcp \
  --header "Authorization: Bearer ds_你的令牌"
```

**Claude Desktop**（需要 Node.js，通过 `mcp-remote` 桥接）。设置 → Developer → Edit Config，
在 `claude_desktop_config.json` 中加入以下内容，然后重启 Claude Desktop：

```json
{
  "mcpServers": {
    "dockerserver": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://compute.llmfreedomforge.com/mcp",
               "--header", "Authorization: Bearer ${DOCKERSERVER_TOKEN}"],
      "env": { "DOCKERSERVER_TOKEN": "ds_你的令牌" }
    }
  }
}
```

之后直接说，例如：*"用 2 个 CPU 在 dockerserver 上运行这个脚本，把输出给我看。"*

**claude.ai / Claude Desktop 自定义连接器（不需要 Node.js）：** 设置 → Connectors（连接器）→
*Add custom connector* → URL 填 `https://compute.llmfreedomforge.com/mcp`（OAuth 客户端 ID/密钥留空）
→ *Connect*。会打开一个 fast-compute 页面：粘贴你的 `ds_…` 令牌，点 **Approve**。应用拿到的是
它自己的令牌（有效期 1 小时，自动续期），不会拿到你的 `ds_` 令牌。吊销你的 `ds_` 令牌或账号被
禁用后，连接器随即失效。

### 10 个工具（`/mcp`）——只作用于你自己的任务

| 工具 | 参数 | 返回 |
|---|---|---|
| `submit_job` | `command`（必填）；代码用 `files`（`{"路径": "文本"}`）**或** `git_url`（公开 https）；可选 `cpus`、`mem_mb`、`timeout_seconds`、`network`、`image`、`wait`（0–60 秒） | 任务信息；带 `wait` 时，结束后还返回 `logs_tail` |
| `list_jobs` | `status`、`limit`（20） | 你的任务，最新的在前 |
| `get_job` | `job_id` | 状态、退出码、时间、`cost_hours`、CPU/内存平均值和峰值 |
| `get_job_logs` | `job_id`、`tail`（200；0 = 全部） | stdout+stderr（运行中也能看） |
| `stop_job` | `job_id` | 已停止的任务（按已运行时间计费） |
| `get_balance` | — | `balance_hours`、`available_hours`、你的限额 |
| `get_usage` | `start`、`end`（ISO，UTC）、`include_jobs` | 总计和每日的 CPU 小时、GB 小时、费用 |
| `list_env` | `reveal`（false） | 你保存的环境变量（默认遮盖值，`reveal` 时显示） |
| `set_env` | `name`、`value` | 保存 / 替换一个变量（你的所有任务都会带上） |
| `delete_env` | `name` | 删除一个变量 |

`submit_job` 参数示例：

```json
{"command": "python main.py", "files": {"main.py": "print(sum(range(10**7)))"},
 "cpus": 1, "mem_mb": 256, "wait": 30}
```

## 3. REST API

每个请求都要带 `Authorization: Bearer ds_你的令牌`。交互式文档：
https://compute.llmfreedomforge.com/docs

```bash
export DS=https://compute.llmfreedomforge.com TOKEN=ds_你的令牌
```

### 提交任务：`POST /jobs`（multipart 表单）

| 字段 | 说明 |
|---|---|
| `command` | **必填**，在 `/workspace` 中用 `sh -c` 执行 |
| `file` | 单个文件，或 `.zip` / `.tar` / `.tar.gz` / `.tgz`（解压到 `/workspace`），最大 50 MB |
| `git_url` | 公开的 `https://` 仓库（浅克隆） |
| `cpus`、`mem_mb`、`timeout_seconds` | 规格 / 超时 |
| `network` | `true` 表示请求联网（见隔离说明） |
| `image` | 默认 `python:3.12-slim` |

> 用 curl 时 `command` 请用 **`--form-string`**。用 `-F` 的话 curl 会把 `;` 当作选项分隔符，
> 把 `echo a; echo b` 这样的命令悄悄截断。

```bash
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python main.py" \
  -F file=@main.py $DS/jobs
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python train.py" \
  -F file=@project.zip -F cpus=2 -F mem_mb=1024 -F timeout_seconds=1800 $DS/jobs
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python -m pytest -q" \
  -F git_url=https://github.com/OWNER/REPO $DS/jobs
```

### 查看、停止、列出

```bash
curl -s -H "Authorization: Bearer $TOKEN" $DS/jobs/42                 # 状态、费用、用量
curl -s -H "Authorization: Bearer $TOKEN" "$DS/jobs/42/logs?tail=100"  # 输出
curl -s -H "Authorization: Bearer $TOKEN" -X POST $DS/jobs/42/stop     # 停止
curl -s -H "Authorization: Bearer $TOKEN" "$DS/jobs?status=running&limit=20"
curl -s -H "Authorization: Bearer $TOKEN" $DS/me                       # 限额、balance_hours
curl -s -H "Authorization: Bearer $TOKEN" "$DS/me/usage?start=2026-11-01T00:00:00"
```

### Python

```python
import time, requests
DS, H = "https://compute.llmfreedomforge.com", {"Authorization": "Bearer ds_你的令牌"}
job = requests.post(f"{DS}/jobs", headers=H, data={"command": "python main.py", "mem_mb": "512"},
                    files={"file": ("main.py", b"print('hello')")}).json()
while job["status"] in ("queued", "preparing", "running"):
    time.sleep(2)
    job = requests.get(f"{DS}/jobs/{job['id']}", headers=H).json()
print(job["status"], job["exit_code"], job["cost_hours"])
print(requests.get(f"{DS}/jobs/{job['id']}/logs", headers=H).text)
```

## 4. 环境变量

把变量（API 密钥、配置等）保存一次，之后你运行的每个任务都会自动带上；单个任务的 `env` 会覆盖同名变量
（只对该任务生效）。

- **在哪里设置：** `/ui` 页面的 *Environment variables* 区域；MCP 工具 `list_env` / `set_env` /
  `delete_env`；或 REST：`GET /me/env`（默认遮盖，`?reveal=true` 显示值）、
  `PUT /me/env/{NAME}`（body `{"value": "..."}`）、`DELETE /me/env/{NAME}`。
- **单个任务：** MCP 用 `submit_job(..., env={"NAME": "value"})`，REST 用表单字段
  `env='{"NAME":"value"}'`（JSON）。
- **规则：** 名称为 `[A-Za-z_][A-Za-z0-9_]*`（≤ 64 字符）；值 ≤ 4096 字符；最多 50 个变量，总计
  ≤ 32 KiB。保留名称（会被拒绝）：`PATH`、`HOME`、`HOSTNAME`、`PWD`、`PYTHONHOME`、`PYTHONUNBUFFERED`，
  以及以 `LD_` 或 `DOCKERSERVER_` 开头的名称。
- **隐私：** 值加密存储，只有你自己能看到（管理员看不到），不会写进日志；单个任务的值完全不保存，
  任务里只记录变量**名称**（`env_names`）。

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value":"sk-..."}' $DS/me/env/OPENAI_API_KEY
curl -s -H "Authorization: Bearer $TOKEN" --form-string command='python -c "import os; print(os.environ[\"MODE\"])"' \
  -F env='{"MODE":"test"}' $DS/jobs
```

## 5. 网页

https://compute.llmfreedomforge.com/ui ：粘贴令牌后可以看到余额、限额、你的任务（点开可看日志、
停止任务）和每日用量。令牌只保存在当前浏览器标签页里。

## 6. 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| **401** | 令牌缺失、错误或已吊销，或账号被禁用。找管理员。 |
| **402** 额度不足 | 余额不够按这个规格运行 60 秒。减小规格或请管理员充值。 |
| **400** "exceeds your limit" | 降低 `cpus` / `mem_mb` / `timeout_seconds`，或请管理员提高限额。 |
| **400** "larger than any compute node" | 没有节点能容纳这个规格，请减小。 |
| **403** 联网 | 你的账号没有开放联网。 |
| **429** too many active jobs | 等正在运行的任务结束（或停掉一些）。 |
| **429** too many failed authentication attempts | 你的 IP 输错令牌次数太多，等约 5 分钟。 |
| 任务一直 `queued` | 所有节点都在忙，有空闲时会自动开始（最多等 1 小时）。 |
| `failed` 且没有输出 | 看 `exit_code`；137 一般是被杀（超内存时 `error` 会写 out of memory）。 |
| curl 命令在 `;` 处被截断 | 用 `--form-string command=...`。 |
| `ModuleNotFoundError` | 不能联网就不能 `pip install`，请把纯 Python 依赖和代码一起上传。 |
