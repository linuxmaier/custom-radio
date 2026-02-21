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

Admin endpoints are authenticated via the `X-Admin-Token` request header (value = `ADMIN_TOKEN` env var, no "Bearer" prefix). A custom header is used instead of `Authorization` because nginx's `auth_basic` consumes the `Authorization` header for site-wide HTTP Basic Auth, which would prevent the Bearer token from ever reaching the API.

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

## Liquidsoap 2.3 Notes

The script at `liquidsoap/radio.liq` targets **Liquidsoap 2.3.0** (`savonet/liquidsoap:v2.3.0`). Several APIs changed from older versions:

- **`request.dynamic`** (not `request.dynamic.list`): callback must return `request?` (nullable) — use `null()` for no track and `request.create(path)` for a track. No `conservative` parameter.
- **`http.get` response**: the return value *is* the body string directly. Access it as `response` (not `response.contents`). Metadata (`.status_code`, `.headers`, etc.) are attached as methods.
- **`source.on_metadata`**: source is the **first** argument, handler is second: `source.on_metadata(source, handler)`.
- **`settings.init.allow_root.set(true)`**: required when running as root (i.e., in Docker with `USER root`). Without it, Liquidsoap exits immediately.
- **Nested quotes in string interpolation**: `"http://#{environment.get("VAR")}"` causes a parse error — the inner quotes terminate the outer string. Extract env vars into variables first: `x = environment.get("VAR")` then use `"http://#{x}"`.
- **Debugging parse errors**: `--check` on a file path and `--check -` (stdin) can give different errors. The most reliable diagnostic approach is to pipe the file via stdin inside the container: `cat script.liq | liquidsoap --check - 2>&1`. When an error says "Unknown position: Error 2: Parse error" with no line number, use progressive line truncation (`head -N script.liq | liquidsoap --check -`) to bisect to the failing section.
- **`savonet/liquidsoap` image entrypoint**: the image ENTRYPOINT is `/usr/bin/tini -- /usr/bin/liquidsoap`. Any CMD args are passed directly to liquidsoap, not to a shell. Use `--entrypoint sh` when you need to run shell commands inside the container.
- **`settings.request.metadata_decoders`**: do NOT set this. The default (TagLib) correctly reads ID3v2 tags including `comment`. Setting it to `["FFMPEG"]` silently fails ("Cannot find decoder FFMPEG") and leaves metadata unread, breaking `source.on_metadata` track ID lookups.

## Frontend Player Notes

The web player in `frontend/playing.html` embeds an `<audio>` element pointed directly at the Icecast stream (`hostname:8000/radio` — not proxied through nginx). Key behaviours to be aware of:

- **Pausing a live HTTP stream causes the browser to buffer**: on resume, the listener is behind the live edge. There is no way to seek back to live — the only option is to reassign `audio.src` to force a fresh connection, which drops the buffer and rejoins at the current live point.
- **The `pause` event fires multiple times**: browsers fire `pause` when buffering stalls as well as on user-initiated pauses. Always `clearInterval` any existing timer before starting a new one in the pause handler, or multiple timers will accumulate and fight over the displayed value.
- **`audio.currentTime` is meaningless for live streams**: do not use it to measure lag. Track elapsed wall-clock time instead (store `Date.now()` at pause, accumulate into a `totalBehindMs` counter on resume).
- **Port 8000 must be open**: the stream bypasses nginx, so the Icecast port needs to be reachable directly from the browser. In the AWS security group this means port 8000 must be open to the public (already noted in deployment TODOs).
- **Buffering stalls vs user pauses**: the `pause` event fires for both. Use a `userPaused` flag (set to `true` in the click handler before calling `audio.pause()`, cleared on `play`) to distinguish them. Only accumulate behind-live lag and start the countdown timer when `userPaused` is true.

## Known Issues / Workarounds

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
