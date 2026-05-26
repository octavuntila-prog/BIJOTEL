"""Chain-integrity anomaly detection (v2.8.0).

Read the chain DB, run six structural checks, return a single
``IntegrityReport`` summarizing what looks off. Pure read — never
writes to the chain.

Detectors:

1. **Sequence gaps** — ``seq[N+1] != seq[N] + 1``.
2. **Timestamp anomalies** — backward jumps, gaps > ``LARGE_GAP_SEC``,
   bursts > ``BURST_THRESHOLD`` entries in a single wall-clock second.
3. **Hash anomalies** — duplicate ``canonical_hash`` (the CAS layer is
   supposed to prevent this; if it slips through, something is wrong).
4. **Provider distribution shift** — first vs second half of the window;
   if any provider's share moves by > ``PROVIDER_SHIFT_PCT`` points,
   flag it.
5. **Rate anomaly** — first-half rate vs second-half rate; flag if the
   ratio is outside ``[1 - RATE_TOL, 1 + RATE_TOL]``.
6. **Rotation boundaries** — recorded for completeness in the report
   but the detector itself is a stub in v2.8.0: detecting "this row
   was sealed under a different HMAC secret" requires a candidate
   key set, which is out of scope. Operators who need this run
   ``verify-continuity`` with the old + new secret instead.

Thresholds live as module-level constants near the top so they're easy
to tune and reason about. Keep them conservative — false positives in a
monitoring loop are a real pain.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------

#: Window where >N entries land inside the same wall-clock second.
#: Above this, the timestamp detector emits a ``burst`` anomaly.
BURST_THRESHOLD = 10

#: Gap (seconds) between consecutive entries above which a
#: ``large_gap`` anomaly is emitted. 1 hour by default — bigger than
#: any realistic in-process pause, small enough to catch outages.
LARGE_GAP_SEC = 3600.0

#: Provider-share movement (in absolute percentage points) above
#: which a ``ProviderShift`` is reported. 20 = a model that was 70%
#: of traffic suddenly being 50% is flagged.
PROVIDER_SHIFT_PCT = 20.0

#: Relative rate tolerance. The detector flags when the
#: second-half rate is outside ``[1-RATE_TOL, 1+RATE_TOL]`` × first-half.
#: 0.5 = a 50% drop or spike is the trigger.
RATE_TOL = 0.5

#: Minimum window size that lets us run the provider/rate detectors
#: at all. Below this, those detectors silently no-op.
MIN_SPLIT_SIZE = 10


# ---------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class SequenceGap:
    """A jump in ``chain.seq`` larger than 1.

    Cause is almost always one of: crashed writer (span dropped on the
    floor), manual ``DELETE FROM chain WHERE seq = ?``, or a SQLite
    file truncation. None of those are normal.
    """

    after_seq: int   # last seq we saw before the gap
    before_seq: int  # first seq we saw after the gap
    missing_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "after_seq": self.after_seq,
            "before_seq": self.before_seq,
            "missing_count": self.missing_count,
        }


@dataclass(frozen=True)
class TimestampAnomaly:
    """A backward timestamp, a large gap, or a burst.

    ``seq`` is the row whose timestamp triggered the anomaly. For
    ``burst`` it is ``0`` and ``detail`` carries the per-second count
    (one anomaly per high-traffic second, since the row that triggers
    a burst isn't uniquely identifiable).
    """

    seq: int
    type: str       # "backward" | "large_gap" | "burst"
    delta_sec: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "delta_sec": round(self.delta_sec, 3),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HashAnomaly:
    """Two rows with the same ``canonical_hash``.

    Under normal operation this is impossible — the canonical body
    includes timestamps and IDs, so identical bodies don't happen. If
    this fires, either the CAS layer was bypassed, the chain was
    manually edited, or there is a serious bug.
    """

    seq: int
    type: str       # "duplicate_canonical"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "detail": self.detail}


@dataclass(frozen=True)
class ProviderShift:
    """First-half-of-window vs second-half provider mix change.

    Useful for catching "we silently switched gateways" or "the
    Anthropic route went down and traffic auto-failed over to xAI"
    without anyone noticing.
    """

    shifts: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"shifts": self.shifts}


@dataclass(frozen=True)
class RateAnomaly:
    """First-half hourly rate vs second-half hourly rate."""

    baseline_rate: float
    current_rate: float
    change_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_rate": self.baseline_rate,
            "current_rate": self.current_rate,
            "change_pct": self.change_pct,
        }


@dataclass(frozen=True)
class RotationBoundary:
    """HMAC secret rotation boundary (v2.8.0: placeholder, see module docstring)."""

    seq: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "detail": self.detail}


@dataclass(frozen=True)
class IntegrityReport:
    """Aggregate of all detectors over a window.

    ``clean`` is the one-line answer: is the chain behaving normally?
    ``anomaly_count`` is the same number you'd see if you tallied every
    anomaly across all six categories. Both are derived properties so
    the dataclass stays a plain frozen record.
    """

    window: int                                     # how many entries we looked at
    first_seq: int | None                           # first seq in the window (None if empty)
    last_seq: int | None                            # last seq in the window
    sequence_gaps: list[SequenceGap] = field(default_factory=list)
    timestamp_anomalies: list[TimestampAnomaly] = field(default_factory=list)
    hash_anomalies: list[HashAnomaly] = field(default_factory=list)
    provider_shift: ProviderShift | None = None
    rate_anomalies: list[RateAnomaly] = field(default_factory=list)
    rotation_boundaries: list[RotationBoundary] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """``True`` iff every detector reported zero anomalies."""
        return (
            not self.sequence_gaps
            and not self.timestamp_anomalies
            and not self.hash_anomalies
            and self.provider_shift is None
            and not self.rate_anomalies
            and not self.rotation_boundaries
        )

    @property
    def anomaly_count(self) -> int:
        """Total number of distinct anomalies across all categories."""
        return (
            len(self.sequence_gaps)
            + len(self.timestamp_anomalies)
            + len(self.hash_anomalies)
            + len(self.rate_anomalies)
            + len(self.rotation_boundaries)
            + (1 if self.provider_shift is not None else 0)
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict form (REST endpoint, --json CLI flag)."""
        return {
            "window": self.window,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "clean": self.clean,
            "anomaly_count": self.anomaly_count,
            "sequence_gaps": [g.to_dict() for g in self.sequence_gaps],
            "timestamp_anomalies": [t.to_dict() for t in self.timestamp_anomalies],
            "hash_anomalies": [h.to_dict() for h in self.hash_anomalies],
            "provider_shift": (
                self.provider_shift.to_dict() if self.provider_shift else None
            ),
            "rate_anomalies": [r.to_dict() for r in self.rate_anomalies],
            "rotation_boundaries": [b.to_dict() for b in self.rotation_boundaries],
        }


# ---------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------


class ChainIntegrityMonitor:
    """Run the six structural detectors over a sliding window of recent entries.

    Construction is cheap; ``analyze()`` is the one method that hits
    the DB. Re-use the same monitor object across cron ticks to keep
    threshold tuning local.

    Args:
        db_path: Path to the BIJOTEL chain SQLite file.
        window: How many recent entries to load (default 100). Larger
            windows catch slower drifts but cost more SQLite work.
    """

    def __init__(self, db_path: str | Path, window: int = 100) -> None:
        self.db_path = Path(db_path)
        self.window = int(window)

    # ----- public ------------------------------------------------

    def analyze(self) -> IntegrityReport:
        """Load the window and run every detector. Returns the aggregate report.

        Raises:
            FileNotFoundError: ``db_path`` does not exist.
        """
        if not self.db_path.exists():
            raise FileNotFoundError(f"chain DB not found: {self.db_path}")

        entries = self._load_window()
        first_seq = entries[0]["seq"] if entries else None
        last_seq = entries[-1]["seq"] if entries else None

        return IntegrityReport(
            window=len(entries),
            first_seq=first_seq,
            last_seq=last_seq,
            sequence_gaps=self._check_sequence_gaps(entries),
            timestamp_anomalies=self._check_timestamps(entries),
            hash_anomalies=self._check_hashes(entries),
            provider_shift=self._check_provider_distribution(entries),
            rate_anomalies=self._check_rate(entries),
            rotation_boundaries=[],  # placeholder — see module docstring
        )

    # ----- loaders -----------------------------------------------

    def _load_window(self) -> list[dict[str, Any]]:
        """Pull the last ``window`` entries, ordered by seq ascending.

        The list returned is small (one dict per row, no canonical
        body bytes — those would balloon memory for nothing). We pull
        ``canonical_body`` as a string only to extract the provider
        attribute; everything else stays as compact integers/strings.
        """
        sql = (
            "SELECT seq, timestamp_ns, canonical_hash, prev_hash, "
            "canonical_body FROM chain ORDER BY seq DESC LIMIT ?"
        )
        out: list[dict[str, Any]] = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, (self.window,)).fetchall()
        # Reverse so the list is seq-ascending (older first) — this
        # matches how every detector below thinks about ordering.
        for row in reversed(rows):
            seq, ts_ns, canonical_hash, prev_hash, canonical_body = row
            provider = "unknown"
            try:
                body_text = (
                    canonical_body.decode("utf-8")
                    if isinstance(canonical_body, bytes)
                    else canonical_body
                )
                body = json.loads(body_text)
                attrs = body.get("attributes", {})
                provider = (
                    attrs.get("gen_ai.provider.name")
                    or attrs.get("gen_ai.system")
                    or "unknown"
                )
            except (json.JSONDecodeError, AttributeError, TypeError):
                # Malformed canonical body — chain verify would catch
                # this; we just record provider as 'unknown' and move
                # on. The hash detector still sees the canonical_hash.
                pass
            out.append({
                "seq": int(seq),
                "timestamp_ns": int(ts_ns),
                "canonical_hash": canonical_hash,
                "prev_hash": prev_hash,
                "provider": provider,
            })
        return out

    # ----- detectors ---------------------------------------------

    def _check_sequence_gaps(
        self, entries: list[dict[str, Any]]
    ) -> list[SequenceGap]:
        gaps: list[SequenceGap] = []
        for i in range(1, len(entries)):
            expected = entries[i - 1]["seq"] + 1
            actual = entries[i]["seq"]
            if actual != expected:
                gaps.append(SequenceGap(
                    after_seq=entries[i - 1]["seq"],
                    before_seq=actual,
                    missing_count=actual - expected,
                ))
        return gaps

    def _check_timestamps(
        self, entries: list[dict[str, Any]]
    ) -> list[TimestampAnomaly]:
        anomalies: list[TimestampAnomaly] = []
        for i in range(1, len(entries)):
            ts_prev = entries[i - 1]["timestamp_ns"]
            ts_curr = entries[i]["timestamp_ns"]
            delta_sec = (ts_curr - ts_prev) / 1e9
            if delta_sec < 0:
                anomalies.append(TimestampAnomaly(
                    seq=entries[i]["seq"],
                    type="backward",
                    delta_sec=delta_sec,
                    detail=(
                        f"timestamp went back {abs(delta_sec):.3f}s "
                        f"vs seq={entries[i - 1]['seq']}"
                    ),
                ))
            elif delta_sec > LARGE_GAP_SEC:
                anomalies.append(TimestampAnomaly(
                    seq=entries[i]["seq"],
                    type="large_gap",
                    delta_sec=delta_sec,
                    detail=(
                        f"{delta_sec / 60:.1f} min gap since "
                        f"seq={entries[i - 1]['seq']}"
                    ),
                ))

        # Burst detection: group rows by their wall-clock second,
        # any second with > BURST_THRESHOLD rows is suspicious.
        by_second: Counter[int] = Counter(
            e["timestamp_ns"] // 1_000_000_000 for e in entries
        )
        for sec, count in by_second.items():
            if count > BURST_THRESHOLD:
                anomalies.append(TimestampAnomaly(
                    seq=0,
                    type="burst",
                    delta_sec=0.0,
                    detail=f"{count} entries in 1 second at epoch {sec}",
                ))

        return anomalies

    def _check_hashes(
        self, entries: list[dict[str, Any]]
    ) -> list[HashAnomaly]:
        anomalies: list[HashAnomaly] = []
        seen: dict[str, int] = {}
        for e in entries:
            h = e["canonical_hash"]
            if not h:
                continue
            if h in seen:
                anomalies.append(HashAnomaly(
                    seq=e["seq"],
                    type="duplicate_canonical",
                    detail=f"same canonical_hash as seq={seen[h]}",
                ))
            else:
                seen[h] = e["seq"]
        return anomalies

    def _check_provider_distribution(
        self, entries: list[dict[str, Any]]
    ) -> ProviderShift | None:
        if len(entries) < 2 * MIN_SPLIT_SIZE:
            return None  # not enough data to split meaningfully

        mid = len(entries) // 2
        baseline = Counter(e["provider"] for e in entries[:mid])
        current = Counter(e["provider"] for e in entries[mid:])

        base_total = float(mid)
        curr_total = float(len(entries) - mid)

        all_providers = set(baseline.keys()) | set(current.keys())
        shifts: dict[str, dict[str, float]] = {}
        for p in all_providers:
            base_pct = baseline[p] / base_total * 100.0
            curr_pct = current[p] / curr_total * 100.0
            if abs(curr_pct - base_pct) > PROVIDER_SHIFT_PCT:
                shifts[p] = {
                    "baseline_pct": round(base_pct, 1),
                    "current_pct": round(curr_pct, 1),
                    "delta_pct": round(curr_pct - base_pct, 1),
                }

        return ProviderShift(shifts=shifts) if shifts else None

    def _check_rate(
        self, entries: list[dict[str, Any]]
    ) -> list[RateAnomaly]:
        if len(entries) < 2 * MIN_SPLIT_SIZE:
            return []  # split-half needs enough rows on each side

        mid = len(entries) // 2
        first_secs = (
            entries[mid - 1]["timestamp_ns"] - entries[0]["timestamp_ns"]
        ) / 1e9
        second_secs = (
            entries[-1]["timestamp_ns"] - entries[mid]["timestamp_ns"]
        ) / 1e9
        if first_secs <= 0 or second_secs <= 0:
            # Either half collapsed to a single instant — can't form a
            # rate. (Almost certainly burst-style traffic that the
            # timestamp detector will also flag.)
            return []

        first_rate = mid / (first_secs / 3600.0)        # entries/hour
        second_rate = (len(entries) - mid) / (second_secs / 3600.0)
        if first_rate <= 0:
            return []

        ratio = second_rate / first_rate
        if abs(ratio - 1.0) > RATE_TOL:
            return [RateAnomaly(
                baseline_rate=round(first_rate, 2),
                current_rate=round(second_rate, 2),
                change_pct=round((ratio - 1.0) * 100.0, 1),
            )]
        return []


# ---------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------


def analyze_chain_integrity(
    db_path: str | Path,
    *,
    window: int = 100,
) -> IntegrityReport:
    """One-liner for the common case: build a monitor and run it.

    Equivalent to:

        >>> ChainIntegrityMonitor(db_path, window=window).analyze()

    Most callers want this. The class form is useful when you need to
    cache thresholds, batch multiple analyses, or subclass the
    detectors.
    """
    return ChainIntegrityMonitor(db_path, window=window).analyze()
