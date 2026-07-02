# Caddy reverse proxy (required)

Migradora does **not** publish the web UI on the host. The dashboard is only reachable through your existing Caddy stack on the external Docker network `caddy_net`.

Caddy reaches migradora at **`http://migradora:WEBUI_PORT`** (`migradora` is a Docker network alias on `caddy_net`).

## 1. Create `caddy_net` (once)

```bash
docker network create caddy_net   # skip if it already exists
```

## 2. Migradora

In migradora `.env`, set the **internal** HTTP port (must match the host in your Caddy upstream):

```bash
WEBUI_PORT=8080
```

Start:

```bash
docker compose up -d --build --force-recreate
```

Sanity check from any container on `caddy_net`:

```bash
docker run --rm --network caddy_net curlimages/curl:latest \
  curl -sf http://migradora:8080/health
```

Or from the host via the container:

```bash
docker compose exec orchestrator curl -sf http://127.0.0.1:${WEBUI_PORT:-8080}/health
```

## 3. Caddy stack

Point your Caddy upstream at **`migradora`** (not `migradora-orchestrator`).

Example `.env` for a Caddy compose project:

```bash
TAILSCALE_IP=100.x.x.x
HTTPS_PORT_3=8008
UPSTREAM_3=migradora:8080
```

`HTTPS_PORT_3` is the TLS port you open in the browser. `UPSTREAM_3` is plain HTTP to the migradora container (no `https://`).

**Publish the listener port** in Caddy's `docker-compose.yml` (Tailscale IP only — not `0.0.0.0`):

```yaml
ports:
  - "${TAILSCALE_IP}:${HTTPS_PORT_3}:${HTTPS_PORT_3}"
```

Example `Caddyfile` block:

```caddyfile
{$TAILSCALE_IP}:{$HTTPS_PORT_3} {
	reverse_proxy {$UPSTREAM_3}
}
```

Recreate Caddy after editing `.env`, `Caddyfile`, and ports:

```bash
cd ~/docker/caddy
docker compose up -d --force-recreate
```

Open: `https://your-machine.tailnet.ts.net:8008/`

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `network caddy_net not found` | Run `docker network create caddy_net` |
| `ERR_CONNECTION_REFUSED` in browser | Caddy not listening on `HTTPS_PORT_3` — port not published, or Caddy not recreated |
| Caddy `502` / bad gateway | Wrong upstream host/port; migradora not on `caddy_net`; `WEBUI_PORT` mismatch |
| `migradora` name not found | Recreate migradora: `docker compose up -d --force-recreate` |

Quick checks:

```bash
./scripts/check-caddy-upstream.sh

# Caddy listening on tailnet?
ss -tlnp | grep 8008

# Migradora on caddy_net?
docker inspect migradora-orchestrator --format '{{json .NetworkSettings.Networks}}' | jq

# Caddy can reach upstream?
docker exec <caddy-container> wget -qO- http://migradora:8080/health
```
