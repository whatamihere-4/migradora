# Optional: Caddy reverse proxy (Tailscale HTTPS)

Migradora works standalone on `WEBUI_PORT` (default 8080). Use this only if you already run Caddy on `caddy_net` like your other services.

## 1. Migradora stack (join `caddy_net`)

```bash
docker network create caddy_net   # once, if missing
```

In migradora `.env` set the **internal** HTTP port (must match `UPSTREAM_3` in Caddy):

```bash
WEBUI_PORT=8008
```

Start with the Caddy overlay (binds UI to localhost on the host; Caddy reaches the container via `caddy_net`):

```bash
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build --force-recreate
```

The overlay registers network alias **`migradora`** so `UPSTREAM_3=migradora:8008` resolves.

Sanity check from the host:

```bash
curl -sf http://127.0.0.1:8008/health
```

From any container on `caddy_net` (e.g. Caddy):

```bash
docker run --rm --network caddy_net curlimages/curl:latest \
  curl -sf http://migradora:8008/health
```

## 2. Caddy stack

In `~/docker/caddy/.env`:

```bash
HTTPS_PORT_3=8008
UPSTREAM_3=migradora:8008
```

`HTTPS_PORT_3` is the **TLS port you open in the browser**. `UPSTREAM_3` is plain HTTP to the migradora container (no `https://`).

**Publish the new port in Caddy's `docker-compose.yml`** — same as `8486` and `5000`. If this line is missing, the browser gets `ERR_CONNECTION_REFUSED` even when the Caddyfile block exists:

```yaml
ports:
  - "${TAILSCALE_IP}:${HTTPS_PORT_1}:${HTTPS_PORT_1}"
  - "${TAILSCALE_IP}:${HTTPS_PORT_2}:${HTTPS_PORT_2}"
  - "${TAILSCALE_IP}:${HTTPS_PORT_3}:${HTTPS_PORT_3}"   # add this
```

Recreate Caddy after editing `.env`, `Caddyfile`, and ports:

```bash
cd ~/docker/caddy
docker compose up -d --force-recreate
```

Open: `https://regxa.tailb529a.ts.net:8008/`

## 3. Without Caddy overlay

Publish directly on the host (Tailscale IP or `tailscale serve`):

```bash
docker compose up -d --build --force-recreate
curl http://127.0.0.1:8080/health
```

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `ERR_CONNECTION_REFUSED` in browser | Caddy not listening on `HTTPS_PORT_3` — port not published in Caddy `docker-compose.yml`, or Caddy not recreated |
| Caddy `502` / bad gateway | Wrong `UPSTREAM_3` hostname or port; migradora not on `caddy_net`; `WEBUI_PORT` mismatch |
| Works on host `curl 127.0.0.1:8008` but not via Caddy | Caddy not on `caddy_net`, or wrong upstream |
| `migradora` name not found | Recreate with `docker-compose.caddy.yml` (network alias) |

Quick checks:

```bash
# Caddy listening on tailnet?
ss -tlnp | grep 8008

# Migradora on caddy_net?
docker inspect migradora-orchestrator --format '{{json .NetworkSettings.Networks}}' | jq

# Caddy can reach upstream?
docker exec <caddy-container> wget -qO- http://migradora:8008/health
```
