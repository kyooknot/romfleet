#!/bin/bash
# PS2 nightly re-hash wrapper. Launched by cron at 03:00 UTC (23:00 EDT).
# - flock: never double-run if a prior night is somehow still going.
# - nice/ionice: keep CPU + pool I/O gentle so any active users aren't starved.
# - the python runner self-stops at 06:00 EDT; the 10:00-UTC safety-kill cron is a backstop.
LOCK=/tmp/ps2_nightly.lock
LOG=/tmp/ps2_full.log
exec 9>"$LOCK"
if ! flock -n 9; then echo "[$(date -u)] already running, skip" >>"$LOG"; exit 0; fi
cd /opt/romfleet/backend || exit 1
set -a; . /opt/romfleet/backend/.env; set +a
export PYTHONPATH=/opt/romfleet/backend
echo "[$(date -u)] ---- wrapper start ----" >>"$LOG"
nice -n 15 ionice -c2 -n7 /opt/romfleet/venv/bin/python /opt/romfleet/scripts/ps2_nightly.py ps2 >>"$LOG" 2>&1
echo "[$(date -u)] ---- wrapper end ----" >>"$LOG"
