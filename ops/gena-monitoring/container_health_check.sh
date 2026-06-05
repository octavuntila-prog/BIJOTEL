#!/bin/bash
# Container health watchdog — logs WARNING if any gena-* container is NOT
# running healthy. Companion to chain_stall_check.sh: that one watches the
# CHAIN, this one watches the CONTAINERS.
#
# Born from the 2026-06-03 finding: gena-notifier crash-looped 11,068 times
# SILENTLY because nothing watched container state. The chain stall-monitor
# only sees the chain, not the app containers. This closes that gap.
#
# Channel: append-only log at /var/log/bijotel/container_health.log.
# Future: a Telegram/Discord push can hook the same WARNING branch.
#
# Installed hourly via root crontab. Uses absolute /usr/bin/docker because
# cron runs with a minimal PATH.

set -u

DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/container_health.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p /var/log/bijotel

# Containers intentionally stopped — excluded from alerts.
# gena-notifier-1: abandoned skeleton (no impl in image), stopped 2026-06-03
# by design (restart=no). NOT a failure — don't alert on it.
EXCLUDE_RE='^(gena-notifier-1)\|'

# Any gena-* container in a bad state (excluding the intentional stops).
# Good states: "Up X" and "Up X (healthy)". Bad: Restarting / Exited / Dead /
# Created / "(unhealthy)".
bad=$("$DOCKER" ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null \
  | grep -E '^gena-' \
  | grep -vE "$EXCLUDE_RE" \
  | grep -iE '\|(Restarting|Exited|Dead|Created)|\(unhealthy\)' || true)

if [ -n "$bad" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && echo "$TS WARNING [container] $line" >> "$LOG"
  done <<< "$bad"
else
  total=$("$DOCKER" ps --format '{{.Names}}' 2>/dev/null | grep -cE '^gena-')
  echo "$TS OK [containers] $total gena-* up, 0 in bad state" >> "$LOG"
fi
