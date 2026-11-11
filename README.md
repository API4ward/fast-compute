# fast-compute

Compute service: users submit code with an access token (REST or MCP), and each job runs in an
isolated Docker container, with credit billing and CPU / memory usage accounting.

| Directory | What it is | Runs on |
|---|---|---|
| [`server/`](server/) | DockerServer: users and tokens, credits and billing, MCP (`/mcp`, `/mcp-admin`), admin and user web pages, scheduler that runs jobs locally or dispatches them to compute nodes | the bridge (aws-london) |
| [`node/`](node/) | Compute node agent: a small HTTP API the server calls over the tailnet; runs each job in a locked-down container and reports usage | compute machines (e.g. mini) |

The two parts talk only over HTTP (see the API table in `node/README.md`), so each has its own
`requirements.txt` and virtualenv and is deployed independently: on the bridge use `server/`, on a
compute node use `node/`.

## Configuration and secrets

All configuration lives inside each part's directory and is git-ignored:

- `server/.env` (template: `server/.env.example`)
- `node/config.env` (created with a fresh token by `node/start.sh` on first run)

Never commit `.env`, `config.env`, node tokens or admin tokens.
