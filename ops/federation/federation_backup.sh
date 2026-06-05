#!/bin/bash
# federation_backup.sh — backup the federation crown jewels.
#
# Bundles into one tar.gz:
#   * federation.db  — consistent sqlite .backup (operators + cross-anchors)
#   * keys/          — service Ed25519 (receipt signing) + ECDSA (Rekor)
#   * operator/      — this host's operator Ed25519 key
#
# If FED_BAK_PASS points at a passphrase file, the tar is AES-256 encrypted
# (openssl enc -aes-256-cbc -pbkdf2) — same model as the <host>_secrets.enc
# bundle. WITHOUT it the tar is left plaintext (chmod 600); fold it into your
# encrypted secrets bundle manually, or set FED_BAK_PASS. Keeps the last 7
# bundles locally and (optionally) scp's the newest off-host.
#
# The keys are STATIC (only change on rotation); federation.db drifts with each
# cross-anchor — so a daily/weekly local cron + an off-host copy after key
# changes is enough. 3 copies = origin host + off-host peer + operator laptop.
#
# Usage:
#   federation_backup.sh SERVICE_DIR OUT_DIR [OFFHOST_SCP_TARGET]
#     SERVICE_DIR  /opt/bijotel-federation (data/federation.db, keys/, operator/)
#     OUT_DIR      local bundle dir (e.g. /opt/bijotel-backups)
#     OFFHOST...   optional scp target dir, e.g. root@<PEER_IP>:/opt/bijotel-backups
#   Encryption (recommended): FED_BAK_PASS=/path/to/passfile federation_backup.sh ...
set -u

SD="${1:?need SERVICE_DIR}"; OUT="${2:?need OUT_DIR}"; OFFHOST="${3:-}"
LOG=/var/log/bijotel/federation_backup.log
mkdir -p "$OUT" /var/log/bijotel
TS=$(date -u +"%Y%m%dT%H%M%SZ")
WORK=$(mktemp -d)

# consistent db snapshot (WAL-safe)
python3 - "$SD/data/federation.db" "$WORK/federation.db" <<'PY' 2>/dev/null
import sqlite3, sys
s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2])
s.backup(d); d.close(); s.close()
PY

mkdir -p "$WORK/keys" "$WORK/operator"
cp "$SD"/keys/*.pem        "$WORK/keys/"     2>/dev/null
cp "$SD"/operator/bijotel_*.pem "$WORK/operator/" 2>/dev/null

TAR="$OUT/federation_bundle_${TS}.tar.gz"
tar -czf "$TAR" -C "$WORK" . 2>/dev/null
chmod 600 "$TAR"
rm -rf "$WORK"

FINAL="$TAR"
if [ -n "${FED_BAK_PASS:-}" ] && [ -f "$FED_BAK_PASS" ]; then
  if openssl enc -aes-256-cbc -pbkdf2 -salt -in "$TAR" -out "$TAR.enc" -pass file:"$FED_BAK_PASS" 2>/dev/null; then
    rm -f "$TAR"; FINAL="$TAR.enc"; chmod 600 "$FINAL"
  fi
fi

SZ=$(stat -c%s "$FINAL" 2>/dev/null || echo 0)
ENC=$([ "${FINAL##*.}" = "enc" ] && echo "encrypted" || echo "PLAINTEXT(600)")
echo "$(date -u +%FT%TZ) wrote $FINAL ($SZ B, $ENC)" >> "$LOG"

if [ -n "$OFFHOST" ]; then
  if scp -o BatchMode=yes -o ConnectTimeout=20 "$FINAL" "$OFFHOST/" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) pushed off-host: $OFFHOST" >> "$LOG"
  else
    echo "$(date -u +%FT%TZ) ERROR off-host push failed: $OFFHOST" >> "$LOG"
  fi
fi

# prune local to last 7
ls -1t "$OUT"/federation_bundle_*.tar.gz* 2>/dev/null | tail -n +8 | xargs -r rm -f
exit 0
