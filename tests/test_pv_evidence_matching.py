"""Forecast against actual, classified rather than reduced to a residual.

The whole value of this layer is that it keeps the cases apart. A single number
that folds "the forecast was wrong", "the sensor was down", "it was night", "no
forecast was ever obtained" and "one declared site went quiet" into one figure is
not evidence of anything, and a later phase asked to learn from it would learn
the installation's plumbing rather than the source's accuracy.

Nothing here computes a correction. That is asserted twice: once behaviourally --
a deliberately terrible day must not change the next day's forecast -- and once
structurally.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    PV_FLAG_CLIPPING_SUSPECTED,
    PV_FLAG_SHAPE_MISMATCH,
    PV_FLAG_TIMEZONE_CHANGED,
    PV_STATUS_ACTUAL_MISSING,
    PV_STATUS_FORECAST_MISSING,
    PV_STATUS_NIGHT,
    PV_STATUS_NOT_ELAPSED,
    PV_STATUS_PARTIAL_SITES,
    PV_STATUS_PV_BLIND,
    PV_STATUS_VALID,
)
from custom_components.alpha_ems_manager.pv_forecast import (
    PvForecast,
    PvOutcome,
    PvProvenance,
    PvSnapshot,
    build_pv_snapshot,
    fingerprint_pv,
    pv_error_metrics,
    score_pv_day,
)

TARGET = date(2026, 8, 21)
TZ_KEY = "Europe/Amsterdam"
COUNT = 8
NOW = datetime(2026, 8, 22, 0, 5, tzinfo=UTC)


def snapshot(
    predicted: list[float | None],
    *,
    daylight: list[bool] | None = None,
    sites: int = 1,
    contributing: list[int] | None = None,
    available: bool = True,
) -> PvSnapshot:
    """Return a snapshot over ``COUNT`` intervals."""
    lit = daylight if daylight is not None else [True] * COUNT
    return PvSnapshot(
        issued_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC),
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=COUNT,
        horizon_days=0,
        available=available,
        unavailable_reason=None if available else "solcast_query_failed",
        predicted=tuple(predicted),
        p10=tuple(None if v is None else v / 2 for v in predicted),
        p90=tuple(None if v is None else v * 2 for v in predicted),
        daylight=tuple(lit),
        sites_contributing=tuple(
            contributing if contributing is not None else [sites] * COUNT
        ),
        fingerprint="fp",
        provenance=PvProvenance(selected_site_count=sites),
    )


def score(
    predicted: list[float | None],
    actual: list[float | None],
    **kwargs,
) -> PvOutcome:
    """Score one day."""
    snap = kwargs.pop("snapshot", None) or snapshot(
        predicted,
        **{
            key: kwargs.pop(key)
            for key in ("daylight", "sites", "contributing", "available")
            if key in kwargs
        },
    )
    return score_pv_day(
        snap,
        actual=actual,
        finalized_at=NOW,
        tz_key=TZ_KEY,
        interval_count=COUNT,
        target_day=TARGET,
        **kwargs,
    )


# -- the status codes --------------------------------------------------------


def test_both_sides_present_is_valid() -> None:
    """The only code that is scored."""
    outcome = score([1.0] * COUNT, [0.8] * COUNT)

    assert set(outcome.status) == {PV_STATUS_VALID}
    assert len(outcome.scored_indices) == COUNT


def test_a_missing_forecast_interval_is_not_an_error() -> None:
    """The source declined to describe it, so there is nothing to be wrong about."""
    predicted: list[float | None] = [1.0] * COUNT
    predicted[3] = None

    outcome = score(predicted, [0.8] * COUNT)

    assert outcome.status[3] == PV_STATUS_FORECAST_MISSING
    assert 3 not in outcome.scored_indices


def test_a_missing_actual_is_not_an_error_either() -> None:
    """A sensor outage is not the source's fault."""
    actual: list[float | None] = [0.8] * COUNT
    actual[5] = None

    outcome = score([1.0] * COUNT, actual)

    assert outcome.status[5] == PV_STATUS_ACTUAL_MISSING
    assert 5 not in outcome.scored_indices


def test_a_pv_blind_interval_is_never_scored() -> None:
    """Comparing a forecast that was never obtained manufactures error.

    The same mistake the load-side scoring already refuses for a partly observed
    day: an outage becomes an accuracy figure, and the accuracy figure is then
    indistinguishable from a genuinely bad forecast.
    """
    outcome = score([None] * COUNT, [0.8] * COUNT, available=False)

    assert set(outcome.status) == {PV_STATUS_PV_BLIND}
    assert outcome.scored_indices == ()


def test_no_snapshot_at_all_is_pv_blind_rather_than_a_crash() -> None:
    """A day nobody forecast is a day nobody forecast."""
    outcome = score_pv_day(
        None,
        actual=[0.8] * COUNT,
        finalized_at=NOW,
        tz_key=TZ_KEY,
        interval_count=COUNT,
        target_day=TARGET,
    )

    assert set(outcome.status) == {PV_STATUS_PV_BLIND}


def test_a_partial_site_interval_is_not_scored() -> None:
    """A known under-estimate would charge the source for a shortfall it did
    not cause."""
    contributing = [3] * COUNT
    contributing[2] = 2

    outcome = score([1.0] * COUNT, [0.8] * COUNT, sites=3, contributing=contributing)

    assert outcome.status[2] == PV_STATUS_PARTIAL_SITES
    assert 2 not in outcome.scored_indices
    assert outcome.status[0] == PV_STATUS_VALID


def test_a_single_site_installation_never_reports_partial_coverage() -> None:
    """With one declared site there is no partial sum to be had."""
    contributing = [1] * COUNT
    contributing[2] = 0

    outcome = score([1.0] * COUNT, [0.8] * COUNT, sites=1, contributing=contributing)

    assert PV_STATUS_PARTIAL_SITES not in outcome.status


def test_a_night_interval_where_both_are_zero_is_excluded() -> None:
    """A ratio against roughly zero is meaningless rather than perfect."""
    lit = [True] * COUNT
    lit[0] = lit[1] = False
    predicted: list[float | None] = [1.0] * COUNT
    predicted[0] = predicted[1] = 0.0
    actual: list[float | None] = [0.8] * COUNT
    actual[0] = actual[1] = 0.0

    outcome = score(predicted, actual, daylight=lit)

    assert outcome.status[0] == outcome.status[1] == PV_STATUS_NIGHT
    assert 0 not in outcome.scored_indices


def test_generation_measured_at_night_is_still_scored() -> None:
    """A non-zero reading in the dark is a sensor fault, and hiding it is worse."""
    lit = [True] * COUNT
    lit[0] = False
    predicted: list[float | None] = [1.0] * COUNT
    predicted[0] = 0.0
    actual: list[float | None] = [0.8] * COUNT
    actual[0] = 0.5

    outcome = score(predicted, actual, daylight=lit)

    assert outcome.status[0] == PV_STATUS_VALID


def test_an_interval_that_has_not_happened_is_not_evidence() -> None:
    """Scoring the future would make every partial day look badly forecast."""
    outcome = score([1.0] * COUNT, [0.8] * COUNT, elapsed_intervals=3)

    assert outcome.status[4] == PV_STATUS_NOT_ELAPSED
    assert outcome.status[3] == PV_STATUS_VALID
    assert len(outcome.scored_indices) == 4


def test_the_status_counts_add_up_to_the_day() -> None:
    """Every interval gets exactly one code, so nothing can be silently dropped."""
    predicted: list[float | None] = [1.0] * COUNT
    predicted[1] = None
    actual: list[float | None] = [0.8] * COUNT
    actual[2] = None

    outcome = score(predicted, actual)

    assert sum(outcome.status_counts().values()) == COUNT
    assert len(outcome.status) == COUNT


# -- day flags ---------------------------------------------------------------


def test_a_shape_mismatch_is_flagged() -> None:
    """A snapshot of a different length is not comparable interval by interval."""
    snap = snapshot([1.0] * COUNT)
    outcome = score_pv_day(
        snap,
        actual=[0.8] * 100,
        finalized_at=NOW,
        tz_key=TZ_KEY,
        interval_count=100,
        target_day=TARGET,
    )

    assert PV_FLAG_SHAPE_MISMATCH in outcome.flags


def test_a_timezone_change_is_flagged() -> None:
    """Interval identity is defined in a zone; changing it changes the identity."""
    snap = snapshot([1.0] * COUNT)
    outcome = score_pv_day(
        snap,
        actual=[0.8] * COUNT,
        finalized_at=NOW,
        tz_key="America/New_York",
        interval_count=COUNT,
        target_day=TARGET,
    )

    assert PV_FLAG_TIMEZONE_CHANGED in outcome.flags


def test_clipping_is_flagged_and_the_values_are_untouched() -> None:
    """On a big day the forecast exceeds the actual by design.

    An inverter cannot pass more than its limit however bright it is, so a later
    phase must not learn a correction for that. Raised as a suspicion, never
    applied as an adjustment.
    """
    # A 2 kW limit is 0.5 kWh per quarter-hour. The measured day plateaus there
    # while the forecast keeps climbing.
    outcome = score(
        [0.9] * COUNT,
        [0.5] * COUNT,
        ac_limit_kw=2.0,
    )

    assert PV_FLAG_CLIPPING_SUSPECTED in outcome.flags
    # And nothing was corrected.
    assert outcome.actual[0] == 0.5


def test_clipping_is_not_flagged_without_a_configured_limit() -> None:
    """Suppressed rather than guessed: a guessed ceiling would look like evidence."""
    outcome = score([0.9] * COUNT, [0.5] * COUNT)

    assert PV_FLAG_CLIPPING_SUSPECTED not in outcome.flags


def test_a_day_under_the_limit_is_not_flagged_as_clipped() -> None:
    """An ordinary over-forecast must not be excused as physics."""
    outcome = score([0.9] * COUNT, [0.2] * COUNT, ac_limit_kw=2.0)

    assert PV_FLAG_CLIPPING_SUSPECTED not in outcome.flags


# -- the derived metrics -----------------------------------------------------


def test_the_metrics_score_only_comparable_intervals() -> None:
    """Hand-computed, so a change of denominator cannot pass."""
    predicted: list[float | None] = [1.0, 1.0, 1.0, None]
    actual: list[float | None] = [0.8, 1.2, None, 0.9]
    snap = PvSnapshot(
        issued_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC),
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=4,
        horizon_days=0,
        available=True,
        unavailable_reason=None,
        predicted=tuple(predicted),
        p10=(0.5, 0.5, 0.5, None),
        p90=(2.0, 2.0, 2.0, None),
        daylight=(True,) * 4,
        sites_contributing=(1,) * 4,
        fingerprint="fp",
        provenance=PvProvenance(selected_site_count=1),
    )
    outcome = score_pv_day(
        snap,
        actual=actual,
        finalized_at=NOW,
        tz_key=TZ_KEY,
        interval_count=4,
        target_day=TARGET,
    )

    metrics = pv_error_metrics(snap, outcome)

    assert metrics["scored_intervals"] == 2
    assert metrics["predicted_kwh"] == pytest.approx(2.0)
    assert metrics["actual_kwh"] == pytest.approx(2.0)
    # 0.2 under plus 0.2 over.
    assert metrics["absolute_error_kwh"] == pytest.approx(0.4)
    # And they cancel, which is exactly why the signed figure is kept beside it.
    assert metrics["signed_error_kwh"] == pytest.approx(0.0)


def test_the_signed_error_survives_where_the_absolute_one_hides_a_bias() -> None:
    """The sign is the whole diagnostic.

    A structural conversion difference or clipping biases one way; forecast noise
    does not. Every earlier statistic in this project took an absolute value first
    and threw that distinction away.
    """
    outcome = score([1.0] * COUNT, [0.8] * COUNT)
    metrics = pv_error_metrics(snapshot([1.0] * COUNT), outcome)

    assert metrics["signed_error_kwh"] == pytest.approx(COUNT * 0.2)
    assert metrics["absolute_error_kwh"] == pytest.approx(COUNT * 0.2)


def test_nothing_comparable_reports_incomparable_rather_than_zero_error() -> None:
    """A day with no scored intervals is not a perfectly forecast day."""
    outcome = score([None] * COUNT, [0.8] * COUNT, available=False)
    metrics = pv_error_metrics(snapshot([None] * COUNT, available=False), outcome)

    assert metrics["comparable"] is False
    assert metrics["scored_intervals"] == 0
    assert "absolute_error_kwh" not in metrics


def test_metrics_are_deterministic_for_identical_inputs() -> None:
    """Recomputed on demand, so the same two sides must always give the same answer."""
    snap = snapshot([1.0] * COUNT)
    outcome = score([1.0] * COUNT, [0.8] * COUNT)

    assert pv_error_metrics(snap, outcome) == pv_error_metrics(snap, outcome)


# -- issuance ----------------------------------------------------------------


def forecast_for(intervals: tuple[float | None, ...], **kwargs) -> PvForecast:
    """Return a forecast carrying one series."""
    return PvForecast(
        target_day=TARGET,
        tz_key=TZ_KEY,
        interval_count=len(intervals),
        intervals=intervals,
        p10=intervals,
        p90=intervals,
        daylight=(True,) * len(intervals),
        sites_contributing=(1,) * len(intervals),
        provenance=kwargs.pop("provenance", PvProvenance()),
        **kwargs,
    )


def test_the_same_forecast_fingerprints_identically() -> None:
    """Change-triggered issuance depends on this being stable."""
    once = forecast_for((1.0, 2.0, 3.0))

    assert fingerprint_pv(once) == fingerprint_pv(forecast_for((1.0, 2.0, 3.0)))


def test_a_changed_value_changes_the_fingerprint() -> None:
    """Otherwise a revised forecast would never be recorded."""
    assert fingerprint_pv(forecast_for((1.0, 2.0))) != fingerprint_pv(
        forecast_for((1.0, 2.1))
    )


def test_a_changed_declaration_changes_the_fingerprint_even_at_equal_values() -> None:
    """Same numbers, different roofs: a different claim, and not poolable."""
    a = forecast_for((1.0, 2.0), provenance=PvProvenance(selected_sites_identity="aa"))
    b = forecast_for((1.0, 2.0), provenance=PvProvenance(selected_sites_identity="bb"))

    assert fingerprint_pv(a) != fingerprint_pv(b)


def test_a_changed_source_correction_changes_the_fingerprint() -> None:
    """Dampening or actuals blending turning on is a different series."""
    a = forecast_for((1.0,), provenance=PvProvenance(auto_dampening_active=False))
    b = forecast_for((1.0,), provenance=PvProvenance(auto_dampening_active=True))

    assert fingerprint_pv(a) != fingerprint_pv(b)


def test_a_snapshot_records_the_horizon_it_was_issued_at() -> None:
    """A day-ahead forecast and a same-day one are different claims."""
    built = build_pv_snapshot(
        forecast_for((1.0,)), issued_at=NOW, today=TARGET - timedelta(days=1)
    )

    assert built.horizon_days == 1


# -- storage round trip ------------------------------------------------------


def test_a_snapshot_round_trips_byte_for_byte() -> None:
    """The raw series must survive storage exactly, holes included."""
    original = snapshot([1.0, None, 3.0, 0.0, 5.0, None, 7.0, 8.0])

    restored = PvSnapshot.from_dict(TARGET, original.to_dict())

    assert restored is not None
    assert restored.predicted == original.predicted
    assert restored.p10 == original.p10
    assert restored.p90 == original.p90
    assert restored.daylight == original.daylight
    assert restored.sites_contributing == original.sites_contributing
    assert restored.fingerprint == original.fingerprint
    assert restored.issued_at == original.issued_at


def test_the_percentile_series_is_retained_and_not_reduced_to_a_total() -> None:
    """Irrecoverable after the day passes, and free at fetch time."""
    original = snapshot([1.0] * COUNT)

    restored = PvSnapshot.from_dict(TARGET, original.to_dict())

    assert restored is not None
    assert len([v for v in restored.p10 if v is not None]) == COUNT
    assert len([v for v in restored.p90 if v is not None]) == COUNT


def test_an_outcome_round_trips_with_its_codes_and_flags() -> None:
    """The classification is the evidence, so it has to survive."""
    original = score([1.0, None, 1.0, 1.0], [0.8, 0.8, None, 0.9])

    restored = PvOutcome.from_dict(TARGET, original.to_dict())

    assert restored is not None
    assert restored.status == original.status
    assert restored.actual == original.actual
    assert restored.flags == original.flags


def test_a_damaged_snapshot_document_is_refused_rather_than_guessed() -> None:
    """Never plausible-looking numbers."""
    assert PvSnapshot.from_dict(TARGET, None) is None
    assert PvSnapshot.from_dict(TARGET, {}) is None
    assert PvSnapshot.from_dict(TARGET, {"at": "not-a-time", "n": 8}) is None
    assert PvSnapshot.from_dict(TARGET, {"at": NOW.isoformat(), "n": 0}) is None
    assert PvSnapshot.from_dict(TARGET, {"at": NOW.isoformat()}) is None


def test_a_damaged_series_degrades_to_missing_rather_than_to_zero() -> None:
    """The tri-state has to survive corruption too."""
    restored = PvSnapshot.from_dict(
        TARGET,
        {
            "at": NOW.isoformat(),
            "n": 4,
            "a": 1,
            "p": ["nonsense", None, float("nan"), 0.9],
        },
    )

    assert restored is not None
    assert restored.predicted[0] is None
    assert restored.predicted[2] is None
    assert restored.predicted[3] == 0.9


# -- no adaptive correction --------------------------------------------------


def test_a_terrible_day_does_not_change_the_next_forecast() -> None:
    """The behavioural half of "no adaptive correction".

    The scoring layer is a pure function of the two stored sides. It has no output
    that reaches the forecast, which is what makes this true rather than merely
    currently true.
    """
    disaster = score([5.0] * COUNT, [0.0] * COUNT)
    tomorrow = forecast_for((1.0, 2.0, 3.0))

    assert pv_error_metrics(snapshot([5.0] * COUNT), disaster)["absolute_error_kwh"] > 0
    # The next forecast is what the source returned, mapped, and nothing else.
    assert tomorrow.intervals == (1.0, 2.0, 3.0)
    assert fingerprint_pv(tomorrow) == fingerprint_pv(forecast_for((1.0, 2.0, 3.0)))


def test_the_scoring_layer_has_no_way_to_reach_a_forecast() -> None:
    """The structural half, asserted on what the code can touch.

    ``score_pv_day`` is handed a snapshot and a measured series and returns an
    outcome. It never receives a :class:`PvForecast`, so there is no object it
    could adjust even if someone tried -- and the snapshot it does receive is
    frozen. Asserted on the signature and the frozen-ness rather than by grepping
    for the word "correction", which appears in the docstrings that promise there
    is none.
    """
    import dataclasses
    import inspect

    signature = inspect.signature(score_pv_day)

    assert signature.return_annotation == "PvOutcome"
    assert "PvForecast" not in str(signature)
    # Frozen, so even the snapshot it is given cannot be written back to.
    assert dataclasses.fields(PvSnapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot([1.0] * COUNT).predicted = ()


def test_the_forecast_path_never_reads_a_stored_outcome() -> None:
    """The other direction, which is the one that would actually be a correction.

    Building a forecast must not consult how yesterday went. Asserted against the
    pure module's own names: nothing in the mapping path can reach an outcome or a
    metric.
    """
    import ast
    import inspect

    from custom_components.alpha_ems_manager import pv_forecast as module

    tree = ast.parse(inspect.getsource(module.build_forecast))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for forbidden in ("PvOutcome", "score_pv_day", "pv_error_metrics", "actual"):
        assert forbidden not in names, forbidden
