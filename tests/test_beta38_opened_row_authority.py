"""beta.38 Gate 1: an opened frozen row is not withdrawn by absence.

**The 2026-09-01 incident, replayed, in both economic directions.**

A two-row ``net_export`` run was admitted at 20:15 for a 20:30-21:00 window. At
20:30:05 -- the very refresh its first row opened -- the Stage-A solve had moved the
export to tomorrow evening, so nothing affirmed the open row and ``carry_forward``
filed ``stage_a_hold`` against it: ``remaining_battery_kwh 4.827`` of a 5.0 kWh run.
The same refresh then armed 9.7 kW. A terminal for a run that was starting.

Two defects, either sufficient on its own:

1. ``carry_forward``'s withdrawal-by-absence is an unguarded terminal ``return``
   that cannot see the row is open -- and since Stage A's head is ``elapsed + 1``,
   *no* publication can describe an open row, so the last row of every run was
   unaffirmable by construction.
2. ``_plan_authority_holds`` demanded a persisted arm claim, which is written by an
   arm, which happens after the stop is decided in the same refresh. On the refresh
   a row opens the proof it asked for cannot exist.

**Both directions are tested, and they are not the same test.** A Sell's objective
is the *meter* export with battery discharge as a ceiling; a Buy's objective is
*battery charge* with grid import as a ceiling. The lifecycle is shared, the
objectives are not, and a fixture that only exercised one would leave the other's
domain unproven.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    LIFECYCLE_ADMITTED,
)

from .beta38_trace import (
    BOTH_INTENTS,
    BUY_BATTERY_TARGET_KWH,
    SELL_METER_TARGET_KWH,
    authority_of,
    carried_of,
    lifecycle_of,
    moved_elsewhere,
    opens_at,
    publish,
    publish_nothing,
    shrunk_but_overlapping,
    step_clock,
    target_for,
)
from .test_beta24_live_charge import (
    LiveSurface,
    charge_now_price,
    live_coordinator,
    step_once,
)

# ===========================================================================
# the harness
# ===========================================================================


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def site(hass, *, intent: str) -> None:
    """Point the live meters at a coherent site doing what the intent asks.

    Coherent in the balance layer's own terms -- ``pv + import == load + charge``
    and its mirror -- because an incoherent snapshot makes the tick skip accrual
    entirely, and a fixture whose energy never moves proves nothing about a campaign
    that ends when energy stops moving.

    The Sell figures are the measured ones: 10.05 kW from the pack, 1.3 kW of house,
    8.7 kW across the meter. The Buy carries **production alongside grid import**,
    because "free production still pays toward the battery objective" is a Buy-only
    invariant and a zero-PV fixture could not see it fail.
    """
    from .beta36_trace import charging_site
    from .conftest import BATTERY_POWER, GRID_POWER, HOUSE_LOAD, PV_POWER, set_sensor

    if intent == EXECUTION_INTENT_GRID_CHARGE:
        # 2.24 kW into the pack, 0.8 kW of it from production.
        charging_site(hass, battery_w=-2240.0, pv_w=800.0, house_w=500.0)
        return
    house_w, battery_w = 1300.0, 10050.0
    set_sensor(hass, PV_POWER, 0.0, "W", "power")
    set_sensor(hass, HOUSE_LOAD, house_w, "W", "power")
    set_sensor(hass, BATTERY_POWER, battery_w, "W", "power")
    set_sensor(hass, GRID_POWER, -(battery_w - house_w), "W", "power")


async def admitted_before_open(
    hass, config_data, frank, live_surface, monkeypatch, *, intent: str
):
    """Return a coordinator with the run **admitted and its row not yet open**.

    This is the state the incident began from and the one no existing fixture
    reaches: a real ``CarriedRun`` minted by ``carry_forward`` from a real
    publication, a frozen ``AdmittedPlan`` beside it, and **no ownership claim**,
    because nothing has been armed yet. Every part of it is produced by production
    code -- a hand-written claim is the one thing that cannot reproduce this defect,
    because the defect is precisely that no claim exists yet.
    """
    from .forecast_helpers import NORMAL, history_before, seed
    from .frank_capture import synthetic_day
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL, price_at=charge_now_price), tomorrow=None)
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    site(hass, intent=intent)

    publish(coordinator, monkeypatch, (target_for(intent),))
    report = await step_once(hass, coordinator, live_surface, **step_clock(-1))

    # The witnesses, so a later assertion cannot pass against a fixture that never
    # admitted anything.
    assert coordinator._carried is not None, "the run must be carried"
    assert coordinator._plan is not None, "the schedule must be frozen"
    assert coordinator._plan.has_opened(opens_at(-1)) is False, "not open yet"
    assert coordinator.store.execution_record is None, "and nothing armed yet"
    assert carried_of(report).get("ended_reason") is None
    return coordinator


async def open_the_row(hass, coordinator, live_surface, monkeypatch, *, intent: str):
    """Step into the refresh the first row opens on, with Stage A moved elsewhere."""
    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))
    return await step_once(hass, coordinator, live_surface, **step_clock(0))


# ===========================================================================
# 1. the incident itself, both directions
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_an_opened_row_is_not_withdrawn_by_absence(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The 2026-09-01 refresh, and the whole release in one assertion.**

    Stage A still wants to do this -- it published a run of the *same intent* four
    quarters later -- it has simply moved it. ``affirms`` is purely temporal, so
    nothing overlaps the open row and beta.37 read that as a withdrawal of work that
    had already begun.

    *Mutation: restore the unguarded terminal ``return`` in ``carry_forward``, or
    restore the persisted-claim requirement in ``_plan_authority_holds``, and this
    fails in both directions.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    run_before = coordinator._carried
    plan_before = coordinator._plan
    assert run_before is not None and plan_before is not None

    report = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )
    carried = carried_of(report)

    # --- the run survived, and was never even nominally ended -------------
    assert carried.get("ended_reason") is None, carried
    assert carried.get("last_ended") is None, "no terminal for a running row"
    assert coordinator._carried is not None
    assert coordinator._carried.run_id == run_before.run_id
    assert coordinator._last_ended is None

    # --- the authority the withdrawal would have had to outrank -----------
    assert authority_of(report).get("plan_authority_holds") is True, report
    assert coordinator._plan is not None
    assert coordinator._plan.plan_id == plan_before.plan_id, "the same frozen schedule"

    # --- and nothing was latched -----------------------------------------
    assert coordinator._abandoned_admissions == []
    assert coordinator._final_campaigns == []
    assert coordinator._closed_instances == []


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_publishing_nothing_at_all_does_not_end_an_opened_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """The strongest form of the same condition: Stage A publishes nothing.

    A missing solve is a statement about the future and nothing else, and it may not
    end work already under way any more than a relocated one may.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    run_before = coordinator._carried
    assert run_before is not None

    publish_nothing(coordinator, monkeypatch)
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    assert carried_of(report).get("ended_reason") is None
    assert coordinator._carried is not None
    assert coordinator._carried.run_id == run_before.run_id
    assert coordinator._last_ended is None


# ===========================================================================
# 2. admitted vs opened vs started -- three states, pinned independently
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_before_the_row_opens_a_withdrawal_is_still_allowed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The negative control, and the reason this is not "ignore bad news".**

    Nothing physical has happened, so Stage A revising its mind is exactly the
    revision it appears to be. The authority begins when the row opens and not one
    refresh earlier -- and if that distinction ever collapses, the release has traded
    a withdrawal defect for an execution one.

    *Mutation: make ``row_open`` unconditionally true, or ``has_opened`` always
    true, and this fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    assert coordinator._carried is not None

    # Still one quarter before the row opens, and Stage A has moved on.
    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))
    report = await step_once(hass, coordinator, live_surface, **step_clock(-1))

    assert carried_of(report).get("ended_reason") == EXECUTION_STOP_STAGE_A_HOLD
    assert coordinator._carried is None, "an unopened run may still be withdrawn"
    assert EXECUTION_STOP_STAGE_A_HOLD in EXECUTION_WITHDRAWAL_STOP_REASONS


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_frozen_target_and_identity_survive_the_replan(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**A2: nothing about the frozen work moves once its row has opened.**

    Not the objective, not the window, not the campaign it belongs to, not the rows
    still to come. The replan describes a different campaign entirely, so a layer
    that re-read the publication instead of the frozen schedule would show it.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    plan = coordinator._plan
    assert plan is not None
    before = (
        plan.plan_id,
        plan.run_id,
        plan.campaign_id,
        plan.starts_at,
        plan.ends_at,
        # **``admitted_at`` and the key derived from it, because those are what a
        # silent re-derivation moves.** ``admit_plan`` stamps ``admitted_at=now``, so
        # a schedule rebuilt each refresh keeps every economic figure and quietly
        # mints a new ``admission_key`` -- and that key is what the abandonment latch
        # is keyed on. A comparison of targets alone would not see it.
        plan.admitted_at,
        plan.admission_key,
        tuple(
            (row.start, row.battery_kwh, row.grid_export_target_kwh)
            for row in plan.rows
        ),
    )

    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)

    after = coordinator._plan
    assert after is not None
    assert (
        after.plan_id,
        after.run_id,
        after.campaign_id,
        after.starts_at,
        after.ends_at,
        after.admitted_at,
        after.admission_key,
        tuple(
            (row.start, row.battery_kwh, row.grid_export_target_kwh)
            for row in after.rows
        ),
    ) == before
    assert after.campaign_id != "b38-a-different-campaign"


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_second_frozen_row_is_still_reachable(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The boundary is a lookup in the frozen schedule, not a hand-off.**

    Stage A's newest plan describes a run four quarters away. The next executing
    quarter must nevertheless be row two of the schedule frozen at admission, or the
    campaign silently loses everything after the row that happened to be open when
    Stage A changed its mind.
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(0)

    await step_once(hass, coordinator, live_surface, **step_clock(1))

    assert coordinator._quarter is not None, "row two must be derivable"
    assert coordinator._quarter.quarter_start == opens_at(1)
    assert coordinator._carried is not None


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_an_affirming_publication_may_not_shrink_the_accepted_work(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The other way a frozen target could move: adoption rather than replacement.**

    This publication *does* affirm -- same intent, window inside the accepted one --
    so the run continues. It also asks for a quarter of the energy. The accepted
    figures must not move, because progress and the per-row grid ceiling are both
    measured against them: a run judged against 1.13 kWh instead of 4.53 would read
    as finished with two thirds undelivered.

    Withdrawal is not the only way to lose frozen work, and this is the other one.

    *Mutation: make ``affirm`` adopt the published target and this fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    run = coordinator._carried
    assert run is not None
    accepted = run.target.battery_target_kwh

    publish(coordinator, monkeypatch, (shrunk_but_overlapping(intent),))
    report = await step_once(hass, coordinator, live_surface, **step_clock(0))

    # The witness: it really did affirm, so this is the adoption path and not the
    # withdrawal one.
    assert carried_of(report).get("affirmed_by_this_publication") is True, report
    after = coordinator._carried
    assert after is not None
    assert after.run_id == run.run_id
    assert after.target.battery_target_kwh == pytest.approx(accepted)
    assert after.target.battery_target_kwh != pytest.approx(0.25)
    # And the frozen schedule it feeds is untouched too.
    assert coordinator._plan is not None
    assert coordinator._plan.rows == run.target.quarter_schedule


# ===========================================================================
# 3. the objective domains, which are not shared
# ===========================================================================


async def test_a_sell_campaign_is_judged_at_the_meter(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The Sell objective is metered export; battery discharge is the ceiling.

    2.28 + 2.25 = 4.53 kWh at the meter, against a 5.0 kWh battery ceiling. A
    campaign judged on the battery figure would call this finished 0.47 kWh early.
    """
    coordinator = await admitted_before_open(
        hass,
        config_data,
        frank,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )
    await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=EXECUTION_INTENT_NET_EXPORT
    )

    quarter = coordinator._quarter
    assert quarter is not None
    assert quarter.objective_kwh() == pytest.approx(2.28)
    assert quarter.battery_target_kwh == pytest.approx(2.5), "a ceiling, not the aim"
    objective = coordinator._campaign_objective_kwh(coordinator._campaign_id)
    assert objective == pytest.approx(SELL_METER_TARGET_KWH, abs=0.01)


async def test_a_buy_campaign_is_judged_at_the_battery(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """The Buy objective is battery charge; grid authorisation is the ceiling.

    Each row wants 0.56 kWh into the pack and authorises only 0.30 kWh from the
    grid -- production is expected to pay the rest. A campaign judged on the grid
    figure would both under-deliver and, once the budget was spent, refuse free
    production it had been asked to store. That is the beta.36 domain error, and
    this pins the two figures apart.
    """
    coordinator = await admitted_before_open(
        hass,
        config_data,
        frank,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_GRID_CHARGE,
    )
    await open_the_row(
        hass,
        coordinator,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_GRID_CHARGE,
    )

    quarter = coordinator._quarter
    assert quarter is not None
    assert quarter.objective_kwh() == pytest.approx(0.56)
    assert quarter.grid_authorised_kwh == pytest.approx(0.30), "a ceiling"
    assert quarter.objective_kwh() > quarter.grid_authorised_kwh, (
        "the witness: production must pay the difference, or the fixture is vacuous"
    )
    objective = coordinator._campaign_objective_kwh(coordinator._campaign_id)
    assert objective == pytest.approx(BUY_BATTERY_TARGET_KWH, abs=0.01)


# ===========================================================================
# 4. what the authority may never suppress
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_authority_suppresses_absence_and_nothing_else(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The partition, asserted where the suppression lives.**

    The opened-row authority may withhold the withdrawal family and only that
    family. Safety, a lost marker, a stalled dead-man, a failed write, the user's own
    switch, a lost measurement and an unknown quarter after a restart are aborts, are
    disjoint from it, and reach the run by other paths entirely.

    *Mutation: move any abort reason into the withdrawal family and this fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    await open_the_row(hass, coordinator, live_surface, monkeypatch, intent=intent)

    overlap = set(EXECUTION_WITHDRAWAL_STOP_REASONS) & set(EXECUTION_ABORT_STOP_REASONS)
    assert overlap == set(), overlap
    assert EXECUTION_STOP_STAGE_A_HOLD in EXECUTION_WITHDRAWAL_STOP_REASONS


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_a_foreign_claim_still_refuses_authority(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**The half of the authority predicate that beta.38 kept.**

    "No claim" and "somebody else's claim" are different answers. The first means
    nothing else owns anything and the opened row is the only authority there is; the
    second means this plan is not what is running and must not speak for it.
    Collapsing them into "any claim will do" would let a stale record from a previous
    attempt authorise the next one.

    *Mutation: weaken the clause to ``True`` and this fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    at_open = opens_at(0)

    # No claim at all: the opened row is the authority. This is the beta.37 gap.
    assert coordinator.store.execution_record is None
    coordinator._quarter = coordinator._plan.executing_quarter(at_open)
    assert coordinator._quarter is not None
    assert coordinator._plan_authority_holds(at_open) is True

    # A record naming a different run: refused, exactly as before.
    coordinator.store.execution_record = {"run_id": "somebody-elses-run"}
    assert coordinator._plan_authority_holds(at_open) is False

    # And the run's own record: allowed again.
    coordinator.store.execution_record = {"run_id": coordinator._plan.run_id}
    assert coordinator._plan_authority_holds(at_open) is True

    # **And a dead attempt has no authority at all, claim or no claim.** The
    # abandonment latch is the one clause of this predicate that beta.36 added, and
    # it is what stops a torn-down admission from re-arming while the intention
    # behind it is still being published.
    coordinator._remember_abandoned_admission(coordinator._plan.admission_key)
    assert coordinator._plan_authority_holds(at_open) is False


# ===========================================================================
# 4b. the guards that defend paths no replay reaches
# ===========================================================================
#
# **Three invariants whose failure mode is unreachable today, and which are
# tested anyway.** Each is protected twice over, or guards a state the current
# lifecycle cannot construct -- so breaking one of them changes no replay, and a
# scenario test would pass against the broken code. That is precisely the case a
# mutation harness exists to find, and the honest answer is a direct test rather
# than a contrived scenario or a quietly dropped mutation.


@pytest.mark.parametrize("intent", BOTH_INTENTS)
def test_an_opened_schedule_is_returned_unchanged_not_re_derived(intent: str) -> None:
    """``carry_plan_verbose`` keeps an opened plan; it does not rebuild it.

    Rebuilding produces an *equal* schedule -- the carried run's target is
    immutable, so every economic figure survives -- and a different
    ``admitted_at``, because ``admit_plan`` stamps ``now``. That timestamp is what
    ``admission_key`` is derived from, and the abandonment latch is keyed on it:
    a schedule silently re-admitted every refresh can never be latched, because
    the key a teardown recorded names an admission that no longer exists.

    Unreachable in a replay for the good reason that the figures do all survive.

    *Mutation: drop the keep-clause and this fails.*
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.execution import (
        admit,
        admit_plan,
        carry_plan_verbose,
        parse_target,
    )

    raw = target_for(intent)
    parsed = parse_target(raw)
    assert parsed is not None
    admitted_at = opens_at(-1)
    run = admit(parsed, admitted_at)
    plan = admit_plan(parsed, run=run, now=admitted_at)
    assert plan is not None and plan.has_opened(opens_at(0))

    later = opens_at(0) + timedelta(minutes=5)
    kept, refusal = carry_plan_verbose(
        plan,
        (raw,),
        later,
        run=run,
        executable_intents=frozenset({intent}),
    )

    assert refusal is None
    assert kept is not None
    assert kept.admitted_at == plan.admitted_at, "the schedule was re-derived"
    assert kept.admission_key == plan.admission_key
    # The witness: a rebuild would genuinely have moved it, so the equality above
    # is not true by accident of the clock.
    assert later != plan.admitted_at


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_campaign_start_freeze_is_idempotent(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """Called twice, the freeze does nothing the second time.

    beta.38 gave it two callers -- the send site, so the freeze and the physical
    start are one transition, and the report, for every path that reaches a
    started campaign without passing through a write. The ``is None`` latch is
    what makes two callers safe, and without it a second arm inside one campaign
    would re-freeze against whatever the objective had become by then.

    No current replay arms twice inside one campaign, which is why this is
    asserted directly on the helper rather than through a scenario.

    *Mutation: drop the ``_campaign_started_at is not None`` latch and this
    fails.*
    """
    from datetime import timedelta

    coordinator = await admitted_before_open(
        hass,
        config_data,
        frank,
        live_surface,
        monkeypatch,
        intent=intent,
    )
    await open_the_row(
        hass,
        coordinator,
        live_surface,
        monkeypatch,
        intent=intent,
    )
    started = coordinator._campaign_started_at
    frozen = coordinator._campaign_frozen_target_kwh
    assert started is not None, "the witness: the campaign must have begun"
    assert frozen is not None

    # A second call, an hour later, with the objective moved underneath it.
    coordinator._campaign_opening_target_kwh = 99.0
    coordinator._note_campaign_started(started + timedelta(hours=1))

    assert coordinator._campaign_started_at == started
    assert coordinator._campaign_frozen_target_kwh == frozen


async def test_an_instance_that_filed_its_terminal_files_no_second_one(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """One started attempt files exactly one terminal, enforced not hoped for.

    ``campaign_instance_identity`` digests ``(campaign_id, opened_at)`` at
    microsecond resolution, so a genuinely new attempt always gets a new id and
    the closed set cannot bar it. That also means no replay can reach a *second*
    close of the *same* instance -- the guard is defensive, and the only way to
    test it is to put the coordinator back in the state it defends against.

    *Mutation: make ``_instance_closed`` answer ``False`` and this fails.*
    """
    coordinator = await admitted_before_open(
        hass,
        config_data,
        frank,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )
    await open_the_row(
        hass,
        coordinator,
        live_surface,
        monkeypatch,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )
    campaign = coordinator._campaign_id
    instance = coordinator._campaign_instance_id
    assert campaign is not None and instance is not None
    assert coordinator._campaign_started_at is not None

    now = opens_at(1)
    coordinator._close_campaign(now, EXECUTION_STOP_WINDOW_ENDED)
    first = coordinator._closed_campaign
    assert first is not None, "the witness: a terminal was filed"
    assert instance in coordinator._closed_instances

    # The state the guard exists for: the same instance, put back.
    coordinator._campaign_id = campaign
    coordinator._campaign_instance_id = instance
    coordinator._campaign_started_at = now
    coordinator._closed_campaign = None
    coordinator._close_campaign(now, EXECUTION_STOP_WINDOW_ENDED)

    assert coordinator._closed_campaign is None, "a second terminal for one attempt"
    assert coordinator._campaign_id is None


# ===========================================================================
# 5. the structural fact that makes all of this necessary
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
def test_a_runs_final_row_can_never_be_affirmed_once_it_opens(intent: str) -> None:
    """**Why withdrawal-by-absence was the ordinary state, not an edge case.**

    ``affirms`` needs a publication whose window starts at or before the carried
    window's end. Stage A's horizon head is ``elapsed + 1``, so once the *last* row
    has opened the earliest run Stage A can describe begins exactly at that end.

    **A first draft of this claimed no publication could ever affirm a final row,
    and the test disproved it.** One can: a run of the same intent beginning
    precisely where this one ends, which ``affirms``'s ``<=`` accepts and which is
    genuinely Stage A saying "and keep going". What is impossible is affirmation by
    a publication that starts *later* -- and "Stage A moved the work" is precisely
    that. So the final row survives only while Stage A keeps abutting it, and every
    other future it might choose reads as a withdrawal of something already running.
    Both halves are asserted, because only the pair makes the claim exact.
    """
    from datetime import timedelta

    from custom_components.alpha_ems_manager.execution import (
        admit,
        affirms,
        parse_target,
    )

    target = parse_target(target_for(intent))
    assert target is not None
    carried = admit(target, opens_at(-1))

    def head_at(moment):
        later = parse_target(
            target_for(
                intent,
                plan_id="a-later-solve",
                window_start=moment.isoformat(),
                window_end=(moment + timedelta(minutes=30)).isoformat(),
                quarter_schedule=[],
            )
        )
        assert later is not None
        return later

    # **The one head that can still affirm, and it is a continuation.** At the
    # refresh the final row opens, Stage A's head is exactly ``window_end`` -- so a
    # run of the same intent beginning precisely where this one ends *does* overlap
    # by ``affirms``'s ``<=``, and is affirmed. That is right: it is Stage A saying
    # "and keep going".
    assert affirms(carried, head_at(target.window_end)) is True

    # **Every head beyond it cannot, and "Stage A moved the work" is exactly that.**
    # One interval later is already unreachable, and the head only advances.
    for quarters_ahead in range(1, 8):
        head = target.window_end + quarters_ahead * timedelta(minutes=15)
        assert affirms(carried, head_at(head)) is False, head

    # The incident's own publication, four quarters out, is in that set.
    assert affirms(carried, parse_target(moved_elsewhere(intent))) is False


# ===========================================================================
# 6. the lifecycle field, which could not answer any of this before
# ===========================================================================


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_lifecycle_field_leaves_idle(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """``_note_lifecycle`` had no callers, so this field read ``idle`` for ever.

    On 2026-09-01 a reader looking at a 10 kW export saw ``lifecycle.state: "idle"``.
    "Is the lifecycle terminal while hardware moves?" is the question this release
    exists to answer, and it was being answered by a constant.

    *Mutation: unwire ``_note_lifecycle`` and this fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    admitted = lifecycle_of(coordinator.control_report or {})
    assert admitted.get("state") == LIFECYCLE_ADMITTED, admitted
    assert admitted.get("entered_at") is not None

    report = await open_the_row(
        hass, coordinator, live_surface, monkeypatch, intent=intent
    )
    opened = lifecycle_of(report)
    assert opened.get("state") != "idle", opened


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_no_refresh_reports_an_armed_campaign_as_unstarted(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """**F4: the freeze happens in the transition, not one refresh later.**

    ``_note_campaign_progress`` ran inside the control report and read
    ``activation_confirmed``, a flag set afterwards in ``_async_dispatch``. So on the
    refresh that actually armed the hardware the campaign had not started yet, and
    the capture says so: ``started: false`` and ``frozen_target_kwh: null`` beside an
    executing quarter.

    **Asserted on the coordinator immediately after each refresh, not on the report
    inside it.** A first version of this looked only at refreshes whose *published*
    ownership read ``owned`` -- and survived the mutation, because the report is built
    before the write and the arming refresh therefore still publishes ``none``. The
    one refresh the defect lived in was the only one the test never examined. The
    state after the write is where the question can actually be answered.

    *Mutation: gate the freeze on ``activation_confirmed`` alone again and this
    fails.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    # **And the converse, which is the other way to get this wrong.** Nothing has
    # been armed, so the campaign has not begun and must not claim it has -- a freeze
    # that fired on admission would judge the run against a promise made before any
    # energy moved.
    assert coordinator.store.execution_record is None
    assert coordinator._campaign_started_at is None
    assert coordinator._campaign_frozen_target_kwh is None

    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))

    armed_refreshes = 0
    for index in (0, 0, 1, 1):
        await step_once(hass, coordinator, live_surface, **step_clock(index))
        if coordinator.store.execution_record is None:
            continue
        # A claim exists, so an arm has landed and the hardware is ours. From this
        # instant on there is no refresh in which the campaign may read unstarted.
        armed_refreshes += 1
        assert coordinator._campaign_started_at is not None, index
        assert coordinator._campaign_frozen_target_kwh is not None, index

    assert armed_refreshes > 0, "the witness: the replay must actually arm something"


@pytest.mark.parametrize("intent", BOTH_INTENTS)
async def test_the_frozen_campaign_target_never_moves_after_it_is_set(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
    intent: str,
) -> None:
    """A verdict is only meaningful against what was promised when work began.

    *Mutation: let the freeze re-run, or let it read the latest publication, and
    this fails -- the replan describes a 4.0 kWh campaign that is not this one.*
    """
    coordinator = await admitted_before_open(
        hass, config_data, frank, live_surface, monkeypatch, intent=intent
    )
    publish(coordinator, monkeypatch, (moved_elsewhere(intent),))

    frozen: float | None = None
    started = None
    for index in (0, 0, 1, 1):
        await step_once(hass, coordinator, live_surface, **step_clock(index))
        current = coordinator._campaign_frozen_target_kwh
        if current is None:
            continue
        if frozen is None:
            frozen = current
            started = coordinator._campaign_started_at
        assert current == pytest.approx(frozen), "the frozen target moved"
        # **And the instant it was frozen at, which is the half a value comparison
        # misses.** With the ``is not None`` latch removed the freeze re-runs every
        # refresh; on this replay the objective it re-reads is the *frozen schedule*,
        # so the number happens not to move -- and a test that only compared numbers
        # survived. ``started_at`` is when work began, not when somebody last looked.
        assert coordinator._campaign_started_at == started, "the start instant moved"

    assert frozen is not None, "the witness: the campaign must have started"
    assert frozen != pytest.approx(4.0), "and not from the replacement publication"
