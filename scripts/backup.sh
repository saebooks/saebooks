#!/usr/bin/env bash
# SAE Books — nightly pg_dump with retention.
#
# Runs pg_dump against the `db` service of the saebooks docker compose
# stack, writes a timestamped gzipped dump to $SAEBOOKS_BACKUP_DIR, then
# prunes old dumps per the retention policy.
#
# Retention policy:
#   * Keep 7 most recent daily dumps
#   * Keep 4 most recent weekly dumps (Sunday)
#   * Keep 12 most recent monthly dumps (1st of month)
#
# Every run appends one JSON line to $SAEBOOKS_BACKUP_DIR/backups.jsonl
# describing the outcome. The admin UI reads that log.
#
# Usage:
#   ./scripts/backup.sh              # one-off run
#   systemd timer                    # scheduled (see ops/systemd/)
#
# Env:
#   SAEBOOKS_BACKUP_DIR    where dumps live (default: $HOME/saebooks-backups)
#   SAEBOOKS_COMPOSE_DIR   path to the saebooks compose project
#                          (default: directory above this script)
#   SAEBOOKS_DB_SERVICE    compose service name (default: db)
#   SAEBOOKS_DB_USER       postgres user (default: saebooks)
#   SAEBOOKS_DB_NAME       postgres database (default: saebooks)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SAEBOOKS_COMPOSE_DIR:=$(cd "${SCRIPT_DIR}/.." && pwd)}"
: "${SAEBOOKS_BACKUP_DIR:=${HOME}/saebooks-backups}"
: "${SAEBOOKS_DB_SERVICE:=db}"
: "${SAEBOOKS_DB_USER:=saebooks}"
: "${SAEBOOKS_DB_NAME:=saebooks}"

RETAIN_DAILY=7
RETAIN_WEEKLY=4
RETAIN_MONTHLY=12

mkdir -p "${SAEBOOKS_BACKUP_DIR}"

TS_START=$(date +%s)
TS_LABEL=$(date +%Y%m%d-%H%M%S)
DUMP_FILE="${SAEBOOKS_BACKUP_DIR}/saebooks-${TS_LABEL}.sql.gz"
LOG_FILE="${SAEBOOKS_BACKUP_DIR}/backups.jsonl"

log_line() {
    # Append a JSON line to the log. Fields are pre-escaped by the caller.
    local status="$1"; local file="$2"; local size="$3"; local duration="$4"
    local error="${5:-}"
    printf '{"ts":"%s","status":"%s","file":"%s","size_bytes":%s,"duration_s":%s,"error":"%s"}\n' \
        "$(date -Is)" "$status" "$file" "$size" "$duration" "$error" \
        >> "${LOG_FILE}"
}

notify() {
    # Best-effort Telegram notification via claude-notify if available.
    if command -v claude-notify >/dev/null 2>&1; then
        claude-notify "$1" >/dev/null 2>&1 || true
    fi
}

# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------
cd "${SAEBOOKS_COMPOSE_DIR}"

# Use `docker compose exec -T` (no TTY). pg_dump writes to stdout,
# we pipe through gzip on the host. `-Z0` so gzip-on-host does the work.
if ! docker compose exec -T \
        "${SAEBOOKS_DB_SERVICE}" \
        pg_dump -U "${SAEBOOKS_DB_USER}" -d "${SAEBOOKS_DB_NAME}" \
                --no-owner --no-privileges --clean --if-exists \
        2>/tmp/saebooks-backup.err | gzip -9 > "${DUMP_FILE}"; then
    DURATION=$(( $(date +%s) - TS_START ))
    ERR=$(tr -d '"\n' < /tmp/saebooks-backup.err | cut -c1-500)
    rm -f "${DUMP_FILE}"
    log_line "FAIL" "" "0" "${DURATION}" "${ERR}"
    notify "SAE Books backup FAILED: ${ERR}"
    exit 1
fi

SIZE=$(stat -c %s "${DUMP_FILE}")
DURATION=$(( $(date +%s) - TS_START ))

# Sanity: dump must be non-trivial (>4 KiB). An empty gzip stream is ~20 bytes.
if [[ "${SIZE}" -lt 4096 ]]; then
    rm -f "${DUMP_FILE}"
    log_line "FAIL" "" "0" "${DURATION}" "dump too small (${SIZE} bytes)"
    notify "SAE Books backup FAILED: dump too small (${SIZE} bytes)"
    exit 1
fi

log_line "OK" "$(basename "${DUMP_FILE}")" "${SIZE}" "${DURATION}" ""

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
# Strategy: walk files sorted newest→oldest, classify each by its timestamp:
#   - daily bucket always (limit RETAIN_DAILY)
#   - weekly bucket if DOW=0 (Sunday)    (limit RETAIN_WEEKLY)
#   - monthly bucket if DOM=01           (limit RETAIN_MONTHLY)
# A file is kept if any bucket still has room. Otherwise deleted.

cd "${SAEBOOKS_BACKUP_DIR}"

daily=0; weekly=0; monthly=0
while IFS= read -r -d '' f; do
    # Extract YYYYMMDD from filename saebooks-YYYYMMDD-HHMMSS.sql.gz
    fname=$(basename "$f")
    ymd=$(echo "$fname" | sed -n 's/^saebooks-\([0-9]\{8\}\)-.*/\1/p')
    if [[ -z "$ymd" ]]; then
        continue   # unknown file, leave alone
    fi
    dow=$(date -d "${ymd:0:4}-${ymd:4:2}-${ymd:6:2}" +%u)   # 1-7, 7=Sun
    dom=${ymd:6:2}

    keep="no"
    if (( daily < RETAIN_DAILY )); then keep="yes"; daily=$((daily+1)); fi
    if [[ "$dow" == "7" ]] && (( weekly < RETAIN_WEEKLY )); then keep="yes"; weekly=$((weekly+1)); fi
    if [[ "$dom" == "01" ]] && (( monthly < RETAIN_MONTHLY )); then keep="yes"; monthly=$((monthly+1)); fi

    if [[ "$keep" == "no" ]]; then
        rm -f -- "$f"
    fi
done < <(find . -maxdepth 1 -name 'saebooks-*.sql.gz' -print0 | sort -zr)

echo "backup OK: ${DUMP_FILE} (${SIZE} bytes in ${DURATION}s)"
