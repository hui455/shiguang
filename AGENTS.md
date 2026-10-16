# Instructions for AI agents

This repo is an image-generation MCP server. Two deployment targets:

- **Local plugin** for Claude Code / Claude Desktop: see README.md. No server needed.
- **Remote server** for claude.ai web + mobile (custom connector): the procedure below.

If you were asked to "deploy this so claude.ai can connect", follow the remote procedure exactly.

## Remote deployment procedure

### 0. Collect inputs first (ask the user for anything missing; never invent values)

| Input | Notes |
| --- | --- |
| `OPENAI_API_KEY` | Required. Ask the user. |
| `OPENAI_BASE_URL` | Only if the user uses an OpenAI-compatible relay. Must end in `/v1` for most relays. |
| `IMAGE_MODEL` | Default `gpt-image-2.5-flare`. Use what the user names. |
| Public hostname | A domain (or subdomain) whose DNS A/AAAA record points at this server. Check with `dig +short <domain>` vs `curl -s ifconfig.me`. |

Server requirements: outside mainland China (OpenAI blocks it), Docker + Docker Compose v2 (`docker compose version`).

### 1. Get the code

```bash
git clone https://github.com/hui455/openai-image-gen-community.git
cd openai-image-gen-community
```

The repo may be private; if clone fails with 404/auth errors, ask the user to authenticate git (`gh auth login`) on this server.

### 2. Write `.env`

```bash
cp .env.example .env
SECRET=$(openssl rand -hex 24)
```

Set in `.env`:
- `OPENAI_API_KEY`, optional `OPENAI_BASE_URL`, `IMAGE_MODEL`
- `MCP_SECRET=$SECRET` (letters/digits only, at least 24 chars; the server refuses to start otherwise)
- `PUBLIC_BASE_URL=https://<domain>` (no trailing slash)
- `DOMAIN=<domain>` (only used by the bundled Caddy)

Then `chmod 600 .env`. Never commit `.env`, never paste the key or secret into logs, issues or commit messages.

### 3. Decide how HTTPS is terminated

Check what holds ports 80/443: `sudo ss -ltnp | grep -E ':(80|443)\s'`

- **Nothing on 80/443** → use the bundled Caddy (automatic Let's Encrypt):
  `docker compose --profile caddy up -d --build`
- **Nginx / Caddy / Traefik already running** → do NOT start the bundled Caddy. Run
  `docker compose up -d --build` (listens on `127.0.0.1:8000`) and add a site/vhost for
  `<domain>` that proxies to `http://127.0.0.1:8000`. Required for Nginx:
  `proxy_http_version 1.1; proxy_buffering off; proxy_read_timeout 300s;` plus
  `Host` and `X-Forwarded-Proto` headers (full block in DEPLOY.md). Reload the proxy after
  `nginx -t` passes. Get a certificate the way that proxy already does (e.g. certbot).
- **Port 8000 already taken** → change the left side of `127.0.0.1:8000:8000` in
  `docker-compose.yml` and point the proxy at the new port.

Do not stop or reconfigure unrelated services the user already runs. If a change to an
existing proxy is needed, make the smallest addition (one new server block) and keep a
backup of any file you edit.

### 4. Firewall

Ports 80 and 443 must be open to the internet (cloud security group and `ufw`/`firewalld`).
Port 8000 must NOT be exposed publicly; the compose file binds it to 127.0.0.1 only.

### 5. Verify (mandatory)

```bash
docker compose ps
docker compose logs --tail 50 image-gen
scripts/verify.sh "https://<domain>" "$SECRET"
```

The deployment is done only when `verify.sh` prints `ALL CHECKS PASSED`. If a check fails:

| Failure | Likely cause |
| --- | --- |
| healthz not reachable | DNS not pointing here, firewall, proxy vhost missing, container not running |
| not HTTPS / TLS errors | certificate not issued yet (Caddy needs DNS correct + port 80 open), wrong vhost |
| MCP initialize fails but healthz works | proxy buffering on, wrong path, secret mismatch between `.env` and the URL |
| api_key not set | `.env` not loaded; run `docker compose up -d` again after editing `.env` |

Optional real test (costs a little money): call `generate_image` with `quality: "low"` through
a client, or ask the user to try it from claude.ai.

### 6. Report back to the user

Tell the user, in their language:
1. The connector URL: `https://<domain>/<MCP_SECRET>/mcp` — this URL is a password; say so.
2. How to add it: claude.ai → connector settings → add custom connector → paste the URL, no auth fields.
3. How to update later: `git pull && docker compose up -d --build` (add `--profile caddy` if used).
4. Suggest setting a spending limit on the OpenAI key.

## Layout

```
plugins/openai-image-gen/server.py   # the MCP server (stdio by default, HTTP when MCP_TRANSPORT=http)
Dockerfile, docker-compose.yml       # remote deployment
scripts/verify.sh                    # post-deploy checks
DEPLOY.md                            # human-readable deployment guide (Chinese)
```
