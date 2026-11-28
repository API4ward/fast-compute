# fast-compute

Compute service: users submit code with an access token (REST or MCP), and each job runs in an
isolated Docker container, with credit billing and CPU / memory usage accounting.

| Directory | What it is | Runs on |
|---|---|---|
| [`server/`](server/) | DockerServer: users and tokens, credits and billing, MCP (`/mcp`, `/mcp-admin`), product homepage, admin and user web pages (English/Chinese; the admin's Settings set up and debug AWS Lambda nodes), scheduler that runs jobs locally or dispatches them to compute nodes | the bridge (aws-london) |
| [`node/`](node/) | Compute node agent: a small HTTP API the server calls over the tailnet; runs each job in a locked-down container and reports usage | compute machines (e.g. mini) |
| [`lambda-node/`](lambda-node/) | AWS Lambda compute node: function image + handler (one invocation = one job) and `deploy.sh` that sets it up in AWS; connected and debugged in `/admin-ui` → Settings; the server talks to it through S3 and Lambda | AWS Lambda |

The two parts talk only over HTTP (see the API table in `node/README.md`), so each has its own
`requirements.txt` and virtualenv and is deployed independently: on the bridge use `server/`, on a
compute node use `node/`.

## Configuration and secrets

All configuration lives inside each part's directory and is git-ignored:

- `server/.env` (template: `server/.env.example`)
- `server/data/settings.json` (what admins save in `/admin-ui` → Settings; secrets in it are
  encrypted with `DOCKERSERVER_SECRET_KEY` from `server/.env`)
- `node/config.env` (created with a fresh token by `node/start.sh` on first run)

Never commit `.env`, `config.env`, node tokens or admin tokens.
