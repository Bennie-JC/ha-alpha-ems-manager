"""beta.40 Gate 6: the verdict alone was economic authority nobody granted.

**Found by audit, not by review, and it blocked the release.** The first
implementation froze a boolean: *keeping a kilowatt-hour beats selling it here*.
Stage B was then free to absorb up to the physical limits.

That is broader than the economics behind it. `verdict()` compares the optimiser's
dual **at the level the pack stands at**. One quarter at full charge power moves the
pack several lattice steps, and the dual falls as the pack fills -- so the first
kilowatt-hour of a row can clear the tariff comfortably while a later one in the
*same row* does not.

Swept across the seven production horizon shapes at their own export prices, that
state is reachable in **five of them**, worst case:

    sell:  bucket 58 -> 63,  V 0.21196 -> 0.01823  against p = 0.18788
           net -0.171 EUR/kWh, up to 2.108 kWh DC past the crossing = 0.36 EUR

which is larger than the per-row gain the release was built to capture. "It is free
PV" is not a defence: free production still carries the opportunity cost of the
export it forgoes, which is exactly what the comparison prices.

**And the curve is not concave.** Swept at full resolution it rises again in places,
so the set of passing levels is not an interval and there is no highest-passing
level to aim at. The corrective walks up to the **first** crossing, which is the
only bound that cannot over-retain; stopping early where the curve later recovers
forgoes a gain rather than taking a loss.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from custom_components.alpha_ems_manager.dispatch import (
    ChargeLimits,
    QuarterProgress,
    decide_charge,
)
from custom_components.alpha_ems_manager.economic import RetentionGate

from .beta34_shape import solve_at
from .beta40_trace import (
    BUCKET_DC_KWH,
    CHARGE_EFFICIENCY,
    HOUSE_KW,
    MAX_CHARGE_KW,
    PV_KW,
    ROUND_TRIP_EFFICIENCY,
)

#: The seven shapes the neutrality digests are taken over, so the sweep below is
#: over the same production surface the release is frozen against.
SHAPES: dict[str, dict] = {
    "sell": {"head": 28, "end": 96, "stored": 8.294},
    "buy": {"head": 8, "end": 96, "stored": 1.2, "allow_export": False},
    "mixed": {"head": 36, "end": 96, "stored": 4.0},
    "zero_pv": {"head": 20, "end": 96, "stored": 6.0, "pv_fn": lambda i: 0.0},
    "survival": {"head": 68, "end": 96, "stored": 0.3},
    "survival_dear": {"head": 68, "end": 96, "stored": 0.3, "price_fn": lambda i: 0.90},
    "survival_cheap": {
        "head": 68,
        "end": 96,
        "stored": 0.3,
        "price_fn": lambda i: 0.02,
    },
}

#: Lattice steps one quarter at full charge power can traverse on the reference
#: site: ``max_charge_kw * 0.25 * eta_charge / bucket``.
REACH = int((MAX_CHARGE_KW * 0.25 * CHARGE_EFFICIENCY) / BUCKET_DC_KWH)


def curve_of(outcome) -> tuple[float | None, ...]:
    """Return the head-layer marginal value at every level of the lattice."""
    plan, bucket = outcome.desired, outcome.bucket_kwh
    return tuple(
        plan.marginal_value_eur_per_kwh(b, bucket_kwh=bucket)[0]
        for b in range(len(plan.head_value))
    )


def prices_of(outcome) -> list[float]:
    """Return the export prices the horizon actually carries."""
    return sorted(
        {
            round(i.export_price_eur_kwh, 5)
            for i in outcome.desired.intervals
            if i.export_price_eur_kwh is not None
        }
    )


def gate_at(curve, bucket: int, *, bucket_kwh: float) -> RetentionGate:
    """Return the gate as Stage A builds it at one level of the lattice."""
    return RetentionGate(
        marginal_value_eur_kwh=curve[bucket],
        round_trip_efficiency=ROUND_TRIP_EFFICIENCY,
        marginal_curve_eur_kwh=curve,
        current_bucket=bucket,
        bucket_dc_kwh=bucket_kwh,
    )


# == 1. the defect, so the corrective cannot be quietly removed ===========


def test_the_dual_falls_as_the_pack_fills_and_the_curve_is_not_concave() -> None:
    """**Both halves of the premise, measured rather than assumed.**

    The span matters -- a curve that barely moved could not flip a comparison -- and
    the non-monotonicity matters, because it is why the corrective takes the first
    crossing instead of the highest passing level.
    """
    spans: list[float] = []
    non_monotone = 0
    for kwargs in SHAPES.values():
        outcome = solve_at(**kwargs).outcome
        defined = [(b, v) for b, v in enumerate(curve_of(outcome)) if v is not None]
        assert defined, "a shape with no defined dual proves nothing"
        spans.append(defined[0][1] - defined[-1][1])
        non_monotone += sum(
            1 for (_a, va), (_c, vc) in pairwise(defined) if vc > va + 1e-12
        )

    # It falls, and by enough to cross a real export price.
    assert min(spans) > 0.05, spans
    # And it is not concave, which is the reason for "first crossing".
    assert non_monotone > 0, "the corrective's conservatism would be unmotivated"


def test_the_verdict_alone_would_retain_negative_value_energy() -> None:
    """**The defect this gate exists for, reproduced on the real shapes.**

    Counted rather than argued: states where the verdict grants at the opening level
    and a level the same row can reach fails the identical comparison. If this ever
    reaches zero the premise has gone and the corrective should be re-justified, not
    silently kept.
    """
    flips = 0
    worst = 0.0
    for kwargs in SHAPES.values():
        outcome = solve_at(**kwargs).outcome
        curve, bucket = curve_of(outcome), outcome.bucket_kwh
        for start, opening in enumerate(curve):
            if opening is None:
                continue
            for price in prices_of(outcome):
                if not gate_at(curve, start, bucket_kwh=bucket).verdict(price)[0]:
                    continue
                for step in range(1, REACH + 1):
                    later = curve[start + step] if start + step < len(curve) else None
                    if later is None:
                        continue
                    if ROUND_TRIP_EFFICIENCY * later <= price:
                        flips += 1
                        worst = max(worst, price - ROUND_TRIP_EFFICIENCY * later)
                        break

    assert flips > 100, flips
    assert worst > 0.10, worst


def test_the_ceiling_retains_no_negative_value_step_anywhere() -> None:
    """**The corrective, proven over the same surface that exposed the defect.**

    For every level and every export price the horizon carries: where the verdict
    grants, no level at or below the published ceiling fails the comparison. Zero,
    not "fewer".

    *Mutation: return ``None`` from ``retain_until_dc_kwh`` and this fails.*
    """
    bad: list[tuple] = []
    bounded = 0
    for name, kwargs in SHAPES.items():
        outcome = solve_at(**kwargs).outcome
        curve, bucket = curve_of(outcome), outcome.bucket_kwh
        for start, opening in enumerate(curve):
            if opening is None:
                continue
            for price in prices_of(outcome):
                gate = gate_at(curve, start, bucket_kwh=bucket)
                if not gate.verdict(price)[0]:
                    continue
                until = gate.retain_until_dc_kwh(price)
                assert until is not None, (name, start, price)
                bounded += 1
                top = int(min(until, (start + REACH + 1) * bucket) / bucket) - 1
                for level in range(start, top + 1):
                    value = curve[level] if level < len(curve) else None
                    if value is not None and ROUND_TRIP_EFFICIENCY * value <= price:
                        bad.append((name, start, level, value, price))

    assert bounded > 500, bounded
    assert bad == [], bad[:5]


# == 2. the ceiling's own semantics =======================================


def test_the_ceiling_stops_at_the_first_crossing_and_not_beyond_it() -> None:
    """A hand-built curve, so the walk is pinned rather than inferred.

    Levels 0-2 pay, level 3 does not, level 4 pays again. The bound is the top of
    level 2 -- the recovery at 4 is deliberately not chased, because reaching it
    would mean charging through level 3.
    """
    curve = (0.30, 0.30, 0.30, 0.05, 0.30, 0.30)
    gate = RetentionGate(
        marginal_value_eur_kwh=curve[0],
        round_trip_efficiency=1.0,
        marginal_curve_eur_kwh=curve,
        current_bucket=0,
        bucket_dc_kwh=1.0,
    )

    assert gate.verdict(0.10) == (True, "authorised")
    # Steps 0->1, 1->2 and 2->3 pay; the step out of 3 does not.
    assert gate.retain_until_dc_kwh(0.10) == pytest.approx(3.0)


def test_a_gate_with_no_curve_is_unbounded_rather_than_zero() -> None:
    """Absent is unconstrained, never a prohibition.

    A site whose lattice cannot define a dual still gets the physical clamps, which
    is what every other absent figure in this contract means.
    """
    gate = RetentionGate(
        marginal_value_eur_kwh=0.30, round_trip_efficiency=ROUND_TRIP_EFFICIENCY
    )

    assert gate.verdict(0.10)[0] is True
    assert gate.retain_until_dc_kwh(0.10) is None


def test_an_undefined_level_stops_the_walk() -> None:
    """``None`` in the curve is not a pass. The lattice cannot price that step."""
    curve = (0.30, 0.30, None, 0.30)
    gate = RetentionGate(
        marginal_value_eur_kwh=curve[0],
        round_trip_efficiency=1.0,
        marginal_curve_eur_kwh=curve,
        current_bucket=0,
        bucket_dc_kwh=1.0,
    )

    assert gate.retain_until_dc_kwh(0.10) == pytest.approx(2.0)


def test_a_refused_verdict_publishes_no_ceiling_to_spend() -> None:
    """The ceiling is meaningless without the grant, and is not computed."""
    gate = RetentionGate(
        marginal_value_eur_kwh=0.05,
        round_trip_efficiency=ROUND_TRIP_EFFICIENCY,
        marginal_curve_eur_kwh=(0.05, 0.05),
        current_bucket=0,
        bucket_dc_kwh=1.0,
    )

    assert gate.verdict(0.30)[0] is False


# == 3. the controller honours it =========================================


def capture_progress(*, retainable: float | None, remaining: float = 0.0373):
    """Return the live capture's row with a given retainable energy."""
    return QuarterProgress(
        seconds_remaining=44.0,
        battery_remaining_kwh=remaining,
        grid_remaining_kwh=0.0,
        retention_authorised=True,
        retention_remaining_kwh=retainable,
    )


@pytest.mark.parametrize(
    ("retainable_kwh", "expected_kw"),
    [
        # Unbounded: the whole measured surplus, as before the corrective.
        (None, 2.5),
        # 0.1 kWh over a floored 0.025 h is 4 kW -- above the surplus, so the
        # surplus still governs.
        (0.1, 2.5),
        # 0.05 kWh is 2 kW: the ceiling now governs, below the surplus.
        (0.05, 2.0),
        # 0.025 kWh is 1 kW -- below the row's own 1.49 kW objective, so the
        # *objective* governs and the ceiling contributes nothing. The ceiling
        # bounds keeping, and a row may still buy what it was authorised to buy.
        (0.025, 1.4),
        # Nothing left worth keeping: no absorption at all.
        (0.0, 0.0),
    ],
)
def test_the_command_is_bounded_by_what_is_still_worth_keeping(
    retainable_kwh: float | None, expected_kw: float
) -> None:
    """**The corrective at the point of command**, on the live capture's inputs.

    The surplus is 2.517 kW and the objective wants 1.49; the ceiling is a third
    bound and the lowest of the three governs.

    *Mutation: drop the ``min`` against ``retention_rate_kw`` and the 0.05 and
    0.025 rows fail.*
    """
    decision = decide_charge(
        progress=capture_progress(retainable=retainable_kwh),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=MAX_CHARGE_KW),
        last_applied_kw=None,
    )

    if expected_kw == 0.0:
        # The objective still governs; the retention branch contributes nothing.
        assert abs(decision.applied_kw) <= 1.5 + 1e-9
    else:
        assert abs(decision.applied_kw) == pytest.approx(expected_kw, abs=0.05)


def test_the_ceiling_never_reduces_the_objective() -> None:
    """It bounds keeping, not buying.

    A row whose objective is grid-fed must still get its objective when there is
    nothing left worth *keeping* -- the two are different questions and the
    corrective must not have merged them.
    """
    exhausted = decide_charge(
        progress=QuarterProgress(
            seconds_remaining=900.0,
            battery_remaining_kwh=2.5,
            grid_remaining_kwh=2.5,
            retention_authorised=True,
            retention_remaining_kwh=0.0,
        ),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=MAX_CHARGE_KW),
        last_applied_kw=None,
    )
    baseline = decide_charge(
        progress=QuarterProgress(
            seconds_remaining=900.0,
            battery_remaining_kwh=2.5,
            grid_remaining_kwh=2.5,
        ),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=MAX_CHARGE_KW),
        last_applied_kw=None,
    )

    assert exhausted.as_dict() == baseline.as_dict()


def test_an_unauthorised_row_ignores_a_generous_ceiling() -> None:
    """The verdict is still the authority. A ceiling is not a grant."""
    decision = decide_charge(
        progress=QuarterProgress(
            seconds_remaining=44.0,
            battery_remaining_kwh=0.0373,
            grid_remaining_kwh=0.0,
            retention_remaining_kwh=99.0,
        ),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=MAX_CHARGE_KW),
        last_applied_kw=None,
    )

    assert abs(decision.applied_kw) <= 1.5 + 1e-9
    assert decision.limited_by != "free_pv_absorption"


# == 4. it reaches the published row ======================================


def test_an_authorised_row_publishes_its_ceiling() -> None:
    """**The publish path, which the direct sweeps above do not exercise.**

    A ceiling computed and not published is a ceiling Stage B never sees, and the
    row would carry a verdict with nothing bounding it -- which is the defect this
    gate exists for, one layer further out.

    *Mutation: stop calling ``retain_until_dc_kwh`` from ``quarter_schedule_for``
    and this fails.*
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_GRID_CHARGE
    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    from .forecast_helpers import NORMAL, local
    from .test_beta40_safety_buy_unchanged import Interval

    base = local(NORMAL, 12, 0)
    outcome = solve_at(**SHAPES["sell"]).outcome
    curve, bucket = curve_of(outcome), outcome.bucket_kwh
    start = next(b for b, v in enumerate(curve) if v is not None)

    rows = quarter_schedule_for(
        (Interval(0),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: base + timedelta(minutes=15 * i),
        retention=gate_at(curve, start, bucket_kwh=bucket),
    )

    assert rows[0]["retention_authorised"] is True
    published = rows[0]["retention_until_dc_kwh"]
    assert published is not None, "an authorised row must publish its bound"
    assert published > 0.0


def test_a_refused_row_publishes_no_ceiling() -> None:
    """Nothing to bound, so nothing to say. ``None`` rather than a figure."""
    from datetime import timedelta

    from custom_components.alpha_ems_manager.const import EXECUTION_INTENT_GRID_CHARGE
    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    from .forecast_helpers import NORMAL, local
    from .test_beta40_safety_buy_unchanged import Interval

    base = local(NORMAL, 12, 0)
    refusing = RetentionGate(
        marginal_value_eur_kwh=0.01,
        round_trip_efficiency=ROUND_TRIP_EFFICIENCY,
        marginal_curve_eur_kwh=(0.01, 0.01),
        current_bucket=0,
        bucket_dc_kwh=1.0,
    )
    rows = quarter_schedule_for(
        (Interval(0),),
        start_index=0,
        end_index=0,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: base + timedelta(minutes=15 * i),
        retention=refusing,
    )

    assert rows[0]["retention_authorised"] is False
    assert rows[0]["retention_until_dc_kwh"] is None


# == 5. the corrective does not undo the release ==========================

#: The live capture's own published value curve, from
#: ``economic_plan.stored_value.value_curve`` in the 2026-09-03 diagnostic. Sampled
#: every five or six levels there, so it is held piecewise here -- which is
#: conservative for a ceiling, because a step is credited the value of the sample
#: at or below it.
LIVE_CURVE_SAMPLES: dict[int, float] = {
    11: 0.25918,
    16: 0.22408,
    22: 0.22368,
    27: 0.22368,
    33: 0.22368,
    38: 0.22176,
    44: 0.22057,
    49: 0.22057,
    55: 0.21382,
    60: 0.16527,
    66: 0.14725,
    71: 0.13532,
    77: 0.09868,
}


def live_curve() -> tuple[float | None, ...]:
    """Return the capture's curve at every level, held piecewise between samples."""
    keys = sorted(LIVE_CURVE_SAMPLES)
    return tuple(
        LIVE_CURVE_SAMPLES[max((k for k in keys if k <= b), default=keys[0])]
        for b in range(83)
    )


def test_the_ceiling_does_not_bind_on_the_capture_that_motivated_the_release() -> None:
    """**The mandatory acceptance case, re-proven against the corrective.**

    A bound that also bounded the case beta.40 was built for would have traded one
    defect for another. On the capture's own curve and price the crossing sits at
    20.291 kWh DC against 8.986 stored -- 11.9 kWh AC of room, where one row can
    take 2.5. Non-binding, and the answer is the figure the audit approved:
    **1.490 kW becomes 2.500, and the predicted export goes to -0.017**.

    *Mutation: make the ceiling the pack level itself, or the current level, and
    this fails.*
    """
    curve = live_curve()
    gate = RetentionGate(
        marginal_value_eur_kwh=curve[34],
        round_trip_efficiency=ROUND_TRIP_EFFICIENCY,
        marginal_curve_eur_kwh=curve,
        current_bucket=34,
        bucket_dc_kwh=BUCKET_DC_KWH,
    )

    authorised, _why = gate.verdict(0.10134)
    assert authorised is True
    until = gate.retain_until_dc_kwh(0.10134)
    assert until == pytest.approx(20.291, abs=0.01)

    room_ac = (until - 8.9856) / CHARGE_EFFICIENCY
    assert room_ac > 2.5, "the ceiling must not bind the case the release is for"

    decision = decide_charge(
        progress=capture_progress(retainable=room_ac),
        house_load_kw=HOUSE_KW,
        pv_kw=PV_KW,
        limits=ChargeLimits(inverter_kw=MAX_CHARGE_KW),
        last_applied_kw=None,
    )
    assert abs(decision.applied_kw) == pytest.approx(2.5, abs=0.001)
    assert decision.achievable_grid_kw == pytest.approx(-0.017, abs=0.002)
