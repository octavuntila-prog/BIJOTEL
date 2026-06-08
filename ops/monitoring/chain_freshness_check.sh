#!/bin/bash
# chain_freshness_check.sh — TIGHT chain-head freshness alarm (audit ISSUE-6).
#
# The 6h stall check (chain_stall_check.sh) and the daily morning digest cannot
# catch the 2026-06-05 incident class: sealing froze for ~27 min while spans
# were being generated, and ~10-11 entries were lost before a manual check
# noticed. This runs every 15 min with a tight (default 45 min) threshold so a
# sealing freeze on an ACTIVE chain is flagged within the hour, not after 6h.
#
# Alerting: writes an always-on log line, and — if Telegram credentials are
# present at /opt/.watchdog_secrets (BOT_TOKEN + CHAT_ID, same file the ARA
# morning digest uses) — pushes an alert, debounced to at most one per hour
# while the freeze persists. With no creds file it is log-only (drop the file
# in later to enable push, no code change).
#
# Use on an ACTIVE chain only (GENA seals ~14/h). A slow chain (e.g. ARA)
# would false-alarm at 45 min — keep the 6h stall check there instead.
#
# Usage:  chain_freshness_check.sh CONTAINER CHAINDB [THRESHOLD_SEC]
# Cron:   */15 * * * * /opt/.../chain_freshness_check.sh gena-v3-atelier-1 \
#                       /data/bijotel_chain.db 2700
set -u

C="${1:?need CONTAINER}"; DB="${2:?need CHAINDB}"; THRESHOLD_SEC="${3:-2700}"
DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/chain_freshness.log
STATE=/var/log/bijotel/.chain_freshness_alerted
SECRETS=/opt/.watchdog_secrets
NOW=$(date -u +%s)
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p /var/log/bijotel

last_ts=$("$DOCKER" exec "$C" python3 -c "import sqlite3; r=sqlite3.connect('$DB').execute('SELECT timestamp_ns FROM chain ORDER BY seq DESC LIMIT 1').fetchone(); print(int(r[0]/1e9) if r else 0)" 2>/dev/null)

msg=""
if [ -z "$last_ts" ] || [ "$last_ts" = "0" ]; then
  msg="WARNING [$C] chain unreadable/empty (container down or not sealing?)"
elif [ "$((NOW - last_ts))" -gt "$THRESHOLD_SEC" ]; then
  msg="WARNING [$C] sealing FROZEN — head $(((NOW - last_ts) / 60))min old (threshold $((THRESHOLD_SEC / 60))min)"
fi

if [ -z "$msg" ]; then
  echo "$TS OK [$C] head $(((NOW - last_ts) / 60))min old" >> "$LOG"
  rm -f "$STATE"   # recovered → reset the debounce
  exit 0
fi

echo "$TS $msg" >> "$LOG"
# Debounce: at most one push per hour while frozen.
last_alert=$(stat -c %Y "$STATE" 2>/dev/null || echo 0)
if [ "$((NOW - last_alert))" -gt 3600 ]; then
  if [ -r "$SECRETS" ]; then
    # shellcheck disable=SC1090
    . "$SECRETS"
    curl -s --max-time 10 \
      "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      -d chat_id="${CHAT_ID}" \
      -d text="🔴 BIJOTEL freshness alarm — $msg" >/dev/null 2>&1
  fi
  touch "$STATE"
fi
