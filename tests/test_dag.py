"""Tests for F16 / Bijuteria #2 completion — Merkle DAG."""

from __future__ import annotations

from pathlib import Path

import pytest

from bijotel.processors.dag import DAGNode, MerkleDAG


@pytest.fixture
def dag_db(tmp_path: Path) -> Path:
    return tmp_path / "dag.db"


def test_add_node_and_get(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    dag.add_node("hash_a", refs=[])
    node = dag.get_node("hash_a")
    assert isinstance(node, DAGNode)
    assert node.content_hash == "hash_a"
    assert node.refs == []


def test_add_node_with_refs(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    dag.add_node("child", refs=["parent_1", "parent_2"])
    node = dag.get_node("child")
    assert node.refs == ["parent_1", "parent_2"]


def test_get_refs_outbound(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    dag.add_node("child", refs=["p1", "p2"])
    assert sorted(dag.get_refs("child")) == ["p1", "p2"]


def test_get_inbound_refs(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    dag.add_node("child1", refs=["parent"])
    dag.add_node("child2", refs=["parent"])
    inbound = sorted(dag.get_inbound("parent"))
    assert inbound == ["child1", "child2"]


def test_missing_node_returns_none(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    assert dag.get_node("nonexistent") is None
    assert dag.get_refs("nonexistent") == []


def test_resolve_simple_chain(dag_db: Path) -> None:
    """A → B → C: resolve(A) returns A, B, C in DFS order."""
    dag = MerkleDAG(dag_db)
    dag.add_node("C", refs=[])
    dag.add_node("B", refs=["C"])
    dag.add_node("A", refs=["B"])
    result = dag.resolve("A")
    assert result["root"] == "A"
    assert result["order"] == ["A", "B", "C"]
    assert set(result["nodes"].keys()) == {"A", "B", "C"}
    assert result["missing"] == []
    assert result["cycle_breaks"] == []


def test_resolve_no_refs(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    dag.add_node("solo", refs=[])
    result = dag.resolve("solo")
    assert result["order"] == ["solo"]
    assert result["nodes"]["solo"].refs == []


def test_resolve_missing_root(dag_db: Path) -> None:
    """resolve() on a non-existent hash returns it in 'missing'."""
    dag = MerkleDAG(dag_db)
    result = dag.resolve("ghost")
    assert result["nodes"] == {}
    assert result["missing"] == ["ghost"]


def test_resolve_circular_ref_protection(dag_db: Path) -> None:
    """A → B → A (cycle): resolve breaks via visited-set, records in cycle_breaks."""
    dag = MerkleDAG(dag_db)
    dag.add_node("A", refs=["B"])
    dag.add_node("B", refs=["A"])
    result = dag.resolve("A")
    # Both nodes resolved exactly once each
    assert set(result["order"]) == {"A", "B"}
    # Cycle detected and recorded
    assert "A" in result["cycle_breaks"]


def test_idempotent_add_node(dag_db: Path) -> None:
    """Re-adding the same hash is a no-op (refs not overwritten)."""
    dag = MerkleDAG(dag_db)
    dag.add_node("X", refs=["Y", "Z"])
    dag.add_node("X", refs=["different"])  # ignored
    assert dag.get_refs("X") == ["Y", "Z"]
    assert len(dag) == 1


def test_dag_len_and_contains(dag_db: Path) -> None:
    dag = MerkleDAG(dag_db)
    assert len(dag) == 0
    assert "x" not in dag
    dag.add_node("x", refs=[])
    assert len(dag) == 1
    assert "x" in dag
