#!/bin/bash
# export_verify_check.sh — daily L2 (Ed25519 signed export) liveness check.
#
# Promotes the Ed25519-signed-export path from "verified on demand" to
# "continuously exercised": each run exports the chain HEAD signed with the
# host's Ed25519 export key, then `verify-export` checks the asymmetric
# signature AND HMAC continuity (the container carries BIJOTEL_HMAC_SECRET in
# env). Read-only over the chain — never writes the live db. Logs PASS/FAIL so
# a broken export/sign/verify path is caught as a regression, not silently.
#
# Usage:
#   export_verify_check.sh CONTAINER CHAINDB KEYDIR [LAST]
#     CONTAINER  bijotel container holding the chain
#     CHAINDB    chain db path INSIDE the container
#     KEYDIR     dir (inside the container) with bijotel_private.pem +
#                bijotel_public.pem (Ed25519 export key, `bijotel keygen`)
#     LAST       head window to export (default 50)
# Cron (daily): 0 5 * * * /opt/.../export_verify_check.sh <c> <db> <keydir> 50
set -u

C="${1:?need CONTAINER}"; DB="${2:?need CHAINDB}"; KEYDIR="${3:?need KEYDIR}"; LAST="${4:-50}"
DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/export_verify.log
mkdir -p /var/log/bijotel
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

OUT=$("$DOCKER" exec "$C" sh -c "bijotel export --db '$DB' --last $LAST --sign-key '$KEYDIR/bijotel_private.pem' -o /tmp/_l2exp.json >/dev/null 2>&1 && bijotel verify-export /tmp/_l2exp.json --public-key '$KEYDIR/bijotel_public.pem' 2>&1 | tail -2; rm -f /tmp/_l2exp.json" 2>&1 | tr '\n' ' ')
echo "$TS [$C] ${OUT:-no-output}" >> "$LOG"
