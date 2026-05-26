"""``bijotel.federation`` — client-side federation surface (v2.11.0).

The operator-side companion to the protocol designed in
``docs/design/cross-org-federation.md``. Lets a BIJOTEL operator
register their Ed25519 identity with a federation service, submit
signed exports for cross-anchoring, and verify cross-anchor receipts
without needing the federation service to exist yet (every command
supports ``--dry-run`` and the verify path works against a saved
receipt JSON file).

Public API:
    ``FederationClient`` — minimal stdlib-``urllib`` HTTP client for
        the federation REST API. POST /register, POST /submit, GET
        /status, GET /operator/{id}, GET /anchor/{id},
        GET /verify/{id}.
    ``RegistrationReceipt`` — frozen dataclass returned by /register.
    ``SubmissionReceipt`` — frozen dataclass returned by /submit.
    ``CrossAnchorReceipt`` — frozen dataclass for the operator's copy
        of a cross-anchor (the artefact auditors verify against).
    ``verify_cross_anchor_receipt(receipt, *, federation_public_key_pem)``
        — local verification of a cross-anchor receipt without any
        network call. Confirms the federation signature over the
        receipt's payload.

Federation service implementation lives in a separate repo
(``github.com/octavuntila-prog/bijotel-federation``). The client side
here is decoupled — operators can use it against any federation that
implements the protocol.
"""

from __future__ import annotations

from bijotel.federation.client import FederationClient
from bijotel.federation.types import (
    CrossAnchorReceipt,
    RegistrationReceipt,
    SubmissionReceipt,
    verify_cross_anchor_receipt,
)

__all__ = [
    "CrossAnchorReceipt",
    "FederationClient",
    "RegistrationReceipt",
    "SubmissionReceipt",
    "verify_cross_anchor_receipt",
]
