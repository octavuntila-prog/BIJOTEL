"""F15 / Bijuteria #15: Intelligent inference routing (Pareto cost × quality × latency).

Pick the LLM for each query on the Pareto frontier of cost / quality / latency,
not by static allocation. Simple queries → cheap-fast (Haiku, gpt-4o-mini);
complex queries → balanced (Sonnet, gpt-4o); critical queries → top quality
(Opus). Budget constraints force downgrade when ceiling reached.

Three building blocks:

* :class:`TaskClassifier` — heuristic complexity scorer over messages.
  Returns float in ``[0.0, 1.0]``. Features: token-count proxy, code-block
  presence, multi-step / reasoning markers, structural complexity.
* :class:`ModelRegistry` — registry of available models with profiles
  ``(cost, quality, latency)`` each normalized to ``[0, 1]``. Extensible
  via :meth:`add_model` / :meth:`remove_model`.
* :class:`ParetoRouter` — given a complexity score and an optional
  :class:`Budget`, return the recommended model name. Resolves trade-off
  per complexity band; honors budget by downgrading.
* :class:`Budget` — per-agent daily USD ceiling, SQLite-backed
  (same hardening pattern as ``daily_token_budget``: WAL + busy_timeout
  + atomic INSERT-or-UPDATE). UTC date reset.

:func:`routing_recommendation` is the PolicyEngine rule factory: warn
when the request model differs from the recommended one (i.e. user is
paying for Opus on a "hello" prompt, or running Haiku on a complex
multi-step proof).

Provenance: original BIJOTEL implementation; design informed by
Bijuteria #15 catalog entry. Cost / quality / latency profiles approximate
Anthropic + OpenAI pricing as of 2026-05 (refresh via custom
``ModelRegistry`` for current numbers).
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bijotel.policy.decision import Decision

BUSY_TIMEOUT_MS = 5000

_LOG = logging.getLogger("bijotel.routing")


# ============================================================================
# TaskClassifier — heuristic complexity score
# ============================================================================


# Words that suggest multi-step reasoning / chain-of-thought territory
_COMPLEXITY_MARKERS = (
    "explain", "analyze", "compare", "derive", "prove", "design",
    "architect", "refactor", "debug", "optimize", "evaluate",
    "step by step", "step-by-step", "reasoning", "because", "therefore",
    "however", "trade-off", "trade off", "tradeoff",
)

# Words that suggest simple lookup / brief response
_SIMPLE_MARKERS = (
    "hello", "hi ", "hey", "thanks", "thank you",
    "yes", "no ", "ok ", "okay",
)

# Code block detector (markdown fenced) and inline code/symbol density
_CODE_FENCE = re.compile(r"```\w*\n.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_MATH_SYMBOLS = re.compile(r"[∑∫∂√∞≤≥≠±÷×]|\\[a-zA-Z]+\{")


class TaskClassifier:
    """Heuristic complexity classifier over LLM request messages.

    Returns a float in ``[0.0, 1.0]``:
      * 0.0–0.3: simple (hello, short Q&A, single-fact lookups)
      * 0.3–0.7: medium (analytical, multi-paragraph, code review)
      * 0.7–1.0: complex (math derivations, multi-step proofs, debugging)

    Heuristic features (weighted sum, capped at 1.0):

    +--------------------------------+--------+
    | Feature                        | Weight |
    +================================+========+
    | char_count / 2000 (token proxy)| 0.30   |
    | has_code_block                 | 0.25   |
    | has_inline_code (dense)        | 0.10   |
    | has_math_symbols               | 0.15   |
    | complexity_marker_count        | 0.20   |
    +--------------------------------+--------+
    | simple_marker_dominant         | −0.30  |
    +--------------------------------+--------+

    The exact weights are a defensible starting point; override the entire
    classifier with a custom one for domain-specific routing (legal,
    medical, code-only, etc.).
    """

    # Cap chars at 2000 for the token-proxy feature; longer prompts saturate.
    _CHAR_SATURATION = 2000.0

    def classify(self, messages: list[dict] | str) -> float:
        """Compute complexity score in [0.0, 1.0]."""
        text = self._extract_text(messages)
        if not text:
            return 0.0

        score = 0.0
        # Length proxy
        score += min(len(text) / self._CHAR_SATURATION, 1.0) * 0.30
        # Code presence
        if _CODE_FENCE.search(text):
            score += 0.25
        # Dense inline code (>=3 occurrences)
        if len(_INLINE_CODE.findall(text)) >= 3:
            score += 0.10
        # Math/LaTeX
        if _MATH_SYMBOLS.search(text):
            score += 0.15
        # Reasoning markers
        text_lower = text.lower()
        marker_hits = sum(1 for m in _COMPLEXITY_MARKERS if m in text_lower)
        score += min(marker_hits / 3.0, 1.0) * 0.20
        # Simple-dominant downweight (very short + simple markers)
        if len(text) < 50 and any(s in text_lower for s in _SIMPLE_MARKERS):
            score -= 0.30

        return max(0.0, min(1.0, score))

    @staticmethod
    def _extract_text(messages: list[dict] | str) -> str:
        if isinstance(messages, str):
            return messages
        parts: list[str] = []
        if isinstance(messages, list):
            for m in messages:
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict):
                            t = p.get("text") or p.get("content") or ""
                            if isinstance(t, str):
                                parts.append(t)
        return " ".join(p for p in parts if p)


# ============================================================================
# ModelRegistry — cost / quality / latency profiles
# ============================================================================


@dataclass(frozen=True)
class ModelProfile:
    """Normalized profile in [0, 1] per axis.

    cost: relative pricing (1.0 = most expensive in registry).
    quality: capability score (1.0 = top-tier reasoning).
    latency: response time (1.0 = slowest).
    """

    cost: float
    quality: float
    latency: float


# Anthropic + OpenAI mid-2026 approximate profiles. Normalize per-axis to
# the most-expensive in this baseline (Opus). Override via ModelRegistry()
# constructor for current pricing.
DEFAULT_MODELS: dict[str, ModelProfile] = {
    "claude-haiku-4-5-20251001": ModelProfile(cost=0.05, quality=0.70, latency=0.30),
    "claude-haiku-4-5":           ModelProfile(cost=0.05, quality=0.70, latency=0.30),
    "claude-sonnet-4-20250514":   ModelProfile(cost=0.20, quality=0.90, latency=0.60),
    "claude-sonnet-4":            ModelProfile(cost=0.20, quality=0.90, latency=0.60),
    "claude-opus-4":              ModelProfile(cost=1.00, quality=1.00, latency=1.00),
    "claude-opus-4-7":            ModelProfile(cost=1.00, quality=1.00, latency=1.00),
    "gpt-4o-mini":                ModelProfile(cost=0.03, quality=0.65, latency=0.25),
    "gpt-4o":                     ModelProfile(cost=0.50, quality=0.95, latency=0.70),
    "gpt-4-turbo":                ModelProfile(cost=0.80, quality=0.95, latency=0.80),
}


class ModelRegistry:
    """Registry of available models with Pareto profiles.

    Args:
        models: Initial model→profile map. Defaults to :data:`DEFAULT_MODELS`
            (Anthropic Haiku/Sonnet/Opus + OpenAI gpt-4o family, profiles
            normalized to Opus=1.0 cost). Pass a custom dict to override
            with current pricing or to add provider-specific models.
    """

    def __init__(self, models: dict[str, ModelProfile] | None = None) -> None:
        # `models or DEFAULT` would treat empty-dict as "no override" — use
        # explicit None check so an empty registry stays empty (callers may
        # want this to test fallback behavior).
        self._models: dict[str, ModelProfile] = (
            dict(DEFAULT_MODELS) if models is None else dict(models)
        )

    def add_model(self, name: str, profile: ModelProfile) -> None:
        self._models[name] = profile

    def remove_model(self, name: str) -> None:
        self._models.pop(name, None)

    def get(self, name: str) -> ModelProfile | None:
        return self._models.get(name)

    def all(self) -> dict[str, ModelProfile]:
        return dict(self._models)

    def __contains__(self, name: str) -> bool:
        return name in self._models

    def __len__(self) -> int:
        return len(self._models)


# ============================================================================
# Budget — per-agent SQLite-backed daily USD ceiling
# ============================================================================


class Budget:
    """Per-agent daily USD budget with atomic spend tracking.

    SQLite-backed (same db as chain.db recommended) with the same hardening
    pattern as ``daily_token_budget``: WAL + busy_timeout + atomic
    INSERT-or-UPDATE. UTC date reset.

    Args:
        db_path: SQLite file path.
        daily_limit_usd: Hard ceiling per UTC day.
        agent_id: Logical agent identifier (default ``"default"``).
            Allows tracking multiple agents independently on the same db.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        daily_limit_usd: float,
        agent_id: str = "default",
    ) -> None:
        if daily_limit_usd <= 0:
            raise ValueError(f"daily_limit_usd must be > 0, got {daily_limit_usd}")
        self._db_path = Path(db_path)
        self._daily_limit = float(daily_limit_usd)
        self._agent_id = agent_id
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
                CREATE TABLE IF NOT EXISTS routing_budget (
                    agent_id TEXT NOT NULL,
                    date_utc TEXT NOT NULL,
                    spent_usd REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (agent_id, date_utc)
                )
            """)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _today(self) -> str:
        return datetime.datetime.now(datetime.UTC).date().isoformat()

    def can_spend(self, estimated_cost: float) -> bool:
        """Return True iff ``spent_today + estimated_cost <= daily_limit``."""
        if estimated_cost < 0:
            return False
        return (self.spent_today() + estimated_cost) <= self._daily_limit

    def record_spend(self, actual_cost: float) -> None:
        """Atomically add ``actual_cost`` to today's ledger. Negative costs ignored."""
        if actual_cost <= 0:
            return
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO routing_budget (agent_id, date_utc, spent_usd)
                    VALUES (?, ?, ?)
                    ON CONFLICT(agent_id, date_utc) DO UPDATE
                        SET spent_usd = spent_usd + excluded.spent_usd
                    """,
                    (self._agent_id, self._today(), actual_cost),
                )
                conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def spent_today(self) -> float:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT spent_usd FROM routing_budget WHERE agent_id=? AND date_utc=?",
                (self._agent_id, self._today()),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def remaining(self) -> float:
        return max(0.0, self._daily_limit - self.spent_today())


# ============================================================================
# ParetoRouter — pick model on Pareto frontier
# ============================================================================


class ParetoRouter:
    """Select model on Pareto frontier given a complexity score + optional budget.

    Complexity bands (default thresholds):
      * < 0.30: simple → cheapest model with quality ≥ 0.6
      * 0.30 – 0.70: medium → best (quality / cost) ratio model
      * > 0.70: complex → highest-quality affordable model

    With a :class:`Budget`, the router downgrades to the cheapest model
    that still has quality ≥ 0.6 when budget is exhausted (graceful
    degradation rather than hard-fail; the host can still proceed and
    a separate policy rule can deny if needed).
    """

    SIMPLE_THRESHOLD = 0.30
    COMPLEX_THRESHOLD = 0.70
    MIN_USABLE_QUALITY = 0.60

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        # Explicit None check: an empty ModelRegistry has __len__=0 (falsy),
        # so `registry or ModelRegistry()` would silently substitute defaults
        # when the caller passed an empty registry on purpose.
        self._registry = registry if registry is not None else ModelRegistry()

    def route(
        self,
        complexity: float,
        budget: Budget | None = None,
        estimated_call_cost: float = 0.01,
    ) -> str:
        """Recommend a model name. Returns the cheapest fallback if registry empty."""
        models = self._registry.all()
        if not models:
            raise ValueError("ModelRegistry is empty; cannot route")

        complexity = max(0.0, min(1.0, complexity))

        # Budget exhaustion → cheapest usable model regardless of complexity
        if budget is not None and not budget.can_spend(estimated_call_cost):
            return self._cheapest_usable(models)

        if complexity < self.SIMPLE_THRESHOLD:
            return self._cheapest_usable(models)
        if complexity > self.COMPLEX_THRESHOLD:
            return self._highest_quality(models)
        return self._best_quality_per_cost(models)

    def _cheapest_usable(self, models: dict[str, ModelProfile]) -> str:
        usable = {
            n: p for n, p in models.items() if p.quality >= self.MIN_USABLE_QUALITY
        } or models
        return min(usable.items(), key=lambda kv: kv[1].cost)[0]

    def _highest_quality(self, models: dict[str, ModelProfile]) -> str:
        return max(models.items(), key=lambda kv: kv[1].quality)[0]

    def _best_quality_per_cost(self, models: dict[str, ModelProfile]) -> str:
        # Avoid division by zero
        def ratio(p: ModelProfile) -> float:
            return p.quality / max(p.cost, 1e-6)

        return max(models.items(), key=lambda kv: ratio(kv[1]))[0]


# ============================================================================
# PolicyEngine integration
# ============================================================================


@dataclass(frozen=True)
class RoutingAdvice:
    """Result of a routing-recommendation policy check."""

    requested_model: str
    recommended_model: str
    complexity: float
    suboptimal: bool
    direction: str  # "over-provisioned" | "under-provisioned" | "ok"
    reason: str = ""


def routing_recommendation(
    registry: ModelRegistry | None = None,
    *,
    classifier: TaskClassifier | None = None,
    router: ParetoRouter | None = None,
    mode: str = "warn",
) -> Callable[[dict], Decision]:
    """PolicyEngine rule: warn when the requested model differs from optimal.

    Pure advisory by default (``mode="warn"``); does not block. ``mode="deny"``
    is supported for strict-routing deployments but is uncommon (most users
    want recommendation, not enforcement).

    Args:
        registry: Override default model registry.
        classifier: Override default :class:`TaskClassifier`.
        router: Override default :class:`ParetoRouter`.
        mode: ``"warn"`` (default) or ``"deny"``.

    Raises:
        ValueError: if ``mode`` is invalid.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    reg = registry or ModelRegistry()
    cls = classifier or TaskClassifier()
    rtr = router or ParetoRouter(registry=reg)

    def rule(request: dict) -> Decision:
        requested = request.get("model", "")
        messages = request.get("messages", [])
        complexity = cls.classify(messages)
        recommended = rtr.route(complexity)

        if requested == recommended:
            return Decision.allow()
        if requested not in reg:
            # Unknown model: can't compare profiles fairly
            return Decision.allow()

        req_profile = reg.get(requested)
        rec_profile = reg.get(recommended)
        if req_profile is None or rec_profile is None:
            return Decision.allow()

        direction = (
            "over-provisioned"
            if req_profile.cost > rec_profile.cost
            else "under-provisioned"
        )
        reason = (
            f"Routing: complexity {complexity:.2f} -> "
            f"recommended {recommended} (cost {rec_profile.cost:.2f}, "
            f"quality {rec_profile.quality:.2f}); "
            f"using {requested} (cost {req_profile.cost:.2f}) "
            f"is {direction}"
        )
        if len(reason) > 200:
            reason = reason[:197] + "..."

        if mode == "deny":
            return Decision.deny(rule="routing_recommendation", reason=reason)
        return Decision.warn(rule="routing_recommendation", reason=reason)

    return rule


@dataclass
class _BudgetState:
    """For test fixtures: snapshot Budget state for assertions."""

    daily_limit: float
    spent_today: float
    remaining: float
    agent_id: str
    extra: dict = field(default_factory=dict)
