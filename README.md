# Migradora — Gofile → Filester Mirror (JDownloader2)

Production Docker pipeline that mirrors large video libraries from **Gofile.io** to **Filester.me** using **JDownloader2** (local Deprecated API, no MyJDownloader cloud account).

## Features

- Gofile downloads via JD2's maintained Gofile plugin
- Local Deprecated API on port 3128 (no external services)
- Serial pipeline: one download at a time, then split → upload → cleanup → next
- SQLite queue — idempotent, resumable across restarts
- Automatic file splitting for uploads over 10 GB
- FastAPI dashboard (`/health`, `/status`, `/jobs`)
- Filester storage monitoring with auto-pause

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit GOFILE_FOLDER_URLS, FILESTER_API_KEY
```

### 2. First-run JDownloader setup

**Do not** copy anything into `data/jd2/config/cfg/` before JD2 has started once — a partial `cfg/` folder breaks the container init script.

```bash
# Ensure config volume is empty (see troubleshooting if you already copied files)
mkdir -p data/jd2/config   # empty directory only

docker compose up -d jdownloader

# Wait ~2 minutes until web UI loads:
#   http://your-vps:5800

# Enable local Deprecated API (port 3128)
chmod +x scripts/jd2-enable-api.sh
./scripts/jd2-enable-api.sh
docker compose restart jdownloader

# Verify API
curl http://localhost:3128/help

# Start orchestrator (pipeline + dashboard)
docker compose up -d orchestrator
```

In the JD2 web UI (http://your-vps:5800), also confirm:
- Settings → Advanced → `RemoteAPI` → Deprecated API **enabled**
- Deprecated API localhost only: **disabled**
- Max simultaneous downloads: **1**
- Default download folder: `/output`

### 3. Discover and run

```bash
docker compose exec orchestrator python -m migradora discover
curl http://localhost:8080/status
docker compose logs -f orchestrator jdownloader
```

The pipeline runs automatically inside the orchestrator — no separate worker containers.

## Architecture

```
jdownloader   → Gofile downloads (JD2 plugin)
orchestrator  → discovery, pipeline coordinator, dashboard, monitors
SQLite queue  → coordinates jobs at ./data/state/queue.db
```

**Serial pipeline** (one file at a time):

1. Claim next `pending` job from queue
2. `POST /linkgrabberv2/addLinks` to JD2 (one URL)
3. Poll until download finished
4. Split if > 9.5 GiB
5. Upload part(s) to Filester, verify, delete local files
6. Mark `uploaded`, proceed to next job

## Services

| Service | Port | Role |
|---------|------|------|
| `orchestrator` | `${DASHBOARD_PORT}` | API dashboard + pipeline |
| `jdownloader` | `5800` (UI), `3128` (API) | Downloads |

## CLI

```bash
docker compose exec orchestrator python -m migradora discover
docker compose exec orchestrator python -m migradora status
docker compose exec orchestrator python -m migradora resume
```

## HTTP API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | JD2 API + pipeline health |
| `GET /status` | Queue stats, pipeline phase, Filester storage |
| `GET /jobs?status=failed` | List jobs |
| `POST /discover` | Crawl Gofile folders via JD2 linkgrabber |
| `POST /resume` | Resume paused queue |
| `POST /pause` | Pause queue |

## VPN (optional — Gofile IP blocks)

Gofile free downloads are often limited **per IP**, not per VPS disk usage. The old orchestrator also paused on **account** traffic (~222 GB lifetime on a Gofile token) — that monitor is gone, but the pause may still be stuck in SQLite. Clear it with:

```bash
docker compose exec orchestrator python -m migradora resume
```

### Enable PIA via gluetun

Add to `.env`:

```bash
VPN_ENABLED=true
PIA_OPENVPN_USER=your_pia_username
PIA_OPENVPN_PASSWORD=your_pia_password
PIA_SERVER_REGIONS=Netherlands,Switzerland,France,Germany
```

Start with VPN (JD2 downloads go through PIA; orchestrator stays on normal network):

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn up -d
```

Requires Docker Compose v2.23+ (`ports: !override` in the VPN overlay). Check with `docker compose version`.

JD2 web UI and API move to gluetun's ports (same host ports `5800` / `3128`).

### Rotate egress IP

```bash
chmod +x scripts/vpn-rotate.sh scripts/vpn-status.sh
./scripts/vpn-status.sh
./scripts/vpn-rotate.sh
docker compose exec orchestrator python -m migradora resume
```

Or via API:

```bash
curl -X POST http://localhost:8080/vpn/rotate
```

With `VPN_ROTATE_ON_BAN=true`, the pipeline auto-rotates VPN when JD2 reports a Gofile traffic/block error.

## Monitoring

```bash
docker compose logs -f orchestrator jdownloader
curl http://localhost:8080/status | jq
tail -f data/logs/migradora.jsonl
```

## Resuming after restart

```bash
docker compose up -d
```

Queue state persists in SQLite. Stale `downloading` jobs reset to `pending` after `STALE_JOB_TIMEOUT_SEC`.

## Large file splitting

Files over `FILESTER_MAX_FILE_BYTES` are split with `split(1)`. Reassemble manually:

```bash
cat video.part*.mp4 > video.mp4
```

## Warnings

1. **Filester 10 GB account storage** may halt migration early — monitor `/status`
2. **JD2 uses significant RAM** (~512MB–1GB) — acceptable tradeoff for reliable Gofile support
3. **Deprecated API** may change in future JD2 versions — pin image if needed
4. **Runtime**: multi-TB migrations take weeks on free tiers

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `jq: ... GraphicalUserInterfaceSettings.json: No such file` on jdownloader start | Partial `cfg/` broke init. Run `./scripts/jd2-reset-config.sh`, then follow first-run steps |
| `curl :3128/help` connection refused | Run `./scripts/jd2-enable-api.sh` then `docker compose restart jdownloader` |
| `JD2 API not reachable` in discover | Same as above; wait for `curl http://localhost:3128/help` |
| Discover finds 0 files / crawl timeout | Rebuild orchestrator after updates; confirm folder expands in JD2 web UI (:5800) |
| Pipeline stuck on downloading | Gofile IP/traffic block — enable VPN, run `./scripts/vpn-rotate.sh`, resume |
| `paused_traffic` / 222 GB message | Stale pause from old account monitor — `python -m migradora resume`; use VPN for IP blocks |
| JD2 `Invalid download directory` / stuck at 0% | Run `./scripts/jd2-fix-download-dir.sh`, restart jdownloader, delete failed package in web UI, `resume` |
| Queue paused (storage) | Free Filester account space |

## License

MIT
