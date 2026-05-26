"""AWS Nitro Enclave attestation backend — stub for v2.10.0.

AWS Nitro Enclaves expose a Nitro Security Module (NSM) device inside
the enclave. Calling ``NSM_Attest(nonce, user_data, public_key)``
returns a CBOR-encoded attestation document signed by AWS's per-region
PKI. The signature chain roots up to AWS's published Nitro Attestation
PKI, so any verifier with the root certificate can validate without
talking to AWS at verify time.

This backend would call ``boto3`` / the ``aws-nitro-enclaves`` Python
SDK to produce the document, then base64-encode it as ``quote_b64``.

Activation path documented in the ``NotImplementedError`` message.
"""

from __future__ import annotations

from bijotel.attestation.base import AttestationQuote


class AWSNitroAttestation:
    """Stub for AWS Nitro Enclave attestation.

    Real implementation lives in v2.11+ once a Nitro deployment exists
    to test against. The contract here is the API the v2.11 code will
    expose; this stub locks the name + arguments.
    """

    #: Required by the AttestationBackend protocol.
    name = "nitro"

    def __init__(self) -> None:
        raise NotImplementedError(
            "AWS Nitro attestation requires running inside an AWS Nitro "
            "Enclave. The current host is not an EC2 Nitro instance "
            "and does not expose the /dev/nsm device.\n"
            "\n"
            "Activation path:\n"
            "  1. Launch an EC2 instance type that supports Nitro "
            "Enclaves (e.g. m5n.metal, c5.4xlarge with EnclaveOptions).\n"
            "  2. Build and start a Nitro Enclave image (.eif) that "
            "carries bijotel + the operator's signing keys.\n"
            "  3. pip install bijotel[nitro]  (planned in v2.11+).\n"
            "  4. Re-run: bijotel archive --attest nitro ...\n"
            "\n"
            "For now, use `--attest software` — same interface, "
            "Ed25519 + code measurement + platform info."
        )

    def attest(self, data: bytes) -> AttestationQuote:  # pragma: no cover
        raise NotImplementedError("AWSNitroAttestation never constructs.")

    def verify(  # pragma: no cover
        self, quote: AttestationQuote, data: bytes
    ) -> bool:
        raise NotImplementedError("AWSNitroAttestation never constructs.")
