# TEE-Anchored Chain Roots — Design

**Status:** Design ratified by v2.10.0. Software backend ships;
hardware backends (TPM2 / Nitro / GCP / SGX) are stubs that raise
`NotImplementedError` with explicit upgrade instructions until the
hosts that run BIJOTEL have the relevant hardware.

**Last revised:** 2026-05-26.

---

## 1. Problem

BIJOTEL's HMAC chain proves entries weren't altered **after** they
were sealed. Ed25519-signed exports prove a specific key signed the
chain. Rekor anchoring proves the chain existed at a specific time,
witnessed by a public log.

All three layers say nothing about whether the **sealing software
itself** was trustworthy at write time. A compromised host running a
patched `bijotel` binary can produce a chain that passes `verify`,
`verify-export`, and even `anchor verify` — because the host owns the
HMAC secret and the Ed25519 key.

The gap closes only when an external root of trust attests that the
specific code that produced the chain was running on hardware in a
known-good state.

Analogy: HMAC says *the film wasn't edited*; Ed25519 says *I signed
it*; Rekor says *it existed at time T*; **TEE says the camera itself
was genuine, not a replica.**

## 2. Trust hierarchy (after v2.10)

| Layer | What it proves | Trust root | When it shipped |
|---|---|---|---|
| HMAC chain | entries not tampered post-seal | operator's HMAC secret | v1.0 |
| Ed25519 | signed by a specific key | operator's Ed25519 key | v2.1.0 |
| Rekor | existed at time T (publicly witnessed) | Sigstore Rekor log | v2.9.0 |
| **TEE** | **produced by trusted code on verified hardware** | **hardware-rooted key (TPM/Nitro/etc.)** | **v2.10.0** |

The four layers stack; each one closes the trust gap left by the one
below.

## 3. Attestation interface

A single small protocol that every backend implements:

```python
class AttestationBackend(Protocol):
    name: str  # "software-key", "tpm2", "nitro", "gcp-snp", "sgx"

    def attest(self, data: bytes) -> AttestationQuote:
        """Produce an attestation quote binding `data` to platform state.

        `data` is the chain artefact being anchored — typically the
        chain_signature of a v2.1 signed export or the last hmac_hash
        of a v2.2 archive boundary.
        """
        ...

    def verify(self, quote: AttestationQuote, data: bytes) -> bool:
        """Verify a quote produced by this backend over the same `data`."""
        ...
```

`AttestationQuote` is a frozen dataclass with seven fields:

| Field | Type | Purpose |
|---|---|---|
| `backend` | str | which backend produced this (`software-key`, `tpm2`, ...) |
| `quote_b64` | str | base64-encoded backend-specific quote bytes |
| `code_measurement` | str | SHA-256 of the BIJOTEL package source files |
| `platform_info` | dict[str, str] | OS, arch, Python version, hostname |
| `timestamp` | str | ISO-8601 UTC when the quote was produced |
| `data_hash` | str | SHA-256 hex of the input `data` |
| `verified` | bool | result of post-creation self-verify (sanity check) |

The shape stays the same across backends. What varies is what
`quote_b64` contains (Ed25519 signature for software, TPM2_Quote
structure for TPM, NSM document for Nitro, ...).

## 4. Backends in v2.10

| Backend | Hardware | Status | Honest scope |
|---|---|---|---|
| `SoftwareAttestation` | none — Ed25519 key on disk | **ships in v2.10** | Not hardware-rooted. Labeled `software-key`. Still useful: deterministic code hash, signed platform info, identical interface for upgrade. |
| `TPM2Attestation` | TPM 2.0 chip | stub | raises `NotImplementedError` with `tpm2-tools` install hint until hardware available |
| `AWSNitroAttestation` | AWS Nitro Enclave | stub | raises with Nitro deployment hint |
| `GCPConfidentialAttestation` | AMD SEV-SNP on GCP Confidential VM | stub | raises with GCP CVM deployment hint |
| `AzureSGXAttestation` | Intel SGX on Azure DCsv3 | stub | raises with Azure SGX deployment hint |

Stubs aren't dead code — they're a contract. The day someone deploys
on Nitro, swapping `--attest software` for `--attest nitro` is the
only change the operator makes. Same flag, same archive schema, same
verify path.

This is the **same discipline as substrate-guard L5**: ship the
Ed25519 software signature path now with `tpm_available: false`
honestly labeled; upgrade to hardware-rooted when the host supports
it. The paper documents this as the right pattern; v2.10 adopts it.

## 5. Software backend mechanics

```
SoftwareAttestation.attest(data):
    1. code_measurement = sha256(concat(read_bytes(f) for f in sorted(bijotel/*.py)))
    2. platform_info = {os, arch, python, hostname, bijotel_version}
    3. payload = json_canon({
           "data_hash":         sha256(data).hex(),
           "code_measurement":  code_measurement,
           "platform":          platform_info,
           "timestamp":         utcnow().isoformat(),
       })
    4. signature = Ed25519.sign(private_pem, payload)
    5. return AttestationQuote(
           backend="software-key",
           quote_b64=base64(signature),
           code_measurement, platform_info, timestamp,
           data_hash=sha256(data).hex(),
           verified=True
       )
```

`verify` reconstructs the payload from the quote fields and re-runs
Ed25519 verify. Mismatch → `False`. The trust assumption is that the
verifier has the operator's expected public key out-of-band (same as
how `bijotel anchor verify --public-key` works).

What the software backend can prove:

- ✓ The bijotel source files hashed to a specific SHA-256 (catches
  on-disk tampering of the package between install and run)
- ✓ The platform was as described (os, arch, Python, hostname)
- ✓ A specific Ed25519 key signed the bundle (operator identity)

What it cannot prove:

- ✗ The CPU running the code was untampered (requires TEE)
- ✗ Memory wasn't being read by a malicious hypervisor (requires
  AMD SEV-SNP / Intel TDX / similar)
- ✗ The Ed25519 private key was generated in a secure enclave
  (requires TPM with on-chip key generation)

The CHANGELOG, docs, and the `backend="software-key"` field all say
this explicitly. No magic claims.

## 6. Integration with `bijotel archive`

```
bijotel archive \
    --before 2026-05-20 \
    --output archive.db \
    --sign-key keys/private.pem \
    --attest software             # NEW in v2.10
```

The archive's existing metadata table gains four columns (all NULL
when `--attest` is omitted, so existing archives keep working):

```sql
ALTER TABLE archive_metadata ADD COLUMN attestation_backend TEXT;
ALTER TABLE archive_metadata ADD COLUMN attestation_quote TEXT;  -- full JSON
ALTER TABLE archive_metadata ADD COLUMN attestation_code_measurement TEXT;
ALTER TABLE archive_metadata ADD COLUMN attestation_platform_info TEXT; -- JSON
```

Sealed at archive time. Verifiable post-hoc by running the same
backend's verify against `(archive.chain_signature, attestation_quote)`.

## 7. Verify path (v2.10)

```
bijotel archive-verify \
    --db archive.db \
    --attest-backend software \
    --attest-public-key keys/public.pem
```

(Implementation slated for v2.11 — the v2.10 verifier is the Python
API `SoftwareAttestation().verify(quote, data)`. A CLI wrapper is
trivial but out of scope for the 2.5h v2.10 release.)

## 8. Out of scope (v2.10)

- **TPM 2.0 swapping in for software backend** — requires hardware.
  Stub raises with clear install hint. Add when first host with TPM
  is provisioned.
- **AWS Nitro / GCP Confidential / Azure SGX** — same. Stubs.
- **Cross-backend verify** — software-signed quotes can't be verified
  by a TPM-only verifier, by design.
- **Quote freshness windows** — `timestamp` field is recorded but the
  verifier doesn't yet enforce "quote was produced within N seconds
  of the archive sealing". Easy follow-up.
- **Bundled SBOM** — `code_measurement` is currently `sha256` of all
  `.py` files in the bijotel package. A CycloneDX SBOM with proper
  dependency hashes is the cleaner long-term primitive. v2.11+.

## 9. Open questions

1. **What goes in `data` for the archive case?** Three sensible
   choices: chain_signature (Ed25519 sig of the segment), last
   hmac_hash, or a SHA-256 of the canonical archive metadata. v2.10
   uses the chain_signature so the attestation binds operator
   identity + segment state in one quote.
2. **Should the attestation key be different from the export key?**
   For software backend they could be the same (same Ed25519). For
   hardware backends the attestation key is hardware-rooted and
   doesn't exist on disk — separate by definition. v2.10's software
   backend accepts a separate `--attest-key` flag but defaults to the
   `--sign-key` if absent.
3. **Quote nonce?** TPM2_Quote takes a nonce to defeat replay; our
   software backend embeds the timestamp instead. Hardware backends
   will pass nonce through their native APIs.

## 10. Why ship software-only first

Three reasons:

1. **Honest baseline.** Operators who don't have TPM/Nitro can still
   get the code-measurement + platform-info + Ed25519 attestation
   today. That's a meaningful improvement over "we trust the host
   blindly".
2. **Interface lock-in.** Shipping software-only locks the
   `AttestationBackend` protocol so hardware backends can slot in
   later without ABI churn.
3. **Documentation.** The stubs ship side-by-side with the working
   software backend, with explicit "deploy on $HARDWARE to enable
   this" messages. Operators see the upgrade path the moment they
   hit `--help`.

The cost is real but contained: BIJOTEL has to be honest that
software attestation is software, not hardware. The
`backend="software-key"` label and the CHANGELOG's "Honest scope"
section both do that. No magic claims.
