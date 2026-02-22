# Claude Code Instructions — Family Radio

## Project Overview

Self-hosted internet radio station. Four Docker services: Icecast (stream), Liquidsoap (programmer), FastAPI API (logic), Nginx (proxy). See README.md for full architecture.

## Key Design Decisions

- **Single uvicorn worker + SQLite WAL**: deliberate. Do not add Celery, Redis, or Postgres unless the user explicitly asks. The project is designed for family-scale volume.
- **Background worker thread** in `api/worker.py`: polls `jobs` table every 5s. Do not convert to async tasks — the librosa/ffmpeg work is CPU-bound and blocking.
- **All scheduling logic in Python**: Liquidsoap only asks "what's next?" — it does not make programming decisions. Keep it this way.
- **Single audio format**: all tracks are converted to MP3/128kbps by ffmpeg. Do not introduce format variations.
- **DB is source of truth for track metadata**: `title` and `artist` live in the `tracks` table, not in MP3 tags. `/internal/next-track` returns a Liquidsoap annotate URI (`annotate:title="...",artist="...":file_path`) so Liquidsoap gets metadata from the API response and sets ICY StreamTitle correctly without reading any file tags. MP3 files also have `title` and `artist` ID3 tags written by ffmpeg during conversion as a recovery aid in case the DB is ever lost — they are not read at runtime.

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
- `internal.py` — GET /internal/next-track (returns annotate URI with title/artist from DB), POST /internal/track-started/{id}
- `admin.py`    — GET/POST /admin/config, POST /admin/skip, DELETE /admin/track/{id}
- `status.py`   — GET /status, GET /library, GET /track/{id}

All public routes go through nginx at `/api/`. Internal routes are Docker-network-only (blocked by nginx).

Admin endpoints are authenticated via the `X-Admin-Token` request header (value = `ADMIN_TOKEN` env var, no "Bearer" prefix). A custom header is used instead of `Authorization` because nginx's `auth_basic` consumes the `Authorization` header for site-wide HTTP Basic Auth, which would prevent the Bearer token from ever reaching the API.

**Skip mechanism**: POST /admin/skip connects to the Liquidsoap telnet server (`liquidsoap:1234`) and sends `dynamic.flush_and_skip`. This immediately stops the current track and fetches a fresh next track. The telnet server is enabled in `radio.liq` with `settings.server.telnet.set(true)` and bound to `0.0.0.0` so it's reachable from the API container. Do not use `icecast_out.skip` — it operates at the output layer and does not reliably interrupt the audio stream.

## Database

SQLite at `/data/radio.db` (Docker volume). Schema initialised in `database.py:init_db()`. Tables: `tracks`, `play_log`, `jobs`, `config`.

Config keys: `programming_mode`, `rotation_tracks_per_block`, `rotation_current_submitter_idx`, `rotation_block_start_log_id`, `last_returned_track_id`, `feature_min/max_*` (4 audio features).

Rotation block tracking uses `rotation_block_start_log_id` (a `play_log.id` watermark) rather than a counter. Block completion is determined by counting `play_log` entries from the current submitter since that watermark, plus 1 if `last_returned_track_id` belongs to the current submitter (handles the prefetch/track-started race condition). `last_returned_track_id` is cleared before each skip so the flushed prefetch track does not incorrectly count as an exclusion in the subsequent selection.

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

## Frontend Admin Notes

The admin library list (`frontend/admin.html`) is loaded once on login and does not auto-refresh (by design — the page is not intended to be a live dashboard). The one exception: while any track has `status='pending'`, a 5-second polling interval runs via `managePoll()` and refreshes the library until all tracks settle. `managePoll()` is called after every `loadLibrary()` and self-manages the interval (starts it when pending tracks exist, clears it when they're gone).

## Known Issues / Workarounds

- **yt-dlp + Deno**: yt-dlp requires Deno as of late 2025. Deno is installed in `api/Dockerfile`.
- **YouTube downloads blocked on AWS**: yt-dlp gets "Sign in to confirm you're not a bot" from AWS datacenter IPs. Fix: pass cookies from a signed-in YouTube session via `--cookies /app/cookies/youtube.txt`. Use a throwaway Google account. See TODO.md §6 for full implementation steps. File upload works as a fallback in the meantime.
- **`docker-compose.override.yml` must not exist on the production server**: This file is for local dev only (disables certbot, uses HTTP-only nginx). If it is present on the server, certbot will be silently disabled and nginx will use the local config. Delete it after cloning: `rm docker-compose.override.yml`.
- **nginx env vars**: `nginx/default.conf.template` uses `${SERVER_HOSTNAME}`. The official `nginx:alpine` image processes `/etc/nginx/templates/*.template` files with `envsubst` at startup. The `SERVER_HOSTNAME` env var must be set in docker-compose.yml for nginx.
- **`file.filename` is a string, not a bool**: In `api/routers/submit.py`, `has_file = file is not None and file.filename` evaluates to the filename string when a file is provided. Always wrap in `bool()` before using in arithmetic (e.g. `sum()`), otherwise a `TypeError: unsupported operand type(s) for +: 'int' and 'str'` will be raised.

## Secrets and Gitignored Files

- `.env` — never commit; `.env.example` is the template
- `nginx/.htpasswd` — generated locally with `htpasswd -cb nginx/.htpasswd family PASSPHRASE`

## Production Server

- **Domain**: `radio-maier.live` (DNS at Porkbun, A record → Elastic IP)
- **Instance**: EC2 t3.small, us-west-2, instance ID `i-04cf8f3c771ae92c8`
- **Access**: AWS SSM Session Manager — no SSH port. CLI access via named profile:
  ```bash
  aws ssm send-command --profile family-radio --region us-west-2 \
    --instance-ids i-04cf8f3c771ae92c8 \
    --document-name AWS-RunShellScript \
    --parameters commands=["your command here"]
  ```
  Git operations on the server must run as the `ubuntu` user: `sudo -u ubuntu git -C /home/ubuntu/radio pull`
- **Repo location**: `/home/ubuntu/radio`
- **TLS cert**: Let's Encrypt via certbot, expires 2026-05-23, auto-renewed by the certbot container every 12h

## Development Tips

- **Local full-stack**: `docker compose up --build` — `docker-compose.override.yml` is automatically applied and handles local differences (HTTP-only nginx, certbot disabled, `SERVER_HOSTNAME=localhost`)
- **nginx config templates**: two templates exist — `nginx/default.conf.template` (production, HTTPS + certbot) and `nginx/local.conf.template` (local, HTTP only). The override file maps the local one into the nginx container.
- To test the API locally without Docker: `cd api && uvicorn main:app --reload`
  Set env vars: `ADMIN_TOKEN=dev DB_PATH=./radio.db MEDIA_DIR=./media`
- To rebuild after Python changes: `docker compose up -d --build api`
- To tail API logs: `docker compose logs -f api`
- Liquidsoap reconnects to Icecast automatically on failure; no manual intervention needed.
