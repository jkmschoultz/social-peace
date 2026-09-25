#!/usr/bin/env bash
# Scheduler entrypoint (Linux/macOS).
#   pipeline     render enough new renders to keep the posting queue stocked (evening, after the last slot)
#   publish-due  post the queue head if a schedule.slots time has passed (every ~5 min)
#   prune        drop old rejected renders
# Review happens out of band, whenever you open `social-peace review`. The slot
# times live in config/config.yaml, not here — publish-due just runs often.
#
# systemd timers: scripts/systemd/ (preferred — Persistent= catches up after sleep).
# crontab -e  equivalent:
#   */5 * * * *  /path/to/social-peace/scripts/run_daily.sh publish-due >> /path/to/social-peace/logs/cron.log 2>&1
#   5 19 * * *   /path/to/social-peace/scripts/run_daily.sh pipeline    >> /path/to/social-peace/logs/cron.log 2>&1
# With no argument it runs pipeline + publish-due + prune in sequence.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

STEP="${1:-all}"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] social-peace $STEP  (python: $PY)"

case "$STEP" in
  pipeline)         "$PY" -m social_peace pipeline ;;
  publish-due)      "$PY" -m social_peace publish-due ;;
  publish-approved) "$PY" -m social_peace publish-approved ;;
  prune)            "$PY" -m social_peace prune ;;
  all)
    "$PY" -m social_peace pipeline
    "$PY" -m social_peace publish-due
    "$PY" -m social_peace prune          # drop rejected renders past retention.rejected_days
    ;;
  *) echo "usage: run_daily.sh [pipeline|publish-due|publish-approved|prune|all]" >&2; exit 2 ;;
esac
