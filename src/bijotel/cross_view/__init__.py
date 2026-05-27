"""Unified view across multiple BIJOTEL chains — v2.13.0.

Operators with more than one BIJOTEL-instrumented ecosystem (e.g.
GENA + ARA, or one production + one staging) need a single place to
see totals, provider distribution, and per-chain health without
flipping between dashboards.

``CrossEcosystemView`` aggregates stats from N chains (local SQLite
files or pre-exported JSON files), produces a flat summary dict, and
runs lightweight integrity checks per chain.

It does NOT merge the chains. Each chain stays sovereign — different
HMAC secrets, different Ed25519 keys, different operators. The view is
read-only, observational, no chain modification.

Usage::

    from bijotel.cross_view import CrossEcosystemView

    view = CrossEcosystemView()
    view.add_chain("GENA", db_path="/data/bijotel_chain.db")
    view.add_chain("ARA",  db_path="/app/data/bijotel_chain.db")
    print(view.summary())

CLI equivalent::

    bijotel cross-view \\
        --chain "GENA=/data/bijotel_chain.db" \\
        --chain "ARA=/app/data/bijotel_chain.db" \\
        --json
"""

from __future__ import annotations

from bijotel.cross_view.view import (
    ChainStats,
    CrossEcosystemView,
    load_chain_stats_from_db,
    load_chain_stats_from_export,
)

__all__ = [
    "ChainStats",
    "CrossEcosystemView",
    "load_chain_stats_from_db",
    "load_chain_stats_from_export",
]
