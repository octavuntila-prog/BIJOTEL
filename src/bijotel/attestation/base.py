"""Attestation protocol + the common ``AttestationQuote`` dataclass.

Every backend implements two methods (``attest`` / ``verify``) and
returns the same ``AttestationQuote`` shape. What varies across
backends is what ``quote_b64`` actually contains:

  * software: Ed25519 signature over the canonical-JSON payload
  * tpm2:     TPM2_Quote structure (PCR list + signature)
  * nitro:    AWS Nitro Security Module attestation document (CBOR)
  * gcp-snp:  SEV-SNP report blob
  * sgx:      SGX quote (DCAP / EPID)

The verifier matches on ``backend`` and routes to the right code path;
the protocol stays uniform.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AttestationQuote:
    """A single attestation, produced by a backend.

    Shape is constant across backends so the archive-metadata schema
    doesn't fork per backend. The opaque per-backend bytes live in
    ``quote_b64``; everything else is portable.

    Attributes:
        backend: One of ``software-key``, ``tpm2``, ``nitro``,
            ``gcp-snp``, ``sgx``. Lower-case, dash-separated.
            Verifiers route on this.
        quote_b64: Base64-encoded backend-specific quote bytes. For
            the software backend this is an Ed25519 signature over
            the canonical-JSON payload (see ``SoftwareAttestation``).
        code_measurement: SHA-256 of the BIJOTEL package source. Lets
            a verifier confirm the code that produced the quote
            matches a known-good measurement.
        platform_info: ``{os, arch, python, hostname, bijotel_version}``
            — small, fixed key set so verifiers can pattern-match.
        timestamp: ISO-8601 UTC when the quote was produced.
        data_hash: SHA-256 hex of the ``data`` bytes the quote
            attests to. Equivalent to a binding digest; the verifier
            recomputes this from ``data`` to confirm the quote refers
            to the artefact in hand.
        verified: ``True`` iff the backend's own post-creation
            self-verify passed. A sanity bit, not a trust statement.
    """

    backend: str
    quote_b64: str
    code_measurement: str
    platform_info: dict[str, str] = field(default_factory=dict)
    timestamp: str = ""
    data_hash: str = ""
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict — used for archive metadata."""
        return asdict(self)

    def to_json(self) -> str:
        """Canonical-shape JSON (sorted keys). Used at storage time."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@runtime_checkable
class AttestationBackend(Protocol):
    """The two-method contract every attestation backend implements.

    Backends are constructed with their own setup (Ed25519 key for the
    software backend; ``tpm2-tools`` for TPM2; nitro client SDK for
    Nitro; etc.) but expose this uniform interface.
    """

    #: Short identifier used to populate ``AttestationQuote.backend``.
    #: Verifiers route on this string.
    name: str

    def attest(self, data: bytes) -> AttestationQuote:
        """Produce a quote that binds ``data`` to the platform state.

        ``data`` is the artefact being anchored — for BIJOTEL this is
        typically a chain_signature (v2.1 signed export) or the last
        ``hmac_hash`` of a v2.2 archive boundary.
        """
        ...

    def verify(self, quote: AttestationQuote, data: bytes) -> bool:
        """Verify a quote produced by this same backend.

        Backends do NOT need to verify quotes produced by *other*
        backends — cross-backend verify is out of scope (see design
        doc §8). A TPM-only verifier rejects software quotes; a
        software-only verifier rejects TPM quotes. That's correct.
        """
        ...
