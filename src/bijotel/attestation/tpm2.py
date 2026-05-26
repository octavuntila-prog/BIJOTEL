"""TPM 2.0 attestation backend — stub for v2.10.0.

A TPM 2.0 chip on the host machine can produce a ``TPM2_Quote`` over a
nonce that includes the PCR values describing the firmware/secure-boot
state, signed by the AIK (Attestation Identity Key) whose root is the
TPM's manufacturer-issued EK certificate. That's the gold standard for
"this code ran on this physical machine in this state".

This stub exists for two reasons:

1. **Contract**. The ``AttestationBackend`` protocol is what the rest
   of BIJOTEL is built against. Locking the name + signature now means
   the day someone deploys on a host with a real TPM, swapping the
   stub for the real implementation is a drop-in.
2. **Discoverability**. Operators see the option in ``--help`` and the
   error message points them at exactly what's needed to activate it.

Current hosts (Hetzner CPX32/CPX52) do not expose a TPM. The day we
deploy on:

  * Bare-metal with TPM 2.0 chip
  * AWS instance with the TPM-enabled launch template
  * GCP Confidential VM (which exposes a vTPM)
  * Azure Trusted Launch VM
  * Any modern laptop/desktop (fTPM via Intel PTT / AMD fTPM)

… this stub gets replaced with a real implementation backed by the
``tpm2-pytss`` PyPI package (which wraps tpm2-tss). The interface
stays exactly the same.
"""

from __future__ import annotations

from bijotel.attestation.base import AttestationQuote


class TPM2Attestation:
    """Stub for TPM 2.0 hardware attestation.

    Raises ``NotImplementedError`` at construction with an explicit
    upgrade path. We refuse to construct rather than fail later in
    ``attest`` because the operator finding out "TPM isn't available
    here" should happen at config-load time, not in the middle of an
    archive run.
    """

    #: Required by the AttestationBackend protocol.
    name = "tpm2"

    def __init__(self) -> None:
        raise NotImplementedError(
            "TPM 2.0 attestation requires a hardware TPM accessible via "
            "/dev/tpmrm0 (or equivalent) and the `tpm2-pytss` PyPI "
            "package. The current host does not have a usable TPM. "
            "\n"
            "Activation path:\n"
            "  1. Deploy on hardware with TPM 2.0 (bare-metal with TPM "
            "chip, GCP Confidential VM, Azure Trusted Launch, modern "
            "fTPM-capable CPU).\n"
            "  2. pip install bijotel[tpm2]  (planned in v2.11+ — "
            "tpm2-pytss is the wrapper).\n"
            "  3. Re-run: bijotel archive --attest tpm2 ...\n"
            "\n"
            "For now, use `--attest software` for software-key "
            "attestation — same interface, Ed25519 + code measurement "
            "+ platform info."
        )

    # The methods are defined to make the protocol structural-typing
    # checks happy and to give static analyzers a clear signal, but
    # they're unreachable because __init__ refuses.

    def attest(self, data: bytes) -> AttestationQuote:  # pragma: no cover
        raise NotImplementedError("TPM2Attestation never constructs.")

    def verify(  # pragma: no cover
        self, quote: AttestationQuote, data: bytes
    ) -> bool:
        raise NotImplementedError("TPM2Attestation never constructs.")
