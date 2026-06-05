#!/bin/bash
# chain_regression_check.sh — hourly F12 regression detection over a BIJOTEL chain.
#
# Runs `bijotel regression` against a chain.db inside a container and appends the
# report (baseline stats + drift verdict per dimension) to a log. Mirrors GENA's
# substrate-side regression cron, parametrized so the same script serves any host
# (GENA, ARA, …). Read-only over the chain; never blocks the host.
#
# Usage:
#   chain_regression_check.sh CONTAINER DBPATH [WINDOW]
#     CONTAINER  bijotel container name (e.g. ai-research-agency-backend-1)
#     DBPATH     chain db path INSIDE the container (e.g. /app/data/bijotel_chain.db)
#     WINDOW     rolling baseline window in spans (default 100)
#
# Cron (hourly): 0 * * * * /opt/chain_regression_check.sh <container> <db> 100
# Absolute /usr/bin/docker because cron runs with a minimal PATH.
set -u

CONTAINER="${1:?need CONTAINER}"
DB="${2:?need DBPATH}"
WINDOW="${3:-100}"

DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/regression.log
mkdir -p /var/log/bijotel

{
  echo "=== $(date -u +"%Y-%m-%dT%H:%M:%SZ") [$CONTAINER] window=$WINDOW ==="
  "$DOCKER" exec "$CONTAINER" bijotel regression --db "$DB" --window "$WINDOW"
} >> "$LOG" 2>&1
