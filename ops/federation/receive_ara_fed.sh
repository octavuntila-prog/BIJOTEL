#!/bin/bash
# receive_ara_fed.sh — peer-side receiver for the off-host federation bundle
# (audit DR-3). Pinned as the forced command on a restricted ssh key in the
# peer's authorized_keys, so the pushing host cannot run anything else: it just
# streams the (already AES-256-encrypted) bundle to a timestamped file and
# prunes old copies. Mirrors the chain-backup receiver pattern.
umask 077
DIR=/opt/bijotel-backups
mkdir -p "$DIR"
cat > "$DIR/ara_fed_$(date -u +%Y%m%dT%H%M%SZ).tar.gz.enc"
# Keep only the newest 7 off-host federation bundles.
ls -1t "$DIR"/ara_fed_*.tar.gz.enc 2>/dev/null | tail -n +8 | xargs -r rm -f
