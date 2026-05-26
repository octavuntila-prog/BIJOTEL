"""Azure DCsv3 / Intel SGX attestation backend — stub for v2.10.0.

Intel SGX exposes per-enclave attestation via the DCAP (Data Center
Attestation Primitives) flow. Inside an SGX enclave, calling
``sgx_get_quote(report)`` returns a quote signed by the platform's
Provisioning Certification Key (PCK), whose chain roots at Intel.

Most cloud SGX today is via Azure DCsv3 series VMs (or DC-series with
SGX2). Activation in v2.11+ via a ``[sgx]`` extra that pulls in the
Open Enclave SDK Python bindings or the Azure Confidential Computing
attestation client.
"""

from __future__ import annotations

from bijotel.attestation.base import AttestationQuote


class AzureSGXAttestation:
    """Stub for Azure DCsv3 / Intel SGX attestation."""

    #: Required by the AttestationBackend protocol.
    name = "sgx"

    def __init__(self) -> None:
        raise NotImplementedError(
            "Intel SGX attestation requires running inside an SGX "
            "enclave on a CPU that exposes SGX2. The current host "
            "does not have SGX available.\n"
            "\n"
            "Activation path:\n"
            "  1. Deploy on an Azure DCsv3 / DCdsv3 VM (or any host "
            "with an SGX-enabled Intel CPU and the SGX driver loaded).\n"
            "  2. Build an SGX enclave image that carries bijotel + "
            "the signing keys, signed for production deployment.\n"
            "  3. pip install bijotel[sgx]  (planned in v2.11+ — "
            "wraps the DCAP attestation flow).\n"
            "  4. Re-run: bijotel archive --attest sgx ...\n"
            "\n"
            "For now, use `--attest software` — same interface, "
            "Ed25519 + code measurement + platform info."
        )

    def attest(self, data: bytes) -> AttestationQuote:  # pragma: no cover
        raise NotImplementedError("AzureSGXAttestation never constructs.")

    def verify(  # pragma: no cover
        self, quote: AttestationQuote, data: bytes
    ) -> bool:
        raise NotImplementedError("AzureSGXAttestation never constructs.")
