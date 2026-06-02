#!/bin/bash
# Daily Rekor (Sigstore) anchor of a BIJOTEL chain head — public, third-party
# witness that "operator attested head Y at time T". Signs with ECDSA P-256
# (bijotel >= 2.13.2; Rekor rejects Ed25519). Idempotent-ish: one entry/run.
#
# Usage: rekor_anchor.sh <container> <db_path_in_container> <ecdsa_key_in_container>
# Cron (daily 03:30):
#   30 3 * * * /opt/.../rekor_anchor.sh <c> <db> <key>
set -u

C="${1:?container}"; DB="${2:?db path}"; KEY="${3:?ecdsa key path}"
LOG=/var/log/bijotel/anchor.log
mkdir -p /var/log/bijotel
TS=$(date -u +%Y%m%dT%H%M%SZ)
ANCHOR_DIR="$(dirname "$DB")/anchors"
OUT="$ANCHOR_DIR/anchor_${TS}.json"

docker exec "$C" mkdir -p "$ANCHOR_DIR" 2>/dev/null
docker exec "$C" bijotel anchor publish \
    --db "$DB" --sign-key "$KEY" --output "$OUT" >> "$LOG" 2>&1
rc=$?
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$C] anchor publish exit=$rc -> $OUT" >> "$LOG"
exit "$rc"
