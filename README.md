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
| `WEBUI_PORT` | Internal container port for the web dashboard (Caddy upstream), default `8080` |
| `FILESTER_API_KEY` | Filester API key |
| `FILESTER_ROOT_FOLDER_NAME` | Optional wrapper folder on Filester (leave empty to mirror Gofile names directly, e.g. `VR/Studio1`) |

### 2. Share folders from source account

On account #1 (owns the files), share each folder and paste the links into `GOFILE_FOLDER_URLS`.

For a tree like `VR/Studio1/...`, share the **VR** folder only — discovery walks subfolders recursively:

```bash
GOFILE_FOLDER_URLS=https://gofile.io/d/YOUR_VR_FOLDER_ID
```

Filester folders mirror the Gofile path (`VR` → `Studio1` → files). Split uploads go into a subfolder named after the video file.

### 3. Caddy (required)

The web UI is **not** exposed on the host. You need Caddy on the external Docker network `caddy_net` (same pattern as your other services).

```bash
docker network create caddy_net   # once, if missing
```

Wire Caddy to **`migradora`** on `caddy_net` (network alias — use this hostname in your Caddy config, not `migradora-orchestrator`):

```bash
# Caddy .env
UPSTREAM_3=migradora:8080          # must match WEBUI_PORT in migradora .env
HTTPS_PORT_3=8008                  # TLS port you open in the browser
TAILSCALE_IP=100.x.x.x
```

```caddyfile
# Caddyfile
{$TAILSCALE_IP}:{$HTTPS_PORT_3} {
	reverse_proxy {$UPSTREAM_3}
}
```

Publish only on your Tailscale IP in Caddy's `docker-compose.yml`:

```yaml
ports:
  - "${TAILSCALE_IP}:${HTTPS_PORT_3}:${HTTPS_PORT_3}"
```

Full walkthrough: [docs/CADDY.md](docs/CADDY.md).

### 4. Start

```bash
docker compose up -d --build
```

### 5. Discover and run

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

Open the dashboard through Caddy (e.g. `https://your-machine.tailnet.ts.net:8008/`).

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
| `./scripts/test-gofile-download-speed.sh [url]` | Resolve CDN link + speed test (`--probe-servers` benchmarks mirrors) |
| `./scripts/test-filester-upload-speed.sh` | Measure raw VPS→Filester upload throughput |
| `./scripts/reset-failed-jobs.sh` | Reset failed/stuck jobs |
| `./scripts/health-check.sh` | Quick VPS diagnostics |
| `./scripts/diag-transfer.sh` | Upload speed vs Filester API call rate |
| `./scripts/upload-watchdog.sh` | Restart orchestrator when upload stays below threshold |
| `./scripts/check-caddy-upstream.sh` | Verify Caddy can reach `migradora` on `caddy_net` |
| `./scripts/check-webui-port.sh` | Verify app health inside the container |

## Large files

Files over `FILESTER_MAX_FILE_BYTES` (~9.5 GiB) are split before upload. Set `FILESTER_SPLIT_MODE`:

| Mode | Parts | Peak disk | Rejoin |
|------|-------|-----------|--------|
| `bytes` (default) | `movie.mp4.part001`, … | source + one part | `cat movie.mp4.part* > movie.mp4` |
| `ffmpeg_slice` | `movie.PART1.mp4`, … (playable) | source + one part | paste `mkvmerge` one-liner (see README) |

`ffmpeg_slice` uses more CPU than `bytes` but keeps the same low disk footprint — useful on small VPS disks when you want independently playable parts. Splits are aligned to video keyframes so each part is playable on its own. **Merge:** open a terminal in the parts folder, paste one command ([Windows PowerShell](#merge-parts-windows) or [Mac/Linux Terminal](#merge-parts-mac-linux)) — requires [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge` on PATH). Extraction uses ffmpeg input seek by default (`SPLITTER_EXTRACT_BACKEND=ffmpeg`); legacy `mkvmerge` fifo mode is much slower on large MP4s.

### Merge parts (Windows)

1. Install [MKVToolNix](https://mkvtoolnix.download/) if needed.
2. Open **PowerShell** in the folder with the `PART1` / `PART2` files (Shift+right-click → “Open PowerShell window here”, or `cd` into the folder).
3. Paste this **one line**, press Enter:

```powershell
$p1=(gci *.PART1.*|select -first 1).Name;$o=$p1-replace'\PART1\.';$t=1;$h=($o-replace'\..+$','')+'.merge_trim_frames';if(Test-Path $h){$t=[int](gc $h)};$x=@('-o',$o,$p1);for($i=2;$i -le 20;$i++){if(Test-Path ($p1-replace'PART1',"PART$i")){if($t-gt0){$x+=('--split','parts-frames:'+$t+'-')};$x+=('+'+($p1-replace'PART1',"PART$i"))}};mkvmerge @x
```

### Merge parts (Mac/Linux)

1. Install MKVToolNix (`mkvmerge` on PATH).
2. Open **Terminal** in the parts folder (`cd` to the folder).
3. Paste this **one line**, press Enter:

```bash
f=$(ls -1v *.PART1.* | head -1); o=${f/.PART1./.}; t=1; [ -f "${o%.*}.merge_trim_frames" ] && t=$(tr -d '\r\n ' < "${o%.*}.merge_trim_frames"); mkvmerge -o "$o" "$f" $(i=2; while [ -f "${f/.PART1./.PART$i.}" ]; do [ "$t" -gt 0 ] && echo --split parts-frames:$t-; echo +${f/.PART1./.PART$i.}; i=$((i+1)); done)
```

Older uploads without a `.merge_trim_frames` file default to trimming one frame at each join (fixes a brief hitch on legacy splits).

Split uploads are placed in a Filester subfolder under the studio folder, named after the original video filename (e.g. `VR/Studio1/My Scene.mp4/` containing the parts). This is automatic when a file exceeds `FILESTER_MAX_FILE_BYTES` — no extra env var.

`FILESTER_SPLIT_MODE` only chooses **how** to split (`bytes` vs `ffmpeg_slice`), not whether parts are grouped into a subfolder.

## Resuming after restart

```bash
docker compose up -d
```

Queue state persists in `data/state/queue.db`. Stale `downloading` jobs reset to `pending` after `STALE_JOB_TIMEOUT_SEC`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Dashboard unreachable on host `:8080` | Expected — use Caddy HTTPS URL; run `./scripts/check-caddy-upstream.sh` |
| `network caddy_net not found` | `docker network create caddy_net`, then `docker compose up -d --force-recreate` |
| Caddy `502` | Upstream must be `migradora:WEBUI_PORT`; see [docs/CADDY.md](docs/CADDY.md) |
| `GOFILE_TOKEN is required` | Set token from premium account in `.env`, rebuild |
| `error-notPremium` | Token account needs active premium (subscription or PAYG with credits) |
| Discover finds 0 files | Check folder is shared; test with `test-gofile-resolve.sh` |
| Queue paused (disk) | Free space under `data/downloads`, or set `DISK_PAUSE_SKIP_JOB=true` to skip oversized jobs and continue |
| Queue paused (storage) | Only if you set `FILESTER_STORAGE_PAUSE_PCT` and hit an account cap from the API |
| Download size mismatch | Re-run job: `./scripts/reset-failed-jobs.sh` |
| Upload slows to <1 MB/s | Run `./scripts/diag-transfer.sh`; close dashboard tabs; redeploy with `FILESTER_STATS_CACHE_SEC=60` |
| Upload degrades over time | Enable `UPLOAD_WATCHDOG_ENABLED=true` and run `./scripts/upload-watchdog.sh` in tmux/systemd |

## License

MIT
