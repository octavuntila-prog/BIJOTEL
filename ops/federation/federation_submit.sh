#!/bin/bash
# federation_submit.sh — one operator's periodic chain-head submission.
#
# Exports the chain HEAD (--last N) from inside the operator's bijotel
# container, then `bijotel federation submit` (Ed25519 bearer auth) to the
# federation service. Run on EACH operator host via cron. Once
# >= MIN_PARTICIPANTS submissions are pending, federation_build_anchor.sh (on
# the federation host) groups them into a Rekor-anchored cross-anchor.
#
# The submit one-shot runs with `--network host` so http://127.0.0.1:<port>
# resolves to the federation port on this host — either the local service
# (federation host) or the SSH-tunnel entrance (peer host; open the tunnel
# before calling this).
#
# Usage:
#   federation_submit.sh CONTAINER CHAINDB IMAGE OPERATOR_ID OPKEY_DIR SERVICE_URL [LAST]
#     CONTAINER    bijotel container holding the chain (e.g. <app>-backend-1)
#     CHAINDB      chain db path INSIDE that container
#     IMAGE        image carrying the bijotel CLI (used for the submit one-shot)
#     OPERATOR_ID  this operator's id (from `bijotel federation register`)
#     OPKEY_DIR    host dir with this operator's bijotel_private.pem
#     SERVICE_URL  federation base url reachable here (e.g. http://127.0.0.1:8088)
#     LAST         head window in entries (default 10)
#
# Cron example (federation host operator):
#   0 3 * * * /opt/.../federation_submit.sh <app>-backend-1 /app/data/bijotel_chain.db \
#             <image> op_xxxxxxxxxxxx /opt/.../operator http://127.0.0.1:8088 10
set -u

C="${1:?need CONTAINER}"; DB="${2:?need CHAINDB}"; IMG="${3:?need IMAGE}"
OPID="${4:?need OPERATOR_ID}"; OPDIR="${5:?need OPKEY_DIR}"; URL="${6:?need SERVICE_URL}"
LAST="${7:-10}"

DOCKER=/usr/bin/docker
LOG=/var/log/bijotel/federation_submit.log
mkdir -p /var/log/bijotel
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# 1. export the chain HEAD inside the live container (has the HMAC secret + db),
# Ed25519-SIGNED with the operator's registered key so the federation can
# cryptographically verify authenticity before witnessing it (audit ISSUE-1).
# The key is copied into the container only for the export, then removed.
"$DOCKER" cp "$OPDIR/bijotel_private.pem" "$C":/tmp/_opkey.pem >/dev/null 2>&1
if ! "$DOCKER" exec "$C" bijotel export --db "$DB" --last "$LAST" \
       --sign-key /tmp/_opkey.pem -o /tmp/_fedexp.json >/dev/null 2>&1; then
  "$DOCKER" exec "$C" rm -f /tmp/_opkey.pem /tmp/_fedexp.json 2>/dev/null
  echo "$TS [$OPID] ERROR signed export failed ($C:$DB)" >> "$LOG"; exit 1
fi
"$DOCKER" cp "$C":/tmp/_fedexp.json "$OPDIR/_fedexp.json" >/dev/null 2>&1
"$DOCKER" exec "$C" rm -f /tmp/_fedexp.json /tmp/_opkey.pem 2>/dev/null
chmod 644 "$OPDIR/_fedexp.json" 2>/dev/null

# 2. submit (client signs the per-request Ed25519 bearer token)
OUT=$("$DOCKER" run --rm --network host -v "$OPDIR":/op --entrypoint bijotel "$IMG" \
        federation submit --service "$URL" --operator-id "$OPID" \
        --private-key /op/bijotel_private.pem --export /op/_fedexp.json 2>&1 \
      | grep -E "submission_id|verified|ERROR" | tr '\n' ' ')
rm -f "$OPDIR/_fedexp.json"
echo "$TS [$OPID] ${OUT:-no-output}" >> "$LOG"
