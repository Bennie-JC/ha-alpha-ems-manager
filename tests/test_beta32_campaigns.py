"""beta.32 Phase B: the objective's own topology, and what may be announced.

**The symptom.** A live 17:45 diagnostic reported 13 economic runs with 3
direction changes, fragmented export runs, and meter objectives of 0.00-0.02 kWh.
The natural reading is that the optimiser is churning.

**It is not, and this suite is the proof.** ``runs_from`` starts a new run
whenever an interval's *action label* differs from the run's first label, and the
label flips between ``discharge`` and ``export`` beneath a varying house load. On
a realistic today+tomorrow horizon the solver flagged **three** run-state
transitions and charged three switching fees, while ``runs_from`` published
**fifteen** runs -- with ``charged_switching_fee`` false on every artefact split,
because the solver never saw them.

Phase A's export deadband did **not** fix this, and measurement said so: it moved
the count from 10 to 15, because it only changes which side of the alternation
carries which label. The fragmentation fix is this phase alone, and it is a
grouping rather than a decision: campaigns are maximal contiguous stretches of one
``run_state``, so ``len(campaigns) == direction_changes`` by construction.

**Materiality is deliberately narrower than "is this campaign valid".** It is a
*Sell announcement* rule. See ``campaigns_from`` and the tests at the foot of this
module: applying the local value test to a Buy marked a deliberate 16.944 kWh
charge campaign immaterial, because buying always costs money locally -- which is
what buying is.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
    ECONOMIC_IMMATERIAL_BELOW_TRADE_GAIN,
    ECONOMIC_IMMATERIAL_NOT_EXECUTABLE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_SERVE_LOAD,
    MIN_EXECUTABLE_QUARTER_KWH,
)

from .beta32_harness import LIMITS, flat, live_shape, solve_shape


def test_the_campaign_count_equals_the_switching_fees_the_solver_charged() -> None:
    """**The decision-neutrality proof, and the whole point of the layer.**

    A campaign is a maximal stretch of one run state, and the fee is charged at
    exactly each transition into one -- so the two counts are the same number
    computed two ways. If they ever diverge, the grouping has started deciding
    something, which it must never do.
    """
    plan = live_shape().desired

    assert len(plan.campaigns) == plan.direction_changes
    assert len(plan.campaigns) == sum(1 for e in plan.intervals if e.run_start)


def test_fifteen_label_slices_become_three_economic_campaigns() -> None:
    """The measured before/after on the live shape, asserted exactly.

    Fifteen published runs, three run-state transitions, three campaigns. The run
    count is *not* asserted to fall: runs are label slices and they remain the
    honest record of what each interval was doing. What falls is the number of
    things a person is shown.
    """
    plan = live_shape().desired

    assert len(plan.runs) == 15
    assert len(plan.campaigns) == 3
    assert [campaign.direction for campaign in plan.campaigns] == [
        ECONOMIC_DIRECTION_DISCHARGE,
        ECONOMIC_DIRECTION_CHARGE,
        ECONOMIC_DIRECTION_DISCHARGE,
    ]


def test_the_fee_is_charged_once_per_campaign_and_never_on_a_label_flip() -> None:
    """Where beta.31's fragmentation came from, stated as an invariant.

    Exactly one campaign in each contiguous stretch carries the fee, and the
    fifteen runs between them carry it on three. A published run that did not pay
    is an artefact split, not a decision.
    """
    plan = live_shape().desired

    charged = [c for c in plan.campaigns if c.switching_fee_eur > 0.0]
    assert len(charged) == len(plan.campaigns)
    assert sum(1 for run in plan.runs if run.charged_switching_fee) == len(
        plan.campaigns
    )
    assert plan.switching_cost_eur == pytest.approx(
        sum(c.switching_fee_eur for c in plan.campaigns)
    )


def test_a_discharge_campaign_separates_self_consumption_from_real_export() -> None:
    """**The four-layer model, on the reproduced live campaign.**

    Battery 8.750 kWh AC, meter 2.648 kWh, so roughly 6.1 kWh AC served the house.
    beta.31 published that campaign as several ``export`` runs whose
    ``energy_kwh`` was the tiny meter figure, making a 96 %-self-consumption
    campaign look like a small sale.

    The campaign is one object; its segments carry the intents; only the
    ``net_export`` segments are executable; and ``serve_load`` emits nothing,
    because Stage B commands nothing for it.
    """
    plan = live_shape().desired
    campaign = plan.campaigns[0]

    assert campaign.battery_ac_kwh == pytest.approx(8.750, abs=0.01)
    assert campaign.grid_export_kwh == pytest.approx(2.648, abs=0.01)
    # The campaign's own objective is the meter figure, summed over export
    # segments -- not the battery movement, and not one segment's share.
    assert campaign.objective_kwh == pytest.approx(2.640, abs=0.01)

    intents = {segment.intent for segment in campaign.segments}
    assert intents == {EXECUTION_INTENT_SERVE_LOAD, EXECUTION_INTENT_NET_EXPORT}
    for segment in campaign.segments:
        if segment.intent == EXECUTION_INTENT_SERVE_LOAD:
            assert segment.objective_kwh == 0.0
            assert not segment.executable
        else:
            assert segment.objective_kwh > 0.0
            assert segment.executable


def test_self_consumption_is_published_at_one_boundary_and_named_for_it() -> None:
    """**The dimensional test.** Both inputs are AC, so the difference is AC.

    An earlier draft called this ``_dc_kwh`` while subtracting two AC figures, and
    the design note it came from computed ``discharge_ac - export/eta`` -- a DC
    quantity subtracted from an AC one. Hand-computed: 8.750 - 2.648 = 6.102 AC,
    and 6.102 / 0.948683 = 6.432 DC.
    """
    campaign = live_shape().desired.campaigns[0]

    assert campaign.self_consumption_ac_kwh == pytest.approx(6.102, abs=0.01)
    assert campaign.self_consumption_dc_kwh(
        LIMITS.discharge_efficiency
    ) == pytest.approx(6.432, abs=0.01)
    # The DC figure is always the larger: it takes more from the pack than reaches
    # the terminal. A test that passed with them equal would not be checking the
    # conversion at all.
    assert (
        campaign.self_consumption_dc_kwh(LIMITS.discharge_efficiency)
        > campaign.self_consumption_ac_kwh
    )
    # And a charge campaign consumes nothing on the house's behalf.
    assert live_shape().desired.campaigns[1].self_consumption_ac_kwh == 0.0


def test_a_charge_campaign_is_never_judged_by_its_local_marginal_cost() -> None:
    """**The measurement that forced materiality to be direction-aware.**

    Buying always costs money against leaving the battery alone -- that *is*
    buying. So ``-marginal_cost_eur`` is negative for every charge campaign, and a
    universal ``(-marginal) > fee`` test marked this deliberate 16.944 kWh
    DP-selected campaign immaterial. Its value was realised in the two discharge
    campaigns it enabled, whose marginal costs are -2.72 and -4.29 EUR.

    A Buy's value is inter-temporal and no campaign-local quantity can express it,
    so the Buy test is executability and the optimiser remains the authority for
    the economics.
    """
    plan = live_shape().desired
    charge = plan.campaigns[1]

    assert charge.direction == ECONOMIC_DIRECTION_CHARGE
    assert charge.battery_charge_ac_kwh == pytest.approx(16.944, abs=0.01)
    # The local test would have failed, and it is not applied.
    assert charge.marginal_cost_eur > 0.0
    assert -charge.marginal_cost_eur < charge.switching_fee_eur
    # It stays announceable, because it is executable.
    assert charge.sell_announcement_material
    assert charge.immaterial_reason is None
    # And the value really is elsewhere.
    discharges = [
        c.marginal_cost_eur
        for c in plan.campaigns
        if c.direction == ECONOMIC_DIRECTION_DISCHARGE
    ]
    assert all(value < 0.0 for value in discharges)


def test_a_sell_campaign_with_real_local_value_is_material() -> None:
    """The rule where it is meaningful: value realised inside the campaign."""
    plan = live_shape().desired
    sells = [c for c in plan.campaigns if c.direction == ECONOMIC_DIRECTION_DISCHARGE]

    assert sells
    for campaign in sells:
        assert -campaign.marginal_cost_eur > campaign.switching_fee_eur
        assert campaign.sell_announcement_material


def test_a_charge_below_the_actuator_resolution_is_not_announceable() -> None:
    """The Buy test is physical, and it does bite when there is nothing to send.

    Constructed rather than solved: a campaign whose charge objective is below
    ``MIN_EXECUTABLE_QUARTER_KWH`` cannot be delivered by any command, so there is
    nothing to tell a person about. This is *not* a second economic Buy gate --
    the optimiser's decision is not second-guessed, only its executability.
    """
    from custom_components.alpha_ems_manager.economic import (
        EconomicInterval,
        campaigns_from,
    )

    tiny = EconomicInterval(
        index=0,
        action="charge",
        start_energy_dc_kwh=10.0,
        battery_delta_dc_kwh=0.01,
        battery_charge_ac_kwh=0.010,
        battery_discharge_ac_kwh=0.0,
        grid_import_kwh=0.010,
        grid_export_kwh=0.0,
        pv_curtailed_kwh=0.0,
        cost_eur=0.003,
        import_price_eur_kwh=0.30,
        export_price_eur_kwh=0.17,
        run_start=True,
        run_state=ECONOMIC_DIRECTION_CHARGE,
    )

    campaigns = campaigns_from((tiny,), minimum_trade_gain_eur=0.20)

    assert len(campaigns) == 1
    assert tiny.battery_charge_ac_kwh < MIN_EXECUTABLE_QUARTER_KWH
    assert not campaigns[0].sell_announcement_material
    assert campaigns[0].immaterial_reason == ECONOMIC_IMMATERIAL_NOT_EXECUTABLE


def test_a_sell_artefact_below_the_trade_gain_is_not_announceable() -> None:
    """The other reason, and it carries its own token.

    A discharge campaign whose whole local advantage is under the fee the solver
    charged it. Constructed, because the deadband now removes most of these at
    source -- which is the point of doing Phase A first.
    """
    from custom_components.alpha_ems_manager.economic import (
        EconomicInterval,
        campaigns_from,
    )

    thin = EconomicInterval(
        index=0,
        action="export",
        start_energy_dc_kwh=10.0,
        battery_delta_dc_kwh=-0.2635,
        battery_charge_ac_kwh=0.0,
        battery_discharge_ac_kwh=0.250,
        grid_import_kwh=0.0,
        grid_export_kwh=0.250,
        pv_curtailed_kwh=0.0,
        cost_eur=-0.010,
        idle_cost_eur=0.0,
        import_price_eur_kwh=0.30,
        export_price_eur_kwh=0.04,
        run_start=True,
        run_state=ECONOMIC_DIRECTION_DISCHARGE,
    )

    campaigns = campaigns_from((thin,), minimum_trade_gain_eur=0.20)

    # Advantage 0.010 EUR against a 0.20 EUR fee.
    assert -campaigns[0].marginal_cost_eur == pytest.approx(0.010)
    assert not campaigns[0].sell_announcement_material
    assert campaigns[0].immaterial_reason == ECONOMIC_IMMATERIAL_BELOW_TRADE_GAIN


def test_an_idle_interval_belongs_to_no_campaign() -> None:
    """Idle separates campaigns, the same treatment ``hold`` gets in ``runs_from``.

    Absorption is the exception and needs no code here: ``_resolved_run_state``
    already folds it into whichever run is in progress, so a solar quarter inside a
    charge campaign does not split it.
    """
    plan = live_shape().desired
    covered = {
        index
        for campaign in plan.campaigns
        for index in range(campaign.start_index, campaign.end_index + 1)
    }
    idle = {e.index for e in plan.intervals if e.run_state == "idle"}

    assert not (covered & idle)


def test_a_campaign_covers_a_contiguous_span_of_one_direction() -> None:
    """Structural: no campaign may straddle a direction change or a gap."""
    plan = live_shape().desired
    by_index = {entry.index: entry for entry in plan.intervals}

    for campaign in plan.campaigns:
        span = range(campaign.start_index, campaign.end_index + 1)
        assert campaign.interval_count == len(span)
        assert {by_index[i].run_state for i in span} == {campaign.direction}


def test_segments_partition_their_campaign_exactly() -> None:
    """No interval counted twice, none dropped, and the objectives reconcile."""
    plan = live_shape().desired

    for campaign in plan.campaigns:
        indices: list[int] = []
        for segment in campaign.segments:
            indices.extend(range(segment.start_index, segment.end_index + 1))
        assert indices == list(range(campaign.start_index, campaign.end_index + 1))
        if campaign.direction == ECONOMIC_DIRECTION_DISCHARGE:
            exported = sum(
                segment.objective_kwh
                for segment in campaign.segments
                if segment.intent == EXECUTION_INTENT_NET_EXPORT
            )
            assert campaign.objective_kwh == pytest.approx(exported)


def test_a_pure_self_consumption_campaign_sells_nothing_and_claims_nothing() -> None:
    """A discharge campaign with no export segment is not a sell.

    Driven with export disabled at a sub-bucket load, which is exactly the R5
    shape: the battery serves the house for six hours and the campaign's sell
    objective is zero, so there is nothing for Activity to announce as a sale.
    """
    plan = solve_shape(
        load_fn=flat(0.19),
        price_fn=lambda index: 0.35 if index < 24 else 0.12,
        n=48,
        stored=16.0,
        allow_export=False,
    ).desired

    for campaign in plan.campaigns:
        if campaign.direction != ECONOMIC_DIRECTION_DISCHARGE:
            continue
        assert campaign.objective_kwh == pytest.approx(0.0, abs=1e-9)
        assert EXECUTION_INTENT_NET_EXPORT not in {
            segment.intent for segment in campaign.segments
        }


def test_the_campaign_layer_changed_no_published_figure() -> None:
    """Phase B is a grouping. Every economic figure must be the solver's own.

    Asserted by reconciliation rather than by a golden file: the campaigns sum to
    the intervals they cover, so the layer cannot have invented or lost energy.

    **Idle intervals are excluded deliberately.** ``_resolved_run_state`` folds
    ambient absorption into whichever run is in progress and leaves it idle
    otherwise, so production the house cannot use -- stored while nothing is
    planned -- belongs to no campaign. Comparing a campaign total against *every*
    interval would compare it against energy no campaign claims, and would fail by
    exactly that absorption.
    """
    plan = live_shape().desired
    covered = [entry for entry in plan.intervals if entry.run_state != "idle"]

    assert sum(c.grid_import_kwh for c in plan.campaigns) == pytest.approx(
        sum(entry.grid_import_kwh for entry in covered), abs=1e-6
    )
    assert sum(c.grid_export_kwh for c in plan.campaigns) == pytest.approx(
        sum(entry.grid_export_kwh for entry in covered), abs=1e-6
    )
    assert sum(c.battery_ac_kwh for c in plan.campaigns) == pytest.approx(
        sum(
            entry.battery_charge_ac_kwh + entry.battery_discharge_ac_kwh
            for entry in covered
        ),
        abs=1e-6,
    )
    # And what was excluded really is ambient, not a gap in the grouping.
    for entry in plan.intervals:
        if entry.run_state == "idle" and entry.battery_charge_ac_kwh > 0.0:
            assert entry.absorbing


def test_an_empty_run_is_never_published() -> None:
    """R7, on the shape that produced one: a ``curtail_pv`` run with no energy."""
    plan = live_shape().desired

    for run in plan.runs:
        moved = (
            run.battery_charge_ac_kwh
            + run.battery_discharge_ac_kwh
            + run.grid_import_kwh
            + run.grid_export_kwh
            + run.pv_curtailed_kwh
        )
        assert moved > 0.0, run.action


def test_a_load_serving_row_reports_its_own_objective() -> None:
    """R6: ``battery_kwh`` reads the interval, not the intent.

    Through beta.31 a ``serve_load`` row resolved its battery figure to
    ``battery_charge_ac_kwh`` -- which is zero on a discharging interval -- so every
    quarter of every load-serving run was stamped ``no_objective``. A false
    diagnostic on exactly the rows a reader most wants to understand.

    Driven at ``quarter_schedule_for`` directly, which is where the row is built,
    rather than through the published payload: the execution targets need a
    calendar the economic layer does not own.
    """
    from datetime import UTC, datetime, timedelta

    from custom_components.alpha_ems_manager.economic import (
        EconomicInterval,
        quarter_schedule_for,
    )

    base = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)
    serving = tuple(
        EconomicInterval(
            index=index,
            action="discharge",
            start_energy_dc_kwh=12.0,
            battery_delta_dc_kwh=-0.2635,
            battery_charge_ac_kwh=0.0,
            battery_discharge_ac_kwh=0.250,
            grid_import_kwh=0.02,
            grid_export_kwh=0.0,
            pv_curtailed_kwh=0.0,
            cost_eur=0.006,
            import_price_eur_kwh=0.30,
            export_price_eur_kwh=0.17,
            run_start=index == 0,
            run_state=ECONOMIC_DIRECTION_DISCHARGE,
        )
        for index in range(2)
    )

    rows = quarter_schedule_for(
        serving,
        start_index=0,
        end_index=1,
        intent=EXECUTION_INTENT_SERVE_LOAD,
        moment=lambda index: base + timedelta(minutes=15 * index),
    )

    assert len(rows) == 2
    # The battery moved 0.250 kWh in each quarter, so the row has an objective.
    for row in rows:
        assert row["battery_kwh"] == pytest.approx(0.250, abs=1e-6)
        assert row["not_executable"] != "no_objective"
