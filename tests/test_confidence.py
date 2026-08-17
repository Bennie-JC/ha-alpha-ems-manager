"""Learning confidence.

These tests assert *principles* rather than exact percentages: that the score is
bounded, that it grows with history, and that each quality component can pull it
down independently. Pinning the precise output of the formula would make future
tuning impossible, which is the opposite of useful.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.confidence import compute_confidence
from custom_components.alpha_ems_manager.const import SLOTS_PER_DAY

from .synthetic import flat_day, history

REFERENCE = date(2026, 8, 17)

#: Day counts used to assert monotonic growth.
GROWTH_LADDER = (0, 2, 7, 30, 90, 180, 365)


def score(records, reference=REFERENCE, balance=None) -> float:
    """Return just the percentage, for readability."""
    return compute_confidence(records, reference, balance).percent


def perfect(days: int) -> list:
    """Return ``days`` of complete, perfectly consistent history."""
    return history(REFERENCE, days, 10.0)


# -- bounds ------------------------------------------------------------------


@pytest.mark.parametrize("days", GROWTH_LADDER)
def test_the_score_is_always_within_zero_and_one_hundred(days: int) -> None:
    """The published percentage is clamped, whatever the inputs."""
    assert 0.0 <= score(perfect(days)) <= 100.0


def test_no_history_scores_zero() -> None:
    """A brand-new installation claims nothing."""
    assert score([]) == pytest.approx(0.0)


def test_a_single_day_is_still_very_low() -> None:
    """One day of data is not a model."""
    assert score(perfect(1)) < 10.0


# -- growth ------------------------------------------------------------------


def test_confidence_grows_monotonically_with_history() -> None:
    """More good days never lowers the score."""
    scores = [score(perfect(days)) for days in GROWTH_LADDER]
    assert scores == sorted(scores)
    # And it genuinely moves, rather than being flat.
    assert scores[-1] > scores[0] + 50


@pytest.mark.parametrize(
    ("fewer", "more"), [(0, 2), (2, 7), (7, 30), (30, 90), (90, 180)]
)
def test_each_step_up_the_ladder_raises_confidence(fewer: int, more: int) -> None:
    """Every documented milestone is strictly better than the one before."""
    assert score(perfect(more)) > score(perfect(fewer))


def test_the_documented_milestones_land_in_sensible_bands() -> None:
    """Qualitative behaviour matches what the README promises.

    Wide bands on purpose: they encode "very low / usable / good / strong"
    rather than the exact output of the current formula.
    """
    assert score(perfect(2)) < 15.0  # very low
    assert 10.0 < score(perfect(7)) < 40.0  # an initial usable model
    assert 40.0 < score(perfect(30)) < 80.0  # moderate to good
    assert score(perfect(90)) > 80.0  # strong
    assert score(perfect(365)) > 90.0  # mature


# -- quality components ------------------------------------------------------


def test_poor_quarter_coverage_lowers_confidence() -> None:
    """Ninety days of gappy data must not look like ninety days of good data.

    This is the case the formula exists to prevent: a high day count alone
    cannot buy a high score.
    """
    good = perfect(90)
    gappy = history(REFERENCE, 90, 10.0, accepted_intervals=int(SLOTS_PER_DAY * 0.82))

    assert score(gappy) < score(good)


def test_high_variance_lowers_confidence() -> None:
    """An erratic household is genuinely harder to forecast."""
    steady = perfect(60)
    erratic = [
        flat_day(REFERENCE - timedelta(days=offset), 2.0 if offset % 2 else 20.0)
        for offset in range(1, 61)
    ]

    assert score(erratic) < score(steady)


def test_stale_data_lowers_confidence() -> None:
    """A model that stopped receiving data loses confidence over time."""
    current = perfect(60)
    stale = [
        flat_day(REFERENCE - timedelta(days=offset), 10.0) for offset in range(30, 90)
    ]

    assert score(stale) < score(current)


def test_good_energy_balance_helps_and_bad_balance_hurts() -> None:
    """The optional balance check moves the score in the expected direction."""
    records = perfect(60)
    healthy = score(records, balance=1.0)
    unhealthy = score(records, balance=0.1)

    assert unhealthy < healthy


def test_a_missing_balance_signal_is_not_a_penalty() -> None:
    """Users without a full sensor set are not permanently marked down.

    With no balance samples the component is dropped and the remaining weights
    renormalise, so the score sits between the good and bad balance cases rather
    than being dragged toward zero.
    """
    records = perfect(60)
    absent = score(records, balance=None)

    assert score(records, balance=0.1) < absent
    assert absent <= score(records, balance=1.0)


def test_incomplete_days_do_not_count_toward_learned_days() -> None:
    """A day that never gathered enough quarters is not a learned day."""
    thin = history(REFERENCE, 30, 10.0, accepted_intervals=20)
    breakdown = compute_confidence(thin, REFERENCE)

    assert breakdown.learned_days == 0
    assert breakdown.percent == pytest.approx(0.0)


# -- breakdown ---------------------------------------------------------------


def test_the_breakdown_explains_the_score() -> None:
    """Every component is reported so diagnostics can show the derivation."""
    breakdown = compute_confidence(perfect(45), REFERENCE, balance_score=0.9)

    assert breakdown.learned_days == 45
    assert 0.0 <= breakdown.maturity <= 1.0
    assert 0.0 <= breakdown.quality <= 1.0
    assert breakdown.coverage == pytest.approx(1.0)
    assert breakdown.balance == pytest.approx(0.9)
    assert breakdown.percent == pytest.approx(
        100.0 * breakdown.maturity * breakdown.quality
    )


def test_the_breakdown_serialises_for_diagnostics() -> None:
    """``as_dict`` produces plain JSON-safe values."""
    payload = compute_confidence(perfect(10), REFERENCE).as_dict()

    assert set(payload) == {
        "percent",
        "learned_days",
        "maturity",
        "coverage",
        "recency",
        "stability",
        "balance",
        "quality",
        "measured_coverage",
    }
    for value in payload.values():
        assert value is None or isinstance(value, (int, float))
