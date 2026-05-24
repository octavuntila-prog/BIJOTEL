"""F3 / Bijuteria #3: AI energy + carbon accounting per LLM call.

"Fiecare token are un cost în wați. Măsoară-l."

Each LLM call burns watts. Each watt has an associated grams-CO2
depending on where the inference ran. Most BIJOTEL-instrumented
stacks already record ``gen_ai.usage.input_tokens`` and
``gen_ai.usage.output_tokens`` per span — this module turns those
two integers into Wh and gCO2, persists them, and surfaces
aggregates ("how much CO2 did v3-atelier produce this week?").

Three layers, each independently testable and swappable:

* :class:`EnergyEstimator` — pure tokens-to-Wh function. Uses
  per-model rates from public benchmarks + Anthropic/OpenAI papers.
  Conservative defaults; overrides via constructor for hosts who
  measured their own datacenter.

* :class:`CarbonCalculator` — Wh to grams CO2 via regional grid
  intensity. Defaults cover US East/West, EU North/West/Central,
  plus a "world average" fallback. Override per region.

* :class:`EnergyTracker` — SQLite-backed accumulator. Same
  hardening pattern as :class:`bijotel.layers.routing.Budget`:
  WAL + busy_timeout + atomic INSERT. UNIQUE index on
  ``span_seq`` makes backfill idempotent. Aggregation queries
  return :class:`EnergySummary` with human-friendly equivalents
  (km driven, phone charges).

* :class:`EnergySpanProcessor` — OTel SpanProcessor. Reads tokens
  + model from span attributes, records via tracker. Crash-isolated
  (same pattern as HmacChainSpanProcessor) — instrumentation
  failures never disturb the host's LLM call path.

* :func:`energy_budget` — :class:`PolicyEngine` rule factory.
  Warns when *today's* accumulated Wh (per agent) crosses the
  configured ceiling. Uses :class:`EnergyTracker` for the daily
  total; reset is implicit (UTC day boundary).

Provenance: BIJOTEL-original, v1.9.0. Closes the last Tier 4 gap
in the catalog.

Honest scope: numbers are *estimates*, not measurements. The
per-1K-tokens rates are public approximations; carbon intensity
varies by hour-of-day on real grids; Anthropic doesn't publish
per-call Wh. So treat these numbers as **directional, not exact** —
useful for "are we trending up?" and "agent A uses N× more than
agent B," not for ISO-14064 reporting.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bijotel.policy.decision import Decision

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import ReadableSpan, Span

BUSY_TIMEOUT_MS = 5000

_LOG = logging.getLogger("bijotel.energy")


# ============================================================================
# EnergyEstimator — tokens → Wh
# ============================================================================


# Wh per 1K tokens (sum of input + output). Conservative public-data
# estimates. Override via EnergyEstimator(rates=...) for hosts that
# benchmarked their own datacenter.
#
# Sources (range, not exact):
#   * Anthropic: no per-call Wh published; estimates derive from
#     parameter count × inference-step energy curves from academic
#     benchmarks (Patterson et al. 2021, Luccioni et al. 2023).
#   * OpenAI: same — derived, not measured.
#
# These are deliberately CONSERVATIVE (high end of plausible range)
# so downstream "how much CO2" doesn't underestimate.
DEFAULT_ENERGY_RATES: dict[str, float] = {
    # Anthropic
    "claude-haiku-4-5":          0.0010,
    "claude-haiku-4-5-20251001": 0.0010,
    "claude-sonnet-4":           0.0030,
    "claude-sonnet-4-20250514":  0.0030,
    "claude-opus-4":             0.0100,
    "claude-opus-4-5":           0.0100,
    "claude-opus-4-6":           0.0100,
    "claude-opus-4-7":           0.0100,
    # OpenAI
    "gpt-4o-mini":               0.0010,
    "gpt-4o":                    0.0050,
    "gpt-4-turbo":               0.0070,
}
# Used when the model isn't in the table — picks the middle band.
DEFAULT_RATE_FALLBACK = 0.0030


class EnergyEstimator:
    """Estimate Wh per LLM call from token counts.

    Args:
        rates: Per-1K-tokens Wh map. Defaults to
            :data:`DEFAULT_ENERGY_RATES`. Pass a custom dict to
            override pricing for a different fleet / new model.
        fallback: Wh per 1K tokens for unknown models. Defaults
            to :data:`DEFAULT_RATE_FALLBACK`.
    """

    def __init__(
        self,
        rates: dict[str, float] | None = None,
        *,
        fallback: float = DEFAULT_RATE_FALLBACK,
    ) -> None:
        if fallback <= 0:
            raise ValueError(f"fallback must be > 0, got {fallback}")
        self._rates = dict(DEFAULT_ENERGY_RATES) if rates is None else dict(rates)
        self._fallback = fallback

    def estimate_wh(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Return estimated Wh for one call.

        Math: ``(tokens_in + tokens_out) / 1000 × rate[model]``.
        Both halves use the same rate — input + output processing on
        modern transformer inference are amortized similarly enough
        that the simplification isn't material at directional precision.

        Negative inputs are treated as 0 (defensive: surfaces of
        instrumentation glitches as "zero energy" rather than as
        negative carbon credit).
        """
        ti = max(0, int(tokens_in))
        to = max(0, int(tokens_out))
        total = ti + to
        rate = self._rates.get(model, self._fallback)
        return (total / 1000.0) * rate

    @property
    def rates(self) -> dict[str, float]:
        return dict(self._rates)


# ============================================================================
# CarbonCalculator — Wh → grams CO2
# ============================================================================


# gCO2 per kWh by region. Values from public grid intensity reports
# (Electricity Maps, IEA 2024-2025 averages). These shift with grid
# mix — re-tune annually for accuracy.
DEFAULT_GRID_INTENSITY: dict[str, float] = {
    "us-east":     380.0,  # PJM / mid-Atlantic
    "us-west":     250.0,  # CAISO + Pacific NW hydro
    "eu-west":     300.0,  # UK / France
    "eu-north":     30.0,  # Sweden / Norway (hydro + nuclear)
    "eu-central":  350.0,  # Germany / Poland
    "asia-pacific": 500.0, # mostly coal-heavy
    "world":       450.0,  # IEA global average
}
DEFAULT_REGION = "us-east"  # Anthropic primary inference region (Virginia)


class CarbonCalculator:
    """Convert Wh to grams CO2 via regional grid carbon intensity.

    Args:
        region: Key into :data:`DEFAULT_GRID_INTENSITY` (e.g.
            ``"us-east"``, ``"eu-north"``). Falls back to
            ``"world"`` if unknown.
        intensity_overrides: Optional dict to override or extend
            :data:`DEFAULT_GRID_INTENSITY` (e.g. site-measured value).
    """

    def __init__(
        self,
        region: str = DEFAULT_REGION,
        *,
        intensity_overrides: dict[str, float] | None = None,
    ) -> None:
        grid = dict(DEFAULT_GRID_INTENSITY)
        if intensity_overrides:
            grid.update(intensity_overrides)
        self._grid = grid
        self._region = region if region in grid else "world"
        self._intensity = grid[self._region]

    @property
    def region(self) -> str:
        return self._region

    @property
    def intensity_g_per_kwh(self) -> float:
        return self._intensity

    def wh_to_co2_grams(self, wh: float) -> float:
        """Return grams CO2 for ``wh`` watt-hours."""
        if wh <= 0:
            return 0.0
        kwh = wh / 1000.0
        return kwh * self._intensity


# ============================================================================
# EnergySummary — aggregate result shape
# ============================================================================


@dataclass(frozen=True)
class ModelEnergy:
    model: str
    calls: int
    tokens: int
    wh: float
    co2_grams: float


@dataclass(frozen=True)
class AgentEnergy:
    agent_id: str
    calls: int
    tokens: int
    wh: float
    co2_grams: float


@dataclass(frozen=True)
class EnergySummary:
    """Aggregate stats from :meth:`EnergyTracker.summary`.

    Includes human-friendly equivalents — most people don't have
    intuition for "0.2 grams CO2" but do for "a few seconds of
    driving" or "1/50th of a phone charge".
    """

    total_calls: int
    total_tokens: int
    total_wh: float
    total_co2_grams: float
    co2_kg: float
    equivalent_km_driven: float  # 1 km gasoline car ≈ 120 gCO2
    equivalent_phone_charges: float  # 1 charge ≈ 10 Wh
    equivalent_kettle_boils: float  # 1 boil ≈ 100 Wh
    per_model: dict[str, ModelEnergy] = field(default_factory=dict)
    per_agent: dict[str, AgentEnergy] = field(default_factory=dict)
    since: str | None = None
    until: str | None = None

    @property
    def has_data(self) -> bool:
        return self.total_calls > 0


# Equivalents — conservative public-data approximations.
G_CO2_PER_KM_GASOLINE = 120.0  # typical European compact, EPA 2024
WH_PER_PHONE_CHARGE = 10.0     # average smartphone 0-100% charge
WH_PER_KETTLE_BOIL = 100.0     # ~1.5L water, induction kettle


def _compute_equivalents(total_wh: float, total_co2_grams: float) -> tuple[float, float, float]:
    """(km_driven, phone_charges, kettle_boils)."""
    return (
        total_co2_grams / G_CO2_PER_KM_GASOLINE,
        total_wh / WH_PER_PHONE_CHARGE,
        total_wh / WH_PER_KETTLE_BOIL,
    )


# ============================================================================
# EnergyTracker — SQLite-backed accumulator
# ============================================================================


class EnergyTracker:
    """Per-call energy log with aggregation queries.

    Args:
        db_path: SQLite file. Recommended: same DB as ``chain.db`` so
            a single file backup covers everything. Tables created on
            first construction.
        estimator: :class:`EnergyEstimator`. Defaults to a fresh
            instance with :data:`DEFAULT_ENERGY_RATES`.
        calculator: :class:`CarbonCalculator`. Defaults to
            ``us-east``.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        estimator: EnergyEstimator | None = None,
        calculator: CarbonCalculator | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._estimator = estimator or EnergyEstimator()
        self._calculator = calculator or CarbonCalculator()
        self._lock = threading.Lock()
        self._init_db()

    @property
    def estimator(self) -> EnergyEstimator:
        return self._estimator

    @property
    def calculator(self) -> CarbonCalculator:
        return self._calculator

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS energy_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ns INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    tokens_in INTEGER NOT NULL DEFAULT 0,
                    tokens_out INTEGER NOT NULL DEFAULT 0,
                    wh REAL NOT NULL,
                    co2_grams REAL NOT NULL,
                    region TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'default',
                    span_seq INTEGER UNIQUE
                )
            """)
            # Indexes for common aggregation queries.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_ts ON energy_log(timestamp_ns)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_model ON energy_log(model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_energy_agent ON energy_log(agent_id)")
            conn.execute("COMMIT")
        finally:
            conn.close()

    def record(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        *,
        timestamp_ns: int | None = None,
        agent_id: str = "default",
        span_seq: int | None = None,
    ) -> tuple[float, float]:
        """Record one call. Returns ``(wh, co2_grams)`` for the host's convenience.

        Idempotent on ``span_seq``: passing the same seq twice INSERTs
        once. Useful for backfill from chain.db (chain.seq is unique).
        ``span_seq=None`` always inserts (live recording).
        """
        wh = self._estimator.estimate_wh(model, tokens_in, tokens_out)
        co2 = self._calculator.wh_to_co2_grams(wh)
        ts = int(timestamp_ns) if timestamp_ns is not None else (
            int(datetime.datetime.now(datetime.UTC).timestamp() * 1e9)
        )

        with self._lock:
            conn = sqlite3.connect(self._db_path, isolation_level=None)
            try:
                conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if span_seq is None:
                        conn.execute(
                            """
                            INSERT INTO energy_log
                                (timestamp_ns, model, tokens_in, tokens_out,
                                 wh, co2_grams, region, agent_id, span_seq)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                            """,
                            (ts, model, int(tokens_in), int(tokens_out),
                             wh, co2, self._calculator.region, agent_id),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO energy_log
                                (timestamp_ns, model, tokens_in, tokens_out,
                                 wh, co2_grams, region, agent_id, span_seq)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (ts, model, int(tokens_in), int(tokens_out),
                             wh, co2, self._calculator.region, agent_id, int(span_seq)),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    with contextlib.suppress(sqlite3.OperationalError):
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return wh, co2

    def summary(
        self,
        *,
        since: int | str | None = None,
        until: int | str | None = None,
        agent_id: str | None = None,
    ) -> EnergySummary:
        """Aggregate stats over a time/agent window.

        Args:
            since: Lower bound (inclusive). Accepts an int ns
                timestamp OR an ISO-8601 string ("2026-05-24T00:00:00Z").
                ``None`` = no lower bound (since epoch).
            until: Upper bound (exclusive). Same shape as ``since``.
            agent_id: Filter to one agent. ``None`` = all agents.
        """
        ts_since = _to_ns(since)
        ts_until = _to_ns(until)

        where_parts: list[str] = []
        params: list = []
        if ts_since is not None:
            where_parts.append("timestamp_ns >= ?")
            params.append(ts_since)
        if ts_until is not None:
            where_parts.append("timestamp_ns < ?")
            params.append(ts_until)
        if agent_id is not None:
            where_parts.append("agent_id = ?")
            params.append(agent_id)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        with sqlite3.connect(self._db_path) as conn:
            agg = conn.execute(
                f"""SELECT COUNT(*), COALESCE(SUM(tokens_in + tokens_out), 0),
                           COALESCE(SUM(wh), 0), COALESCE(SUM(co2_grams), 0)
                    FROM energy_log {where}""",
                params,
            ).fetchone()
            total_calls, total_tokens, total_wh, total_co2 = agg

            per_model: dict[str, ModelEnergy] = {}
            for r in conn.execute(
                f"""SELECT model, COUNT(*), SUM(tokens_in + tokens_out),
                           SUM(wh), SUM(co2_grams)
                    FROM energy_log {where}
                    GROUP BY model""",
                params,
            ).fetchall():
                per_model[r[0]] = ModelEnergy(
                    model=r[0], calls=int(r[1]), tokens=int(r[2] or 0),
                    wh=float(r[3] or 0), co2_grams=float(r[4] or 0),
                )

            per_agent: dict[str, AgentEnergy] = {}
            for r in conn.execute(
                f"""SELECT agent_id, COUNT(*), SUM(tokens_in + tokens_out),
                           SUM(wh), SUM(co2_grams)
                    FROM energy_log {where}
                    GROUP BY agent_id""",
                params,
            ).fetchall():
                per_agent[r[0]] = AgentEnergy(
                    agent_id=r[0], calls=int(r[1]), tokens=int(r[2] or 0),
                    wh=float(r[3] or 0), co2_grams=float(r[4] or 0),
                )

        km, charges, boils = _compute_equivalents(float(total_wh), float(total_co2))
        return EnergySummary(
            total_calls=int(total_calls or 0),
            total_tokens=int(total_tokens or 0),
            total_wh=float(total_wh or 0),
            total_co2_grams=float(total_co2 or 0),
            co2_kg=float(total_co2 or 0) / 1000.0,
            equivalent_km_driven=km,
            equivalent_phone_charges=charges,
            equivalent_kettle_boils=boils,
            per_model=per_model,
            per_agent=per_agent,
            since=_to_iso(ts_since),
            until=_to_iso(ts_until),
        )

    def spent_today_wh(self, *, agent_id: str = "default") -> float:
        """Sum of Wh for the current UTC day, filtered to ``agent_id``."""
        today_start_ns = int(
            datetime.datetime.combine(
                datetime.datetime.now(datetime.UTC).date(),
                datetime.time(0, 0, 0, tzinfo=datetime.UTC),
            ).timestamp() * 1e9
        )
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(wh), 0) FROM energy_log
                   WHERE timestamp_ns >= ? AND agent_id = ?""",
                (today_start_ns, agent_id),
            ).fetchone()
            return float(row[0] or 0)


def _to_ns(v: int | str | None) -> int | None:
    """Accept int ns OR ISO-8601 string; return ns or None."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    # Parse ISO-8601
    try:
        # Strip trailing Z for fromisoformat compatibility
        s = v[:-1] if v.endswith("Z") else v
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return int(dt.timestamp() * 1e9)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Bad timestamp {v!r}: {e}") from e


def _to_iso(ns: int | None) -> str | None:
    """Inverse of :func:`_to_ns` for display."""
    if ns is None:
        return None
    return datetime.datetime.fromtimestamp(ns / 1e9, tz=datetime.UTC).isoformat()


# ============================================================================
# EnergySpanProcessor — record on every span
# ============================================================================


class EnergySpanProcessor:
    """OTel SpanProcessor that records energy per LLM call.

    Crash-isolated: any exception during attribute extraction,
    estimation, or write is caught, logged at WARN, and SUPPRESSED.
    The host's call path never sees an energy-tracking failure.

    Args:
        tracker: :class:`EnergyTracker` to write into.
        agent_id_attr: Span attribute that carries the agent
            identifier (e.g. ``"agent.name"``). When the attribute
            is missing, ``"default"`` is used.

    Note: this class doesn't inherit from
    :class:`opentelemetry.sdk.trace.SpanProcessor` directly to avoid
    pulling OTel into the import chain at module load time. The
    methods match the protocol so registering with a TracerProvider
    works as expected.
    """

    def __init__(
        self,
        tracker: EnergyTracker,
        *,
        agent_id_attr: str = "agent.name",
    ) -> None:
        self._tracker = tracker
        self._agent_attr = agent_id_attr

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """SpanProcessor protocol: no-op on start."""
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """Extract model + tokens from span attrs, record one row."""
        try:
            attrs = span.attributes or {}
            model = (
                attrs.get("gen_ai.request.model")
                or attrs.get("gen_ai.response.model")
                or ""
            )
            if not model:
                return  # not a GenAI span; ignore silently
            ti = int(attrs.get("gen_ai.usage.input_tokens", 0) or 0)
            to = int(attrs.get("gen_ai.usage.output_tokens", 0) or 0)
            if ti == 0 and to == 0:
                return  # no usage data
            agent_id = str(attrs.get(self._agent_attr, "default"))
            self._tracker.record(
                model=str(model),
                tokens_in=ti,
                tokens_out=to,
                timestamp_ns=span.end_time,
                agent_id=agent_id,
            )
        except Exception as e:
            _LOG.warning(
                "energy span record failed (host unaffected): %s: %s",
                type(e).__name__,
                e,
            )

    def shutdown(self) -> None:
        """SpanProcessor protocol: no-op (each record opens its own conn)."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """SpanProcessor protocol: no-op (writes are sync in on_end)."""
        return True


# ============================================================================
# Policy rule
# ============================================================================


def energy_budget(
    *,
    tracker: EnergyTracker,
    daily_limit_wh: float,
    agent_id: str = "default",
    mode: str = "warn",
) -> Callable[[dict], Decision]:
    """PolicyEngine rule: warn/deny when today's Wh exceeds the daily limit.

    Daily totals come from :meth:`EnergyTracker.spent_today_wh`.
    UTC day boundary. When the limit is exceeded, the rule fires
    once per call until midnight (or until the host calls a model
    cheap enough to keep the next slice under threshold).

    Args:
        tracker: :class:`EnergyTracker` instance backing the limit
            check. Same db_path as the host's
            :class:`EnergySpanProcessor`.
        daily_limit_wh: Wh ceiling per UTC day, per ``agent_id``.
        agent_id: Which agent's tally to read. Default
            ``"default"``. Hosts that ran a multi-agent fleet
            install one rule per agent (or write a thin wrapper
            that picks ``agent_id`` from the request dict).
        mode: ``"warn"`` (default) or ``"deny"``.

    Raises:
        ValueError: invalid mode or non-positive limit.
    """
    if mode not in ("warn", "deny"):
        raise ValueError(f"mode must be 'warn' or 'deny', got {mode!r}")
    if daily_limit_wh <= 0:
        raise ValueError(f"daily_limit_wh must be > 0, got {daily_limit_wh}")

    def rule(request: dict) -> Decision:  # noqa: ARG001
        spent = tracker.spent_today_wh(agent_id=agent_id)
        if spent < daily_limit_wh:
            return Decision.allow()
        reason = (
            f"daily energy budget exceeded for {agent_id}: "
            f"{spent:.2f} Wh used vs {daily_limit_wh:.2f} Wh ceiling"
        )
        if mode == "deny":
            return Decision.deny(rule="energy_budget", reason=reason)
        return Decision.warn(rule="energy_budget", reason=reason)

    return rule


__all__ = [
    "AgentEnergy",
    "CarbonCalculator",
    "DEFAULT_ENERGY_RATES",
    "DEFAULT_GRID_INTENSITY",
    "EnergyEstimator",
    "EnergySpanProcessor",
    "EnergySummary",
    "EnergyTracker",
    "ModelEnergy",
    "energy_budget",
]
