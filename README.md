# Family Radio Station

A self-hosted internet radio station for families to share music. Members submit songs via a web form (file upload, YouTube link, or Spotify link). The stream can be tuned into from any radio client (VLC, browser, etc.).

## Architecture

Four Docker services:

- **Icecast** — stream server listeners connect to at `http://domain:8000/radio`
- **Liquidsoap** — programs the stream; asks the API for the next track and handles ICY metadata
- **Python/FastAPI** — manages submissions, downloads, audio analysis, library, and scheduling
- **Nginx** — reverse proxy for the web UI; handles HTTPS and HTTP Basic Auth

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

# 3. Build and start
docker compose up -d --build

# 4. Get a TLS certificate (first time only)
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d radio.yourfamily.com \
  --email you@example.com --agree-tos --no-eff-email
```

### Verify

- Icecast status: `http://domain:8000/status-json.xsl`
- Submit a YouTube link via the web UI
- Poll `GET /api/track/{id}` until `status: "ready"` (usually under 5 minutes)
- Open VLC → Media → Open Network Stream → `https://domain:8000/radio`

## Web UI

| Page | URL | Purpose |
|------|-----|---------|
| Submit | `/` | Add a song (file, YouTube, or Spotify) |
| Now Playing | `/playing.html` | See what's on and recent history |
| Admin | `/admin.html` | Change mode, skip track, manage library |

All pages are behind HTTP Basic Auth (shared family username/password from `.env`). The admin page additionally requires a Bearer token.

## Programming Modes

Switchable live from the admin page — no restart needed.

- **Rotation** (default): Round-robin through submitters, playing N songs per block (configurable, default 3).
- **Mood**: Picks the next track by minimum Euclidean distance in audio feature space from the currently playing track. Features: tempo (BPM), RMS energy, spectral centroid, zero-crossing rate.

## Submitting Music

### File Upload
Upload MP3, WAV, FLAC, M4A, OGG, or OPUS files up to 200MB.

### YouTube
Paste any YouTube video URL. Title and artist are extracted from the video metadata.

### Spotify
Paste a Spotify track URL. **Note:** Spotify downloads via `spotdl` may fail due to known API instability (as of early 2026). If a submission fails, paste a YouTube Music link instead.

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

Internal endpoints (`/internal/`) are Docker-network-only and blocked at the nginx level.

## Environment Variables

See `.env.example` for the full list:

| Variable | Description |
|----------|-------------|
| `SERVER_HOSTNAME` | Your domain (e.g. `radio.yourfamily.com`) |
| `ICECAST_SOURCE_PASSWORD` | Liquidsoap → Icecast password |
| `ICECAST_ADMIN_PASSWORD` | Icecast web admin password |
| `ICECAST_RELAY_PASSWORD` | Icecast relay password |
| `ADMIN_TOKEN` | Bearer token for admin API endpoints |
| `SITE_USER` | HTTP Basic Auth username (default: `family`) |
| `SITE_PASSPHRASE` | HTTP Basic Auth password |

## Project Structure

```
family-radio/
├── docker-compose.yml
├── .env.example
├── nginx/
│   └── default.conf.template
├── icecast/
│   └── icecast.xml
├── liquidsoap/
│   ├── Dockerfile
│   └── radio.liq
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
└── frontend/
    ├── index.html
    ├── playing.html
    ├── admin.html
    └── static/
        └── style.css
```

## Development Choices

### Icecast burst-on-connect disabled

Icecast's default behaviour is to send a burst of buffered audio (~65 KB, roughly 4 seconds at 128 kbps) to each new listener on connect. This pre-fills the player's buffer so playback starts smoothly.

We disable it (`burst-on-connect=0`) because the burst causes audible bouncing when a browser player connects to the live stream. The browser plays the burst (which is a few seconds behind the live edge), catches up to live, and if a brief reconnect happens it gets another burst and jumps back again. The back-and-forth is more disruptive than the alternative.

The tradeoff: without a burst, playback on connect depends entirely on live audio arriving fast enough to fill the player buffer. On a slow or high-latency connection there is a small risk of a brief stall or silence at startup. For a home-network family station this is acceptable. If it becomes a problem, setting `burst-size` to a small value (e.g. 8192 bytes, ~0.5 s) is a reasonable middle ground.

## Technical Notes

- **SQLite WAL mode** with a single uvicorn worker avoids write contention without needing Redis/Postgres.
- **Background worker**: a single daemon thread polls the `jobs` table every 5 seconds. No Celery needed at family scale.
- **Track identity**: each MP3 has its UUID written into the ID3 `comment` tag by ffmpeg during processing. Liquidsoap reads this tag back to call `/internal/track-started/{id}`.
- **TLS renewal**: the certbot container runs `certbot renew` every 12 hours automatically.
- **yt-dlp** requires Deno as of late 2025; the API Dockerfile installs it.
