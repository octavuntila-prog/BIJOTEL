#!/bin/bash
# Quick read of chain-stall monitor state. Run on GENA:
#   /opt/substrate-v2/scripts/chain_status.sh
set -u
LOG=/var/log/bijotel/chain_stall.log

echo "=== Recent chain stall checks (last 10) ==="
if [ -s "$LOG" ]; then
  tail -10 "$LOG"
else
  echo "  (no log yet — cron hasn't run)"
fi

echo ""
echo "=== WARNINGs (last 20) ==="
WARNINGS=$(grep "WARNING" "$LOG" 2>/dev/null | tail -20)
if [ -n "$WARNINGS" ]; then
  echo "$WARNINGS"
else
  echo "  (none)"
fi
