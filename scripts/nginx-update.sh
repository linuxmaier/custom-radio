#!/bin/bash
# Family Radio — nightly nginx edge image refresh
#
# nginx is the only internet-facing service (TLS termination on 80/443), so we
# keep its base image current on a nightly cadence rather than only on manual
# deploys. Pulls nginx:alpine; if a newer image was published since the running
# container was created, recreates the container. No-op when already current.
#
# Idempotent: safe to run repeatedly. Only the nginx service is touched — the
# api/liquidsoap/icecast/bgutil containers are left running untouched.
#
# Cron: see /etc/cron.d/radio-nginx-update (runs 04:00 America/Chicago, DST-aware)
# Log:  /var/log/radio-nginx-update.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LOG="/var/log/radio-nginx-update.log"
COMPOSE=(docker compose -f "${REPO_ROOT}/docker-compose.yml")

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

cd "$REPO_ROOT"

BEFORE=$(docker image inspect --format '{{.Id}}' nginx:alpine 2>/dev/null || echo "none")
log "Checking nginx:alpine for updates (current image: ${BEFORE})"

"${COMPOSE[@]}" pull nginx >>"$LOG" 2>&1
AFTER=$(docker image inspect --format '{{.Id}}' nginx:alpine 2>/dev/null || echo "none")

if [ "$BEFORE" = "$AFTER" ]; then
    log "nginx:alpine already up to date — nothing to do"
    exit 0
fi

log "Newer nginx:alpine pulled (${AFTER}) — recreating container"
"${COMPOSE[@]}" up -d nginx >>"$LOG" 2>&1

# Give nginx a moment to start, then confirm it is serving.
sleep 3
VER=$(docker exec radio-nginx-1 nginx -v 2>&1 || echo "VERSION CHECK FAILED")
log "nginx recreated — ${VER}"

# Drop the now-dangling old nginx image so nightly pulls don't accumulate disk.
docker image prune -f >>"$LOG" 2>&1 || true
log "nginx update complete"
