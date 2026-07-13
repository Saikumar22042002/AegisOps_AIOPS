#!/usr/bin/env bash
# PR-5 BACKUP — scheduled pg_dump of the AegisOps application database.
# Postgres is the ONLY store that must be backed up:
#   - Redis      = ephemeral coordination (heartbeats, channels) — rebuildable, not backed up.
#   - Neo4j      = derived mirror — rebuild with `python -m app.admin rebuild-world-model`.
#   - TF state   = object-store bucket versioning (see the A3 remote-backend docs).
#
# Run from cron/systemd-timer on the DB host or a sidecar. Keeps the last RETENTION dumps.
#
#   BACKUP_DIR=/var/backups/aegisops RETENTION=14 ./pg_backup.sh
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL (postgresql://user:pass@host:port/db)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/aegisops}"
RETENTION="${RETENTION:-14}"          # how many dumps to keep
mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/aegisops-${stamp}.dump"

# Custom format (-Fc) → parallel, selective restore via pg_restore.
pg_dump --dbname="$DATABASE_URL" --format=custom --no-owner --file="$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"

# Prune old dumps beyond RETENTION (newest kept).
ls -1t "$BACKUP_DIR"/aegisops-*.dump 2>/dev/null | tail -n +"$((RETENTION + 1))" | while read -r old; do
  rm -f "$old"
  echo "pruned $old"
done
