# Optional: Caddy reverse proxy (Tailscale HTTPS)

Migradora works standalone on `WEBUI_PORT` (default 8080). Use this only if you already run Caddy on `caddy_net` like your other services.

## 1. Join `caddy_net` (optional compose overlay)

```bash
docker network create caddy_net   # once, if missing

docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build
```

`docker-compose.caddy.yml` attaches migradora to `caddy_net` and binds the web UI to **127.0.0.1** only (Caddy proxies it; no public host port).

## 2. Add a Caddy site (your `~/docker/caddy` stack)

In `~/docker/caddy/.env` pick a free HTTPS port, e.g.:

```bash
HTTPS_PORT_3=8487
UPSTREAM_3=migradora-orchestrator:8008
```

Set migradora `.env`:

```bash
WEBUI_PORT=8008
```

In `Caddyfile`:

```caddy
https://{$DOMAIN}:{$HTTPS_PORT_3} {
        tls {$CERT_FILE} {$KEY_FILE}
        reverse_proxy {$UPSTREAM_3}
}
```

Recreate both stacks:

```bash
cd ~/git/migradora
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --force-recreate

cd ~/docker/caddy
docker compose up -d --force-recreate
```

Open: `https://regxa.tailb529a.ts.net:8487/`

## 3. Without Caddy overlay

Publish directly on the host (Tailscale IP or `tailscale serve`):

```bash
docker compose up -d --build --force-recreate
curl http://127.0.0.1:8008/health
```

`http://100.100.x.x:8008/` works on your tailnet if the port is published on `0.0.0.0`.
