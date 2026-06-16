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

### 2. Enable JD2 Deprecated API (first run)

```bash
# Seed API config template
mkdir -p data/jd2/config/cfg
cp jd2/config-templates/org.jdownloader.api.RemoteAPIConfig.json \
   data/jd2/config/cfg/

docker compose up -d
```

Open **http://your-vps:5800** (JD2 web UI) and confirm:
- Settings → Advanced → search `RemoteAPI` → Deprecated API enabled
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
| `JD2 API not reachable` | Enable Deprecated API, disable localhost-only, expose port 3128 |
| Discover finds 0 files | Check JD2 logs; verify Gofile plugin; test URL in JD2 UI |
| Pipeline stuck on downloading | `docker compose logs jdownloader`; check Gofile traffic limits |
| Queue paused (storage) | Free Filester account space |

## License

MIT
