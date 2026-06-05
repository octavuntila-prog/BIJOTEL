#!/bin/bash
# federation_health_check.sh — watchdog for the bijotel-federation service.
#
# Probes GET /status. If it does not return HTTP 200, relaunches the service via
# start.sh (which re-exports the key PEMs and runs `docker compose up -d`). This
# covers crash-loop, a removed container, and the host-reboot-needs-recreate
# case. Read-only when healthy; only acts on an actual outage.
#
# Cron (every 5 min):
#   */5 * * * * /opt/bijotel-federation/federation_health_check.sh
# Absolute /usr/bin paths because cron runs with a minimal PATH.
set -u

FED_DIR="${FED_DIR:-/opt/bijotel-federation}"
URL="${FED_URL:-http://127.0.0.1:8088/status}"
LOG=/var/log/bijotel/federation_health.log
mkdir -p /var/log/bijotel
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$URL" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  exit 0
fi

echo "$TS DOWN (http=$code) — relaunching via start.sh" >> "$LOG"
cd "$FED_DIR" || { echo "$TS ERROR: $FED_DIR missing" >> "$LOG"; exit 1; }
./start.sh >> "$LOG" 2>&1
sleep 10
code2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$URL" 2>/dev/null || echo 000)
echo "$TS post-relaunch http=$code2" >> "$LOG"
[ "$code2" = "200" ]
