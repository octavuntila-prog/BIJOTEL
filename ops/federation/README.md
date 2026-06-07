# bijotel-federation — operations runbook

Deploy + operate the cross-org chain-federation **service** (the reference
implementation in `bijotel-federation/`) and wire two or more BIJOTEL
**operators** to it, so independent chains get periodically **cross-anchored**
and the cross-anchor hash is timestamped in **Sigstore Rekor**.

This is the operator-facing complement to the protocol code. All scripts are
parametrized — no host specifics, IPs, operator IDs, or keys live here.

## Roles

- **Federation host** — runs the `bijotel-federation` container (FastAPI,
  port 8088). Holds the federation's Ed25519 key (signs cross-anchor receipts)
  and an ECDSA P-256 key (Rekor). Bound to `127.0.0.1` only.
- **Operator** — any host running a BIJOTEL chain. Holds its own Ed25519
  operator key, registers once, then periodically submits its signed chain
  head. An operator may be the federation host itself or a remote peer.

## Networking (no internet exposure)

The service binds `127.0.0.1:8088`. A remote operator reaches it over an SSH
tunnel keyed to **permitopen that one port only**:

```
# on the federation host, authorized_keys entry for the peer's tunnel key:
no-agent-forwarding,no-X11-forwarding,no-pty,permitopen="127.0.0.1:8088" ssh-ed25519 AAAA... peer-tunnel
# on the peer, before submit:
ssh -i ~/.ssh/<tunnel_key> -fN -L 18088:127.0.0.1:8088 root@<FEDERATION_IP>
# now http://127.0.0.1:18088 on the peer == the service
```

`restrict,permitopen=...` does NOT work on all OpenSSH builds (restrict
disables forwarding and permitopen did not re-enable it in testing) — use the
explicit `no-*` form above. Verify the restriction: a forward to any other
port must fail with `administratively prohibited`.

## 1. Deploy the service (federation host)

```
mkdir -p /opt/bijotel-federation/{keys,data}
cd /opt/bijotel-federation
bijotel keygen --output-dir keys --type ed25519     # receipt-signing key
bijotel keygen --output-dir keys --type ecdsa       # Rekor key
chown -R 999:999 data           # the image's fed user owns the volume
cp <repo>/ops/federation/{docker-compose.yml,start.sh} .
# build the image natively on the host's arch (do NOT ship an amd64 tar to arm64):
docker build -t bijotel-federation:0.2.0 <path-to>/bijotel-federation
./start.sh
curl -s http://127.0.0.1:8088/status      # -> 200 JSON
```

> The CLI entrypoint hard-exits without the key env vars — `start.sh` exports
> them from `keys/` before `docker compose up`. The data volume must be owned by
> uid 999 or sqlite fails with `unable to open database file`.

## 2. Register operators

```
# each operator, once:
bijotel keygen --output-dir <opdir> --type ed25519
bijotel federation register --service <URL> \
  --public-key <opdir>/bijotel_public.pem --private-key <opdir>/bijotel_private.pem \
  --org "<NAME>" --output <opdir>/registration_receipt.json
# operator_id is deterministic: op_<sha256(pubkey)[:12]> (re-register is idempotent)
curl -s <URL>/status      # operators_total increments
```

Run the CLI from a container that has the bijotel CLI with `--network host` so
`127.0.0.1:<port>` resolves to the local service or the tunnel entrance:

```
docker run --rm --network host -v <opdir>:/op --entrypoint bijotel <image> \
  federation register --service http://127.0.0.1:8088 --public-key /op/... ...
```

## 3. Cross-anchor cycle (cron)

Each operator submits its chain head; the federation host builds the anchor:

| script | host | cron (example, daily) |
|---|---|---|
| `federation_submit.sh`       | each operator     | `0 3 * * *` (peer opens its tunnel first) |
| `federation_build_anchor.sh` | federation host   | `30 3 * * *` (after submits land) |

Verify a cross-anchor (offline, no trust in the service):

```
curl -s <URL>/anchor/<anchor_id> -o receipt.json
bijotel federation verify receipt.json --federation-key <federation_public.pem>
#   -> pubkey_matches_expected / signature_verified / cross_anchor_hash_recomputed : True
```

## 4. Survivability

- **Watchdog** — `federation_health_check.sh` on the federation host
  (`*/5 * * * *`): probes `/status`, relaunches via `start.sh` on outage.
  `restart: unless-stopped` handles crashes + reboots; the watchdog handles the
  recreate-needed case.
- **Backup** — `federation_backup.sh` bundles `federation.db` + all keys.
  Set `FED_BAK_PASS=<passfile>` to AES-256 encrypt (recommended). Keep 3 copies
  (origin + off-host peer + laptop). Keys are static; the db drifts per anchor.
- **Restore test** — decrypt/untar into a scratch dir, confirm a key PEM loads
  and `federation.db` opens + lists operators.
- **Morning digest** — `morning_digest.sh` (`0 6 * * *`): a daily Telegram push
  that checks ARTIFACT FRESHNESS (anchor < 24h, every operator submitted < 24h,
  backups written < 24h, chain advancing, rekor anchor today, 0 DOWN events,
  regression log fresh) — catching the silent-death paths the down/stall
  watchdogs miss. ALWAYS sends (green = one line; red = ⚠ per failed check) so a
  quiet digest can't be mistaken for a dead one. Peer liveness is inferred from
  artifacts the peer pushes here (its chain backup + its federation submission)
  — no cross-host SSH; the peer's own rekor/stall stay in its local crons.

## Security notes

- Keys never enter git, chat, or logs — only fingerprints. This repo ships
  placeholders and parametrized scripts only.
- The service is peer-reachable only (127.0.0.1 + permitopen tunnel), never
  internet-exposed.
- Rekor anchoring is best-effort: a 409 (entry exists) is treated as
  already-anchored; a network failure is non-fatal and the cross-anchor stays
  locally Ed25519-verifiable.
