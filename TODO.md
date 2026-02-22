# TODO

## 1. Local MVP Test

Get the full stack running locally to validate the end-to-end flow before touching a server.

### Adjustments needed for local running
- [x] Add a `docker-compose.override.yml` for local use: skip certbot, serve nginx on HTTP only (no TLS), and remove the HTTPS server block (or use a self-signed cert)
- [x] Confirm the `moul/icecast` image actually accepts the env-variable-style config in `icecast.xml` — it may need a different base image or a startup script to substitute values

### Smoke test checklist
- [x] `docker compose up --build` completes without errors
- [x] Icecast status page responds at `http://localhost:8000/status-json.xsl`
- [x] Liquidsoap connects to Icecast (check `docker compose logs liquidsoap`)
- [x] Web UI loads at `http://localhost` (or whichever port nginx is on locally)
- [x] Submit a YouTube link → track appears in library as `pending`
- [x] Track transitions to `ready` within a few minutes
- [x] `GET /api/status` shows the track as now-playing after Liquidsoap picks it up
- [x] VLC can connect to `http://localhost:8000/radio` and plays audio
- [x] ICY metadata (artist/title) shows in VLC's Media Information window
- [x] Admin page: switch mode to Mood, verify next track changes without a restart
- [x] Admin page: skip button advances to the next track
- [x] Admin auth via `X-Admin-Token` header works correctly

---

## 2. AWS Deployment

After the local test passes, deploy to a single EC2 instance.

### Infrastructure decisions to make
- [ ] Choose instance type (t3.small is probably enough; t3.medium if librosa analysis feels slow)
- [ ] Decide on storage: default EBS root volume is fine for the DB; add a separate EBS volume for `/media` so it survives instance replacement
- [ ] Pick a domain and set up Route 53 (or point an existing domain's A record at the instance IP)
- [ ] Decide whether to use an Elastic IP (recommended so the IP doesn't change on restart)

### Deployment steps
- [ ] Launch EC2 instance (Ubuntu 24.04 LTS), install Docker + Docker Compose plugin
- [ ] Open security group ports: 22 (SSH), 80 (HTTP), 443 (HTTPS), 8000 (Icecast stream)
- [ ] Clone repo to instance, copy `.env.example` → `.env`, fill in real secrets
- [ ] Generate `.htpasswd`: `htpasswd -cb nginx/.htpasswd family PASSPHRASE`
- [ ] `docker compose up -d --build`
- [ ] Run certbot to get TLS cert for the domain
- [ ] Verify HTTPS and stream, run the same smoke test checklist as above

### Things to harden before sharing the link with family
- [ ] Set a strong `SITE_PASSPHRASE` and `ADMIN_TOKEN` in `.env`
- [ ] Confirm nginx is not exposing `/internal/` routes externally
- [ ] Set up a simple backup: daily cron to snapshot the SQLite DB and sync `/media` to S3 (or just snapshot the EBS volume)
- [ ] Test that the certbot auto-renewal loop works (`docker compose logs certbot`)

---

## 3. Observability & Alerting

### Automated health checks / integration tests
- [ ] Endpoint liveness: `/api/status` returns 200 and a reasonable `now_playing` field
- [ ] Download pipeline smoke test: submit a known-stable YouTube URL, poll until `status=ready`
- [ ] Liquidsoap → Icecast: stream is reachable and producing audio bytes (not silence)
- [ ] Metadata: ICY `StreamTitle` matches the track that `/api/status` says is playing

### AWS-level alerting (CloudWatch or similar)
- [ ] EC2 instance health check alarm
- [ ] Disk usage alarm on the `/media` EBS volume (yt-dlp fills it up quietly)
- [ ] HTTP 5xx rate alarm on nginx logs

### Application-level alerting
- [ ] Jobs stuck in `pending` longer than N minutes → indicates yt-dlp or yt-dlp dependency breakage
- [ ] Track download failure rate (failed jobs / submitted jobs over a rolling window)
- [ ] No-track-playing fallback: if Liquidsoap calls `/internal/next-track` and the library is empty, log a warning and alert
- [ ] yt-dlp version check: periodically verify the installed version isn't months behind the latest release

---

## 4. Storage Management (Production)

- [ ] Revisit the best approach for managing the `/media` partition in production
  - Options include: EBS volume (simple, survives instance replacement), S3 + local cache (cheaper at scale, more complex), EFS (shared across instances, overkill for now)
  - Consider a storage cap + eviction policy: e.g. delete least-recently-played tracks when disk usage exceeds a threshold
  - Decide whether deleted tracks should be re-downloadable on demand or require re-submission

---

## 5. Deferred Testing

- [ ] **Mode switch mid-rotation**: switch programming mode from Rotation to Mood (and back) while a track is playing; verify the scheduler picks up the new mode on the very next track without a restart. Requires a larger library (5+ tracks with audio features extracted) to make the Mood output meaningfully different from Rotation.

---

## 6. Future Features

### Submission comments
- [ ] Allow submitters to add an optional short comment when submitting a song (e.g. "this one always reminds me of summer road trips")
  - Add `comment` field to the `tracks` table and `POST /submit` endpoint
  - Show the comment on the Now Playing page alongside the submitter's name when the track is on air
  - Include the comment in the AI DJ interlude script if that feature is built

### Spotify integration
- [ ] Re-add Spotify track submission via `spotdl`
  - Was removed due to spotdl API instability (Feb 2026) — revisit once spotdl is stable
  - Add `spotify_url` form field back to `/submit` endpoint
  - Add `download_spotify()` back to `api/downloader.py` and handle `source_type='spotify'` in `api/worker.py`
  - Re-add spotdl to `api/Dockerfile` (`pip install spotdl`)
  - Re-add Spotify tab to `frontend/index.html`
  - Consider wrapping in try/except and surfacing a user-friendly warning if download fails

### AI DJ interludes
- [ ] Periodically generate a short spoken interlude between tracks: recap the last few songs and who submitted them, then intro the next one
  - Use a TTS model (e.g. OpenAI TTS or ElevenLabs) to synthesize the voice clip
  - Use an LLM to write the script, given: last N track titles/artists/submitters, next track title/artist/submitter
  - Generate the audio clip ahead of time (during the gap before the next track is needed) and store it in `/media`
  - Liquidsoap schedules it as a regular audio file between two music tracks
  - Decide on frequency: every N tracks, every N minutes, or weighted random
  - Decide on persona/voice: consistent character, or vary it
