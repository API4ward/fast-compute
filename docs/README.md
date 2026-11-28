# fast-compute docs / 文档

Public address / 公网地址：**https://compute.llmfreedomforge.com**

| | English | 中文 |
|---|---|---|
| Users: MCP, REST API, web page, billing, limits, troubleshooting<br>用户：MCP、REST API、网页、计费、限额、常见问题 | [en/user-guide.md](en/user-guide.md) | [zh/user-guide.md](zh/user-guide.md) |
| Admins: users, tokens, credits, disable vs. suspend, Settings (AWS Lambda nodes, homepage advisor), admin MCP, CLI<br>管理员：用户、令牌、额度、停用与封禁、设置（AWS Lambda 节点、首页产品顾问）、管理 MCP、CLI | [en/admin-guide.md](en/admin-guide.md) | [zh/admin-guide.md](zh/admin-guide.md) |
| Operators: architecture, `.env`, compute nodes, systemd, Caddy, operations<br>运维：架构、`.env`、计算节点、systemd、Caddy、日常运维 | [en/deployment.md](en/deployment.md) | [zh/deployment.md](zh/deployment.md) |

Quick start / 快速开始（Claude Code）：

```bash
claude mcp add --transport http dockerserver https://compute.llmfreedomforge.com/mcp \
  --header "Authorization: Bearer ds_YOUR_TOKEN"
```
