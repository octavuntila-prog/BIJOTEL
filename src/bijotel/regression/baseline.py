"""Rolling baseline aggregation for regression detection (F12).

Computes summary statistics over a window of recent spans, used by
``RegressionDetector`` as the reference distribution. New observations
are compared against this baseline to detect drift.

Design choices:
- **3 universal dimensions**: ``input_tokens``, ``output_tokens``, ``cost``.
  Cover the most common drift signals without per-domain configuration.
- **Cost computed on-the-fly** from ``DEFAULT_PRICES`` price table — no
  reliance on pre-stored cost values (which BIJOTEL chain doesn't store).
- **Returns None on insufficient data** (<5 samples) — explicit signal,
  not zero-defaults that mask the absence.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from bijotel.policy.prices import DEFAULT_PRICES

# v2.4.0 adds three optional dimensions backed by OTel GenAI semconv
# v1.41 attributes. Existing chains that don't carry these attributes
# return None per span — the detector treats None as "no datapoint"
# (compute_baseline returns None on insufficient samples), so old
# chains run regression on the same three legacy dimensions.
VALID_DIMENSIONS = (
    "input_tokens",
    "output_tokens",
    "cost",
    "cache_ratio",        # v2.4.0: cache_read / (cache_read + input)
    "reasoning_ratio",    # v2.4.0: reasoning_output / (reasoning + output)
    "ttfc_ms",            # v2.4.0: time_to_first_chunk in ms
)
MIN_SAMPLES = 5


@dataclass(frozen=True)
class DimensionStats:
    """Summary statistics for one dimension over a baseline window.

    Attributes:
        dimension: Dimension name (e.g. "input_tokens", "cost").
        sample_count: Actual samples used (>= MIN_SAMPLES, <= window).
        mean: Arithmetic mean.
        stdev: Sample standard deviation. ``0.0`` if all values identical.
        p25: 25th percentile.
        p50: Median.
        p75: 75th percentile.
        iqr: Interquartile range (p75 - p25). ``0.0`` if all values identical.
        min_val: Minimum observed value.
        max_val: Maximum observed value.
    """

    dimension: str
    sample_count: int
    mean: float
    stdev: float
    p25: float
    p50: float
    p75: float
    iqr: float
    min_val: float
    max_val: float


def _extract_dimension_value(
    canonical_body_json: dict, dimension: str
) -> float | None:
    """Extract a single dimension value from a parsed canonical_body dict.

    Returns ``None`` if the value is missing OR if cost cannot be computed
    (model not in price table). Caller should treat ``None`` as "this span
    contributes no datapoint" — NOT zero.
    """
    attrs = canonical_body_json.get("attributes", {})

    if dimension == "input_tokens":
        v = attrs.get("gen_ai.usage.input_tokens")
        return float(v) if v is not None else None

    if dimension == "output_tokens":
        v = attrs.get("gen_ai.usage.output_tokens")
        return float(v) if v is not None else None

    if dimension == "cost":
        model = attrs.get("gen_ai.request.model", "")
        in_tok = attrs.get("gen_ai.usage.input_tokens")
        out_tok = attrs.get("gen_ai.usage.output_tokens")
        if model not in DEFAULT_PRICES or in_tok is None or out_tok is None:
            return None
        prices = DEFAULT_PRICES[model]
        return (in_tok * prices["input"] + out_tok * prices["output"]) / 1000

    # v2.4.0 — OTel GenAI semconv v1.41 dimensions.
    # All three return None when the attribute is absent, so old
    # chains (no cache / reasoning / streaming attrs) simply yield no
    # datapoints and compute_baseline returns None — graceful
    # backward-compat without a try/except wrapper at the caller.

    if dimension == "cache_ratio":
        in_tok = attrs.get("gen_ai.usage.input_tokens")
        cache_read = attrs.get("gen_ai.usage.cache_read.input_tokens")
        if in_tok is None or cache_read is None:
            return None
        denom = float(in_tok) + float(cache_read)
        if denom <= 0:
            return None
        return float(cache_read) / denom

    if dimension == "reasoning_ratio":
        out_tok = attrs.get("gen_ai.usage.output_tokens")
        reasoning = attrs.get("gen_ai.usage.reasoning.output_tokens")
        if out_tok is None or reasoning is None:
            return None
        denom = float(out_tok) + float(reasoning)
        if denom <= 0:
            return None
        return float(reasoning) / denom

    if dimension == "ttfc_ms":
        ttfc = attrs.get("gen_ai.response.time_to_first_chunk")
        return float(ttfc) if ttfc is not None else None

    return None


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile. Input MUST be sorted."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (n - 1) * (p / 100)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def compute_baseline(
    db_path: str | Path,
    dimension: str,
    *,
    window: int = 100,
    filter_model: str | None = None,
    upper_seq: int | None = None,
) -> DimensionStats | None:
    """Compute baseline stats over a rolling window of recent spans.

    Args:
        db_path: Path to BIJOTEL chain.db.
        dimension: One of "input_tokens", "output_tokens", "cost".
        window: Maximum number of recent spans to aggregate.
        filter_model: Optional model name (exact match on
            ``gen_ai.request.model``). Filtering applied BEFORE windowing.
        upper_seq: Optional upper bound on chain seq (exclusive). Useful
            for "compute baseline from spans BEFORE this seq" — ensures
            new observations being evaluated aren't part of their own
            baseline.

    Returns:
        ``DimensionStats`` if at least ``MIN_SAMPLES`` valid samples found,
        ``None`` otherwise (insufficient data signal).

    Raises:
        ValueError: dimension not in VALID_DIMENSIONS.
        FileNotFoundError: db_path does not exist.
    """
    if dimension not in VALID_DIMENSIONS:
        raise ValueError(
            f"dimension must be one of {VALID_DIMENSIONS}, got {dimension!r}"
        )

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"chain.db not found: {db_path}")

    sql = "SELECT canonical_body FROM chain"
    where_clauses = []
    params: list = []
    if upper_seq is not None:
        where_clauses.append("seq < ?")
        params.append(upper_seq)
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY seq DESC"

    values: list[float] = []
    with sqlite3.connect(db_path) as conn:
        for (body_blob,) in conn.execute(sql, params):
            try:
                body = json.loads(
                    body_blob.decode("utf-8") if isinstance(body_blob, bytes)
                    else body_blob
                )
            except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                continue

            # Apply model filter if given
            if filter_model is not None:
                attrs = body.get("attributes", {})
                if attrs.get("gen_ai.request.model") != filter_model:
                    continue

            v = _extract_dimension_value(body, dimension)
            if v is None:
                continue
            values.append(v)
            if len(values) >= window:
                break

    if len(values) < MIN_SAMPLES:
        return None

    sorted_vals = sorted(values)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    p25 = _percentile(sorted_vals, 25)
    p50 = _percentile(sorted_vals, 50)
    p75 = _percentile(sorted_vals, 75)

    return DimensionStats(
        dimension=dimension,
        sample_count=len(values),
        mean=mean,
        stdev=stdev,
        p25=p25,
        p50=p50,
        p75=p75,
        iqr=p75 - p25,
        min_val=min(values),
        max_val=max(values),
    )
