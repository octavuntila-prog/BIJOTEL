#!/bin/bash
# federation_offhost_push.sh — push the latest ENCRYPTED federation bundle
# off-host to the peer via a restricted ssh key (audit DR-3).
#
# The federation backup (federation_backup.sh) writes AES-256-encrypted bundles
# locally; without an off-host copy they die with the host. This ships the
# newest bundle to the peer, which stores it opaquely under a command-locked
# key (receive_ara_fed.sh). The encryption passphrase is deliberately NOT
# shipped off-host — keep it with the operator; the peer copy is useless without
# it (that is the point of encrypting before it leaves the host).
#
# Usage:  federation_offhost_push.sh BAKDIR PEER_SSH [KEY]
#   BAKDIR    dir holding federation_bundle_*.tar.gz.enc (e.g. /opt/bijotel-backups)
#   PEER_SSH  ssh target of the peer (root@IP)
#   KEY       restricted push key (default ~/.ssh/bijotel_fedbak)
# Cron (daily, after the 05:00 federation_backup.sh):
#   15 5 * * * /opt/.../federation_offhost_push.sh /opt/bijotel-backups root@<peer>
set -u

BAKDIR="${1:?need BAKDIR}"
PEER="${2:?need PEER_SSH (root@IP)}"
KEY="${3:-$HOME/.ssh/bijotel_fedbak}"
LOG=/var/log/bijotel/federation_offhost.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p /var/log/bijotel

latest=$(ls -1t "$BAKDIR"/federation_bundle_*.tar.gz.enc 2>/dev/null | head -1)
if [ -z "$latest" ]; then
  echo "$TS ERROR no encrypted federation bundle in $BAKDIR" >> "$LOG"
  exit 1
fi

if ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
       -o ConnectTimeout=20 "$PEER" < "$latest"; then
  echo "$TS OK pushed $(basename "$latest") ($(stat -c %s "$latest") B) off-host" >> "$LOG"
else
  echo "$TS ERROR push failed: $(basename "$latest")" >> "$LOG"
  exit 1
fi
