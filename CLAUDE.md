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
nginx/        nginx template configs:
                default.conf.template     = production HTTPS
                local.conf.template       = local HTTP-only
certbot/      Dockerfile extending certbot/certbot with docker-cli + reload-nginx.sh script
              (reload-nginx.sh finds the nginx container by its family-radio.service=nginx Docker
              label and sends nginx -s reload; called by the certbot --deploy-hook after renewal)
scripts/      Host-level scripts (backup.sh — daily S3 backup)
```

The `bgutil-provider` service uses the upstream `brainicism/bgutil-ytdlp-pot-provider` image (no local directory). It runs a Node.js HTTP server on port 4416 (internal Docker network only) that generates YouTube Proof of Origin (`po_token`) tokens. The `bgutil-ytdlp-pot-provider` pip package (installed in the api image) is the yt-dlp plugin that calls it automatically on each YouTube download.

## API Layout

Routers in `api/routers/`:
- `auth.py`     — POST /auth/request-access, GET+POST /auth/verify, POST /auth/claim, POST /auth/logout, POST /auth/bootstrap, GET/PATCH /auth/me, GET /auth/claimable-names, GET/POST /auth/users, POST /auth/users/{id}/approve|reject, DELETE /auth/users/{id}, POST /auth/passkey/register/begin|complete, POST /auth/passkey/authenticate/begin|complete, GET /auth/passkey/list, DELETE /auth/passkey/{credential_id}
- `submit.py`   — POST /submit, GET /check-duplicate, GET /submitters, DELETE /track/{id} (own-track deletion)
- `internal.py` — GET /internal/next-track (returns annotate URI with title/artist from DB), POST /internal/track-started/{id}
- `admin.py`    — GET/POST /admin/config, POST /admin/skip, DELETE /admin/track/{id}, GET /admin/youtube-cookies/status, POST /admin/youtube-cookies
- `status.py`   — GET /status (returns now_playing, recent, pending_count, station_name, public_stream_url), GET /library, GET /public-library, GET /track/{id}
- `push.py`     — GET /manifest.json (dynamic PWA manifest), GET /push/vapid-key, POST /push/subscribe, POST /push/unsubscribe

All public routes go through nginx at `/api/`. Internal routes are Docker-network-only (blocked by nginx).

User endpoints (all routes except `/auth/request-access`, `/auth/verify`, `/auth/claim`, `/auth/bootstrap`, and `/push/vapid-key`) require a valid session cookie via `require_user` (FastAPI `Depends`). The dependency reads the `session` cookie, hashes it, joins `sessions` + `users`, checks expiry, slides the session TTL, and returns `{id, email, name, status}`. 401 is raised for missing/invalid/expired sessions.

Admin endpoints are authenticated via the `X-Admin-Token` request header (value = `ADMIN_TOKEN` env var, no "Bearer" prefix).

`IS_LOCAL = os.environ.get("SERVER_HOSTNAME", "localhost") in ("localhost", "")` — when True, auth endpoints include `debug_url` and `debug_token` in responses instead of sending email (no SMTP needed for local dev).

**Skip mechanism**: POST /admin/skip connects to the Liquidsoap telnet server (`liquidsoap:1234`) and sends `dynamic.flush_and_skip`. This immediately stops the current track and fetches a fresh next track. The telnet server is enabled in `radio.liq` with `settings.server.telnet.set(true)` and bound to `0.0.0.0` so it's reachable from the API container. Do not use `icecast_out.skip` — it operates at the output layer and does not reliably interrupt the audio stream.

## Database

SQLite at `/data/radio.db` (Docker volume). Schema initialised in `database.py:init_db()`. Tables: `tracks`, `play_log`, `jobs`, `config`, `push_subscriptions`, `users`, `auth_tokens`, `claim_codes`, `sessions`, `passkey_credentials`, `passkey_challenges`.

Auth tables added in Phase 1 per-user auth: `users` (email, name, status: pending/approved/rejected), `auth_tokens` (magic link tokens; 15-min TTL, single-use), `claim_codes` (6-digit bridge codes for cross-browser PWA cookie scoping; 5-min TTL, single-use), `sessions` (httpOnly cookie sessions; 30-day sliding TTL). All token/code values are stored as SHA-256 hashes. Foreign keys with ON DELETE CASCADE propagate user deletion to all auth rows. `tracks.user_id` and `push_subscriptions.user_id` are nullable FK references to `users.id` (added via ALTER migration).

Auth tables added in Phase 2 passkeys: `passkey_credentials` (id: credential_id b64url PK, user_id FK, public_key blob, sign_count, aaguid, created_at, last_used_at), `passkey_challenges` (challenge b64url PK, user_id nullable FK, type: `registration`|`authentication`, expires_at; 5-min TTL; deleted on consume or at next challenge insert).

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
- `shell()` — top-level `x-data` on `<body>`: routing (`view` + `hashchange`), persistent `<audio>` element (`this._audio`), all player state, status polling, media session, AirPlay, cast, auth state
- `submitView()`, `libraryView()`, `adminView()` — nested `x-data`; inherit parent scope so they can read `nowPlaying`, `playing`, etc.
- Library and admin views are lazy-loaded: `shell.init()` watches `view` and dispatches `load-library` / `load-admin` window events on first navigation to those views

**Auth state in `shell()`:**
- `user` — `{id, email, name}` or `null`; `authChecked` — `false` until `GET /api/auth/me` resolves (prevents flash of unauthed content)
- `loginState` — `'email'` | `'pending'` | `'code'`; `loginEmail`, `loginMsg`, `loginError`, `loginBusy`, `claimCode`
- `setupName`, `setupError`, `setupBusy` — for display-name-setup view shown on first login; `claimableNames: []` — populated by `GET /api/auth/claimable-names` when setup view appears; allows existing submitters to reclaim their track history by picking their name
- `shareUrl` — YouTube URL captured from Android Web Share Target query params (`?url=` or `?text=`); set synchronously in `init()` before the auth fetch, cleared by `initSubmit()` after pre-filling the form

**Auth flow in `init()`:**
- `init()` is `async`; `_initRunning` guard at the top prevents double-execution (Alpine calls `init()` twice: once for the `init` method on the data object, once for `x-init="init()"` on `<body>`)
- Immediately after the guard, query params are parsed for Web Share Target: `_extractYouTubeUrl()` checks `?url=` then `?text=`, stores any match in `this.shareUrl`, and calls `history.replaceState` to clean the URL — all synchronously, before the auth fetch
- First action: `GET /api/auth/me` — 200 → set `user`, proceed normally; 401 → set `view = 'login'`
- After auth succeeds, if `shareUrl` is set: `view = 'submit'` (overrides `_applyHash`). Same logic in `_routeToApp()` (called after passkey/magic-link sign-in).
- `_applyHash()` forces `view = 'login'` if `!this.user`
- `refresh()` (status poller) handles 401 by clearing `user` and setting `view = 'login'`

**Web Share Target (`initSubmit()`):**
- The manifest (`/api/manifest.json`) includes `share_target` with `action: "/"`, `method: "GET"`, params `title`/`text`/`url`. Android adds the PWA to the OS share sheet; sharing a YouTube URL opens `/?url=...` or `/?text=...`.
- `initSubmit()` uses `$watch('view', ...)` to pre-fill the form when `view` transitions to `'submit'`. Do NOT change this to `$watch('shareUrl', ...)` or an immediate apply — Alpine initialises child `x-data` components (including `submitView`) while `init()` is suspended at `await fetch('/api/auth/me')`, so `initSubmit()` runs before the auth check completes. Clearing `shareUrl` immediately would prevent the auth-success branch from seeing it and navigating to the submit view.

**Views added by auth:**
- `login` — three states: email entry → `POST /api/auth/request-access` (approved users get magic link; others see pending message); code entry → `POST /api/auth/claim` → cookie set, `needs_name` check. Passkey users can sign in without email via the passkey flow (no code needed).
- `setup` — display name input, shown after first login when `user.name === null` → `PATCH /api/auth/me`. Includes a `<datalist>` of claimable submitter names from `GET /api/auth/claimable-names`; picking an existing name backfills `tracks.user_id` for all matching unclaimed tracks.

**Alpine.js scope in nested `x-data`:** nested components (`submitView()`, `libraryView()`, `adminView()`) access parent `shell()` properties via direct property names (e.g. `user`, `nowPlaying`). Do NOT use `$root.user` — `$root` in Alpine v3 does not reliably expose parent `x-data` properties in nested scopes.

**Key player behaviours:**
- **Resume always reconnects to live**: resume deliberately discards the buffered stream by reassigning `audio.src` with a cache-busting timestamp (`?t=Date.now()`). No "resume from where you left off."
- **The `pause` event fires for buffering stalls too**: browsers fire `pause` on stalls as well as user-initiated pauses. State briefly shows paused, then `playing` fires again — harmless.
- **Persistent player bar**: fixed at the bottom once `everPlayed` is true. Contains play/pause, cast button, and track title (tapping navigates to `/#playing`). Volume slider is Now Playing view only.
- **VLC URL format**: `https://family:passphrase@domain/stream`

## Google Cast SDK (Casting)

The cast button uses the **Google Cast SDK** (`cast_sender.js`), NOT the Remote Playback API. The SDK was chosen because the Remote Playback API proxies the stream through the local browser tab — Chrome's background tab throttling (~1 min after losing focus) paused the local `<audio>` element and immediately paused the Chromecast. The Cast SDK tells the Chromecast to connect directly to `publicStreamUrl` (the `PUBLIC_STREAM_TOKEN` route), making it fully independent of the local tab's state.

Key implementation details:

- **Dynamic loading + `__onGCastApiAvailable`**: `_loadCastSdk()` injects the SDK script and defines `window.__onGCastApiAvailable` (the SDK's required callback) before the script loads. The callback closes over `this` so it can call `this._setupCastSdk()` directly. A `window.__castSdkLoading` guard prevents double-injection — Alpine calls `init()` twice (once automatically for the `init` method on the data object, once from the `x-init="init()"` attribute on `<body>`).

- **`_setupCastSdk()`**: initialises `CastContext` with `DEFAULT_MEDIA_RECEIVER_APP_ID`. `CAST_STATE_CHANGED` drives `castAvailable` and `casting`. `SESSION_STATE_CHANGED` handles `SESSION_ENDED`: clears cast state and auto-resumes local audio by reassigning `this._audio.src` with a cache-busting timestamp and calling `play()`.

- **`cast()`**: calls `context.requestSession()` (shows device picker); on success calls `session.loadMedia()` with a `MediaInfo` of type `audio/mpeg`, `StreamType.LIVE`, and now-playing metadata. Pauses local audio before `loadMedia()` so there is no double audio. Tapping the cast button while already casting calls `requestSession()` again, which shows the Cast management dialog (includes "Stop Casting"). On `SESSION_ENDED` the SDK fires the event and local audio resumes automatically.

- **After extended background time**: Chrome may lose track of the SDK session while the tab is backgrounded. If this happens, the cast button shows the "start new cast" picker rather than the management dialog — the Chromecast itself keeps playing unaffected, since it is connected directly to Icecast.

## Frontend Admin Notes

The admin library list (in `adminView()` within `frontend/index.html`) is loaded once on login and does not auto-refresh (by design — not intended as a live dashboard). The one exception: while any track has `status='pending'`, a 5-second polling interval runs via `managePoll()` and refreshes the library until all tracks settle.

The admin view also has a **YouTube Cookies** card that shows whether a cookies file is present (via `GET /admin/youtube-cookies/status`) and provides a file upload form (`POST /admin/youtube-cookies`). Cookie status is loaded once on login alongside the library.

## Known Issues / Workarounds

- **Docker Compose does NOT auto-propagate `.env` vars to containers**: Every env var read by `api/*.py` via `os.environ.get()` must also be explicitly listed under `api.environment` in `docker-compose.yml`. Being present in `.env` (and `.env.example`) is not enough — the container only sees vars that are explicitly forwarded. Omissions cause silent failures with no error: `SMTP_HOST` missing → `send_alert()` silently no-ops; `SERVER_HOSTNAME` missing → admin URL in alert emails shows `(admin panel)`. When adding a new env var to API code, always update all three: `.env.example`, `docker-compose.yml` (under each service that needs it), and the README env var table.

- **yt-dlp + Deno + remote components**: yt-dlp requires Deno (installed in `api/Dockerfile`) and the `--remote-components ejs:github` flag to solve YouTube's JS challenge. Without it, yt-dlp can authenticate but cannot unlock audio formats (returns "Only images are available for download").
- **YouTube downloads blocked on AWS**: yt-dlp gets "Sign in to confirm you're not a bot" from AWS datacenter IPs. Fix (Mar 2026): valid logged-in cookies (exported from a signed-in throwaway Google account) cause YouTube to serve HLS/SABR streams via the `web_safari` player client, which bypasses the ejs sig/n deciphering challenge entirely. bgutil-provider 1.3.1+ handles po_token generation for the initial bot check. yt-dlp nightly is used (`pip install --pre --upgrade yt-dlp` in Dockerfile) for the best HLS support. **Cookies must be exported while actually signed in** (export after browsing YouTube, so the file contains `SID`, `SSID`, `LOGIN_INFO`, `ST-*`, `__Secure-YNID` tokens) — unauthenticated cookie exports still fail. Upload via admin panel → YouTube Cookies.
- **bgutil-provider must be pulled on every production deploy** — it uses an upstream image that is never rebuilt locally. Always include `docker compose pull bgutil-provider` before `docker compose up`. Running an outdated bgutil image (pre-1.3.0) causes po_token generation to fail silently with "Sign in to confirm you're not a bot".
- **YouTube cookie quality — multiple stations**: Each station uses a separate throwaway Google account and a separate cookie file. **Use separate browser profiles** (e.g. Chrome profile A for station A, profile B for station B) when generating cookies — do not sign both accounts into the same browser simultaneously. Shared browser fingerprinting cookies in a multi-account session can shorten cookie lifespan and trigger YouTube's bot detection faster. To export cookies, use the "Get cookies.txt LOCALLY" Chrome/Firefox extension (Netscape format) from within the correct profile, then upload via the admin panel.
- **`docker-compose.override.yml` is gitignored**: local dev only (disables certbot, uses HTTP-only nginx, exposes Icecast port 8000). Copy `docker-compose.override.yml.example` to use it locally. It is not present on the production server and will not be restored by `git pull`.
- **nginx env vars**: `nginx/default.conf.template` uses `${SERVER_HOSTNAME}` and `${PUBLIC_STREAM_TOKEN}`. The official `nginx:alpine` image processes `/etc/nginx/templates/*.template` files with `envsubst` at startup. `PUBLIC_STREAM_TOKEN` defaults to empty string via `${PUBLIC_STREAM_TOKEN:-}`, which disables the public stream route.

- **Magic link prefetcher protection**: `GET /auth/verify` validates the token and returns a "Get my sign-in code" button page — it does NOT consume the token. `POST /auth/verify` (the button's form target) consumes the token and returns the claim code page. This split prevents iMessage, Gmail, and other email/messaging clients from silently burning single-use tokens via link prefetch GETs before the user taps the link.

- **`file.filename` is a string, not a bool**: In `api/routers/submit.py`, `has_file = file is not None and file.filename` evaluates to the filename string when a file is provided. Always wrap in `bool()` before using in arithmetic (e.g. `sum()`), otherwise a `TypeError: unsupported operand type(s) for +: 'int' and 'str'` will be raised.
- **API container needs at least 1g mem_limit**: librosa feature extraction is memory-intensive and will cause an OOM kill if the API container is constrained to 600m. The `mem_limit` in `docker-compose.yml` is set to `1g` — do not lower it. OOM kills leave jobs in `processing` state (handled by `reset_stuck_jobs()` on next startup, but the track has to be fully reprocessed).

## Secrets and Gitignored Files

- `.env` — never commit; `.env.example` is the template
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
- **nginx config templates**: two templates — `nginx/default.conf.template` (production, HTTPS) and `nginx/local.conf.template` (local, HTTP only). The override file maps `local.conf.template` into the nginx container.
- To test the API locally without Docker: `cd api && uvicorn main:app --reload`
  Set env vars: `ADMIN_TOKEN=dev DB_PATH=./radio.db MEDIA_DIR=./media`
- **After any Python file change, always use `--build`**: `docker compose up -d --build api && docker compose restart nginx`. Without `--build`, Docker restarts the container using the old image — the code change is silently ignored and the old behaviour persists. nginx must also be restarted because it caches the api container's IP at startup (otherwise it serves 502s).
- **Production deploy command** (via SSM): `sudo -u ubuntu git -C /home/ubuntu/radio pull && docker compose -f /home/ubuntu/radio/docker-compose.yml pull bgutil-provider && docker compose -f /home/ubuntu/radio/docker-compose.yml up -d --build api bgutil-provider && docker compose -f /home/ubuntu/radio/docker-compose.yml restart nginx`. The `pull bgutil-provider` step is required — `bgutil-provider` uses an upstream image that is never rebuilt locally, so it won't update without an explicit pull.
- To tail API logs: `docker compose logs -f api`
- Liquidsoap reconnects to Icecast automatically on failure; no manual intervention needed.
