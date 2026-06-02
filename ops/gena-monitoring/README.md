# GENA chain-stall monitor — off-host backup + assessment

> Created 2026-06-02 (Part 5 of the post-ARA-restore follow-ups).
> Local backup only; **not** part of the published BIJOTEL OSS package
> (these are host-specific ops scripts — kept out of the repo to keep it clean).

## What these are

The freeze-lesson-#1 monitor that detects a **silently stalled BIJOTEL chain**
(the exact failure that hid ARA's May 31 sealing stop for ~5 days). Three pieces,
all live on **GENA** (`<GENA_HOST>`) host-only:

| File here | Deployed to (GENA) | Purpose |
|---|---|---|
| `chain_stall_check.sh` | `/opt/substrate-v2/scripts/chain_stall_check.sh` | Hourly: WARN if `gena-v3-atelier-1` chain hasn't sealed in 6h |
| `chain_status.sh` | `/opt/substrate-v2/scripts/chain_status.sh` | Manual: read last 10 checks + recent WARNINGs |
| `logrotate-bijotel-chain-stall` | `/etc/logrotate.d/bijotel-chain-stall` | Weekly rotate of `/var/log/bijotel/chain_stall.log` |

**Cron (root):** `15 * * * * /opt/substrate-v2/scripts/chain_stall_check.sh`

## Assessment 2026-06-02 (the Part 5 question: "are these at risk?")

- `/opt/substrate-v2` is **NOT a git repo** — so there's no GENA repo to commit into.
- **No** `deploy.sh` / `rsync --delete` / `git reset --hard` mechanism exists on GENA.
  → Unlike ARA, GENA has **no redeploy that would wipe these scripts**. The ARA
  failure mode (git redeploy erased a host-only integration) **does not apply here.**
- Residual (softer) risk: the scripts are **single-copy on one host, unversioned**.
  If the GENA host is rebuilt or `/opt/substrate-v2` is manually wiped, they're gone.
  → Mitigation: **this off-host backup**. Recoverable now even if the host dies.

**Verdict:** safe from redeploy-wipe; backed up here against host loss. No urgent
action. Optional hardening: commit into a GENA source repo if/when one exists.

## ARA monitoring — now covered a better way

`chain_stall_check.sh` carries a `TODO(freeze-end+): add check_chain "ARA"`.
That is now **satisfied without cross-host access**: after the 2026-06-02 ARA
restore, ARA monitors **its own** chain inside `/opt/watchdog.sh`
(`check_bijotel_stall`, 6h threshold, debounced **Telegram** alert + RECOVERED
notice). So GENA does **not** need to reach across to ARA — each host watches
itself. The GENA TODO can be considered closed (left in the script as history).

## Restore (if GENA loses these)

```bash
scp chain_stall_check.sh chain_status.sh root@<GENA_HOST>:/opt/substrate-v2/scripts/
scp logrotate-bijotel-chain-stall root@<GENA_HOST>:/etc/logrotate.d/bijotel-chain-stall
ssh root@<GENA_HOST> 'chmod +x /opt/substrate-v2/scripts/chain_*.sh; \
  ( crontab -l 2>/dev/null; echo "15 * * * * /opt/substrate-v2/scripts/chain_stall_check.sh" ) | sort -u | crontab -'
```
