#!/bin/bash
# morning_digest.sh — daily freshness digest for the BIJOTEL federation + chain,
# pushed to Telegram. Complements the service-down + chain-stall watchdogs by
# catching the SILENT-DEATH paths they miss: cross-anchors that stop advancing,
# backups that stop being written, operator submits that stop landing, the rekor
# anchor failing. Those degrade with NO existing alert.
#
# Verifies ARTIFACT FRESHNESS (timestamps / mtimes), NOT "OK" strings in logs —
# a 3-day-old "OK" line would sail through a grep. And it ALWAYS sends (green or
# red): a digest that goes silent on success is indistinguishable from a digest
# that died (the hung-vs-dead lesson). Green = one compact line; red = ⚠ per
# failed check.
#
# GENA-side visibility — NO cross-host SSH (restricted keys unchanged). GENA
# liveness is inferred from the artifacts it PUSHES here: its chain backup
# (gena_chain_*.db.gz, via the inter-host backup key) and its federation
# submission (operators.last_submission_at). GENA's OWN rekor anchor + chain
# stall monitor stay in GENA-local crons and are deliberately NOT covered here.
#
# Usage:
#   morning_digest.sh FED_CONTAINER CHAIN_CONTAINER CHAIN_DB BACKUP_DIR [SECRETS]
# Cron (06:00, federation host):
#   0 6 * * * /opt/bijotel-federation/morning_digest.sh bijotel-federation \
#       <chain-container> <chain-db-in-container> /opt/bijotel-backups /opt/.watchdog_secrets
# DIGEST_DEBUG=1 prints the composed message to stdout (for live-proof). The
# message carries only status data — never secrets.
set -u

FED_C="${1:?need FED_CONTAINER}"
CHAIN_C="${2:?need CHAIN_CONTAINER}"
CHAIN_DB="${3:?need CHAIN_DB}"
BAK="${4:?need BACKUP_DIR}"
SECRETS="${5:-/opt/.watchdog_secrets}"

DOCKER=/usr/bin/docker
LOGDIR=/var/log/bijotel
HEALTH_LOG="$LOGDIR/federation_health.log"
ANCHOR_LOG="$LOGDIR/anchor.log"
REGR_LOG="$LOGDIR/regression.log"
DIGEST_LOG="$LOGDIR/morning_digest.log"
mkdir -p "$LOGDIR"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TODAY=$(date -u +%Y-%m-%d)
CUT24=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)

issues=0
ok_facts=""
fail_lines=""
val() { echo "$1" | sed -n "s/^$2=//p"; }   # extract KEY=VALUE

# ── CHECK 1+2: federation.db — last anchor + per-operator submit freshness ──
FED=$("$DOCKER" exec "$FED_C" python3 -c "
import sqlite3, datetime
cut=(datetime.datetime.utcnow()-datetime.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
c=sqlite3.connect('/data/federation.db')
a=c.execute('select max(anchored_at) from cross_anchors').fetchone()[0] or ''
print('ANCHOR_AT='+a)
print('ANCHOR_FRESH='+('1' if a>cut else '0'))
ops=c.execute('select operator_id,last_submission_at from operators').fetchall()
print('OPS_TOTAL=%d'%len(ops))
print('OPS_FRESH=%d'%sum(1 for _,t in ops if (t or '')>cut))
print('OPS_STALE='+','.join(o for o,t in ops if not((t or '')>cut)))
" 2>/dev/null)

ANCHOR_AT=$(val "$FED" ANCHOR_AT); ANCHOR_FRESH=$(val "$FED" ANCHOR_FRESH)
OPS_TOTAL=$(val "$FED" OPS_TOTAL); OPS_FRESH=$(val "$FED" OPS_FRESH); OPS_STALE=$(val "$FED" OPS_STALE)

if [ "$ANCHOR_FRESH" = "1" ]; then ok_facts="${ok_facts}anchor ${ANCHOR_AT} · "
else issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>anchor</b>: niciun cross-anchor in 24h (ultimul: ${ANCHOR_AT:-none})\n"; fi

if [ -n "$OPS_TOTAL" ] && [ "$OPS_FRESH" = "$OPS_TOTAL" ] && [ "$OPS_TOTAL" -gt 0 ]; then
  ok_facts="${ok_facts}${OPS_FRESH}/${OPS_TOTAL} submits · "
else
  issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>submits</b>: ${OPS_FRESH:-0}/${OPS_TOTAL:-?} operatori in 24h (stale: ${OPS_STALE:-?})\n"
fi

# ── CHECK 3: backups fresh (<24h) — GENA push + federation bundle ──
gena_bak=$(find "$BAK" -maxdepth 1 -name 'gena_chain_*.db.gz' -mmin -1440 2>/dev/null | head -1)
fed_bak=$(find "$BAK" -maxdepth 1 -name 'federation_bundle_*' -mmin -1440 2>/dev/null | head -1)
if [ -n "$gena_bak" ] && [ -n "$fed_bak" ]; then
  enc=$(echo "$fed_bak" | grep -q '\.enc$' && echo "enc" || echo "PLAINTEXT")
  ok_facts="${ok_facts}backups ok(${enc}) · "
else
  issues=$((issues+1))
  miss=""; [ -z "$gena_bak" ] && miss="gena_chain push"; [ -z "$fed_bak" ] && miss="$miss fed_bundle"
  fail_lines="${fail_lines}✗ <b>backups</b>: lipsesc fresh(24h): ${miss}\n"
fi

# ── CHECK 4: ARA chain advancing (<24h) + head ──
CH=$("$DOCKER" exec "$CHAIN_C" python3 -c "
import sqlite3, time
c=sqlite3.connect('$CHAIN_DB')
head=c.execute('select max(seq) from chain').fetchone()[0] or 0
lns=c.execute('select max(timestamp_ns) from chain').fetchone()[0] or 0
age=(time.time()*1e9-lns)/1e9/3600 if lns else 9999
print('HEAD=%d'%head); print('AGE_H=%.1f'%age)
print('CHAIN_FRESH='+('1' if age<24 else '0'))
" 2>/dev/null)
HEAD=$(val "$CH" HEAD); AGE_H=$(val "$CH" AGE_H); CHAIN_FRESH=$(val "$CH" CHAIN_FRESH)
if [ "$CHAIN_FRESH" = "1" ]; then ok_facts="${ok_facts}chain head ${HEAD} (${AGE_H}h) · "
else issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>chain</b>: nicio intrare noua in 24h (head ${HEAD:-?}, ultima ${AGE_H:-?}h)\n"; fi

# ── CHECK 5: ARA rekor anchor today (exit 0) ──
if grep -q "$TODAY" "$ANCHOR_LOG" 2>/dev/null && grep "$TODAY" "$ANCHOR_LOG" 2>/dev/null | grep -q 'exit=0'; then
  ok_facts="${ok_facts}rekor ok · "
else
  issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>rekor</b>: niciun anchor publish exit=0 azi (${TODAY})\n"
fi

# ── CHECK 6: federation health — 0 DOWN in last 24h ──
downs=$(awk -v c="$CUT24" '/DOWN/ && $1 > c {n++} END{print n+0}' "$HEALTH_LOG" 2>/dev/null)
downs=${downs:-0}
if [ "$downs" = "0" ]; then ok_facts="${ok_facts}0 DOWN · "
else issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>fed health</b>: ${downs} eveniment(e) DOWN in 24h\n"; fi

# ── CHECK 7: regression log fresh (<2h) ──
if [ -n "$(find "$REGR_LOG" -mmin -120 2>/dev/null)" ]; then ok_facts="${ok_facts}regression fresh"
else issues=$((issues+1)); fail_lines="${fail_lines}✗ <b>regression</b>: log invechit (peste 2h)\n"; fi

# ── compose ──
if [ "$issues" -eq 0 ]; then
  MSG="🔐 <b>BIJOTEL digest</b> ${NOW}\n✅ <b>7/7 green</b> — ${ok_facts}"
else
  MSG="🔐 <b>BIJOTEL digest</b> ${NOW}\n⚠️ <b>${issues}/7 issue(s)</b>\n${fail_lines}<i>ok:</i> ${ok_facts}"
fi
MSG=$(printf '%b' "$MSG")

# ── send (always) ──
BOT_TOKEN=""; CHAT_ID=""
# shellcheck disable=SC1090
[ -f "$SECRETS" ] && . "$SECRETS"
if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
  RESP=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
           -d chat_id="${CHAT_ID}" -d parse_mode="HTML" --data-urlencode text="$MSG")
  TG=$(echo "$RESP" | grep -o '"ok":[a-z]*' | head -1)
else
  TG='"ok":no-secrets'
fi

echo "$NOW issues=$issues tg=${TG:-unknown}" >> "$DIGEST_LOG"
[ "${DIGEST_DEBUG:-0}" = "1" ] && { echo "----- MSG -----"; echo "$MSG"; echo "----- TG: ${TG} -----"; }
exit 0
