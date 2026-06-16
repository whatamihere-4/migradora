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

## VPN (optional)

```bash
docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn up -d
```

Routes `jdownloader` through PIA VPN.

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
| Pipeline stuck on downloading | Gofile traffic limits or JD2 plugin issue — check JD2 logs |
| Queue paused (storage) | Free Filester account space |

## License

MIT
