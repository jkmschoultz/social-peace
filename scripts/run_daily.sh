#!/usr/bin/env bash
# Scheduler entrypoint (Linux/macOS). Renders a review batch, then publishes any
# videos already approved in the web UI. Review happens between the two steps —
# out of band, whenever you open `social-peace review`.
#
# crontab -e  example (render 09:00, publish approved 18:00):
#   0 9  * * *  /path/to/social-peace/scripts/run_daily.sh pipeline         >> /path/to/social-peace/logs/cron.log 2>&1
#   0 18 * * *  /path/to/social-peace/scripts/run_daily.sh publish-approved >> /path/to/social-peace/logs/cron.log 2>&1
# With no argument it does both in sequence.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

STEP="${1:-all}"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] social-peace $STEP  (python: $PY)"

case "$STEP" in
  pipeline)         "$PY" -m social_peace pipeline ;;
  publish-approved) "$PY" -m social_peace publish-approved ;;
  all)
    "$PY" -m social_peace pipeline
    "$PY" -m social_peace publish-approved
    ;;
  *) echo "usage: run_daily.sh [pipeline|publish-approved|all]" >&2; exit 2 ;;
esac
