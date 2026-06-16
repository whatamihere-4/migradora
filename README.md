# Migradora — Gofile → Filester Mirror

Production-ready Docker pipeline to mirror large video libraries from **Gofile.io** (free account, web-scraping fallback) to **Filester.me**, designed for a 50 GB VPS processing terabytes of data over days or weeks.

## Features

- Recursive folder discovery from Gofile share links
- Web-scraping fallback when Gofile API returns `error-notPremium` (March 2026 restriction)
- One-file-at-a-time download/upload to fit 50 GB VPS storage
- Resumable downloads (HTTP Range + `.part` files)
- Automatic file splitting for uploads over 10 GB (Filester limit)
- SQLite queue — idempotent, safe to restart
- Structured JSON logging + Rich console progress
- FastAPI dashboard (`/health`, `/status`, `/jobs`)
- Gofile traffic monitoring via account API
- Filester storage monitoring with auto-pause
- Optional PIA VPN via Gluetun

## Quick start

```bash
cp .env.example .env
# Edit .env: GOFILE_FOLDER_URLS, GOFILE_TOKEN, FILESTER_API_KEY

docker compose up -d --build

# Discover all files from your Gofile folders
docker compose exec orchestrator python -m migradora discover

# Monitor progress
curl http://localhost:8080/status | jq
docker compose logs -f orchestrator downloader uploader
```

Workers start automatically with `docker compose up`. Discovery must be run once (or via `POST /discover`).

## Architecture

```
orchestrator  → discovery, monitors, dashboard, queue coordination
downloader    → claims pending jobs, downloads from Gofile, splits large files
uploader      → claims downloaded jobs, uploads to Filester, deletes local files
```

Shared SQLite queue at `./data/state/queue.db` coordinates all services.

## Configuration

All settings live in a single `.env` file. See [`.env.example`](.env.example) for every variable.

| Variable | Purpose |
|---|---|
| `GOFILE_FOLDER_URLS` | Comma-separated `https://gofile.io/d/...` links |
| `GOFILE_TOKEN` | Token from [gofile.io/myprofile](https://gofile.io/myprofile) |
| `FILESTER_API_KEY` | API key from Filester account settings |
| `MIN_FREE_DISK_GB` | Pause downloads when disk is low |
| `GOFILE_TRAFFIC_PAUSE_GB` | Pause when monthly traffic approaches limit |

## CLI commands

```bash
docker compose exec orchestrator python -m migradora discover   # Scan Gofile folders
docker compose exec orchestrator python -m migradora status     # Queue summary
docker compose exec orchestrator python -m migradora resume       # Resume paused queue
```

## HTTP API

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Service and worker health |
| `/status` | GET | Queue stats, Gofile traffic, Filester storage |
| `/jobs?status=failed` | GET | List jobs by status |
| `/discover` | POST | Trigger folder discovery |
| `/resume` | POST | Resume paused queue |
| `/pause` | POST | Pause queue |

## Monitoring

**Logs:**
- `./data/logs/migradora.jsonl` — structured JSON (one event per line)
- `./data/logs/migradora.log` — human-readable rotating log
- `docker compose logs -f` — live console output with Rich progress

**Dashboard:**
```bash
curl http://localhost:8080/status
```

Shows completion %, per-status counts, Gofile traffic used, and Filester storage.

## Resuming after restart

```bash
docker compose up -d
```

- SQLite WAL persists all job state
- Incomplete `.part` downloads resume via HTTP Range
- Stale `downloading`/`uploading` jobs reset to `pending` after `STALE_JOB_TIMEOUT_SEC`
- Uploaded files are never re-processed unless `discover --force`

## VPN (optional)

Route downloader/uploader through PIA VPN when hitting IP bans:

```bash
# Set PIA_OPENVPN_USER and PIA_OPENVPN_PASSWORD in .env
docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn up -d
```

VPN rotation helps with scraping bans, **not** per-link Gofile traffic limits.

## Large file splitting

Files over `FILESTER_MAX_FILE_BYTES` (default 9.5 GiB) are split with `split(1)` into parts like `video.part001.mp4`. Each part uploads separately. Reassemble manually:

```bash
cat video.part*.mp4 > video.mp4
```

## Gofile client attribution

Download logic adapted from [martadams89/gofile-dl](https://github.com/martadams89/gofile-dl) (MIT License) in `shared/src/migradora/gofile_client.py`.

## Important warnings

1. **Filester 10 GB account storage** — API docs list 10 GB total per account. A 5+ TB migration will hit this limit quickly. Monitor `/status` → `filester.storage_used_pct`. The uploader pauses automatically near capacity.

2. **Gofile ~100 GB/month traffic** (free tier) — At ~5 GB per video, expect ~20 videos/month before the traffic guard pauses downloads. Check usage at [gofile.io/myprofile](https://gofile.io/myprofile) or via `/status` → `gofile.traffic_used_gb`. Stats refresh approximately every 24 hours.

3. **Per-link traffic limits** — Popular share links can hit a separate server-side bandwidth cap (resets in hours to days). VPN cannot bypass this; wait and retry.

4. **Runtime** — 5 TB at 50 Mbps ≈ 10+ days continuous. With free-tier pauses, plan for **weeks**.

5. **Filester 45-day inactivity** — Files without views/downloads for 45 days may be deleted per Filester policy.

6. **Premium last resort** — If web fallback breaks or traffic limits are too restrictive, a Gofile premium subscription enables direct API access (not required by default).

## Troubleshooting

| Symptom | Action |
|---|---|
| `error-notPremium` in logs | Expected for free accounts; web fallback should follow |
| Queue paused (traffic) | Wait for monthly reset or check gofile.io/myprofile |
| Queue paused (storage) | Free space on Filester account or delete old files |
| Queue paused (disk) | Free VPS disk space; local files should delete after upload |
| Download fails repeatedly | Check `docker compose logs downloader`; try VPN profile |
| Worker not alive in `/health` | Check `docker compose ps`; restart affected service |

## Project structure

```
migradora/
├── docker-compose.yml          # Main services
├── docker-compose.vpn.yml      # VPN overlay
├── .env.example                # All configuration
├── shared/                     # Queue, config, logger, Gofile client
├── orchestrator/               # Discovery, monitors, dashboard
├── downloader/                 # Download worker + file splitter
└── uploader/                   # Filester upload worker
```

## License

MIT. Gofile client portions derived from gofile-dl (MIT).
