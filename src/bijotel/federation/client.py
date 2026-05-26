"""Federation HTTP client (stdlib ``urllib``).

Matches the API laid out in §5 of ``cross-org-federation.md``. Wraps:

  * ``POST /federation/register``  (with challenge-response Ed25519)
  * ``POST /federation/submit``    (with bearer-token Ed25519 auth)
  * ``GET  /federation/status``
  * ``GET  /federation/operator/{id}``
  * ``GET  /federation/anchor/{id}``
  * ``GET  /federation/verify/{id}``

No new runtime dependency — pure stdlib HTTP, same pattern as the
Rekor client in ``bijotel.anchoring.rekor``.
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.request
from typing import Any

from bijotel.crypto.ed25519 import sign as ed25519_sign


class FederationClient:
    """Operator-side HTTP client for a BIJOTEL federation service.

    Construct with the federation's base URL. Auth flows that need it
    (``register`` and ``submit``) take a private-key PEM and use
    Ed25519 challenge-response — no passwords, no API keys.

    All methods raise ``RuntimeError`` on network or HTTP errors with
    a specific reason; they never silently retry.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Registration (challenge-response)
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        public_key_pem: bytes,
        private_key_pem: bytes,
        org_name: str,
        contact_email: str = "",
    ) -> dict[str, Any]:
        """Register the operator with the federation.

        Two-call flow:

        1. ``GET /register/challenge`` → server returns a nonce.
        2. ``POST /register`` with the public key, org metadata, and
           an Ed25519 signature over the nonce — proves the operator
           holds the matching private key without exposing it.

        Returns the parsed JSON ``RegistrationReceipt``.
        """
        challenge = self._get(f"{self.base_url}/register/challenge")
        nonce = challenge.get("nonce", "")
        if not nonce:
            raise RuntimeError(f"federation /register/challenge missing nonce: {challenge!r}")

        signature = ed25519_sign(nonce.encode("utf-8"), private_key_pem)
        body = {
            "public_key_pem": public_key_pem.decode("ascii"),
            "org_name": org_name,
            "contact_email": contact_email,
            "nonce": nonce,
            "nonce_signature": base64.b64encode(signature).decode("ascii"),
        }
        return self._post(f"{self.base_url}/register", body)

    # ------------------------------------------------------------------
    # Submission (signed export bodies)
    # ------------------------------------------------------------------

    def submit(
        self,
        *,
        operator_id: str,
        private_key_pem: bytes,
        signed_export_json: str | dict[str, Any],
    ) -> dict[str, Any]:
        """Submit one signed export (``bijotel-chain-v2`` envelope).

        The Authorization header is a self-contained bearer token:
        ``<operator_id>.<nonce>.<nonce_signature_b64>``. The federation
        verifies that the signature matches the registered key.
        """
        if isinstance(signed_export_json, dict):
            body_str = json.dumps(signed_export_json)
        else:
            body_str = signed_export_json

        # Per-request nonce for the auth header. 16 bytes hex = 32 chars.
        nonce = secrets.token_hex(16)
        sig_b64 = base64.b64encode(
            ed25519_sign(nonce.encode("utf-8"), private_key_pem)
        ).decode("ascii")
        token = f"{operator_id}.{nonce}.{sig_b64}"

        return self._post(
            f"{self.base_url}/submit",
            body=body_str,
            extra_headers={"Authorization": f"Bearer {token}"},
            body_is_string=True,
        )

    # ------------------------------------------------------------------
    # Read-only endpoints
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Public health/discovery endpoint. Unauthenticated."""
        return self._get(f"{self.base_url}/status")

    def get_operator(self, operator_id: str) -> dict[str, Any]:
        """Public summary of an operator's federation history."""
        return self._get(f"{self.base_url}/operator/{operator_id}")

    def get_anchor(self, anchor_id: str) -> dict[str, Any]:
        """Fetch one cross-anchor record by ID."""
        return self._get(f"{self.base_url}/anchor/{anchor_id}")

    def verify_anchor(self, anchor_id: str) -> dict[str, Any]:
        """Ask the federation to re-verify a cross-anchor live."""
        return self._get(f"{self.base_url}/verify/{anchor_id}")

    # ------------------------------------------------------------------
    # HTTP primitives
    # ------------------------------------------------------------------

    def _get(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"federation GET {url} → HTTP {e.code}: {detail[:300]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"federation GET {url} network error: {e}") from e

    def _post(
        self,
        url: str,
        body: dict[str, Any] | str,
        *,
        extra_headers: dict[str, str] | None = None,
        body_is_string: bool = False,
    ) -> dict[str, Any]:
        if body_is_string:
            data = body.encode("utf-8") if isinstance(body, str) else body
        else:
            data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"federation POST {url} → HTTP {e.code}: {detail[:300]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"federation POST {url} network error: {e}") from e
