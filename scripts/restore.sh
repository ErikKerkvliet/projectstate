#!/bin/bash
# Restore a backup: scripts/restore.sh /var/backups/projectstate/hourly/projectstate-YYYYMMDD-HHMM.db.gz
set -euo pipefail
SRC=$1; DB=/opt/projectstate/data/projectstate.db
systemctl stop projectstate
cp "$DB" "$DB.pre-restore-$(date -u +%s)" 2>/dev/null || true
rm -f "$DB" "$DB-wal" "$DB-shm"
gunzip -c "$SRC" > "$DB"
chown mcpstate:mcpstate "$DB"
sqlite3 "$DB" "PRAGMA integrity_check;"
systemctl start projectstate
echo restored from $SRC
