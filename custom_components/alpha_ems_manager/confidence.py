"""Learning-confidence scoring.

The score answers one question: how much should the user trust today's and
tomorrow's forecast right now? It must rise as history accumulates, but it must
never reach a high value on the strength of day count alone -- ninety days of
badly gappy data is not a mature model, and saying otherwise would be a lie the
user cannot detect.

The formula is therefore a product of two independent things::

    confidence = 100 x maturity x quality

``maturity`` saturates with the number of learned days::

    maturity = 1 - exp(-valid_days / 30)

which gives roughly 6% at 2 days, 21% at 7, 63% at 30, 95% at 90 and 99.8% at
180. ``quality`` is a weighted mean of four bounded components -- coverage,
recency, stability and energy balance -- each in 0..1. Because the two are
multiplied, a perfect-quality model still cannot look mature early, and a mature
model with poor data cannot look trustworthy.

Any component without data is dropped and the remaining weights renormalise,
so a user who never configured PV or grid entities is not permanently penalised
for the missing energy-balance check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .const import (
    CONFIDENCE_DAYS_TAU,
    CONFIDENCE_QUALITY_WEIGHTS,
    CONFIDENCE_RECENCY_DAYS,
)
from .storage import DayRecord


@dataclass(slots=True)
class ConfidenceBreakdown:
    """The full derivation of a confidence score, for diagnostics."""

    percent: float
    learned_days: int
    maturity: float
    coverage: float | None
    recency: float | None
    stability: float | None
    balance: float | None
    quality: float
    #: Measured-data coverage. Reported alongside baseline coverage so a gap can
    #: be attributed to the right source; not part of the weighted score.
    measured_coverage: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        """Return a plain mapping for the diagnostics payload."""
        return {
            "percent": round(self.percent, 1),
            "learned_days": self.learned_days,
            "maturity": round(self.maturity, 4),
            "coverage": None if self.coverage is None else round(self.coverage, 4),
            "recency": None if self.recency is None else round(self.recency, 4),
            "stability": (None if self.stability is None else round(self.stability, 4)),
            "balance": None if self.balance is None else round(self.balance, 4),
            "quality": round(self.quality, 4),
            "measured_coverage": (
                None
                if self.measured_coverage is None
                else round(self.measured_coverage, 4)
            ),
        }


def _maturity(learned_days: int) -> float:
    """Return the saturating day-count component."""
    if learned_days <= 0:
        return 0.0
    return 1.0 - math.exp(-learned_days / CONFIDENCE_DAYS_TAU)


def _coverage(records: list[DayRecord]) -> float | None:
    """Return the fraction of real intervals carrying a valid *baseline*.

    Baseline coverage, not measured coverage: the forecast is built from
    baseline, so that is the quantity whose completeness should govern trust. A
    configured EV sensor that keeps dropping out lowers this even while the
    measured history stays perfect.
    """
    expected = sum(record.interval_count for record in records)
    if expected <= 0:
        return None
    valid = sum(record.baseline_valid_count for record in records)
    return min(1.0, valid / expected)


def _measured_coverage(records: list[DayRecord]) -> float | None:
    """Return the fraction of real intervals carrying a measured reading.

    Reported for diagnostics only. Comparing it against baseline coverage is
    what tells a user whether a gap came from the house-load source or from the
    flexible-load source.
    """
    expected = sum(record.interval_count for record in records)
    if expected <= 0:
        return None
    valid = sum(record.measured_valid_count for record in records)
    return min(1.0, valid / expected)


def _recency(records: list[DayRecord], reference: date) -> float | None:
    """Return how much of the last week is present.

    A model built on a solid month that then stopped receiving data should not
    keep claiming the confidence it had while it was current.
    """
    window_start = reference - timedelta(days=CONFIDENCE_RECENCY_DAYS)
    recent = {
        record.day
        for record in records
        if window_start <= record.day < reference and record.is_learned
    }
    return len(recent) / CONFIDENCE_RECENCY_DAYS


def _stability(records: list[DayRecord]) -> float | None:
    """Return 1 minus the coefficient of variation of the daily totals.

    A household whose daily consumption swings wildly is genuinely harder to
    forecast, and the score should say so.
    """
    totals = [
        record.baseline_total_kwh for record in records if record.baseline_total_kwh > 0
    ]
    if len(totals) < 2:
        return None
    mean = sum(totals) / len(totals)
    if mean <= 0:
        return None
    variance = sum((value - mean) ** 2 for value in totals) / (len(totals) - 1)
    cv = math.sqrt(variance) / mean
    return max(0.0, min(1.0, 1.0 - cv))


def compute_confidence(
    records: list[DayRecord],
    reference: date,
    balance_score: float | None = None,
) -> ConfidenceBreakdown:
    """Return the confidence breakdown for the learned history in ``records``.

    ``records`` should already be filtered to learned days; ``reference`` is
    today. The result is clamped to 0..100.
    """
    learned = [record for record in records if record.is_learned]
    learned_days = len(learned)
    maturity = _maturity(learned_days)

    components: dict[str, float | None] = {
        "coverage": _coverage(learned),
        "recency": _recency(learned, reference),
        "stability": _stability(learned),
        "balance": balance_score,
    }

    weighted = 0.0
    weight_total = 0.0
    for name, value in components.items():
        if value is None:
            continue
        weight = CONFIDENCE_QUALITY_WEIGHTS[name]
        weighted += weight * max(0.0, min(1.0, value))
        weight_total += weight

    # With no quality signal at all there is nothing to discount by, so maturity
    # stands alone rather than collapsing the score to zero.
    quality = weighted / weight_total if weight_total > 0 else 1.0
    percent = max(0.0, min(100.0, 100.0 * maturity * quality))

    return ConfidenceBreakdown(
        percent=percent,
        learned_days=learned_days,
        maturity=maturity,
        coverage=components["coverage"],
        recency=components["recency"],
        stability=components["stability"],
        balance=components["balance"],
        quality=quality,
        measured_coverage=_measured_coverage(learned),
    )
