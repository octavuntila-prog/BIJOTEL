#!/bin/bash
# chain_backup_push.sh — BIJOTEL off-host chain backup (inter-host cron).
#
# Snapshots the local bijotel chain with a CONSISTENT sqlite .backup (not cp —
# WAL-safe), pushes the gzip to the PEER host through a RESTRICTED ssh key, then
# prunes the peer's backups held locally to the last N.
#
# The restricted key (~/.ssh/bijotel_bak) is locked on the peer's side by:
#   restrict,from="<this-host-ip>",command="/opt/bijotel-backups/receive_<self>.sh"
# so it can ONLY deliver one backup file into /opt/bijotel-backups — nothing
# else (no shell, no pty, no forwarding, only from this host's IP).
#
# Born 2026-06-05: closes the "chain backup single-host" gap. Each host pushes
# its own chain to the other; each holds the other's last N snapshots off-host.
# So host loss never loses the chain — and combined with the encrypted
# <self>_secrets.enc bundle (HMAC secret + keys), a restored chain is verifiable.
#
# Usage:
#   chain_backup_push.sh SELF PEER_SSH CONTAINER DBPATH PEER_LABEL [KEEP]
#     SELF        my chain label (gena|ara) — for log lines + which receiver runs
#     PEER_SSH    ssh target of peer (root@IP)
#     CONTAINER   my bijotel container name
#     DBPATH      chain db path INSIDE my container
#     PEER_LABEL  label of peer backups I hold locally, to prune (gena|ara)
#     KEEP        retention count (default 7)
#
# Installed via root crontab on each host (staggered). Absolute /usr/bin/docker
# because cron runs with a minimal PATH.
set -u

SELF="${1:?need SELF}"; PEER="${2:?need PEER_SSH}"; CONTAINER="${3:?need CONTAINER}"
DBPATH="${4:?need DBPATH}"; PEER_LABEL="${5:?need PEER_LABEL}"; KEEP="${6:-7}"

DOCKER=/usr/bin/docker
KEY="$HOME/.ssh/bijotel_bak"
LOG=/var/log/bijotel/chain_backup.log
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mkdir -p /var/log/bijotel /opt/bijotel-backups
log() { echo "$TS $1" >> "$LOG"; }

# 1. consistent snapshot inside the container, copy out, gzip
if ! "$DOCKER" exec "$CONTAINER" python3 -c \
  "import sqlite3;s=sqlite3.connect('$DBPATH');d=sqlite3.connect('/tmp/_bak.db');s.backup(d);d.close();s.close()" 2>/dev/null; then
  log "ERROR [$SELF] snapshot failed (container=$CONTAINER db=$DBPATH)"; exit 1
fi
"$DOCKER" cp "$CONTAINER":/tmp/_bak.db /tmp/_bak.db >/dev/null 2>&1
"$DOCKER" exec "$CONTAINER" rm -f /tmp/_bak.db 2>/dev/null
gzip -f /tmp/_bak.db
SZ=$(stat -c%s /tmp/_bak.db.gz 2>/dev/null || echo 0)

# 2. push to peer via restricted key (peer forced-command names + stores it)
if ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20 \
     "$PEER" < /tmp/_bak.db.gz; then
  log "OK [$SELF] pushed ${SZ}B to peer"
else
  log "ERROR [$SELF] push to peer failed"; rm -f /tmp/_bak.db.gz; exit 1
fi
rm -f /tmp/_bak.db.gz

# 3. prune peer backups held locally to last KEEP
pruned=$(ls -1t /opt/bijotel-backups/${PEER_LABEL}_chain_*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [ -n "$pruned" ]; then
  echo "$pruned" | xargs -r rm -f
  n=$(echo "$pruned" | grep -c .)
  log "OK [$SELF] pruned $n old ${PEER_LABEL} backup(s), kept $KEEP"
fi
exit 0
