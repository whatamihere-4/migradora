# Migradora — Gofile → Filester Mirror

Minimal Docker pipeline: **Gofile Premium API** → **httpx download** → **split** → **Filester upload**.

Designed for multi-TB migrations on a small VPS (serial downloads, resumable queue).

## Setup

### 1. Configure `.env`

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `GOFILE_TOKEN` | API token from [gofile.io/myProfile](https://gofile.io/myProfile) on account #2 (subscription **or** PAYG) |
| `GOFILE_FOLDER_URLS` | Comma-separated shared folder links from your **source account** (#1) |
| `GOFILE_PASSWORD` | Only if folders are password-protected |
| `WEBUI_PORT` | Web dashboard port (host + container), default `8080` |
| `FILESTER_API_KEY` | Filester API key |
| `FILESTER_ROOT_FOLDER_NAME` | Optional wrapper folder on Filester (leave empty to mirror Gofile names directly, e.g. `VR/Studio1`) |

### 2. Share folders from source account

On account #1 (owns the files), share each folder and paste the links into `GOFILE_FOLDER_URLS`.

For a tree like `VR/Studio1/...`, share the **VR** folder only — discovery walks subfolders recursively:

```bash
GOFILE_FOLDER_URLS=https://gofile.io/d/YOUR_VR_FOLDER_ID
```

Filester folders mirror the Gofile path (`VR` → `Studio1` → files). Large files upload as `.part001`, `.part002`, etc.

### 3. Start

```bash
docker compose up -d --build
```

### 4. Discover and run

```bash
# List files in a folder (sanity check)
./scripts/test-gofile-resolve.sh https://gofile.io/d/YOUR_FOLDER_ID

# Enqueue all files from GOFILE_FOLDER_URLS
docker compose exec orchestrator python -m migradora discover

# Check queue
docker compose exec orchestrator python -m migradora status

# Pipeline runs automatically; or resume if paused
docker compose exec orchestrator python -m migradora resume
```

Open the dashboard at **http://your-vps:WEBUI_PORT/** (default port 8080).

## Architecture

```text
orchestrator (single container)
  ├── API discovery: crawl GOFILE_FOLDER_URLS via Premium API
  ├── Pipeline: resolve CDN URL → download → split → Filester → cleanup
  └── SQLite queue (resumable across restarts)
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web dashboard (queue monitor) |
| `GET /health` | Pipeline health |
| `GET /status` | Queue stats, Filester storage |
| `GET /jobs?status=failed` | List jobs |
| `POST /discover` | Crawl folders and enqueue |
| `POST /resume` | Resume paused queue |
| `POST /retry-failed` | Reset failed jobs to pending |

## Scripts

| Script | Purpose |
|--------|---------|
| `./scripts/test-gofile-resolve.sh [url]` | Test API token (folder list or file resolve) |
| `./scripts/reset-failed-jobs.sh` | Reset failed/stuck jobs |
| `./scripts/health-check.sh` | Quick VPS diagnostics |

## Large files

Files over `FILESTER_MAX_FILE_BYTES` (~9.5 GiB) are split with `split(1)`. Reassemble:

```bash
cat video.part*.mp4 > video.mp4
```

## Resuming after restart

```bash
docker compose up -d
```

Queue state persists in `data/state/queue.db`. Stale `downloading` jobs reset to `pending` after `STALE_JOB_TIMEOUT_SEC`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GOFILE_TOKEN is required` | Set token from premium account in `.env`, rebuild |
| `error-notPremium` | Token account needs active premium (subscription or PAYG with credits) |
| Discover finds 0 files | Check folder is shared; test with `test-gofile-resolve.sh` |
| Queue paused (disk) | Free space under `data/downloads` |
| Queue paused (storage) | Only if you set `FILESTER_STORAGE_PAUSE_PCT` and hit an account cap from the API |
| Download size mismatch | Re-run job: `./scripts/reset-failed-jobs.sh` |

## License

MIT
