# Secret Rotation Playbook

BIJOTEL seals every span with HMAC-SHA256. The secret is the trust
root: anyone who holds it can append valid entries; nobody else can.
Rotating that secret is therefore an operational task you should
expect to do — periodically, and immediately when something feels
wrong.

This page is the procedure. It assumes you've already read the
[Threat Model](../threat-model.md) and understand BIJOTEL's
symmetric-HMAC scope.

---

## When to rotate

- Personnel with secret access leave the team.
- The secret may have been exposed (a leaked log, a misconfigured
  `.env` checked into git, an environment file shared in chat).
- Your compliance policy mandates periodic rotation (e.g. every 90
  days).
- Incident response calls for it.

If none of those apply, you do not need to rotate on a schedule.
BIJOTEL doesn't impose one.

---

## How rotation works

BIJOTEL's chain is append-only. When you change the secret:

1. **New entries** seal under the new secret.
2. **Existing entries** stay sealed under the old secret. They are
   *not* re-signed — re-signing would defeat the purpose of
   tamper-evidence.
3. `bijotel verify` walks the chain and reports the exact `seq` where
   verification under a given secret stops working. That's the
   rotation boundary.

The boundary is the operationally important number. Write it down.

---

## Procedure

### 1. Generate a fresh secret

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Example: a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5061728394a5b6c7d8e9f0
```

64 hex characters = 32 bytes. The verifier rejects anything shorter
than 16 bytes at construction time, so this length is comfortably
above the floor.

### 2. Record the rotation boundary

```bash
bijotel stats --db chain.db
# → reports total entries and the seq of the last entry
# → this seq is your "last entry under the old secret"
```

Write that number into your operations log right now. It's the only
piece of state that distinguishes a clean rotation from a confusing
one six months later.

### 3. Switch the secret

```bash
# Environment variable (preferred):
export BIJOTEL_HMAC_SECRET="<new-64-hex-secret>"

# Or update the .env file the sealing process loads:
#   BIJOTEL_HMAC_SECRET=a1b2c3d4...
```

If you use a secrets manager (Vault, AWS Secrets Manager, GCP Secret
Manager), update the entry there and let your normal sync mechanism
push it out. **Do not** commit the secret to git, and do not write it
to logs.

### 4. Restart the sealing process(es)

```bash
# Docker:
docker compose restart backend

# Or for a multi-container deploy (e.g. GENA):
for c in v3-atelier v4-piata v9-oracle v8-ambasador; do
  docker restart gena-${c}-1
done

# Systemd:
sudo systemctl restart your-app
```

The next LLM call seals under the new secret. There is no warm-up
period; the change is immediate.

### 5. Verify both halves of the chain

Verify the **new half** with the new secret:

```bash
bijotel verify --db chain.db
# → Chain VALID (N entries).
# (where N is the total — but the verifier only checked new entries
#  if you ran it after the rotation; older entries fail under this
#  secret)
```

Verify the **old half** with the old secret in a separate shell:

```bash
BIJOTEL_HMAC_SECRET="<old-secret>" bijotel verify --db chain.db
# → Chain INVALID at seq=N+1   ← this is expected, it's the boundary
# → entries 1..N verify cleanly under the old secret
```

The boundary `seq` returned by the verifier must match the number
you recorded in step 2. If it doesn't, stop and investigate before
proceeding.

### 6. Document the rotation

In your operations log, record:

- Date and time (UTC).
- Boundary `seq` (the last entry sealed under the old secret).
- Reason for rotation (incident, personnel change, scheduled).
- Who performed it.
- Where the old secret is now stored — kept in escrow, or destroyed.

Keep the old secret somewhere recoverable if you may need to verify
historical exports. Destroy it only if your policy explicitly allows
discarding old-half verifiability.

---

## Verifying a chain that has rotated

BIJOTEL today accepts **one** secret at a time. To verify a chain
that has been through one or more rotations, run `verify` once per
segment with the corresponding secret:

```bash
# Segment 1 (entries 1..500, sealed under secret A)
BIJOTEL_HMAC_SECRET="<secret-A>" bijotel verify --db chain.db
# → reports valid up to seq 500, invalid at 501 (boundary)

# Segment 2 (entries 501..6332, sealed under secret B)
BIJOTEL_HMAC_SECRET="<secret-B>" bijotel verify --db chain.db
# → reports invalid at 1 (wrong key for segment 1), valid 501..6332

# Segment 3 (entries 6333+, sealed under secret C)
BIJOTEL_HMAC_SECRET="<secret-C>" bijotel verify --db chain.db
# → reports invalid up to 6332, valid 6333+
```

Each segment is independently verifiable. A future BIJOTEL release
may add a `verify --secrets secretA:0-500,secretB:501-6332,...`
shorthand; until then, the manual sweep above is the procedure.

---

## Exporting across a rotation boundary

`bijotel export` writes the whole chain to a portable JSON file
regardless of how many secrets sealed it. The auditor then needs the
corresponding secret to re-verify each segment:

```bash
# Producer side:
bijotel export --db chain.db -o full_export.json
# → produces a bijotel-chain-v1 file containing every entry

# Auditor side, segment by segment:
bijotel verify-export full_export.json --secret-hex <secret-A>
# → boundary reported at the rotation seq

bijotel verify-export full_export.json --secret-hex <secret-B>
# → next segment verifies, prior segment reported invalid (expected)
```

When handing a rotated chain to an auditor, supply:

- The export file.
- One secret per segment, with the `seq` ranges they correspond to.
- The rotation log entries from step 6 above, so the auditor can
  reconcile boundaries against your records.

Alternatively, export the chain in segments before rotation, each
with its own secret — then each segment is a single-secret object the
auditor can verify in one shot. That's cleaner for compliance
reviews, more work for you.

---

## Security considerations

- **Never log the secret.** Treat it like a database credential.
  Environment variables and secrets managers only.
- **Rotation does not invalidate old entries.** They remain
  verifiable with the old secret. This is intentional — re-signing
  old entries would erase the tamper-evidence property the chain
  exists to provide.
- **An attacker who learns the old secret** can forge entries that
  pass `verify` for the old segment, but cannot affect the segment
  sealed under the new secret. Rotate as soon as exposure is
  suspected to limit the forgeable surface.
- **Verify boundary detection** after every rotation. Don't trust
  that the boundary `seq` is what you think it is — confirm it with
  `bijotel verify` runs against both secrets.

---

## Empirical evidence

This procedure was exercised in BIJOTEL Round 2, Test E3 (Hetzner
CPX52 host, controlled lab chain):

- 50 entries sealed with secret A, 50 with secret B in a single
  `chain.db`.
- `bijotel verify` reported the boundary at exactly `seq=51` under
  each secret (i.e. the verifier flipped at the right entry, in both
  directions).
- Zero false positives on either half — entries 1–50 verified
  cleanly under secret A only, entries 51–100 under secret B only.

The exact same boundary-detection mechanism backs the procedure on
this page. There is no separate "rotation mode" — the verifier
simply tells you where the secret stops working, and you treat that
as the rotation point.

---

## Roadmap

- **Multi-secret verify in one pass.** A `--secrets` argument that
  takes `secret:from-to` triples and verifies the whole chain in one
  invocation. Planned, not yet shipped.
- **Ed25519 signatures on exports.** Asymmetric attestation so an
  auditor never needs the seal-time secret, only the operator's
  public key. Planned (P2 on the roadmap).
- **Automated rotation reminder.** Optional cron that warns when the
  current secret has been in use longer than a configurable
  threshold. Not built; would live in `bijotel ops` if/when added.

If any of these would unblock something concrete for you, file an
issue on
[GitHub](https://github.com/octavuntila-prog/BIJOTEL/issues) with the
use case.
