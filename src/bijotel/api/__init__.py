"""BIJOTEL API package — minimal FastAPI surface for v1.0.0.

The v1.0.0 release ships only the import-safe ``create_app`` factory plus
a ``/health`` endpoint. Full chain / policy / regression endpoints are
scheduled for v1.1.0 (Day 6-7 of the harvest plan).

The ``fastapi`` and ``uvicorn`` dependencies are OPTIONAL — install with::

    pip install bijotel[api]

Importing :func:`bijotel.api.create_app` without those extras raises
``ImportError`` with a clear remediation message.
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str):
    """Lazy-import create_app so missing fastapi doesn't break package import.

    ``import bijotel`` should NEVER fail just because the user didn't install
    the ``[api]`` extra. The factory is only resolved when explicitly accessed
    via ``from bijotel.api import create_app``.
    """
    if name == "create_app":
        from bijotel.api.app import create_app

        return create_app
    raise AttributeError(f"module 'bijotel.api' has no attribute {name!r}")
