#!/bin/bash
# Consistent SQLite backup (online, via the sqlite3 backup API) to a second location on the machine.
# Keeps 48 hourly + 30 daily copies. Verifies integrity of the fresh copy before rotating.
set -euo pipefail
SRC=${DB_PATH:-/opt/projectstate/data/projectstate.db}
DST=${BACKUP_DIR:-/var/backups/projectstate}
mkdir -p "$DST/hourly" "$DST/daily"
chmod 700 "$DST"
stamp=$(date -u +%Y%m%d-%H%M)
out="$DST/hourly/projectstate-$stamp.db"
[ -f "$SRC" ] || { echo "no database at $SRC"; exit 0; }
sqlite3 "$SRC" ".backup '$out'"
ok=$(sqlite3 "$out" "PRAGMA integrity_check;")
if [ "$ok" != "ok" ]; then echo "integrity check failed: $ok"; rm -f "$out"; exit 1; fi
gzip -f "$out"
day=$(date -u +%Y%m%d)
if [ ! -e "$DST/daily/projectstate-$day.db.gz" ]; then cp "$out.gz" "$DST/daily/projectstate-$day.db.gz"; fi
ls -1t "$DST/hourly"/*.gz 2>/dev/null | tail -n +49 | xargs -r rm -f
ls -1t "$DST/daily"/*.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
echo "backup ok: $out.gz ($(du -h "$out.gz" | cut -f1))"
