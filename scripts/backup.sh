#!/bin/bash
# Family Radio — daily backup to S3 or any S3-compatible object store
# - SQLite DB: safe snapshot via Python's sqlite3.Connection.backup()
# - Media: incremental sync of processed MP3 tracks (tracks/ only, not raw/)
#
# Configuration (in .env):
#   BACKUP_DEST=s3://your-bucket-name          (required to enable backups)
#   BACKUP_ENDPOINT_URL=https://...            (optional; for non-AWS S3-compatible stores
#                                               e.g. Cloudflare R2, Backblaze B2, Wasabi)
#
# If BACKUP_DEST is not set, the script exits without doing anything.
#
# Cron: 0 3 * * * root /home/ubuntu/radio/scripts/backup.sh
# Log:  /var/log/radio-backup.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DB_VOLUME="/var/lib/docker/volumes/radio_db/_data"
MEDIA_VOLUME="/var/lib/docker/volumes/radio_media/_data"
LOG="/var/log/radio-backup.log"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

# Load configuration from the repo's .env file
ENV_FILE="${REPO_ROOT}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# Skip if no backup destination is configured
if [ -z "${BACKUP_DEST:-}" ]; then
    log "BACKUP_DEST not set — skipping backup. Set BACKUP_DEST=s3://your-bucket in .env to enable."
    exit 0
fi

BUCKET="${BACKUP_DEST}"

# Build optional endpoint args for S3-compatible providers
ENDPOINT_ARGS=()
if [ -n "${BACKUP_ENDPOINT_URL:-}" ]; then
    ENDPOINT_ARGS=("--endpoint-url" "${BACKUP_ENDPOINT_URL}")
fi

log "Starting backup to ${BUCKET} (${TIMESTAMP})"

# 1. Safe SQLite backup via Python inside the running api container.
#    sqlite3.Connection.backup() is safe during concurrent writes (WAL mode).
#    Writes to the db volume so we can access it from the host.
docker exec radio-api-1 python3 -c "
import sqlite3
src = sqlite3.connect('/data/radio.db')
dst = sqlite3.connect('/data/radio-backup.db')
src.backup(dst)
src.close()
dst.close()
"
log "DB snapshot created"

# 2. Upload timestamped DB backup + overwrite 'latest' for easy restore
aws s3 cp "${ENDPOINT_ARGS[@]}" "${DB_VOLUME}/radio-backup.db" "${BUCKET}/db/radio-${TIMESTAMP}.db" --quiet
aws s3 cp "${ENDPOINT_ARGS[@]}" "${DB_VOLUME}/radio-backup.db" "${BUCKET}/db/radio-latest.db" --quiet
rm -f "${DB_VOLUME}/radio-backup.db"
log "DB uploaded to ${BUCKET}"

# 3. Sync processed MP3s (incremental; no --delete so accidentally-deleted
#    tracks are still recoverable from the backup destination)
aws s3 sync "${ENDPOINT_ARGS[@]}" "${MEDIA_VOLUME}/tracks/" "${BUCKET}/media/tracks/" --quiet
log "Media synced to ${BUCKET}"

log "Backup complete"
