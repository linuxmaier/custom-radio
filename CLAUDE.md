# Claude Code Instructions — Family Radio

## Project Overview

Self-hosted internet radio station. Five Docker services: Icecast (stream), Liquidsoap (programmer), FastAPI API (logic), Nginx (proxy), bgutil-provider (YouTube po_token server). See README.md for full architecture.

## Key Design Decisions

- **Single uvicorn worker + SQLite WAL**: deliberate. Do not add Celery, Redis, or Postgres unless the user explicitly asks. The project is designed for family-scale volume.
- **Background worker thread** in `api/worker.py`: polls `jobs` table every 5s. Do not convert to async tasks — the librosa/ffmpeg work is CPU-bound and blocking. On startup, `reset_stuck_jobs()` runs synchronously in the main thread (before the worker thread starts) to reset any jobs left in `processing` state by a previous crash or OOM kill.
- **All scheduling logic in Python**: Liquidsoap only asks "what's next?" — it does not make programming decisions. Keep it this way.
- **Single audio format**: all tracks are converted to MP3/128kbps by ffmpeg. Do not introduce format variations.
- **DB is source of truth for track metadata**: `title` and `artist` live in the `tracks` table, not in MP3 tags. `/internal/next-track` returns a Liquidsoap annotate URI (`annotate:title="...",artist="...":file_path`) so Liquidsoap gets metadata from the API response and sets ICY StreamTitle correctly without reading any file tags. MP3 files also have `title` and `artist` ID3 tags written by ffmpeg during conversion as a recovery aid in case the DB is ever lost — they are not read at runtime.

## Project Structure

```
api/          Python/FastAPI backend (runs inside Docker)
frontend/     Static HTML/JS/CSS (served by nginx)
liquidsoap/   Liquidsoap script + Dockerfile
icecast/      icecast.xml config
nginx/        nginx template configs (default.conf.template = production HTTPS,
              local.conf.template = local HTTP only) + .htpasswd (gitignored)
certbot/      Dockerfile extending certbot/certbot with docker-cli + reload-nginx.sh script
              (reload-nginx.sh finds the nginx container by its family-radio.service=nginx Docker
              label and sends nginx -s reload; called by the certbot --deploy-hook after renewal)
scripts/      Host-level scripts (backup.sh — daily S3 backup)
```

The `bgutil-provider` service uses the upstream `brainicism/bgutil-ytdlp-pot-provider` image (no local directory). It runs a Node.js HTTP server on port 4416 (internal Docker network only) that generates YouTube Proof of Origin (`po_token`) tokens. The `bgutil-ytdlp-pot-provider` pip package (installed in the api image) is the yt-dlp plugin that calls it automatically on each YouTube download.

## API Layout

Routers in `api/routers/`:
- `submit.py`   — POST /submit, GET /check-duplicate, GET /submitters
- `internal.py` — GET /internal/next-track (returns annotate URI with title/artist from DB), POST /internal/track-started/{id}
- `admin.py`    — GET/POST /admin/config, POST /admin/skip, DELETE /admin/track/{id}, GET /admin/youtube-cookies/status, POST /admin/youtube-cookies
- `status.py`   — GET /status (returns now_playing, recent, pending_count, station_name), GET /library, GET /public-library, GET /track/{id}
- `push.py`     — GET /manifest.json (dynamic PWA manifest), GET /push/vapid-key, POST /push/subscribe, POST /push/unsubscribe

All public routes go through nginx at `/api/`. Internal routes are Docker-network-only (blocked by nginx).

Admin endpoints are authenticated via the `X-Admin-Token` request header (value = `ADMIN_TOKEN` env var, no "Bearer" prefix). A custom header is used instead of `Authorization` because nginx's `auth_basic` consumes the `Authorization` header for site-wide HTTP Basic Auth, which would prevent the Bearer token from ever reaching the API.

**Skip mechanism**: POST /admin/skip connects to the Liquidsoap telnet server (`liquidsoap:1234`) and sends `dynamic.flush_and_skip`. This immediately stops the current track and fetches a fresh next track. The telnet server is enabled in `radio.liq` with `settings.server.telnet.set(true)` and bound to `0.0.0.0` so it's reachable from the API container. Do not use `icecast_out.skip` — it operates at the output layer and does not reliably interrupt the audio stream.

## Database

SQLite at `/data/radio.db` (Docker volume). Schema initialised in `database.py:init_db()`. Tables: `tracks`, `play_log`, `jobs`, `config`, `push_subscriptions`.

Config keys: `programming_mode`, `rotation_tracks_per_block`, `rotation_current_submitter_idx`, `rotation_block_start_log_id`, `last_returned_track_id`, `feature_min/max_*` (4 audio features).

Rotation block tracking uses `rotation_block_start_log_id` (a `play_log.id` watermark) rather than a counter. Block completion is determined by counting `play_log` entries from the current submitter since that watermark, plus 1 if `last_returned_track_id` belongs to the current submitter (handles the prefetch/track-started race condition). `last_returned_track_id` is cleared before each skip so the flushed prefetch track does not incorrectly count as an exclusion in the subsequent selection.

**Per-track cooldown** (`scheduler.py`): once the total `duration_s` of ready tracks exceeds `COOLDOWN_THRESHOLD_S` (3600 s), the rotation query adds `AND t.id NOT IN (SELECT track_id FROM play_log WHERE played_at > ?)` with a cutoff 60 minutes ago (`COOLDOWN_WINDOW_S`). `_pick_rotation_track` accepts a `depth` counter: if a submitter has no eligible tracks, their turn is skipped and the function recurses with `depth + 1`. When `depth >= len(submitters)`, `_pick_global_fallback()` is called, which picks the globally least-recently-played ready track (ignoring cooldown) to prevent silence.

**Track selection within a submitter's block** (`scheduler.py:_pick_rotation_track`): two-tier weighted random:
1. Tracks with 0 plays are guaranteed — if any exist, one is picked at random (`random.choice`).
2. Tracks with >0 plays use weighted random: `weight = 1/sqrt(play_count + 1)`. This gives less-played tracks higher odds without making them guaranteed, and ensures older/well-played tracks still have an appreciable chance each block.

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

## Frontend Notes (SPA)

The frontend is a single-page app (`frontend/index.html`) using Alpine.js v3 with hash-based routing (`/#submit`, `/#playing`, `/#library`, `/#admin`). Default route (empty hash or `/`) → Now Playing view. No page reloads occur during navigation.

**Alpine.js structure:**
- `shell()` — top-level `x-data` on `<body>`: routing (`view` + `hashchange`), persistent `<audio>` element (`this._audio`), all player state, status polling, media session, AirPlay, cast
- `submitView()`, `libraryView()`, `adminView()` — nested `x-data`; inherit parent scope so they can read `nowPlaying`, `playing`, etc.
- Library and admin views are lazy-loaded: `shell.init()` watches `view` and dispatches `load-library` / `load-admin` window events on first navigation to those views

**Key player behaviours:**
- **Resume always reconnects to live**: resume deliberately discards the buffered stream by reassigning `audio.src` with a cache-busting timestamp (`?t=Date.now()`). No "resume from where you left off."
- **The `pause` event fires for buffering stalls too**: browsers fire `pause` on stalls as well as user-initiated pauses. State briefly shows paused, then `playing` fires again — harmless.
- **Persistent player bar**: fixed at the bottom once `everPlayed` is true. Contains play/pause, cast button, and track title (tapping navigates to `/#playing`). Volume slider is Now Playing view only.
- **VLC URL format**: `https://family:passphrase@domain/stream`

## Chrome Remote Playback API (Casting)

The cast button uses the Remote Playback API (`audio.remote.prompt()`). Key sharp edges:

- **`audio.play()` must precede `prompt()`**: Chrome transfers the audio element's current playback *state* to the cast device. If the element is paused/idle when `prompt()` is called, the cast device receives silence. Always call `audio.play()` before `audio.remote.prompt()`.

- **Chrome permanently blocks `prompt()` after external disconnect**: if the user stops casting from Google Home or another external controller, Chrome flags that `HTMLMediaElement` instance as having a terminated remote session. All subsequent `prompt()` calls on that same element immediately reject with `NotAllowedError: The prompt was dismissed`. This is a Chrome non-compliance — the Remote Playback spec says `audio.load()` should run the "remote playback reset algorithm" and clear this flag, but Chrome does not honour it. `audio.load()` confirms it ran (readyState drops 4→0) but `prompt()` still rejects ~10ms later.

- **Fix: replace the `<audio>` DOM element after disconnect**: `shell()` stores the current element as `this._audio`. On disconnect, `_replaceAudioElement(oldEl)` creates a fresh `<audio>`, swaps it into the DOM via `parentNode.replaceChild()`, updates `this._audio`, and re-attaches all audio and remote playback listeners via `_setupAudioListeners(el)` + `_setupRemotePlayback(el)`. A new `HTMLMediaElement` has no remote session history — `prompt()` works again.

- **Chrome fires `disconnect` twice** after an external stop. Guard: `_setupRemotePlayback(el)` closes over `el`; both `connect` and `disconnect` handlers check `el === this._audio` before acting. After `_replaceAudioElement`, `this._audio` points to the new element, so the second `disconnect` from the old element is a no-op.

- **`castAvailable` briefly goes false after replacement**: `watchAvailability` must be re-registered on the new element. Chrome re-detects the cast device and fires the availability callback within a second or two. The cast button disappears briefly then reappears — acceptable UX.

## Frontend Admin Notes

The admin library list (in `adminView()` within `frontend/index.html`) is loaded once on login and does not auto-refresh (by design — not intended as a live dashboard). The one exception: while any track has `status='pending'`, a 5-second polling interval runs via `managePoll()` and refreshes the library until all tracks settle.

The admin view also has a **YouTube Cookies** card that shows whether a cookies file is present (via `GET /admin/youtube-cookies/status`) and provides a file upload form (`POST /admin/youtube-cookies`). Cookie status is loaded once on login alongside the library.

## Known Issues / Workarounds

- **Docker Compose does NOT auto-propagate `.env` vars to containers**: Every env var read by `api/*.py` via `os.environ.get()` must also be explicitly listed under `api.environment` in `docker-compose.yml`. Being present in `.env` (and `.env.example`) is not enough — the container only sees vars that are explicitly forwarded. Omissions cause silent failures with no error: `SMTP_HOST` missing → `send_alert()` silently no-ops; `SERVER_HOSTNAME` missing → admin URL in alert emails shows `(admin panel)`. When adding a new env var to API code, always update all three: `.env.example`, `docker-compose.yml` (under each service that needs it), and the README env var table.

- **yt-dlp + Deno + remote components**: yt-dlp requires Deno (installed in `api/Dockerfile`) and the `--remote-components ejs:github` flag to solve YouTube's JS challenge. Without it, yt-dlp can authenticate but cannot unlock audio formats (returns "Only images are available for download").
- **YouTube downloads blocked on AWS**: yt-dlp gets "Sign in to confirm you're not a bot" from AWS datacenter IPs. Primary fix: the `bgutil-provider` sidecar container generates YouTube Proof of Origin (`po_token`) tokens, which satisfy YouTube's bot check. Secondary fix: cookies from a throwaway Google account can be uploaded via the admin panel (YouTube Cookies section) and stored at `/app/cookies/youtube.txt`; the `./cookies` directory is mounted into the api container at `/app/cookies` and is gitignored. With bgutil-provider running, cookies should be long-lived; without it they were invalidated within minutes from AWS IPs.
- **YouTube cookie quality — multiple stations**: Each station uses a separate throwaway Google account and a separate cookie file. **Use separate browser profiles** (e.g. Chrome profile A for station A, profile B for station B) when generating cookies — do not sign both accounts into the same browser simultaneously. Shared browser fingerprinting cookies in a multi-account session can shorten cookie lifespan and trigger YouTube's bot detection faster. To export cookies, use the "Get cookies.txt LOCALLY" Chrome/Firefox extension (Netscape format) from within the correct profile, then upload via the admin panel.
- **`docker-compose.override.yml` is gitignored**: local dev only (disables certbot, uses HTTP-only nginx, exposes Icecast port 8000). Copy `docker-compose.override.yml.example` to use it locally. It is not present on the production server and will not be restored by `git pull`.
- **nginx env vars**: `nginx/default.conf.template` uses `${SERVER_HOSTNAME}` and `${STATION_NAME}` (for `auth_basic`). The official `nginx:alpine` image processes `/etc/nginx/templates/*.template` files with `envsubst` at startup. Both vars must be set in docker-compose.yml for nginx.
- **nginx `auth_basic off` for PWA assets is intentional**: `/sw.js`, `/api/manifest.json`, and icon PNGs are deliberately exempted from HTTP Basic Auth in both `default.conf.template` and `local.conf.template`. Browsers and mobile OSes fetch these at the system level (not through a user session) when installing a PWA or displaying notifications — they do not carry stored Basic Auth credentials. Do not remove these exemptions. The exposed content is non-sensitive (a service worker, a manifest with the station name, and static images).

- **`file.filename` is a string, not a bool**: In `api/routers/submit.py`, `has_file = file is not None and file.filename` evaluates to the filename string when a file is provided. Always wrap in `bool()` before using in arithmetic (e.g. `sum()`), otherwise a `TypeError: unsupported operand type(s) for +: 'int' and 'str'` will be raised.
- **API container needs at least 1g mem_limit**: librosa feature extraction is memory-intensive and will cause an OOM kill if the API container is constrained to 600m. The `mem_limit` in `docker-compose.yml` is set to `1g` — do not lower it. OOM kills leave jobs in `processing` state (handled by `reset_stuck_jobs()` on next startup, but the track has to be fully reprocessed).

## Secrets and Gitignored Files

- `.env` — never commit; `.env.example` is the template
- `nginx/.htpasswd` — generated locally with `htpasswd -cb nginx/.htpasswd family PASSPHRASE`
- `cookies/` — YouTube session cookies (Google account credentials); upload via admin panel, never commit

## GitHub CLI Tips

- **`gh` infers the repo from the current directory**: Run `gh issue list`, `gh pr list`, etc. directly without `--repo` when already inside the repo. Avoid command substitution like `--repo $(git remote get-url origin)` — it triggers an extra approval prompt in Claude Code UI.

## Git Workflow

Use PRs for all substantive changes (new features, bug fixes, refactors). Direct pushes to `main` are acceptable for docs-only or trivial fixes. The CI workflow (`.github/workflows/ci.yml`) only triggers on `pull_request`, so PRs are required for CI to run.

Typical flow:
```bash
git checkout -b branch-name
# make changes and commit
git push -u origin branch-name
gh pr create --title "..." --body "..."
# CI runs; merge when green
```

PR descriptions must include:
1. **What problem or enhancement this addresses** — reference the issue number when one exists (e.g. "Closes #38")
2. **A brief summary of the approach** — the key design choices made and why, so the diff can be understood in context

## Development Tips

- **Local full-stack**: `docker compose up --build` — `docker-compose.override.yml` is automatically applied and handles local differences (HTTP-only nginx, certbot disabled, `SERVER_HOSTNAME=localhost`)
- **nginx config templates**: two templates exist — `nginx/default.conf.template` (production, HTTPS + certbot) and `nginx/local.conf.template` (local, HTTP only). The override file maps the local one into the nginx container.
- To test the API locally without Docker: `cd api && uvicorn main:app --reload`
  Set env vars: `ADMIN_TOKEN=dev DB_PATH=./radio.db MEDIA_DIR=./media`
- **After any Python file change, always use `--build`**: `docker compose up -d --build api && docker compose restart nginx`. Without `--build`, Docker restarts the container using the old image — the code change is silently ignored and the old behaviour persists. nginx must also be restarted because it caches the api container's IP at startup (otherwise it serves 502s).
- To tail API logs: `docker compose logs -f api`
- Liquidsoap reconnects to Icecast automatically on failure; no manual intervention needed.
