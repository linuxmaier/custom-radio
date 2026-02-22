#!/bin/bash
# Family Radio — daily backup to S3
# - SQLite DB: safe snapshot via Python's sqlite3.Connection.backup()
# - Media: incremental sync of processed MP3 tracks (tracks/ only, not raw/)
#
# Cron: 0 3 * * * root /home/ubuntu/radio/scripts/backup.sh
# Log:  /var/log/radio-backup.log

set -euo pipefail

BUCKET="s3://family-radio-backup"
DB_VOLUME="/var/lib/docker/volumes/radio_db/_data"
MEDIA_VOLUME="/var/lib/docker/volumes/radio_media/_data"
LOG="/var/log/radio-backup.log"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

log "Starting backup (${TIMESTAMP})"

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
aws s3 cp "${DB_VOLUME}/radio-backup.db" "${BUCKET}/db/radio-${TIMESTAMP}.db" --quiet
aws s3 cp "${DB_VOLUME}/radio-backup.db" "${BUCKET}/db/radio-latest.db" --quiet
rm -f "${DB_VOLUME}/radio-backup.db"
log "DB uploaded to S3"

# 3. Sync processed MP3s (incremental; no --delete so accidentally-deleted
#    tracks are still recoverable from S3)
aws s3 sync "${MEDIA_VOLUME}/tracks/" "${BUCKET}/media/tracks/" --quiet
log "Media synced to S3"

log "Backup complete"
