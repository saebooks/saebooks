#!/usr/bin/env bash
# SAE Books — weekly restore test.
#
# Picks the most recent dump in $SAEBOOKS_BACKUP_DIR, restores it into a
# temporary database on the same postgres instance, runs integrity queries,
# then drops the temp database.
#
# The point: an untested backup is not a backup. This proves we can actually
# get data back.
#
# Integrity checks:
#   * At least one row in companies
#   * At least one row in accounts
#   * Row counts in journal_entries, journal_lines, bank_statement_lines
#     approximately match the live database (±5% tolerance — the dump is a
#     snapshot, writes may have happened since)
#
# Results are appended as JSON lines to $SAEBOOKS_BACKUP_DIR/restore-tests.jsonl
# and a notify-hook fires on failure.
#
# Env: same as backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SAEBOOKS_COMPOSE_DIR:=$(cd "${SCRIPT_DIR}/.." && pwd)}"
: "${SAEBOOKS_BACKUP_DIR:=${HOME}/saebooks-backups}"
: "${SAEBOOKS_DB_SERVICE:=db}"
: "${SAEBOOKS_DB_USER:=saebooks}"
: "${SAEBOOKS_DB_NAME:=saebooks}"

TEMP_DB="saebooks_restore_test_$(date +%s)"
LOG_FILE="${SAEBOOKS_BACKUP_DIR}/restore-tests.jsonl"
TS_START=$(date +%s)

log_line() {
    local status="$1"; local dump="$2"; local duration="$3"
    local details="${4:-}"
    printf '{"ts":"%s","status":"%s","dump":"%s","duration_s":%s,"details":"%s"}\n' \
        "$(date -Is)" "$status" "$dump" "$duration" "$(echo "$details" | tr -d '"\n' | cut -c1-500)" \
        >> "${LOG_FILE}"
}

notify() {
    if command -v notify-hook >/dev/null 2>&1; then
        notify-hook "$1" >/dev/null 2>&1 || true
    fi
}

cleanup() {
    # Drop temp DB even on failure. Errors here don't mask the original.
    docker compose -f "${SAEBOOKS_COMPOSE_DIR}/docker-compose.yml" exec -T \
        "${SAEBOOKS_DB_SERVICE}" \
        psql -U "${SAEBOOKS_DB_USER}" -d postgres \
             -c "DROP DATABASE IF EXISTS ${TEMP_DB};" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "${SAEBOOKS_COMPOSE_DIR}"

# ---------------------------------------------------------------------------
# Find latest dump
# ---------------------------------------------------------------------------
LATEST=$(find "${SAEBOOKS_BACKUP_DIR}" -maxdepth 1 -name 'saebooks-*.sql.gz' \
            -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)

if [[ -z "${LATEST}" ]]; then
    log_line "FAIL" "" "0" "no dumps found in ${SAEBOOKS_BACKUP_DIR}"
    notify "SAE Books restore-test: no dumps to test"
    exit 1
fi

DUMP_NAME=$(basename "${LATEST}")

# ---------------------------------------------------------------------------
# Create temp DB
# ---------------------------------------------------------------------------
docker compose exec -T "${SAEBOOKS_DB_SERVICE}" \
    psql -U "${SAEBOOKS_DB_USER}" -d postgres \
         -c "CREATE DATABASE ${TEMP_DB};" \
    > /tmp/saebooks-restore.log 2>&1

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
# Stream the gzip dump through docker exec → psql. Use ON_ERROR_STOP so a
# partial restore counts as failure.
if ! gunzip -c "${LATEST}" | \
        docker compose exec -T "${SAEBOOKS_DB_SERVICE}" \
        psql -v ON_ERROR_STOP=1 -U "${SAEBOOKS_DB_USER}" -d "${TEMP_DB}" \
        >> /tmp/saebooks-restore.log 2>&1; then
    DURATION=$(( $(date +%s) - TS_START ))
    TAIL=$(tail -n 5 /tmp/saebooks-restore.log | tr -d '"\n' | cut -c1-500)
    log_line "FAIL" "${DUMP_NAME}" "${DURATION}" "restore failed: ${TAIL}"
    notify "SAE Books restore-test FAILED on ${DUMP_NAME}: ${TAIL}"
    exit 1
fi

# ---------------------------------------------------------------------------
# Integrity checks
# ---------------------------------------------------------------------------
query() {
    docker compose exec -T "${SAEBOOKS_DB_SERVICE}" \
        psql -U "${SAEBOOKS_DB_USER}" -d "$1" -t -A -c "$2" 2>/dev/null | tr -d ' \n'
}

fails=()

check_nonzero() {
    local table="$1"
    local n; n=$(query "${TEMP_DB}" "SELECT COUNT(*) FROM ${table};")
    if [[ -z "$n" || "$n" == "0" ]]; then
        fails+=("${table} is empty in restored DB")
    fi
    echo "$n"
}

check_close() {
    # Row counts in dump vs live: tolerate ±5% plus a minimum of 3 row drift
    # (a nightly dump taken at midnight, compared against a production DB
    # getting writes throughout the day, can diverge slightly).
    local table="$1"
    local restored; restored=$(query "${TEMP_DB}" "SELECT COUNT(*) FROM ${table};")
    local live;     live=$(query "${SAEBOOKS_DB_NAME}" "SELECT COUNT(*) FROM ${table};")
    if [[ -z "$restored" || -z "$live" ]]; then
        fails+=("${table} count query failed (restored=${restored}, live=${live})")
        return
    fi
    # abs diff
    local diff=$(( restored > live ? restored - live : live - restored ))
    local tol=$(( live / 20 + 3 ))   # 5% + 3
    if (( diff > tol )); then
        fails+=("${table} drift too large: restored=${restored} live=${live} diff=${diff} tol=${tol}")
    fi
}

c_companies=$(check_nonzero companies)
c_accounts=$(check_nonzero accounts)
check_close journal_entries
check_close journal_lines
check_close bank_statement_lines

DURATION=$(( $(date +%s) - TS_START ))

if (( ${#fails[@]} > 0 )); then
    DETAILS=$(IFS='; '; echo "${fails[*]}")
    log_line "FAIL" "${DUMP_NAME}" "${DURATION}" "${DETAILS}"
    notify "SAE Books restore-test FAILED on ${DUMP_NAME}: ${DETAILS}"
    exit 1
fi

DETAILS="companies=${c_companies} accounts=${c_accounts}"
log_line "OK" "${DUMP_NAME}" "${DURATION}" "${DETAILS}"
echo "restore-test OK: ${DUMP_NAME} (${DURATION}s, ${DETAILS})"
