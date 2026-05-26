"""``bijotel.attestation`` — TEE-style attestation for chain artefacts (v2.10.0).

Closes the fourth trust gap (after HMAC chain, Ed25519 signature, and
Rekor anchoring): proves the **sealing software itself** was in a known
state when it produced the artefact. Without this, a compromised host
running a tampered ``bijotel`` binary can produce a chain that still
passes every other verification — because the host owns the HMAC secret
and the Ed25519 key.

Trust hierarchy after v2.10::

    Layer 1  HMAC chain    "entries weren't tampered post-seal"
    Layer 2  Ed25519       "signed by this specific key"
    Layer 3  Rekor (v2.9)  "existed at time T (publicly witnessed)"
    Layer 4  Attestation   "produced by trusted code on verified hardware"

This module ships with a working software backend (``SoftwareAttestation``)
and explicit ``NotImplementedError`` stubs for ``TPM2``,
``AWSNitroAttestation``, ``GCPConfidentialAttestation``, and
``AzureSGXAttestation``. Stubs aren't dead code — they lock the protocol
so hardware backends slot in without ABI churn.

Public API:
    ``AttestationBackend`` (protocol) — the small interface every
        backend implements: ``attest(data)`` + ``verify(quote, data)``.
    ``AttestationQuote`` — frozen dataclass with the seven fields
        every backend populates (see :mod:`base`).
    ``SoftwareAttestation`` — Ed25519 + code-measurement +
        platform-info. Not hardware-rooted. Labeled explicitly.
    ``TPM2Attestation``, ``AWSNitroAttestation``,
        ``GCPConfidentialAttestation``, ``AzureSGXAttestation`` —
        stubs that raise on instantiation with deployment hints.

See ``docs/design/tee-anchored-chains.md`` for the full design and the
honest scope notes.
"""

from __future__ import annotations

from bijotel.attestation.base import (
    AttestationBackend,
    AttestationQuote,
)
from bijotel.attestation.gcp import GCPConfidentialAttestation
from bijotel.attestation.nitro import AWSNitroAttestation
from bijotel.attestation.sgx import AzureSGXAttestation
from bijotel.attestation.software import SoftwareAttestation
from bijotel.attestation.tpm2 import TPM2Attestation

__all__ = [
    "AWSNitroAttestation",
    "AttestationBackend",
    "AttestationQuote",
    "AzureSGXAttestation",
    "GCPConfidentialAttestation",
    "SoftwareAttestation",
    "TPM2Attestation",
]
