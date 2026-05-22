"""F16 / Bijuteria #2 completion: Merkle DAG + resolver over chain entries.

CAS (``CasSpanProcessor``) stores spans content-addressably. This module
adds the DAG layer: chain entries become nodes with optional refs to
other content hashes. Enables:

* **Cross-reference**: a span that builds on a prior span records the
  prior's hash as a ref. ``resolve(hash)`` walks the DAG to surface the
  full chain of dependencies.
* **Dependency tracking**: which spans contributed to this one's input?
* **Portable export**: a span + its closure of refs is a self-contained
  audit unit.

Stored in SQLite alongside chain.db (recommended) with the v0.6.x
hardening pattern: WAL + busy_timeout + DDL-in-IMMEDIATE.

Cycle protection: :meth:`resolve` uses a DFS visited-set to short-circuit
cyclic refs (which would otherwise loop infinitely). Cycles are not
strictly forbidden by :meth:`add_node` because real-world references
between spans may cycle by accident; resolve handles it gracefully.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

_LOG = logging.getLogger("bijotel.dag")


@dataclass(frozen=True)
class DAGNode:
    """A node in the Merkle DAG.

    Attributes:
        content_hash: Primary identifier (typically a CAS body_hash).
        refs: List of other content_hashes this node depends on / references.
        created_ns: Insertion timestamp.
    """

    content_hash: str
    refs: list[str]
    created_ns: int


class MerkleDAG:
    """SQLite-backed Merkle DAG over content hashes.

    Args:
        db_path: SQLite file path (same db as chain.db recommended).

    Schema::

        dag_nodes (
            content_hash TEXT PRIMARY KEY,
            refs_json TEXT NOT NULL,     -- JSON array of referenced hashes
            created_ns INTEGER NOT NULL
        )
        dag_refs (
            from_hash TEXT NOT NULL,
            to_hash TEXT NOT NULL,
            PRIMARY KEY (from_hash, to_hash)
        )

    The denormalized ``dag_refs`` enables fast inbound-reference queries
    (``who references hash X?``) without parsing JSON on every lookup.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dag_nodes (
                    content_hash TEXT PRIMARY KEY,
                    refs_json TEXT NOT NULL,
                    created_ns INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dag_refs (
                    from_hash TEXT NOT NULL,
                    to_hash TEXT NOT NULL,
                    PRIMARY KEY (from_hash, to_hash)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dag_refs_to ON dag_refs(to_hash)"
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def add_node(
        self,
        content_hash: str,
        refs: list[str] | None = None,
        created_ns: int | None = None,
    ) -> None:
        """Insert (or no-op if exists) a DAG node with given refs.

        Idempotent: re-adding the same content_hash is a no-op (refs are
        NOT updated; if you need to change refs, that's a new content_hash).
        """
        import json
        import time

        refs = refs or []
        ts = created_ns if created_ns is not None else time.time_ns()

        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO dag_nodes (content_hash, refs_json, created_ns)
                    VALUES (?, ?, ?)
                    ON CONFLICT(content_hash) DO NOTHING
                    """,
                    (content_hash, json.dumps(refs), ts),
                )
                # Denormalized refs table for inbound queries
                for ref in refs:
                    conn.execute(
                        """
                        INSERT INTO dag_refs (from_hash, to_hash) VALUES (?, ?)
                        ON CONFLICT(from_hash, to_hash) DO NOTHING
                        """,
                        (content_hash, ref),
                    )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def get_node(self, content_hash: str) -> DAGNode | None:
        """Return a single node, or None if not in DAG."""
        import json

        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT content_hash, refs_json, created_ns FROM dag_nodes "
                "WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if row is None:
                return None
            return DAGNode(
                content_hash=row[0],
                refs=json.loads(row[1]),
                created_ns=row[2],
            )

    def get_refs(self, content_hash: str) -> list[str]:
        """Outbound refs (this node → others). Empty list if node not found."""
        node = self.get_node(content_hash)
        return list(node.refs) if node else []

    def get_inbound(self, content_hash: str) -> list[str]:
        """Inbound refs (others → this node). Empty list if no referencers."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT from_hash FROM dag_refs WHERE to_hash = ?",
                (content_hash,),
            ).fetchall()
            return [r[0] for r in rows]

    def resolve(self, content_hash: str, max_depth: int = 100) -> dict:
        """Walk the DAG starting at ``content_hash``, return ordered closure.

        Returns a dict::

            {
                "root": content_hash,
                "nodes": {hash: DAGNode, ...},   # all nodes in closure
                "order": [hash, ...],            # DFS order, root first
                "missing": [hash, ...],          # refs that have no node
                "cycle_breaks": [hash, ...],     # cyclic refs short-circuited
            }

        Cycle-safe via visited-set DFS; ``max_depth`` is an additional
        safety limit on recursion depth (default 100).
        """
        nodes: dict[str, DAGNode] = {}
        order: list[str] = []
        missing: list[str] = []
        cycle_breaks: list[str] = []
        visited: set[str] = set()

        def dfs(h: str, depth: int) -> None:
            if depth > max_depth:
                _LOG.warning("DAG resolve max_depth=%d hit at hash %s", max_depth, h)
                return
            if h in visited:
                cycle_breaks.append(h)
                return
            visited.add(h)
            node = self.get_node(h)
            if node is None:
                missing.append(h)
                return
            nodes[h] = node
            order.append(h)
            for ref in node.refs:
                dfs(ref, depth + 1)

        dfs(content_hash, 0)
        return {
            "root": content_hash,
            "nodes": nodes,
            "order": order,
            "missing": missing,
            "cycle_breaks": cycle_breaks,
        }

    def __contains__(self, content_hash: str) -> bool:
        return self.get_node(content_hash) is not None

    def __len__(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM dag_nodes").fetchone()[0]
