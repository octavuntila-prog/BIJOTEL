"""GCP Confidential VM (AMD SEV-SNP) attestation backend — stub for v2.10.0.

GCP's Confidential VMs back the VM's memory encryption with AMD's
Secure Encrypted Virtualization with Secure Nested Paging (SEV-SNP).
A guest can call into the SNP firmware (via ``/dev/sev-guest`` or
``msr``) to get an attestation report binding the VCEK (Versioned Chip
Endorsement Key) to the current code measurement (the VMCB launch
hash) and an arbitrary 64-byte user-data field.

The verifier roots trust at AMD's published VCEK cert chain. Cleaner
than TPM here because the SEV-SNP report inherently includes the boot
measurement; no need to maintain PCR policies.

Activation in v2.11+ via a ``[gcp]`` extra that pulls in
``google-cloud-confidential-computing`` or
``virtee/sevtool``-equivalent Python bindings.
"""

from __future__ import annotations

from bijotel.attestation.base import AttestationQuote


class GCPConfidentialAttestation:
    """Stub for GCP Confidential VM (AMD SEV-SNP) attestation."""

    #: Required by the AttestationBackend protocol.
    name = "gcp-snp"

    def __init__(self) -> None:
        raise NotImplementedError(
            "GCP Confidential VM (SEV-SNP) attestation requires the "
            "host to be a GCP Confidential VM with SEV-SNP enabled. "
            "The current host has no /dev/sev-guest device.\n"
            "\n"
            "Activation path:\n"
            "  1. Create a GCP Confidential VM "
            "(`--confidential-compute --maintenance-policy=TERMINATE` "
            "on n2d/c2d machine types with SEV-SNP available).\n"
            "  2. pip install bijotel[gcp]  (planned in v2.11+).\n"
            "  3. Re-run: bijotel archive --attest gcp ...\n"
            "\n"
            "For now, use `--attest software` — same interface, "
            "Ed25519 + code measurement + platform info."
        )

    def attest(self, data: bytes) -> AttestationQuote:  # pragma: no cover
        raise NotImplementedError("GCPConfidentialAttestation never constructs.")

    def verify(  # pragma: no cover
        self, quote: AttestationQuote, data: bytes
    ) -> bool:
        raise NotImplementedError("GCPConfidentialAttestation never constructs.")
