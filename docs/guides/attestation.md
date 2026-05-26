# Attestation

**Available from v2.10.0.**

BIJOTEL's previous three trust layers — HMAC chain, Ed25519 signature,
Rekor anchor — all answer questions about the *output* (chain rows
weren't altered, were signed by you, existed at time T). They say
nothing about the *software that produced the output*. A host running
a compromised `bijotel` binary can mint a chain that passes every one
of those checks.

Attestation closes that gap. The day someone deploys on a host with
TPM 2.0, AWS Nitro, GCP Confidential VM, or Azure SGX, an attestation
quote binds the chain artefact to a hardware-rooted measurement of
exactly what code produced it.

## The four-layer trust hierarchy

| Layer | Answers | Trust root |
|---|---|---|
| HMAC chain | "entries weren't tampered post-seal" | operator's HMAC secret |
| Ed25519 | "signed by this specific key" | operator's Ed25519 key |
| Rekor (v2.9) | "existed at time T, publicly witnessed" | Sigstore log + operator key |
| **Attestation (v2.10)** | **"produced by trusted code on verified hardware"** | **TPM / Nitro / SEV-SNP / SGX or software-key** |

## What ships in v2.10.0

| Backend | Hardware | Status |
|---|---|---|
| `software` | none — Ed25519 key on disk | ✅ ships |
| `tpm2` | TPM 2.0 chip | ⚠ stub (raises with upgrade path) |
| `nitro` | AWS Nitro Enclave | ⚠ stub |
| `gcp` | AMD SEV-SNP on GCP Confidential VM | ⚠ stub |
| `sgx` | Intel SGX on Azure DCsv3 | ⚠ stub |

Stubs aren't dead code. They lock the `AttestationBackend` protocol so
hardware backends slot in without ABI churn — the day someone deploys
on a host with a real TPM, swapping `--attest software` for
`--attest tpm2` is the only change. Same flag, same sidecar shape,
same verify path.

## What the software backend actually proves

**It does prove:**

- The BIJOTEL package files hashed to a specific SHA-256 at the moment
  the quote was produced (catches between-install-and-run tampering of
  the `.py` files).
- The platform was as described — OS, arch, Python version, hostname,
  bijotel version.
- A specific Ed25519 key signed the canonical payload (operator
  identity).

**It does NOT prove (these need real TEE backends):**

- The CPU running the code was untampered.
- Memory wasn't being read by a malicious hypervisor.
- The Ed25519 private key was generated in a secure enclave (the disk
  PEM is the trust anchor; if the operator's machine is compromised,
  so is the key).

The `backend` field in every quote is literally `"software-key"` so
the label is honest in the metadata, not just in the docs. Same
discipline as substrate-guard L5.

## CLI

The flag attaches to `bijotel archive`:

```bash
bijotel archive \
  --db chain.db \
  --output archive-2026-05.db \
  --before 2026-05-20 \
  --sign-key keys/private.pem \
  --attest software
```

Output:

```
Archived 4321 entries to archive-2026-05.db
  Window:        seq 1 → 4321
  first prev:    0000000000000000...
  last hmac:     a3f9c8d2e7b1...
  Source DB remaining: 2100 entries
  Signed JSON sidecar: archive-2026-05.db.signed.json
  Attestation sidecar: archive-2026-05.db.attestation.json  (backend=software)
```

The `.attestation.json` sidecar is the operator's audit artefact:

```json
{
  "backend": "software-key",
  "quote_b64": "MEUCIQDx...",
  "code_measurement": "f12a98e7...",
  "platform_info": {
    "os": "Linux 6.8",
    "arch": "x86_64",
    "python": "3.12.10",
    "hostname": "gena-prod-01",
    "bijotel_version": "2.10.0"
  },
  "timestamp": "2026-05-26T14:30:15+00:00",
  "data_hash": "a3f9c8d2...",
  "verified": true
}
```

## Verification (Python API for v2.10; CLI in v2.11)

```python
from bijotel.attestation import SoftwareAttestation, AttestationQuote
import json
from pathlib import Path

# Operator's public key, distributed out-of-band (same key as bijotel keygen).
public_pem = Path("operator-public.pem").read_bytes()

# Load the sidecar that travels with the archive.
sidecar = json.loads(Path("archive-2026-05.db.attestation.json").read_text())
quote = AttestationQuote(**sidecar)

# The "data" attested is the archive's last hmac_hash, as bytes.
import sqlite3
with sqlite3.connect("archive-2026-05.db") as conn:
    last_hmac = conn.execute(
        "SELECT hmac_hash FROM chain ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0]
data = bytes.fromhex(last_hmac)

# Verifier (private key not needed — verifier never signs).
# Pass any Ed25519 *private* key just to instantiate; the public key
# is what actually validates.
verifier = SoftwareAttestation(
    private_key_pem=b"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
    public_key_pem=public_pem,
)
assert verifier.verify(quote, data)  # → True if everything lines up
```

(The instantiation pattern is a v2.10 wart — the verifier needs a
private PEM to construct even though it only uses the public key.
v2.11 will add a dedicated `verify_quote(...)` free function that
takes the public key directly.)

## What a verifier should check

A complete attestation verify covers three things:

1. **`data_hash` matches** the SHA-256 of whatever bytes the operator
   says were anchored (typically the archive's last `hmac_hash`).
2. **`code_measurement`** matches a known-good measurement. The
   operator publishes their bijotel install's measurement once; every
   subsequent quote either matches it or has drifted (the package was
   upgraded, downgraded, or tampered with — all of which want triage).
3. **`platform_info`** is what was expected (host name, OS, bijotel
   version). A quote produced on a different host than claimed is
   suspicious.
4. **Ed25519 signature verifies** against the operator's published
   public key. This is the operator-identity check.

## Upgrade path to hardware-rooted attestation

The day BIJOTEL runs on a host with TPM 2.0, AWS Nitro, GCP CVM, or
SGX:

1. Install the matching extra (planned for v2.11):
   ```bash
   pip install bijotel[tpm2]    # or [nitro], [gcp], [sgx]
   ```
2. Swap the CLI flag:
   ```bash
   bijotel archive ... --attest tpm2   # instead of --attest software
   ```
3. The sidecar shape is identical; `backend` becomes `"tpm2"` (or
   `"nitro"`, `"gcp-snp"`, `"sgx"`) and `quote_b64` carries the
   hardware quote bytes (TPM2_Quote structure, NSM document, etc.).
4. Old archives produced with `--attest software` keep verifying with
   the software backend; new ones produced with `--attest tpm2` need
   the TPM-aware verifier. Both can coexist on the same chain.

## Public API

```python
from bijotel.attestation import (
    AttestationBackend,         # Protocol all backends implement
    AttestationQuote,           # frozen dataclass with .to_dict()/.to_json()
    SoftwareAttestation,        # works today
    TPM2Attestation,            # stub (NotImplementedError)
    AWSNitroAttestation,        # stub
    GCPConfidentialAttestation, # stub
    AzureSGXAttestation,        # stub
)
```

Each (except the stubs) is also re-exported from `bijotel` directly.

## Why ship software-only first

Three reasons covered in `docs/design/tee-anchored-chains.md` §10:

1. **Honest baseline.** Operators without TEE hardware still get
   code-measurement + platform-info + Ed25519-attestation today.
   That's meaningfully better than "we trust the host blindly".
2. **Interface lock-in.** Hardware backends slot into the same
   protocol without ABI churn.
3. **Documentation discoverability.** The stubs ship with explicit
   upgrade paths; operators see the future the moment they hit
   `--help`.

The cost is that BIJOTEL has to be honest that software is software,
not hardware. The `backend="software-key"` label, the CHANGELOG's
honest-scope section, and this page all do that. No magic claims.
