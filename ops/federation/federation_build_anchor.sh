#!/bin/bash
# federation_build_anchor.sh — trigger one cross-anchor build on the federation.
#
# POSTs /_internal/build-anchor. The service groups the pending submissions
# (one per operator, >= MIN_PARTICIPANTS), computes the cross-anchor hash
# (sha256 of the sorted chain-head signatures + timestamp), signs it with the
# federation Ed25519 key, and anchors that hash in Rekor via ECDSA P-256. It is
# a no-op when too few submissions are pending. Run on the federation host,
# after the operators' submit crons have had time to land.
#
# Cron (daily, 30 min after the submit crons):
#   30 3 * * * /opt/bijotel-federation/federation_build_anchor.sh
set -u

URL="${FED_URL:-http://127.0.0.1:8088}"
LOG=/var/log/bijotel/federation_anchor.log
mkdir -p /var/log/bijotel
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

RESP=$(curl -s --max-time 90 -X POST "$URL/_internal/build-anchor" 2>/dev/null || echo '{"error":"curl failed"}')
echo "$TS $RESP" >> "$LOG"
