# Claude Code Instructions — Family Radio

## Project Overview

Self-hosted internet radio station. Four Docker services: Icecast (stream), Liquidsoap (programmer), FastAPI API (logic), Nginx (proxy). See README.md for full architecture.

## Key Design Decisions

- **Single uvicorn worker + SQLite WAL**: deliberate. Do not add Celery, Redis, or Postgres unless the user explicitly asks. The project is designed for family-scale volume.
- **Background worker thread** in `api/worker.py`: polls `jobs` table every 5s. Do not convert to async tasks — the librosa/ffmpeg work is CPU-bound and blocking.
- **All scheduling logic in Python**: Liquidsoap only asks "what's next?" — it does not make programming decisions. Keep it this way.
- **Single audio format**: all tracks are converted to MP3/128kbps by ffmpeg. Do not introduce format variations.

## Project Structure

```
api/          Python/FastAPI backend (runs inside Docker)
frontend/     Static HTML/JS/CSS (served by nginx)
liquidsoap/   Liquidsoap script + Dockerfile
icecast/      icecast.xml config
nginx/        nginx template config + .htpasswd (gitignored)
```

## API Layout

Routers in `api/routers/`:
- `submit.py`   — POST /submit
- `internal.py` — GET /internal/next-track, POST /internal/track-started/{id}
- `admin.py`    — GET/POST /admin/config, POST /admin/skip, DELETE /admin/track/{id}
- `status.py`   — GET /status, GET /library, GET /track/{id}

All public routes go through nginx at `/api/`. Internal routes are Docker-network-only (blocked by nginx).

## Database

SQLite at `/data/radio.db` (Docker volume). Schema initialised in `database.py:init_db()`. Tables: `tracks`, `play_log`, `jobs`, `config`.

Config keys: `programming_mode`, `rotation_tracks_per_block`, `rotation_current_submitter_idx`, `rotation_current_block_count`, `skip_requested`, `feature_min/max_*` (4 audio features).

## Audio Features

Extracted by `api/audio.py` using librosa:
- `tempo_bpm` — from percussive signal
- `rms_energy` — from STFT
- `spectral_centroid` — from STFT
- `zero_crossing_rate` — from raw signal

Normalization bounds are stored in the `config` table and updated after each track is analyzed (`scheduler.py:update_feature_bounds`).

## Known Issues / Workarounds

- **spotdl instability** (Feb 2026): Spotify downloads fail intermittently. The submit endpoint wraps spotdl in try/except and returns a warning. Direct users to YouTube Music links.
- **yt-dlp + Deno**: yt-dlp requires Deno as of late 2025. Deno is installed in `api/Dockerfile`.
- **nginx env vars**: `nginx/default.conf.template` uses `${SERVER_HOSTNAME}`. The official `nginx:alpine` image processes `/etc/nginx/templates/*.template` files with `envsubst` at startup. The `SERVER_HOSTNAME` env var must be set in docker-compose.yml for nginx.

## Secrets and Gitignored Files

- `.env` — never commit; `.env.example` is the template
- `nginx/.htpasswd` — generated locally with `htpasswd -cb nginx/.htpasswd family PASSPHRASE`

## Development Tips

- To test the API locally without Docker: `cd api && uvicorn main:app --reload`
  Set env vars: `ADMIN_TOKEN=dev DB_PATH=./radio.db MEDIA_DIR=./media`
- To rebuild after Python changes: `docker compose up -d --build api`
- To tail API logs: `docker compose logs -f api`
- Liquidsoap reconnects to Icecast automatically on failure; no manual intervention needed.
