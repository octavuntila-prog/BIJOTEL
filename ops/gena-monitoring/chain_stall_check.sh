#!/bin/bash
# Chain stall detector — logs WARNING if a monitored chain stops growing.
#
# Freeze lesson #1 (2026-06-02): ARA stopped sealing on May 31 09:35 and
# nobody knew until a manual check on Day 6. This catches that class of
# silent failure automatically.
#
# Monitors GENA's local chain (inside the v3-atelier container). ARA
# monitoring is a documented follow-up: it needs cross-host access AND
# ARA must be restored first (it isn't sealing at all right now).
#
# Channel: append-only log at /var/log/bijotel/chain_stall.log.
# Future: a Telegram/Discord push can hook the same WARNING branch.
#
# Installed hourly via root crontab. Uses absolute /usr/bin/docker because
# cron runs with a minimal PATH.

set -u

DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/chain_stall.log
THRESHOLD_SEC=21600   # 6 hours — GENA seals ~20/h, so a 6h gap is a real stall
NOW=$(date -u +%s)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p /var/log/bijotel

check_chain() {
  local name="$1"
  local container="$2"
  local db_path="$3"

  # Last entry timestamp in unix seconds (0 / empty on any failure).
  local last_ts
  last_ts=$("$DOCKER" exec "$container" python3 -c "import sqlite3; r=sqlite3.connect('$db_path').execute('SELECT timestamp_ns FROM chain ORDER BY seq DESC LIMIT 1').fetchone(); print(int(r[0]/1e9) if r else 0)" 2>/dev/null)

  if [ -z "$last_ts" ] || [ "$last_ts" = "0" ]; then
    echo "$TIMESTAMP WARNING [$name] cannot read chain or empty (container down / not sealing?)" >> "$LOG"
    return
  fi

  local age=$((NOW - last_ts))
  local age_h=$((age / 3600))

  if [ "$age" -gt "$THRESHOLD_SEC" ]; then
    echo "$TIMESTAMP WARNING [$name] STALLED — last entry ${age_h}h ago (threshold $((THRESHOLD_SEC / 3600))h)" >> "$LOG"
  else
    echo "$TIMESTAMP OK [$name] last entry ${age_h}h ago" >> "$LOG"
  fi
}

# --- Monitored chains ---
check_chain "GENA" "gena-v3-atelier-1" "/data/bijotel_chain.db"

# ARA monitoring: deferred until ARA BIJOTEL restore (see
# ARA_BIJOTEL_RESTORE_PLAN.md). Needs an SSH key GENA->ARA or an ARA
# heartbeat to a GENA-readable location, AND ARA must be sealing again.
# TODO(freeze-end+): add `check_chain "ARA" ...` once ARA is restored.
