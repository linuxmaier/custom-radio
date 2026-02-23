# Family Radio Station

A self-hosted internet radio station for families to share music. Members submit songs via a web form (file upload or YouTube link). The stream can be tuned into from any radio client (VLC, browser, etc.).

## Architecture

Five Docker services:

- **Icecast** — stream server; reachable only within the Docker network (port 8000 is not exposed externally)
- **Liquidsoap** — programs the stream; asks the API for the next track and handles ICY metadata
- **Python/FastAPI** — manages submissions, downloads, audio analysis, library, and scheduling
- **Nginx** — reverse proxy for the web UI and audio stream; handles HTTPS and HTTP Basic Auth; proxies the stream at `/stream`
- **bgutil-provider** — local Node.js server that generates YouTube Proof of Origin (`po_token`) tokens; used by yt-dlp to pass YouTube's bot check from cloud IPs

## Quick Start

### Prerequisites

- Docker and Docker Compose
- A domain name pointed at your VPS
- `apache2-utils` (for `htpasswd`)

### Setup

```bash
# 1. Copy and fill in secrets
cp .env.example .env
$EDITOR .env

# 2. Generate the site password file
htpasswd -cb nginx/.htpasswd family YOUR_PASSPHRASE

# 3. Get a TLS certificate before starting (chicken-and-egg: nginx needs the cert to start)
docker run --rm -p 80:80 \
  -v $(basename $(pwd))_certbot_conf:/etc/letsencrypt \
  certbot/certbot certonly --standalone \
  -d your.domain.com \
  --email you@example.com --agree-tos --no-eff-email

# 4. Build and start
docker compose up -d --build
```

### Verify

- Icecast status (internal only): `docker exec radio-nginx-1 curl http://icecast:8000/status-json.xsl`
- Submit a YouTube link via the web UI
- Poll `GET /api/track/{id}` until `status: "ready"` (usually under 5 minutes)
- Open VLC → Media → Open Network Stream → `https://family:passphrase@domain/stream`

## Local Development

`docker-compose.override.yml` is gitignored so it is never present on the server. For local development, copy the example file before running:

```bash
# 1. Copy secrets and generate .htpasswd (same as production)
cp .env.example .env && $EDITOR .env
htpasswd -cb nginx/.htpasswd family YOUR_PASSPHRASE

# 2. Set up the local override
cp docker-compose.override.yml.example docker-compose.override.yml

# 3. Start (no TLS needed — nginx uses the HTTP-only local config)
docker compose up --build
```

The override does three things: uses `nginx/local.conf.template` (HTTP on port 80, no TLS), sets `SERVER_HOSTNAME=localhost`, and exposes Icecast on port 8000 for debugging. The site is then reachable at `http://localhost`.

## Web UI

| Page | URL | Purpose |
|------|-----|---------|
| Submit | `/` | Add a song (file upload or YouTube link) |
| Now Playing | `/playing.html` | See what's on and recent history |
| Admin | `/admin.html` | Change mode, skip track, manage library |

All pages are behind HTTP Basic Auth (shared family username/password from `.env`). The admin page additionally requires an admin token sent via the `X-Admin-Token` header.

## Programming Modes

Switchable live from the admin page — no restart needed.

- **Rotation** (default): Round-robin through submitters, playing N songs per block (configurable, default 3). Once the library exceeds 1 hour of total runtime, a per-track cooldown kicks in: no track replays within a 60-minute window. If all of a submitter's tracks are on cooldown their turn is skipped; if every submitter is on cooldown the globally least-recently-played track is used as a fallback to avoid silence.
- **Mood**: Picks the next track by minimum Euclidean distance in audio feature space from the currently playing track. Features: tempo (BPM), RMS energy, spectral centroid, zero-crossing rate.

## Submitting Music

### File Upload
Upload MP3, WAV, FLAC, M4A, OGG, or OPUS files up to 200MB.

### YouTube
Paste any YouTube video URL. Title and artist are extracted from the video metadata.

> **Note**: YouTube blocks yt-dlp requests from cloud/datacenter IP ranges (AWS, GCP, etc.). The `bgutil-provider` sidecar handles this by generating Proof of Origin tokens locally, so YouTube downloads should work out of the box. If submissions still fail with a bot-check error, upload a fresh `cookies.txt` (Netscape format, from a signed-in throwaway Google account) via the admin panel → **YouTube Cookies** as a secondary measure.

## API Reference

All public endpoints are proxied through nginx at `/api/`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/submit` | Submit a track (multipart form) |
| `GET` | `/api/status` | Now playing + recent 10 tracks + pending count |
| `GET` | `/api/library` | All tracks with status |
| `GET` | `/api/track/{id}` | Single track (for polling submission status) |
| `GET` | `/api/admin/config` | Get current config (admin token required) |
| `POST` | `/api/admin/config` | Update programming mode / block size |
| `POST` | `/api/admin/skip` | Skip the current track |
| `DELETE` | `/api/admin/track/{id}` | Remove a track and delete its file |
| `GET` | `/api/admin/youtube-cookies/status` | Check whether a cookies file is present |
| `POST` | `/api/admin/youtube-cookies` | Upload a YouTube cookies.txt file |

Internal endpoints (`/internal/`) are Docker-network-only and blocked at the nginx level.

## Environment Variables

See `.env.example` for the full list:

| Variable | Description |
|----------|-------------|
| `SERVER_HOSTNAME` | Your domain (e.g. `radio.yourfamily.com`) |
| `ICECAST_SOURCE_PASSWORD` | Liquidsoap → Icecast password |
| `ICECAST_ADMIN_PASSWORD` | Icecast web admin password |
| `ICECAST_RELAY_PASSWORD` | Icecast relay password |
| `ADMIN_TOKEN` | Token for admin API endpoints (sent via `X-Admin-Token` header) |
| `SITE_USER` | HTTP Basic Auth username (default: `family`) |
| `SITE_PASSPHRASE` | HTTP Basic Auth password |
| `BACKUP_DEST` | Backup destination (e.g. `s3://your-bucket`); leave unset to disable backups |
| `BACKUP_ENDPOINT_URL` | S3-compatible endpoint URL (optional; for non-AWS providers) |

## Backups

`scripts/backup.sh` backs up the SQLite database and processed MP3s to any S3-compatible object store. Backups are opt-in — nothing runs unless `BACKUP_DEST` is set in `.env`.

### Prerequisites

- AWS CLI v2 installed on the **host** (not inside Docker — the script runs as a cron job on the host)
- An S3 bucket (or equivalent) with write access granted to the host

For AWS: attach an IAM policy granting `s3:PutObject` and `s3:GetObject` on your bucket to the instance role (or configure `~/.aws/credentials` with an IAM user).

For S3-compatible providers (Cloudflare R2, Backblaze B2, Wasabi, DigitalOcean Spaces, etc.): the AWS CLI works with any of these via `--endpoint-url`. No AWS account needed.

### Configuration

Add to `.env`:

```bash
BACKUP_DEST=s3://your-bucket-name

# Only needed for non-AWS S3-compatible stores:
# BACKUP_ENDPOINT_URL=https://endpoint.example.com
```

The script sources `.env` automatically, so no changes to the cron job are needed when updating these values.

### Cron setup

```bash
# Install the cron job (runs daily at 03:00 UTC as root)
echo "0 3 * * * root /path/to/radio/scripts/backup.sh" > /etc/cron.d/radio-backup

# Trigger a manual run to verify
/path/to/radio/scripts/backup.sh
tail /var/log/radio-backup.log
```

### What gets backed up

- **Database**: a safe point-in-time snapshot taken via Python's `sqlite3.Connection.backup()` (WAL-safe). Stored as timestamped files plus a `radio-latest.db` alias for quick restore.
- **Media**: an incremental sync of processed MP3s. Files are never deleted from the backup destination, so accidentally removed tracks remain recoverable.

### Restore DB

```bash
aws s3 cp s3://your-bucket/db/radio-latest.db /tmp/radio-restore.db
docker cp /tmp/radio-restore.db radio-api-1:/data/radio.db
docker compose restart api
```

To restore from a specific date: `aws s3 ls s3://your-bucket/db/` to list available snapshots.

### Restore media

```bash
# Full restore
aws s3 sync s3://your-bucket/media/tracks/ \
  /var/lib/docker/volumes/radio_media/_data/tracks/

# Single track
aws s3 cp s3://your-bucket/media/tracks/TRACK_ID.mp3 \
  /var/lib/docker/volumes/radio_media/_data/tracks/TRACK_ID.mp3
```

## Project Structure

```
family-radio/
├── docker-compose.yml
├── .env.example
├── nginx/
│   ├── default.conf.template   # production (HTTPS)
│   └── local.conf.template     # local dev (HTTP only)
├── icecast/
│   └── icecast.xml
├── liquidsoap/
│   ├── Dockerfile
│   └── radio.liq
├── certbot/
│   └── Dockerfile              # extends certbot/certbot with docker-cli
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── worker.py
│   ├── scheduler.py
│   ├── audio.py
│   ├── downloader.py
│   └── routers/
│       ├── submit.py
│       ├── internal.py
│       ├── admin.py
│       └── status.py
├── frontend/
│   ├── index.html
│   ├── playing.html
│   ├── admin.html
│   └── static/
│       └── style.css
└── scripts/
    └── backup.sh               # daily backup script (DB + media; see Backups section)
```

## Development Choices

### Icecast burst-on-connect disabled

Icecast's default behaviour is to send a burst of buffered audio (~65 KB, roughly 4 seconds at 128 kbps) to each new listener on connect. This pre-fills the player's buffer so playback starts smoothly.

We disable it (`burst-on-connect=0`) because the burst causes audible bouncing when a browser player connects to the live stream. The browser plays the burst (which is a few seconds behind the live edge), catches up to live, and if a brief reconnect happens it gets another burst and jumps back again. The back-and-forth is more disruptive than the alternative.

The tradeoff: without a burst, playback on connect depends entirely on live audio arriving fast enough to fill the player buffer. On a slow or high-latency connection there is a small risk of a brief stall or silence at startup. For a home-network family station this is acceptable. If it becomes a problem, setting `burst-size` to a small value (e.g. 8192 bytes, ~0.5 s) is a reasonable middle ground.

## Technical Notes

- **SQLite WAL mode** with a single uvicorn worker avoids write contention without needing Redis/Postgres.
- **Background worker**: a single daemon thread polls the `jobs` table every 5 seconds. No Celery needed at family scale.
- **Track identity**: each MP3 has its UUID written into the ID3 `comment` tag by ffmpeg during processing. Liquidsoap reads this tag back via TagLib to call `/internal/track-started/{id}`. Title and artist are **not** read from file tags at runtime — `/internal/next-track` returns a Liquidsoap annotate URI (`annotate:title="...",artist="...":file_path`) so the DB is the source of truth for display metadata. MP3 files also have `title` and `artist` tags written as a recovery aid if the DB is ever lost.
- **TLS renewal**: the certbot container runs `certbot renew` every 12 hours. After a successful renewal it sends SIGHUP to nginx via a `--deploy-hook` (requires docker-cli in the certbot image and the Docker socket mounted read-only). The deploy hook finds the nginx container by a `family-radio.service=nginx` Docker label rather than a hardcoded container name, so it works regardless of the directory the project is cloned into.
- **Bringing your own TLS cert or terminating TLS upstream**: if you use Cloudflare Tunnel, Tailscale Funnel, a wildcard cert, or another CA, you don't need the certbot service. Disable it (or replace its entrypoint with `sleep infinity`) and swap `nginx/default.conf.template` for `nginx/local.conf.template` in the nginx volumes, updating the template to match your cert paths or removing the TLS block entirely if TLS is handled upstream.
- **yt-dlp** requires Deno as of late 2025 (installed in the API Dockerfile) and the `bgutil-ytdlp-pot-provider` plugin (installed via pip) to pass YouTube's Proof of Origin bot check from cloud IPs. The plugin calls the `bgutil-provider` sidecar container at `http://bgutil-provider:4416` to obtain a `po_token` for each download.
