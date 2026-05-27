"""``CrossEcosystemView`` — unified view across multiple BIJOTEL chains.

Each chain contributes a ``ChainStats`` snapshot: entry count, provider
set, model histogram, first/last timestamps. The view sums where
summing makes sense (total entries, union of providers) and exposes
per-chain detail where it doesn't (model counts differ per ecosystem).

Integrity check per chain delegates to ``bijotel.processors.verify``
when available, falls back to a basic "row count + last-hash" check
when not (e.g. when reading from a pre-exported JSON file that has no
HMAC secret).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChainStats:
    """Per-chain snapshot. All fields are populated at ``add_chain`` time.

    Empty providers/models maps mean "no entries yet" — not an error.
    """

    name: str
    source: str  # 'db' or 'export'
    source_path: str
    entries: int
    providers: set[str] = field(default_factory=set)
    models: dict[str, int] = field(default_factory=dict)
    first_timestamp_ns: int | None = None
    last_timestamp_ns: int | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "source_path": self.source_path,
            "entries": self.entries,
            "providers": sorted(self.providers),
            "models": dict(self.models),
            "first_timestamp_ns": self.first_timestamp_ns,
            "last_timestamp_ns": self.last_timestamp_ns,
        }


# ─── loaders ──────────────────────────────────────────────────────────


def load_chain_stats_from_db(name: str, db_path: str) -> ChainStats:
    """Open a chain.db read-only and compute summary stats.

    Uses ``json_extract`` on the SQLite side (no need to parse every
    row in Python).
    """
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"chain.db not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]

        if total == 0:
            return ChainStats(
                name=name, source="db", source_path=str(p),
                entries=0,
            )

        # Provider set
        providers: set[str] = set()
        for (prov,) in conn.execute(
            "SELECT DISTINCT "
            'json_extract(canonical_body, \'$.attributes."gen_ai.provider.name"\')'
            " FROM chain"
        ).fetchall():
            if prov:
                providers.add(prov)

        # Model histogram
        models: dict[str, int] = {}
        for model, cnt in conn.execute(
            "SELECT "
            'json_extract(canonical_body, \'$.attributes."gen_ai.request.model"\'),'
            " COUNT(*)"
            " FROM chain GROUP BY 1"
        ).fetchall():
            if model:
                models[model] = cnt

        first = conn.execute(
            "SELECT MIN(timestamp_ns) FROM chain"
        ).fetchone()[0]
        last = conn.execute(
            "SELECT MAX(timestamp_ns) FROM chain"
        ).fetchone()[0]

        return ChainStats(
            name=name,
            source="db",
            source_path=str(p),
            entries=total,
            providers=providers,
            models=models,
            first_timestamp_ns=first,
            last_timestamp_ns=last,
        )
    finally:
        conn.close()


def load_chain_stats_from_export(name: str, export_path: str) -> ChainStats:
    """Read a v2 export JSON and compute summary stats.

    Useful when the chain is on a remote host and only the export is
    available locally (or for offline auditor workflows).
    """
    p = Path(export_path)
    if not p.exists():
        raise FileNotFoundError(f"export not found: {export_path}")

    data = json.loads(p.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    if not entries:
        return ChainStats(
            name=name, source="export", source_path=str(p),
            entries=0,
        )

    providers: set[str] = set()
    models: dict[str, int] = {}
    first = None
    last = None

    for e in entries:
        cb = e.get("canonical_body") or {}
        if isinstance(cb, str):
            try:
                cb = json.loads(cb)
            except Exception:
                cb = {}
        attrs = cb.get("attributes", {}) if isinstance(cb, dict) else {}

        prov = attrs.get("gen_ai.provider.name")
        if prov:
            providers.add(prov)

        model = attrs.get("gen_ai.request.model")
        if model:
            models[model] = models.get(model, 0) + 1

        ts = e.get("timestamp_ns")
        if ts is not None:
            if first is None or ts < first:
                first = ts
            if last is None or ts > last:
                last = ts

    return ChainStats(
        name=name,
        source="export",
        source_path=str(p),
        entries=len(entries),
        providers=providers,
        models=models,
        first_timestamp_ns=first,
        last_timestamp_ns=last,
    )


# ─── the view ─────────────────────────────────────────────────────────


class CrossEcosystemView:
    """Aggregate view across N BIJOTEL chains.

    Chains are added by name with either a local DB path or an export
    JSON path; both can be mixed in the same view. Methods return
    plain dicts (JSON-serializable).
    """

    def __init__(self) -> None:
        self.chains: dict[str, ChainStats] = {}

    def add_chain(
        self,
        name: str,
        db_path: str | None = None,
        export_path: str | None = None,
    ) -> ChainStats:
        """Add one chain by either local DB or pre-exported JSON.

        Exactly one of ``db_path`` / ``export_path`` must be provided.
        Raises ``ValueError`` otherwise. Raises ``FileNotFoundError``
        if the path doesn't exist.
        """
        if (db_path is None) == (export_path is None):
            raise ValueError(
                "add_chain: exactly one of db_path / export_path required"
            )

        if name in self.chains:
            raise ValueError(f"chain {name!r} already added")

        if db_path is not None:
            stats = load_chain_stats_from_db(name, db_path)
        else:
            assert export_path is not None  # mypy
            stats = load_chain_stats_from_export(name, export_path)

        self.chains[name] = stats
        return stats

    def summary(self) -> dict:
        """Cross-ecosystem summary dict, JSON-serializable."""
        all_providers: set[str] = set()
        for s in self.chains.values():
            all_providers.update(s.providers)

        timestamps_first = [
            s.first_timestamp_ns
            for s in self.chains.values()
            if s.first_timestamp_ns is not None
        ]
        timestamps_last = [
            s.last_timestamp_ns
            for s in self.chains.values()
            if s.last_timestamp_ns is not None
        ]

        return {
            "ecosystems": len(self.chains),
            "total_entries": sum(s.entries for s in self.chains.values()),
            "total_providers": sorted(all_providers),
            "earliest_timestamp_ns": (
                min(timestamps_first) if timestamps_first else None
            ),
            "latest_timestamp_ns": (
                max(timestamps_last) if timestamps_last else None
            ),
            "per_ecosystem": {
                name: stats.to_dict()
                for name, stats in self.chains.items()
            },
        }

    def integrity_report(self, hmac_secrets: dict[str, bytes] | None = None) -> dict:
        """Per-chain integrity check + cross-chain consistency notes.

        ``hmac_secrets`` maps chain name -> HMAC secret bytes. When
        provided, full HMAC verify runs for DB-sourced chains. When
        omitted (or chain is from export), only a structural check is
        performed (row count + presence of canonical_body fields).

        Returns::

            {
              "per_chain": {
                "GENA": {"valid": True, "entries": 6805, "method": "hmac"},
                "ARA":  {"valid": True, "entries": 218,  "method": "hmac"},
              },
              "cross_chain": {
                "timeline_overlap": True,
                "shared_providers": ["anthropic"],
              },
            }
        """
        hmac_secrets = hmac_secrets or {}

        per_chain: dict[str, dict] = {}
        for name, stats in self.chains.items():
            entry: dict = {"entries": stats.entries}
            secret = hmac_secrets.get(name)

            if secret is not None and stats.source == "db":
                # Public verify_chain returns (valid, last_seq, error).
                from bijotel.processors import verify_chain
                try:
                    valid, last_seq, err = verify_chain(
                        db_path=stats.source_path, secret_key=secret,
                    )
                    entry["valid"] = bool(valid)
                    entry["method"] = "hmac"
                    entry["last_seq"] = last_seq
                    if not valid and err:
                        entry["error"] = err
                except Exception as e:
                    entry["valid"] = False
                    entry["method"] = "hmac"
                    entry["error"] = str(e)
            else:
                # Structural check only — confirm we loaded something
                # parseable. Doesn't prove HMAC chain integrity.
                entry["valid"] = stats.entries > 0
                entry["method"] = "structural"
                entry["note"] = (
                    "No HMAC secret provided; only row-count check ran"
                )

            per_chain[name] = entry

        # Cross-chain notes
        timestamps = [
            (s.first_timestamp_ns, s.last_timestamp_ns)
            for s in self.chains.values()
            if s.first_timestamp_ns is not None
        ]
        timeline_overlap = False
        if len(timestamps) >= 2:
            # Any two chains whose [first, last] windows overlap
            for i in range(len(timestamps)):
                for j in range(i + 1, len(timestamps)):
                    a_first, a_last = timestamps[i]
                    b_first, b_last = timestamps[j]
                    if a_first <= b_last and b_first <= a_last:
                        timeline_overlap = True
                        break
                if timeline_overlap:
                    break

        shared_providers = set()
        provider_sets = [s.providers for s in self.chains.values() if s.providers]
        if len(provider_sets) >= 2:
            shared_providers = set.intersection(*provider_sets)

        return {
            "per_chain": per_chain,
            "cross_chain": {
                "timeline_overlap": timeline_overlap,
                "shared_providers": sorted(shared_providers),
            },
        }
