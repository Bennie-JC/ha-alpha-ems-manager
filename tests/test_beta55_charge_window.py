"""The charge window says which hours it buys in.

A nine-hour "charge campaign" was never nine hours of buying. `_resolved_run_state`
makes a production-absorption quarter **transparent to a charge run and to nothing
else**, with no bound on how many may pass, so one morning purchase, hours of free
solar and one afternoon purchase are grouped as a single campaign -- one run, one
switching fee. Every figure published about it is then a battery-side aggregate: the
span, the objective, and the beta.53 projection instants alike.

Measured on the reference shape through the production solver: a 09:00-15:45 charge
campaign moving **14.722 kWh**, of which **1.493 kWh was bought and 13.229 kWh
arrived free** -- 90 % absorption -- across **five** separate stretches of buying.

**So the correction is a block, not a span, and that distinction is the release.**
Publishing the first purchase to the last would have restated the same misreading one
size smaller: on that campaign it reads 09:00-15:45, the whole thing, while the first
real stretch of buying is a single quarter. Each published block therefore ends at the
first quarter that buys nothing, and a later stretch stays a later stretch.

The optimiser is untouched, and the second half of this file is why: its objective
contains no term depending on interval count, run duration or power density, so for a
given charge energy it already buys in the cheapest quarters. Those tests characterise
a property that already holds, so a future change cannot quietly lose it.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    MIN_EXECUTABLE_QUARTER_KWH,
)
from custom_components.alpha_ems_manager.coordinator import (
    grid_purchase_blocks,
    plan_projection,
)

from .beta32_harness import LIMITS, solve_shape
from .test_beta53_projections import FakeLimits, interval, moment_of

DAY = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)


def at(quarter: int) -> datetime:
    """Return the instant a quarter index opens."""
    return DAY + timedelta(minutes=15 * quarter)


def row(quarter: int, *, grid: float, battery: float = 1.0, refused: str | None = None):
    """Return one published quarter row."""
    return {
        "start": at(quarter).isoformat(),
        "end": at(quarter + 1).isoformat(),
        "not_executable": refused,
        "battery_kwh": battery,
        "grid_authorised_kwh": grid,
    }


def target(*rows, campaign="c-1", intent=EXECUTION_INTENT_GRID_CHARGE):
    """Return one published execution target holding those rows."""
    return {
        "campaign_id": campaign,
        "intent": intent,
        "quarter_schedule": list(rows),
    }


# ===========================================================================
# the block walk
# ===========================================================================


def test_one_contiguous_block_is_published_whole() -> None:
    """Four adjacent purchasing quarters are one block, first start to last end."""
    blocks = grid_purchase_blocks([target(*(row(q, grid=0.5) for q in range(28, 32)))])

    assert blocks == ((at(28), at(32)),)


def test_two_blocks_separated_by_absorption_are_two_blocks() -> None:
    """**The correction, and the shape the release exists for.**

    07:15 buys, 07:30-15:45 absorbs, 16:00 buys. A first-to-last span would call
    07:15-16:15 a purchase window -- nine hours to describe two quarters of buying.
    """
    rows = [row(29, grid=0.5)]
    rows += [row(q, grid=0.0) for q in range(30, 63)]
    rows += [row(64, grid=0.5)]

    blocks = grid_purchase_blocks([target(*rows)])

    assert blocks == ((at(29), at(30)), (at(64), at(65)))
    # The first block is one quarter, and emphatically not the campaign.
    assert blocks[0][1] == at(30)
    assert blocks[0][1] != at(65)


def test_absorption_before_the_first_purchase_does_not_move_the_start() -> None:
    """The block begins where the buying begins, not where the campaign does."""
    rows = [row(q, grid=0.0) for q in range(28, 40)] + [row(40, grid=0.4)]

    assert grid_purchase_blocks([target(*rows)]) == ((at(40), at(41)),)


def test_absorption_after_the_final_purchase_does_not_extend_the_end() -> None:
    """The mirror case, which is the shape actually observed in production."""
    rows = [row(28, grid=0.4)] + [row(q, grid=0.0) for q in range(29, 63)]

    assert grid_purchase_blocks([target(*rows)]) == ((at(28), at(29)),)


def test_a_sub_floor_import_forms_no_block() -> None:
    """One tenth of a kilowatt for a quarter is the smallest an actuator expresses.

    Below it a marginal import is arithmetic, not a purchase anybody could act on.
    """
    below = MIN_EXECUTABLE_QUARTER_KWH / 2

    assert grid_purchase_blocks([target(row(28, grid=below))]) == ()


def test_a_sub_floor_import_does_not_join_two_blocks() -> None:
    """**And that is the sharper half of the rule.**

    A quarter buying nothing worth acting on must break the block rather than bridge
    it, or one sub-floor kilowatt-hour would glue two genuinely separate stretches
    into a span again.
    """
    rows = [
        row(28, grid=0.5),
        row(29, grid=MIN_EXECUTABLE_QUARTER_KWH / 2),
        row(30, grid=0.5),
    ]

    assert grid_purchase_blocks([target(*rows)]) == (
        (at(28), at(29)),
        (at(30), at(31)),
    )


def test_a_refused_row_can_never_buy() -> None:
    """A row nothing dispatches in cannot take energy off the grid."""
    rows = [
        row(28, grid=0.5),
        row(29, grid=0.5, refused="below_actuator_resolution"),
        row(30, grid=0.5),
    ]

    assert grid_purchase_blocks([target(*rows)]) == (
        (at(28), at(29)),
        (at(30), at(31)),
    )


def test_adjacency_is_judged_on_instants_not_list_position() -> None:
    """Two rows adjacent in the list but not in time are two blocks."""
    rows = [row(28, grid=0.5), row(44, grid=0.5)]

    blocks = grid_purchase_blocks([target(*rows)])

    assert len(blocks) == 2


def test_quarters_in_different_targets_still_form_one_block() -> None:
    """**One campaign is published as one target per run.**

    So two purchasing quarters can be adjacent in time while sitting in different
    ``quarter_schedule`` lists, and a walk over one list at a time would split them.
    """
    blocks = grid_purchase_blocks(
        [target(row(29, grid=0.5)), target(row(28, grid=0.5))]
    )

    assert blocks == ((at(28), at(30)),)


def test_an_export_target_contributes_no_purchase_block() -> None:
    """A sale buys nothing, whatever its rows say."""
    rows = [row(76, grid=0.5), row(77, grid=0.5)]

    assert (
        grid_purchase_blocks([target(*rows, intent=EXECUTION_INTENT_NET_EXPORT)]) == ()
    )


def test_the_campaign_filter_narrows_the_walk() -> None:
    """The per-campaign count needs one campaign; the projection wants them all."""
    targets = [
        target(row(28, grid=0.5), campaign="c-1"),
        target(row(40, grid=0.5), campaign="c-2"),
    ]

    assert len(grid_purchase_blocks(targets)) == 2
    assert grid_purchase_blocks(targets, campaign_id="c-1") == ((at(28), at(29)),)
    assert grid_purchase_blocks(targets, campaign_id="c-2") == ((at(40), at(41)),)


# ===========================================================================
# the published pair
# ===========================================================================


def project(targets, *, head: int = 0):
    """Run the projection over a horizon starting at ``head``."""
    intervals = tuple(interval(head + step, start=7.0) for step in range(96 - head))
    return plan_projection(
        intervals=intervals,
        plan_end_energy_dc_kwh=7.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=targets,
        moment=moment_of,
        now=moment_of(head),
    )


def test_the_projection_publishes_only_the_first_block() -> None:
    """**The headline.** A broad charging span beside a narrow purchase block."""
    rows = [row(29, grid=0.5)]
    rows += [row(q, grid=0.0) for q in range(30, 63)]
    rows += [row(64, grid=0.5)]

    payload = project([target(*rows)])

    assert payload["next_grid_purchase_at"] == at(29).isoformat()
    assert payload["next_grid_purchase_end_at"] == at(30).isoformat()
    # And never the far end of the campaign.
    assert payload["next_grid_purchase_end_at"] != at(65).isoformat()


def test_a_pure_absorption_campaign_publishes_no_next_purchase() -> None:
    """It buys nothing, so there is no next purchase -- not a zero-length one."""
    rows = [row(q, grid=0.0) for q in range(28, 63)]

    payload = project([target(*rows)])

    assert payload["next_grid_purchase_at"] is None
    assert payload["next_grid_purchase_end_at"] is None


def test_a_passed_block_leaves_the_horizon_and_the_next_becomes_next() -> None:
    """**No clock comparison is written, and this is why one is not needed.**

    The horizon head advances past a finished block, so the rows that describe it are
    simply no longer published and the following block is the first one left.
    """
    early = [target(row(29, grid=0.5))]
    later = [target(row(64, grid=0.5))]

    first = project(early + later)
    assert first["next_grid_purchase_at"] == at(29).isoformat()

    # The next refresh publishes only what is still ahead.
    second = project(later, head=40)
    assert second["next_grid_purchase_at"] == at(64).isoformat()


def test_an_unavailable_projection_still_has_the_two_keys() -> None:
    """Absent and missing must not be different shapes to a reader."""
    payload = plan_projection(
        intervals=(),
        plan_end_energy_dc_kwh=0.0,
        floor_energy_kwh=2.0,
        limits=FakeLimits(),
        targets=(),
        moment=moment_of,
        now=moment_of(0),
    )

    assert payload["next_grid_purchase_at"] is None
    assert payload["next_grid_purchase_end_at"] is None


def test_no_new_projection_value_is_an_array_or_a_mapping() -> None:
    """Recorder writes every attribute on every state change."""
    payload = project([target(row(29, grid=0.5))])

    for key in ("next_grid_purchase_at", "next_grid_purchase_end_at"):
        assert not isinstance(payload[key], (list, tuple, dict))


# ===========================================================================
# the campaign totals
# ===========================================================================


def campaigns_for(targets):
    """Return the published campaign rows for those targets."""
    from types import SimpleNamespace

    from custom_components.alpha_ems_manager.sensor import _upcoming_campaigns

    return _upcoming_campaigns(SimpleNamespace(execution_targets=targets))


def charge_target(*rows, campaign="c-1", source="mixed"):
    """Return a charge target carrying a per-run charge source."""
    entry = target(*rows, campaign=campaign)
    entry["charge_source"] = source
    return entry


def test_the_two_energies_sum_to_the_published_objective() -> None:
    """``==``, because the split must account for the objective exactly."""
    rows = [row(28, grid=0.4, battery=1.0), row(29, grid=0.0, battery=1.0)]

    published = campaigns_for([charge_target(*rows)])[0]

    assert published["grid_purchase_kwh"] == pytest.approx(0.4)
    assert published["production_charge_kwh"] == pytest.approx(1.6)
    assert published["grid_purchase_kwh"] + published[
        "production_charge_kwh"
    ] == pytest.approx(published["objective_kwh"])


def test_the_totals_span_every_block_while_the_window_covers_one() -> None:
    """**The two questions stay distinct, and this is the test that says so.**"""
    rows = [row(28, grid=0.5), row(40, grid=0.5), row(60, grid=0.5)]

    published = campaigns_for([charge_target(*rows)])[0]

    assert published["grid_purchase_blocks"] == 3
    assert published["grid_purchase_kwh"] == pytest.approx(1.5)


def test_a_pure_absorption_campaign_reports_no_purchase_and_all_production() -> None:
    """Free solar throughout: nothing bought, no blocks, production as the source."""
    rows = [row(q, grid=0.0, battery=1.0) for q in range(28, 32)]

    published = campaigns_for([charge_target(*rows, source="production")])[0]

    assert published["grid_purchase_kwh"] == 0.0
    assert published["production_charge_kwh"] == pytest.approx(4.0)
    assert published["grid_purchase_blocks"] == 0
    assert published["charge_source"] == "production"


def test_runs_that_disagree_about_their_source_make_the_campaign_mixed() -> None:
    """Combined from the per-run verdicts rather than recomputed from thresholds."""
    published = campaigns_for(
        [
            charge_target(row(28, grid=0.9, battery=1.0), source="grid"),
            charge_target(row(40, grid=0.0, battery=1.0), source="production"),
        ]
    )[0]

    assert published["charge_source"] == "mixed"


def test_an_export_campaign_publishes_no_purchase_figure() -> None:
    """Absent, not zero: a sale never had a purchase to report."""
    rows = [row(76, grid=0.0, battery=1.0)]

    published = campaigns_for([target(*rows, intent=EXECUTION_INTENT_NET_EXPORT)])[0]

    assert published["grid_purchase_kwh"] is None
    assert published["objective_boundary"] == "meter"


def test_the_energy_counts_every_bought_kwh_while_the_count_needs_a_window() -> None:
    """**The two thresholds differ on purpose, and this pins it.**

    ``grid_purchase_kwh`` is an energy: every kilowatt-hour the campaign takes off
    the grid is real and belongs in it, which is also what makes it sum with
    ``production_charge_kwh`` to the objective exactly. ``grid_purchase_blocks``
    counts *windows worth acting on*, so it applies the actuator floor.

    A campaign that buys only dribbles therefore reports a small energy and no
    blocks -- which is the honest pair, not a contradiction: it bought a little, in
    no stretch anybody could dispatch against.
    """
    below = MIN_EXECUTABLE_QUARTER_KWH / 2
    rows = [row(28, grid=below, battery=1.0), row(29, grid=below, battery=1.0)]

    published = campaigns_for([charge_target(*rows, source="production")])[0]

    # Published at the same two decimals as every other kWh on this entity, so
    # 0.025 reads as 0.03 -- and the identity below is what a reader can add up.
    assert published["grid_purchase_kwh"] > 0.0
    assert published["grid_purchase_blocks"] == 0
    assert published["grid_purchase_kwh"] + published[
        "production_charge_kwh"
    ] == pytest.approx(published["objective_kwh"])


def test_no_new_campaign_value_is_an_array_or_a_mapping() -> None:
    """The campaign rows are already a list; their values must stay scalar."""
    published = campaigns_for([charge_target(row(28, grid=0.5))])[0]

    for key in (
        "grid_purchase_kwh",
        "production_charge_kwh",
        "grid_purchase_blocks",
        "charge_source",
    ):
        assert not isinstance(published[key], (list, tuple, dict))


# ===========================================================================
# characterisation: the optimiser already concentrates
# ===========================================================================


def load(index: int) -> float:
    """A day of about 22.6 kWh, with a morning shoulder and an evening peak."""
    hour = (index % 96) / 4.0
    return max(
        0.02,
        0.13
        + 0.30 * math.exp(-((hour - 7.5) ** 2) / 2.0)
        + 0.50 * math.exp(-((hour - 19.0) ** 2) / 4.0),
    )


def no_pv(index: int) -> float:
    return 0.0


def cheap_block(cheap_hours: tuple[float, float], cheap: float = 0.06):
    """Return a price shape with one cheap window and an evening peak."""

    def price(index: int) -> float:
        hour = (index % 96) / 4.0
        if cheap_hours[0] <= hour < cheap_hours[1]:
            return cheap
        if 17.0 <= hour < 21.0:
            return 0.34
        return 0.24

    return price


def test_the_purchase_lands_in_the_cheap_block() -> None:
    """**Characterisation.** Nothing spreads a purchase; price alone places it.

    The objective is exactly linear in per-interval energy at a per-interval price,
    with no per-interval fixed cost, so for a given charge energy the cheapest
    quarters are strictly best. This pins that.
    """
    plan = solve_shape(
        load_fn=load,
        pv_fn=no_pv,
        price_fn=cheap_block((11.5, 13.5)),
        n=96,
        stored=0.26 * LIMITS.capacity_kwh,
    ).desired

    bought = [
        entry
        for entry in plan.intervals
        if max(0.0, entry.marginal_grid_import_kwh) >= MIN_EXECUTABLE_QUARTER_KWH
    ]
    assert bought, "the fixture must buy something for this to mean anything"
    hours = [(entry.index % 96) / 4.0 for entry in bought]
    # Every purchase sits in the cheap window, or in the pre-dawn shoulder that is
    # cheaper than the evening it displaces. Never in the evening peak.
    assert max(hours) < 17.0


def test_a_large_price_gap_is_not_crossed_for_contiguity() -> None:
    """Two cheap blocks, an expensive gap: the gap is not bought merely to join them."""

    def price(index: int) -> float:
        hour = (index % 96) / 4.0
        if 10.0 <= hour < 11.0 or 13.0 <= hour < 14.0:
            return 0.05
        if 17.0 <= hour < 21.0:
            return 0.34
        return 0.30

    plan = solve_shape(
        load_fn=load,
        pv_fn=no_pv,
        price_fn=price,
        n=96,
        stored=0.26 * LIMITS.capacity_kwh,
    ).desired

    expensive = [
        entry
        for entry in plan.intervals
        if 11.0 <= (entry.index % 96) / 4.0 < 13.0
        and max(0.0, entry.marginal_grid_import_kwh) >= MIN_EXECUTABLE_QUARTER_KWH
    ]
    assert expensive == []


def test_a_lower_charge_power_limit_widens_the_purchase() -> None:
    """**The one legitimate reason a purchase must occupy more quarters.**

    Not a preference. The lattice discards any move the clamp had to reduce, so the
    largest charge a single quarter can express is the inverter's own limit -- and a
    given energy therefore needs at least ``energy / that`` quarters. A narrower plan
    than that is not available at any price, which is why concentration is bounded by
    physics rather than by the objective.
    """
    from custom_components.alpha_ems_manager.battery import build_limits
    from custom_components.alpha_ems_manager.economic import (
        build_physics_table,
        select_bucket_kwh,
    )

    def peak_for(max_charge_kw: float) -> float:
        limits, missing = build_limits(
            capacity_kwh=21.6,
            max_charge_kw=max_charge_kw,
            max_discharge_kw=10.0,
            round_trip_efficiency_percent=90.0,
            max_soc_percent=100.0,
        )
        assert missing is None
        floor = limits.energy_for_soc(20.0)
        bucket, _rule = select_bucket_kwh(limits, floor_energy_kwh=floor)
        table = build_physics_table(limits, floor_energy_kwh=floor, bucket_kwh=bucket)
        return table.max_representable_charge_kw

    strong, weak = peak_for(10.0), peak_for(2.5)

    assert strong > weak
    # 6 kWh needs at least this many quarters at each limit, and the weaker pack
    # needs strictly more of them -- the duration expands, correctly.
    assert math.ceil(6.0 / (strong * 0.25)) < math.ceil(6.0 / (weak * 0.25))


def test_the_objective_has_no_term_in_interval_count_or_duration() -> None:
    """**Structural, and it is the whole reason no tie-break was added.**

    Every cost the recursion accumulates is an energy at a price, a fee at a run
    start, or a per-kWh rate. None counts intervals, measures a span or reads a power,
    so nothing in the objective prefers a broad charge to a narrow one.
    """
    from custom_components.alpha_ems_manager import economic as economic_module

    source = inspect.getsource(economic_module.solve)

    for forbidden in (
        "interval_count",
        "len(current)",
        "duration",
        "power_density",
        "charging_intervals",
    ):
        assert forbidden not in source, forbidden


# ===========================================================================
# structural: the new figures decide nothing
# ===========================================================================


def test_no_decision_path_reads_a_purchase_window_figure() -> None:
    """The release is observability, and a structural test says so rather than prose."""
    from custom_components.alpha_ems_manager import coordinator as coordinator_module
    from custom_components.alpha_ems_manager import dispatch as dispatch_module

    watched = (
        dispatch_module.decide_charge,
        dispatch_module.decide_export,
        dispatch_module.clamp_charge_kw,
        coordinator_module.AlphaEmsCoordinator._charge_limits,
        coordinator_module.AlphaEmsCoordinator._quarter_progress,
        coordinator_module.AlphaEmsCoordinator._dispatch_setpoint,
    )
    for function in watched:
        source = inspect.getsource(function)
        for forbidden in (
            "grid_purchase",
            "production_charge_kwh",
            "next_grid_purchase",
        ):
            assert forbidden not in source, f"{function.__name__}: {forbidden}"


def test_the_walk_reads_the_grid_figure_and_never_the_battery_one() -> None:
    """Asking the battery figure would call every sunny quarter a purchase."""
    from custom_components.alpha_ems_manager.coordinator import _is_purchasing_row

    source = inspect.getsource(_is_purchasing_row)

    assert "grid_authorised_kwh" in source
    assert "battery_kwh" not in source


def test_a_purchasing_row_is_decided_by_the_grid_figure() -> None:
    """The behavioural half of the test above."""
    from custom_components.alpha_ems_manager.coordinator import _is_purchasing_row

    # A big battery objective fed entirely by production is not a purchase.
    assert _is_purchasing_row(row(28, grid=0.0, battery=9.0)) is False
    # A small battery objective that does buy, is.
    assert _is_purchasing_row(row(28, grid=0.5, battery=0.5)) is True
