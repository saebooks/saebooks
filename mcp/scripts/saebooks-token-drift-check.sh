#!/bin/bash
# Cron-friendly wrapper for `saebooks-token-sync --check`.
#
# Behaviour:
#   - in sync         → log "ok", exit 0, silent
#   - drift detected  → auto-heal via `saebooks-token-sync`, log diff, exit 0
#   - canonical gone  → notify-hook (emergency channel), exit 1
#   - sync itself fails → notify-hook (emergency channel), exit 1
#
# Telegram is reserved for genuine emergencies per CLAUDE.md, so drift
# (recoverable) only goes to the log. Loss of the canonical secret file
# DOES warrant a page — that's a token-rotation-or-recover event.
#
# Suggested crontab:
#   @reboot             $HOME/bin/saebooks-token-drift-check.sh
#   33 3 * * *          $HOME/bin/saebooks-token-drift-check.sh

set -uo pipefail

LOG_DIR="${HOME}/.local/state"
LOG="${LOG_DIR}/saebooks-token-sync.log"
mkdir -p "$LOG_DIR"

ts() { date +'%Y-%m-%dT%H:%M:%S%z'; }
log() { echo "$(ts) $*" >>"$LOG"; }

CANONICAL="${HOME}/.claude/secrets/saebooks-claude-code.env"

# Hard error: canonical missing.
if [ ! -r "$CANONICAL" ]; then
    msg="⚠ SAE Books canonical token file missing: $CANONICAL"
    log "ERROR $msg"
    if command -v notify-hook >/dev/null 2>&1; then
        notify-hook "$msg" || true
    fi
    exit 1
fi

# Drift check — fast path, no side effects.
if "$(dirname "$0")/saebooks-token-sync" --check >/dev/null 2>&1; then
    log "ok (in sync)"
    exit 0
fi

# Drift detected — log the diff, then auto-heal.
{
    echo "$(ts) DRIFT detected — auto-healing"
    "$(dirname "$0")/saebooks-token-sync" --check 2>&1 || true
} >>"$LOG"

if "$(dirname "$0")/saebooks-token-sync" >>"$LOG" 2>&1; then
    log "healed (sync re-ran)"
    exit 0
fi

# Sync itself failed.
msg="⚠ saebooks-token-sync FAILED to heal drift — see $LOG"
log "ERROR $msg"
if command -v notify-hook >/dev/null 2>&1; then
    notify-hook "$msg" || true
fi
exit 1
