#!/bin/sh
# Nightly backups of the tool's and the audit sink's databases and the artifacts, into /backups.
# Checks every ten minutes; runs when the last success is 23 hours old or more (or `once`).
set -u
export PGPASSWORD="$(cat "$PGPASSWORD_FILE")"
keep="${VALXEL_BACKUP_KEEP_DAYS:-14}"

run() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dir="/backups/$stamp"
  mkdir -p "$dir" || return 1
  pg_dump -Fc -d valxel -f "$dir/valxel.dump" || return 1
  pg_dump -Fc -d audit -f "$dir/audit.dump" || return 1
  tar -C /data -czf "$dir/artifacts.tgz" artifacts || return 1
  find /backups -mindepth 1 -maxdepth 1 -type d -mtime +"$keep" -exec rm -rf {} + 2>/dev/null
  date -u +%Y-%m-%dT%H:%M:%SZ > /backups/last-success.tmp && mv /backups/last-success.tmp /backups/last-success
  echo "backup $stamp done"
}

if [ "${1:-}" = "once" ]; then
  run
  exit $?
fi
while true; do
  if [ ! -f /backups/last-success ] || [ -n "$(find /backups/last-success -mmin +1380 2>/dev/null)" ]; then
    run || echo "backup failed" >&2
  fi
  sleep 600
done
