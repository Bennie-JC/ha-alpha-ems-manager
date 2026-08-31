"""beta.37: the civil-day decomposition, and what it deliberately refuses to split.

**Gate 2.** Two figures a dashboard wants -- what the plan is worth today and what it
is worth tomorrow -- and the interesting part is everything that cannot honestly be
decomposed:

* ``cost_eur`` **is** an exact sum over the plan's intervals, so per-day cash is
  exactly additive;
* ``hold_cost_eur`` is a single walk over the whole horizon with no per-interval
  series, and its trajectory depends on the whole horizon, so the *headline
  advantage* cannot be split at all;
* ``edge_value_eur`` is a credit on the plan's **final** stored energy, so it belongs
  to neither day;
* ``grid_charge_margin_eur`` needs a per-interval field that does not exist, so it is
  published whole-horizon only.

The per-day figures are therefore on a different basis from the state, they carry
``interval`` in their names to say so, and a test below asserts as a **non-equality**
that they do not sum to it -- so a future tidy-up cannot silently force an identity
the mathematics forbids.

The boundary is the single integer ``today_interval_count``, which is 92 on a
spring-forward day, 96 normally and 100 on a fall-back day. beta.37 is the first
economic feature whose correctness depends on it, and the shape fixture stopped
hardcoding 96 for that reason.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.economic import economic_value_summary

from .beta34_shape import DAY_INTERVALS, hour_of, risk_of, solve_at

#: Both days, so there is something on each side of the boundary to decompose.
HEAD, STORED = 28, 8.294


def two_day(day_intervals: int = DAY_INTERVALS, **overrides):
    """Return a payload for a horizon spanning today and tomorrow."""
    solved = solve_at(
        head=HEAD,
        end=2 * day_intervals,
        stored=STORED,
        forecast_risk=risk_of(day_intervals=day_intervals),
        **overrides,
    )
    return solved.outcome, economic_value_summary(
        solved.outcome,
        today_interval_count=day_intervals,
        import_price_eur_kwh=0.32,
        export_price_eur_kwh=0.21,
        tomorrow_prices_known=True,
    )


# ===========================================================================
# additivity, on its own basis
# ===========================================================================


def test_the_two_days_partition_the_plan_exactly() -> None:
    """Every interval is in exactly one day, and none is counted twice or lost."""
    outcome, payload = two_day()

    assert payload["today"]["intervals"] > 0
    assert payload["tomorrow"]["intervals"] > 0
    assert (
        payload["today"]["intervals"] + payload["tomorrow"]["intervals"]
        == payload["horizon_intervals"]
        == len(outcome.desired.intervals)
    )


@pytest.mark.parametrize(
    "field",
    [
        "grid_import_kwh",
        "grid_export_kwh",
        "battery_charge_ac_kwh",
        "battery_discharge_ac_kwh",
    ],
)
def test_the_per_day_energies_sum_to_the_whole_horizon(field: str) -> None:
    """**Equalities, not inequalities.** ``>=`` would pass on a double count."""
    outcome, payload = two_day()
    plan = outcome.desired
    whole = sum(getattr(entry, field) for entry in plan.intervals)

    assert payload["today"][field] + payload["tomorrow"][field] == pytest.approx(
        whole, abs=0.02
    )
    assert whole > 0.0, f"the witness: the plan moved {field}"


def test_the_per_day_cash_sums_to_the_plan_cost() -> None:
    """The one figure that genuinely reconciles, and it reconciles exactly.

    ``cost_eur`` is built as a sum over these same intervals, so import cost minus
    export revenue over both days is the plan's cost. This is the assertion that
    makes the per-day cash figures trustworthy rather than plausible.
    """
    outcome, payload = two_day()
    cash = sum(
        payload[day]["grid_import_cost_eur"] - payload[day]["export_revenue_eur"]
        for day in ("today", "tomorrow")
    )

    assert cash == pytest.approx(outcome.desired.cost_eur, abs=0.02)


def test_the_per_day_switching_fee_sums_to_the_plan_fee() -> None:
    """Apportioned to the day a run *starts* in, and it adds up."""
    outcome, payload = two_day()
    fee = (
        payload["today"]["switching_cost_eur"]
        + (payload["tomorrow"]["switching_cost_eur"])
    )

    assert fee == pytest.approx(outcome.desired.switching_cost_eur, abs=1e-4)
    assert outcome.desired.switching_cost_eur > 0.0, "the witness: a fee was charged"


def test_the_per_day_interval_values_sum_to_the_plan_marginal() -> None:
    """The advantage figure that *is* additive, and the reason the split exists."""
    outcome, payload = two_day()
    whole = -sum(entry.marginal_cost_eur for entry in outcome.desired.intervals)
    both = (
        payload["today_interval_value_eur"] + (payload["tomorrow_interval_value_eur"])
    )

    assert both == pytest.approx(whole, abs=1e-3)


# ===========================================================================
# and what it must not be mistaken for
# ===========================================================================


def test_the_day_split_does_not_sum_to_the_sensor_state() -> None:
    """**Asserted as a non-equality, deliberately.**

    The state is the plan against one whole-horizon ambient walk; the per-day figures
    are each interval against its own leave-the-battery-alone baseline. They are two
    different measurements of the same plan, and forcing them to agree would mean
    breaking one of them. A future refactor that "tidied up" the difference would
    fail here rather than quietly publishing a false identity.

    *Mutation: rename these to ``today_value_eur`` / ``tomorrow_value_eur`` and the
    contract test in ``test_beta37_economic_value`` fails; make them sum to the state
    and this one does.*
    """
    _outcome, payload = two_day()
    both = (
        payload["today_interval_value_eur"] + (payload["tomorrow_interval_value_eur"])
    )

    assert both != pytest.approx(payload["state"], abs=1e-3)
    assert "do NOT sum to decision_advantage_eur" in payload["day_split_rule"]


def test_the_terminal_credit_is_not_apportioned_to_a_day() -> None:
    """A boundary term belongs to neither day, so neither block mentions it."""
    outcome, payload = two_day()

    assert outcome.desired.edge_value_eur > 0.0, "the witness: there is a credit"
    for day in ("today", "tomorrow"):
        assert "edge_value_eur" not in payload[day]
        assert "hold_cost_eur" not in payload[day]
        assert "grid_charge_margin_eur" not in payload[day]
    # It is published once, whole-horizon, where it can be interpreted.
    assert payload["plan"]["edge_value_eur"] == pytest.approx(
        outcome.desired.edge_value_eur, abs=1e-4
    )


def test_an_unknown_day_length_refuses_to_guess() -> None:
    """**Zero means "the caller could not establish the day length".**

    Attributing an interval to a day requires knowing where the day ends, and a
    plausible default would put tomorrow's sale in today's column on a DST day. The
    honest answer is an empty slice with null figures.
    """
    solved = solve_at(head=HEAD, end=192, stored=STORED)
    payload = economic_value_summary(
        solved.outcome, today_interval_count=0, import_price_eur_kwh=0.32
    )

    assert payload["available"] is True, "the headline is unaffected"
    assert payload["today"]["intervals"] == 0
    assert payload["tomorrow"]["intervals"] == 0
    assert payload["today_interval_value_eur"] is None
    assert payload["tomorrow_interval_value_eur"] is None


# ===========================================================================
# the midnight boundary, on all three day lengths
# ===========================================================================


@pytest.mark.parametrize("day_intervals", [92, 96, 100])
def test_the_boundary_is_the_days_own_length(day_intervals: int) -> None:
    """**92, 96 and 100, because a civil day is all three.**

    A spring-forward day has 92 chronological intervals and a fall-back day 100.
    Attributing on a hardcoded 96 would put four of tomorrow's intervals in today on
    a short day, and leave four of today's unattributed on a long one -- silently, and
    twice a year.

    *Mutation: hardcode 96, or use ``<=`` on the boundary, and this fails.*
    """
    outcome, payload = two_day(day_intervals)
    plan = outcome.desired

    today = [e for e in plan.intervals if e.index < day_intervals]
    tomorrow = [e for e in plan.intervals if e.index >= day_intervals]

    assert payload["today"]["intervals"] == len(today)
    assert payload["tomorrow"]["intervals"] == len(tomorrow)
    assert len(today) > 0 and len(tomorrow) > 0, "both sides must be populated"
    # The first interval of tomorrow is the boundary itself, not one either side.
    assert min(e.index for e in tomorrow) == day_intervals
    assert max(e.index for e in today) == day_intervals - 1


@pytest.mark.parametrize("day_intervals", [92, 96, 100])
def test_additivity_survives_both_dst_transitions(day_intervals: int) -> None:
    """The reconciliation that matters, on the two days it could break."""
    outcome, payload = two_day(day_intervals)
    cash = sum(
        payload[day]["grid_import_cost_eur"] - payload[day]["export_revenue_eur"]
        for day in ("today", "tomorrow")
    )

    assert cash == pytest.approx(outcome.desired.cost_eur, abs=0.02)
    assert payload["today"]["intervals"] + payload["tomorrow"]["intervals"] == len(
        outcome.desired.intervals
    )


@pytest.mark.parametrize("day_intervals", [92, 96, 100])
def test_the_clock_mapping_follows_the_days_own_length(day_intervals: int) -> None:
    """``hour_of`` had a hardcoded modulus, and this is why it could not stay.

    An index in the second day of the horizon must map to the clock position it
    really has, which is only true if the modulus is the real day length.
    """
    assert hour_of(0, day_intervals=day_intervals) == 0.0
    assert hour_of(day_intervals, day_intervals=day_intervals) == 0.0
    assert hour_of(day_intervals + 4, day_intervals=day_intervals) == 1.0
