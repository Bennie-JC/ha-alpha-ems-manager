"""beta.36: a row ending is not a run ending, and neither is a campaign ending.

**Gate 1 of the release.** Two Live charge campaigns were destroyed on consecutive
days by two different endings taking the same door: a row *reaching its objective*
on 2026-08-30, and a row *resting because production covered it* on 2026-08-31.
Both reached ``_abandon_execution``, which latched the campaign identity for the
session -- and because ``campaign_identity`` is a digest of the campaign's end, that
identity is byte-identical across every republication of one live campaign. So one
teardown barred the campaign from ever admitting a plan again.

See ``beta36_trace`` for both measured sequences.

Every test below names a **published witness** before asserting the fix, so none can
pass on a branch it never reached, and each is red at ``276b931``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.alpha_ems_manager.const import (
    CONTROL_INHIBIT_REASONS,
    CONTROL_MIN_POWER_KW,
    EXECUTION_ABORT_STOP_REASONS,
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    EXECUTION_STOP_NO_BATTERY_PLAN,
    EXECUTION_STOP_QUARTER_EXPIRED,
    EXECUTION_STOP_QUARTER_TARGET_REACHED,
    EXECUTION_STOP_REASON_CLASSES,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    HOLD_REASON_QUARTER_SATISFIED,
    HOLD_REASON_RATE_BELOW_RESOLUTION,
    INHIBIT_HAZARD_REASONS,
    INHIBIT_NO_BATTERY_PLAN,
    INHIBIT_NO_COMMAND_REASONS,
    INHIBIT_NOTHING_TO_COMMAND,
    INHIBIT_POWER_BELOW_DEVICE_MINIMUM,
    INHIBIT_REASON_CLASSES,
    INHIBIT_WITHDRAWAL_REASONS,
    MIN_EXECUTABLE_QUARTER_KWH,
    OWNERSHIP_OWNED,
    QUARTER_END_TARGET_REACHED,
    REASON_VOCABULARY_CAMPAIGN_END,
    SHORTFALL_TARGET_REACHED,
    STOP_SCOPE_CAMPAIGN,
    TICK_HELD_QUARTER_SATISFIED,
    TICK_HELD_RATE_BELOW_RESOLUTION,
)
from custom_components.alpha_ems_manager.economic import (
    RUN_STATE_CHARGE,
    RUN_STATE_IDLE,
    campaign_identity,
)
from custom_components.alpha_ems_manager.execution import (
    CARRY_REFUSED_ADMISSION_ABANDONED,
    admit,
    carry_forward,
    parse_target,
)

from .beta36_trace import (
    CAMPAIGN_DIRECTION,
    CAMPAIGN_ID,
    EXECUTABLE_ROWS,
    GAP_ROW,
    PLAN_A,
    PLAN_B,
    ROW_BATTERY_KWH,
    RUN_GAP_ROW,
    TICK_BATTERY_KWH,
    campaign_end,
    charging_site,
    drive_quarter,
    opens_at,
    published_target,
    publishing,
    step_clock,
    tick_at,
)
from .conftest import BATTERY_POWER
from .forecast_helpers import NORMAL as NORMAL_DAY
from .test_beta24_live_charge import (
    BOOLEAN_EXECUTION_OWNER,
    LiveSurface,
    charge_now_price,
    live_coordinator,
    step_once,
)

pytestmark = pytest.mark.usefixtures("control_surface")

QUARTER = timedelta(minutes=15)


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


async def start_the_charge_campaign(
    hass, config_data, frank, live_surface, monkeypatch
):
    """Arm row 0 of the campaign through production, and return the coordinator.

    **Nothing here writes an ownership claim by hand.** Ownership has to be *proven*
    against the running dispatch, and a synthetic record proves nothing -- so the
    frozen schedule is installed on a clean Live coordinator, the first row is
    derived from it, and two real refreshes arm and then sustain exactly as the
    hardware did.

    Every vacuity gate is asserted **inside the fixture**, because
    ``_close_campaign`` files nothing at all for a campaign that never started: a
    fixture that failed to start one would make every terminal assertion downstream
    pass trivially, which is the shape of green test this release exists to distrust.
    """
    from .forecast_helpers import history_before, seed
    from .frank_capture import synthetic_day
    from .test_economic_published import allow_trading

    coordinator = await live_coordinator(hass, config_data)
    seed(coordinator, history_before(NORMAL_DAY))
    frank.publish(
        today=synthetic_day(NORMAL_DAY, price_at=charge_now_price), tomorrow=None
    )
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    publishing(coordinator, monkeypatch)
    charging_site(hass)

    # **Nothing is installed by hand. beta.36.** The rolling publication is the only
    # input, so ``carry_forward`` mints the run, ``carry_plan`` admits the schedule
    # and the arm claims under the run the schedule adopted -- which is the identity
    # a boundary sustain depends on, and the one a pre-installed plan cannot have,
    # because the run id does not exist until production mints it.
    armed = await step_once(hass, coordinator, live_surface, **step_clock(0))
    assert coordinator._plan is not None, armed
    assert boundary_of(armed).get("sequence") == "arm", boundary_of(armed)
    sustained = await step_once(hass, coordinator, live_surface, **step_clock(0))
    execution = sustained.get("execution") or {}
    assert (execution.get("ownership") or {}).get("state") == OWNERSHIP_OWNED, execution

    # --- the vacuity gates ------------------------------------------------
    #
    # **The identity is checked against production's own function, not against the
    # fixture's constant.** ``_campaign_id == CAMPAIGN_ID`` alone is satisfied by any
    # string the fixture invents, so it proves the coordinator copies a field and
    # nothing more -- and the whole 2026-08-30 failure turns on the coordinator and
    # the optimiser agreeing about what a campaign *is*.
    assert (
        campaign_identity(CAMPAIGN_DIRECTION, dt_util.as_utc(campaign_end()))
        == CAMPAIGN_ID
    ), "the fixture's campaign id is not the one production would derive"
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_started_at is not None, "the campaign must Start"
    assert coordinator._campaign_instance_id is not None
    assert coordinator._campaign_opened_at is not None
    frozen = coordinator._campaign_frozen_target_kwh
    assert frozen is not None and frozen > 1.0, frozen
    # **Read from the plan production is holding, not the one the test installed.**
    # ``carry_plan`` re-admits from the rolling publication and the plan then adopts
    # the carried run's identity, so a fixture asserting against its own object would
    # be checking a schedule the coordinator has already replaced.
    live = coordinator._plan
    assert live is not None
    assert live.plan_id == PLAN_A[0]
    executable = [row for row in live.rows if row.executable]
    assert len(executable) >= 3, "a boundary must be crossable"
    assert all(
        row.objective_kwh(live.intent) >= 10 * MIN_EXECUTABLE_QUARTER_KWH
        for row in executable
    )
    # The coordinator derived the identity; the test did not hand it over.
    assert coordinator._quarter.campaign_id == CAMPAIGN_ID == live.campaign_id
    record = coordinator.store.execution_record or {}
    assert record.get("run_id") == live.run_id
    live_surface.calls.clear()
    return coordinator


#: Why every refresh below sits on a quarter boundary.
#:
#: ``deadman_minutes`` alternates ``(20, 25)`` off the duration the device is
#: currently holding, and ``_deadman_is_stale`` requires each sustain's deadline to
#: be strictly later than the last one's. Fifteen minutes apart that always holds --
#: which is the production cadence, and the invariant the alternation was designed
#: for. Two refreshes five minutes apart do **not**: ``10:50 + 20`` lands on the same
#: instant as ``10:45 + 25``, and the run is correctly judged to have stalled. So a
#: test that needs a refresh mid-campaign takes it at the next boundary rather than
#: inventing an extra one, and the sixty-second cadence is driven by ``tick_at``.
_REFRESH_CADENCE = "one refresh per quarter boundary"


def boundary_of(report) -> dict:
    """Return the write boundary of one control report."""
    return ((report.get("execution") or {}).get("write_boundary")) or {}


def admission_of(report) -> dict:
    """Return the admission block of one control report."""
    return ((report.get("execution") or {}).get("admission")) or {}


# ===========================================================================
# S8 / S16 -- the two partitions, which are pure and cost nothing to check
# ===========================================================================


def test_every_stop_reason_belongs_to_exactly_one_vocabulary() -> None:
    """**S8. Seven reasons belonged to none of the three, and four of them stop.**

    ``_decide`` sets ``reset_required=owned`` for ``target_reached``,
    ``battery_ceiling``, ``grid_ceiling`` and ``headroom_reached`` -- four ordinary
    *successful* endings -- and every one reached the total-teardown helper and
    latched its campaign, because the classification the teardown consulted did not
    contain them. ``EXECUTION_COMPLETION_STOP_REASONS`` existed and was read in one
    place: the outcome verdict, never the teardown path.

    A partition costs nothing to assert and would have caught this before hardware.
    """
    from custom_components.alpha_ems_manager.const import EXECUTION_STOP_REASONS

    classes = EXECUTION_STOP_REASON_CLASSES
    assert set(classes) == {"withdrawal", "completion", "abort"}

    seen: dict[str, list[str]] = {}
    for name, members in classes.items():
        for reason in members:
            seen.setdefault(reason, []).append(name)

    twice = {reason: where for reason, where in seen.items() if len(where) > 1}
    assert not twice, f"a stop reason in two vocabularies: {twice}"
    unclassified = set(EXECUTION_STOP_REASONS) - set(seen)
    assert not unclassified, f"a stop reason in no vocabulary: {unclassified}"
    # The four successes of the paragraph above, named so a regression is legible.
    for reason in (
        EXECUTION_STOP_QUARTER_TARGET_REACHED,
        EXECUTION_STOP_QUARTER_EXPIRED,
        EXECUTION_STOP_CAMPAIGN_COMPLETE,
    ):
        assert reason in EXECUTION_COMPLETION_STOP_REASONS
        assert reason not in EXECUTION_ABORT_STOP_REASONS


def test_every_inhibit_belongs_to_exactly_one_class() -> None:
    """**S16, and the hazard class is derived by subtraction on purpose.**

    ``unsafe_while_owned`` promoted *any* unsafe verdict on an owned live dispatch to
    ``EXECUTION_STOP_SAFETY``, and the specific ``inhibit_reason`` was then discarded
    -- the campaign terminal carries no verdict field. Two members of the vocabulary
    are not hazards at all, and one of them destroyed a working campaign.

    The partition is fail-closed: withdrawal and no-command are *enumerated*, hazard
    is everything else, so an inhibit added in a later release is a hazard until
    somebody argues otherwise in a diff. A default-permit here would be strictly
    worse than the bug.
    """
    classes = INHIBIT_REASON_CLASSES
    assert set(classes) == {"hazard", "withdrawal", "no_command"}

    seen: dict[str, list[str]] = {}
    for name, members in classes.items():
        for reason in members:
            seen.setdefault(reason, []).append(name)

    twice = {reason: where for reason, where in seen.items() if len(where) > 1}
    assert not twice, f"an inhibit in two classes: {twice}"
    assert set(seen) == set(CONTROL_INHIBIT_REASONS)

    assert INHIBIT_NO_BATTERY_PLAN in INHIBIT_WITHDRAWAL_REASONS
    assert INHIBIT_NOTHING_TO_COMMAND in INHIBIT_NO_COMMAND_REASONS
    # **The second member of the no-command class, and the 2026-08-31 one.** A rate
    # that quantises into ``(0, CONTROL_MIN_POWER_KW)`` is not a hazard: ``const.py``
    # has said since beta.24 that the undelivered energy is at most one step over one
    # interval and the inverter's own behaviour covers it. That reasoning sat beside
    # an abort family declared unsuppressable, and nothing ever read the two against
    # each other.
    assert INHIBIT_POWER_BELOW_DEVICE_MINIMUM in INHIBIT_NO_COMMAND_REASONS
    assert INHIBIT_POWER_BELOW_DEVICE_MINIMUM not in INHIBIT_HAZARD_REASONS
    # Nothing in the hazard list was weakened, and it is still the majority.
    assert len(INHIBIT_HAZARD_REASONS) > len(CONTROL_INHIBIT_REASONS) / 2
    for reason in ("soc_stale", "would_export", "dispatch_active"):
        assert reason in INHIBIT_HAZARD_REASONS


def test_the_withdrawal_and_abort_families_still_do_not_overlap() -> None:
    """beta.35's own partition, re-asserted over the widened sets.

    Widening three vocabularies at once is exactly how two of them come to share a
    member, and a reason that could be both withheld *and* fatal would resolve by
    evaluation order.
    """
    withdrawal = set(EXECUTION_WITHDRAWAL_STOP_REASONS)
    abort = set(EXECUTION_ABORT_STOP_REASONS)
    completion = set(EXECUTION_COMPLETION_STOP_REASONS)

    assert not withdrawal & abort
    assert not withdrawal & completion
    assert not abort & completion
    assert EXECUTION_STOP_STAGE_A_HOLD in withdrawal
    assert EXECUTION_STOP_NO_BATTERY_PLAN in withdrawal
    assert EXECUTION_STOP_SAFETY in abort


# ===========================================================================
# S1 / S2 -- a quarter reaching its target is a success
# ===========================================================================


async def test_a_row_reaching_its_target_holds_and_the_campaign_survives(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S1. The 2026-08-30 defect, reproduced by measurement and then fixed.**

    Row 1's objective is 0.28 kWh and the site delivers 0.02333 kWh a tick, so the
    target is met on tick 12 -- strictly inside the row, with four executable rows
    and a second admitted plan still ahead. beta.35 called
    ``_async_end_quarter`` there, which called the abort helper, which latched the
    campaign for the session.

    The row now **rests**: zero is commanded once, ownership, the claim, the frozen
    schedule and the campaign instance all stay, and the next boundary picks up the
    next row.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan_admitted_at = coordinator._plan.admitted_at
    instance = coordinator._campaign_instance_id

    # Row 0 falls short of 0.56 kWh at 1.4 kW and hands over.
    await drive_quarter(hass, coordinator, live_surface, 0)
    await step_once(hass, coordinator, live_surface, **step_clock(1))
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(1)

    # Row 1 reaches its own objective part-way through.
    reasons = await drive_quarter(hass, coordinator, live_surface, 1)

    # --- the published witness, first ------------------------------------
    assert TICK_HELD_QUARTER_SATISFIED in reasons, reasons
    assert coordinator._quarter_target_reached_at is not None
    assert coordinator._quarter_battery_kwh >= ROW_BATTERY_KWH[1]

    # --- and then the fix -------------------------------------------------
    assert coordinator._plan is not None, "beta.35 nulled the schedule here"
    assert coordinator._plan.admitted_at == plan_admitted_at, "same admission"
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_instance_id == instance
    assert coordinator._closed_campaign is None, "a success files no terminal"
    assert coordinator._hold_reason == HOLD_REASON_QUARTER_SATISFIED

    # And the refresh says so too, in its own vocabulary. The tick and the write
    # boundary are two writers and both have to agree that this row is resting --
    # a refresh that went on sustaining would re-arm a charge into a met objective
    # for the remainder of the quarter.
    resting = await step_once(hass, coordinator, live_surface, **step_clock(1))
    assert boundary_of(resting).get("sequence") == "hold", boundary_of(resting)
    assert boundary_of(resting).get("hold_reason") == HOLD_REASON_QUARTER_SATISFIED

    # The rest is a rest: zero commanded, and nothing torn down.
    assert coordinator._applied_setpoint_kw == 0.0
    provenance = coordinator._row_provenance(opens_at(1))
    assert provenance["hold_writes"] >= 1, provenance

    # And row 2 then arms under the same campaign.
    report = await step_once(hass, coordinator, live_surface, **step_clock(2))
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(2)
    assert coordinator._quarter.campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_quarters_admitted >= 2
    assert boundary_of(report).get("sequence") in ("sustain", "arm", "hold")


async def test_a_completion_is_not_an_abandonment_but_a_hazard_still_is(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S2, with its own negative control in the same test.**

    The fix must not degenerate into "never remember". The 2026-08-29 zombie -- a
    torn-down campaign whose frozen schedule advanced a row and re-armed the
    inverter fifteen minutes later -- is what the latch exists to prevent, and a
    genuine hazard must still latch the admission it aborted.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)
    await step_once(hass, coordinator, live_surface, **step_clock(1))
    await drive_quarter(hass, coordinator, live_surface, 1)

    assert coordinator._quarter_target_reached_at is not None, "the witness"
    assert not coordinator._abandoned_admissions, "a success latches nothing"
    assert not coordinator._final_campaigns
    report = await step_once(hass, coordinator, live_surface, **step_clock(2))
    assert admission_of(report).get("abandoned_admissions") == 0

    # **The teardown helper itself, at both ends of the branch.** The three stop
    # paths all reach it, and one of them -- the refresh's own reset -- fires for
    # endings that are not aborts at all: a run reaching its ``window_end``, a
    # withdrawal standing once the plan's authority is genuinely spent. Latching
    # those would kill an admission that did nothing wrong.
    plan = coordinator._plan
    assert plan is not None
    key = plan.admission_key
    coordinator._abandon_execution(opens_at(2), EXECUTION_STOP_WINDOW_ENDED)
    assert key not in coordinator._abandoned_admissions, (
        "a window closing on time is not an abandonment"
    )

    # And the negative control: a real abort of the same admission does latch it,
    # under the *admission* key and not the campaign identity -- which is what made
    # one teardown bar a live campaign for a whole session.
    coordinator._plan = plan
    coordinator._abandon_execution(opens_at(2), EXECUTION_STOP_SAFETY)
    assert key in coordinator._abandoned_admissions
    assert plan.campaign_id not in coordinator._abandoned_admissions
    assert coordinator._plan is None
    assert coordinator._closed_campaign is not None


# ===========================================================================
# S3 / S4 / S9 -- the schedule survives, and so does the per-row rate
# ===========================================================================


async def test_the_campaign_walks_every_row_of_both_plans(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S3, S4 and S9 in one drive, because they are one behaviour.**

    beta.35 lost the schedule at row 1 and never recovered it: ``_plan is None`` at
    every refresh thereafter, for ever. So this walks all eight rows -- through a
    mid-plan completion, through a ``serve_load`` gap, and across the boundary
    between two admitted plans of one campaign -- and asserts at every one that the
    frozen schedule is still there and the per-row rate is still being used.

    ``desired_grid_kw`` is the discriminator that cannot be faked:
    ``control_intent_for``, the run-level fallback the 2026-08-30 charge ran on for
    five and a half hours, structurally cannot produce a per-row rate.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    completed_before = len(coordinator._completed_quarters)
    instance = coordinator._campaign_instance_id
    admissions: set[str] = {coordinator._plan.admission_key}
    walked: list[int] = []

    for index in range(len(ROW_BATTERY_KWH)):
        # Row 0 was armed by the fixture; a second refresh in the same minute would
        # fail the dead-man's own advancement test. See ``_REFRESH_CADENCE``.
        report = (
            coordinator.control_report or {}
            if index == 0
            else await step_once(hass, coordinator, live_surface, **step_clock(index))
        )
        if index == GAP_ROW:
            # A ``serve_load`` interval inside a plan is published and never armable.
            # The schedule must survive it: beta.35 tore it down and lost every row
            # after it, which is D2b.
            assert coordinator._plan is not None, "the gap must not clear the schedule"
            assert coordinator._campaign_id == CAMPAIGN_ID
            continue
        if index == RUN_GAP_ROW:
            # The space between two runs. No plan covers it and none should -- but
            # the *campaign* continues, because Stage A is still publishing its
            # second run. This is the branch ``_campaign_objective_kwh`` has a
            # fallback for and nothing had ever exercised.
            assert coordinator._campaign_id == CAMPAIGN_ID, "the campaign continues"
            assert coordinator._closed_campaign is None
            continue
        assert coordinator._plan is not None, f"row {index}: schedule gone"
        assert coordinator._plan.campaign_id == CAMPAIGN_ID
        # **One admission per plan, and one instance for the whole campaign.**
        # Recovering by re-admitting after an illegitimate teardown looks identical
        # to never tearing down if only the *presence* of a plan is asserted -- and
        # a re-admission means a re-arm, a fresh claim and a re-anchored dead-man
        # every time. Two plans legitimately produce two admission keys; a third
        # would mean something re-admitted mid-plan.
        admissions.add(coordinator._plan.admission_key)
        assert len(admissions) <= 2, admissions
        assert coordinator._campaign_instance_id == instance, "one attempt, throughout"
        assert coordinator._closed_campaign is None, "no terminal mid-campaign"
        assert not coordinator._abandoned_admissions
        assert coordinator._quarter is not None, f"row {index}: no admitted row"
        assert coordinator._quarter.quarter_start == opens_at(index)
        controller = report.get("controller") or {}
        if controller.get("desired_grid_kw") is not None:
            assert controller["desired_grid_kw"] == pytest.approx(
                coordinator._quarter.initial_desired_grid_kw, abs=0.05
            ), f"row {index}: the run-level fallback has no per-row rate"
        walked.append(index)
        await drive_quarter(hass, coordinator, live_surface, index, ticks=14)

    assert walked == list(EXECUTABLE_ROWS), walked
    # Both plans were admitted, and the second one belongs to the same campaign.
    assert coordinator._plan is not None
    assert coordinator._plan.plan_id == PLAN_B[0]
    assert coordinator._campaign_realized_kwh > 0.0
    # Every executable row but the last: row 7 is still open when the walk ends,
    # and a row that has not finished has nothing to record.
    assert len(coordinator._completed_quarters) - completed_before >= (
        len(EXECUTABLE_ROWS) - 1
    )


# ===========================================================================
# S5 / S13 -- a withdrawal cannot reset a campaign still executing
# ===========================================================================


async def test_a_stage_a_hold_is_withheld_while_the_row_is_executing(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S5. beta.35 built this suppression and then made it unreachable.**

    ``_plan_authority_holds`` returns ``False`` whenever ``self._plan is None``, so
    once the completion latch had nulled the schedule the withdrawal at 12:45:06Z had
    nothing to outrank. With the schedule intact it is withheld, published, and the
    dispatch keeps running.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)

    assert coordinator._plan_authority_holds(opens_at(1)) is True
    monkeypatch.setattr(
        type(coordinator), "_execution_targets", lambda self, **kwargs: ()
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    authority = boundary_of(report).get("authority") or {}

    assert authority.get("plan_authority_holds") is True, authority
    assert authority.get("withheld_stop_reason") == EXECUTION_STOP_STAGE_A_HOLD
    assert boundary_of(report).get("stop_reason") is None
    assert coordinator._plan is not None
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert hass.states.get(BOOLEAN_EXECUTION_OWNER).state == "on"


async def test_stage_a_publishing_no_battery_plan_is_withheld_not_fatal(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S13. The two kinds of nothing, and only one of them is about the future.**

    Stage A publishing no ``BatteryPlan`` is the most extreme form of Stage A
    revising the future, so it is a withdrawal and is bounded exactly as every other
    withdrawal is -- by the plan's own end, by the row covering this instant, and by
    the vendor dead-man. Through beta.35 it arrived at the write boundary as an
    unsafe verdict and was promoted to ``safety``: one missing solve, one destroyed
    campaign.

    The paired guard is in ``test_a_genuine_hazard_still_aborts_unsuppressed``.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)
    key = coordinator._plan.admission_key
    assert coordinator._plan_authority_holds(opens_at(1)) is True

    # Stage A solves and publishes nothing for one refresh. Injected at the report
    # boundary because that is where ``plan is None`` becomes ``plan_problem``, and
    # the whole point is to exercise the real gate rather than a stand-in for it.
    kind = type(coordinator)
    original = kind._build_control_report

    def without_a_battery_plan(self, *, plan, **kwargs):
        return original(self, plan=None, **kwargs)

    monkeypatch.setattr(kind, "_build_control_report", without_a_battery_plan)
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    boundary = boundary_of(report)
    authority = boundary.get("authority") or {}
    safety = report.get("safety") or {}

    # --- the published witness, first ------------------------------------
    assert safety.get("inhibit_reason") == INHIBIT_NO_BATTERY_PLAN, safety
    assert safety.get("safe") is False

    # --- and then the fix -------------------------------------------------
    assert boundary.get("stop_reason") is None, "beta.35 reported safety here"
    assert authority.get("plan_authority_holds") is True
    assert authority.get("withheld_stop_reason") == EXECUTION_STOP_NO_BATTERY_PLAN
    assert coordinator._plan is not None
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert key not in coordinator._abandoned_admissions
    assert coordinator._closed_campaign is None

    # The classification behind it, so a regression in either half is legible.
    assert EXECUTION_STOP_NO_BATTERY_PLAN in EXECUTION_WITHDRAWAL_STOP_REASONS
    assert INHIBIT_NO_BATTERY_PLAN not in INHIBIT_HAZARD_REASONS
    # The published string is unchanged, so an existing automation is unaffected.
    assert INHIBIT_NO_BATTERY_PLAN == "no_plan"
    assert INHIBIT_NOTHING_TO_COMMAND != INHIBIT_NO_BATTERY_PLAN

    # The paired guard: with the authority genuinely spent, the same condition is
    # fatal. "Ignore bad news" is not the fix.
    coordinator._plan = None
    assert coordinator._plan_authority_holds(opens_at(1)) is False


# ===========================================================================
# S10 / S11 / S12 -- resting because the rate collapsed, and recovering
# ===========================================================================


async def test_a_rate_below_the_actuator_resolution_holds_and_recovers(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S10 and S11. The 2026-08-31 defect, and the property that makes it a rest.**

    Production rises to cover the house, the authorised rate collapses below two
    actuator steps, and beta.35 turned that into ``EXECUTION_STOP_SAFETY``: an
    unsuppressable total abort of a campaign whose plant was working perfectly.

    A sub-resolution rate is not a satisfied row and the difference is
    recoverability: it comes back on its own the moment production drops, **inside
    the same row**, and the row resumes with the same admission.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan_admitted_at = coordinator._plan.admitted_at
    instance = coordinator._campaign_instance_id
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=3)
    before = coordinator._quarter_battery_kwh
    assert before > 0.0, "the row was charging before the sun came out"

    # **The measured state of 2026-08-31, reproduced in its own two terms.**
    #
    # ``decide_charge`` caps the battery at ``pv_surplus + grid_rate_cap``, so the
    # rate collapses only when *both* collapse. The row's import ceiling is spent --
    # the capture showed 70 % and 96 % on the two rows that mattered -- and PV then
    # covers the house with a sliver to spare. The accumulator is set rather than
    # driven because fifteen minutes at 1.4 kW cannot spend a ceiling; it is a
    # measured quantity either way, and the suite already sets it this way.
    row = coordinator._quarter
    assert row is not None
    coordinator._quarter_grid_import_kwh = row.grid_authorised_kwh
    charging_site(hass, pv_w=SITE_COVERS_THE_HOUSE, battery_w=-50.0)

    at = opens_at(0) + timedelta(minutes=5)
    setpoint = coordinator._dispatch_setpoint(at)
    assert setpoint is not None
    # The witness: the rate genuinely collapsed, and for the documented reason.
    assert abs(setpoint.applied_kw) < CONTROL_MIN_POWER_KW, setpoint.as_dict()
    await tick_at(hass, coordinator, live_surface, at)

    assert coordinator._last_tick_reason == TICK_HELD_RATE_BELOW_RESOLUTION
    assert coordinator._hold_reason == HOLD_REASON_RATE_BELOW_RESOLUTION
    assert coordinator._quarter_target_reached_at is None, "not satisfied -- resting"
    assert coordinator._plan is not None, "beta.35 destroyed the schedule here"
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_instance_id == instance
    assert coordinator._closed_campaign is None, "no terminal, and no safety stop"
    assert not coordinator._abandoned_admissions

    # S11: the clamp lifts and the same row resumes.
    charging_site(hass)
    coordinator._quarter_grid_import_kwh = 0.0
    await tick_at(hass, coordinator, live_surface, opens_at(0) + timedelta(minutes=6))
    await tick_at(hass, coordinator, live_surface, opens_at(0) + timedelta(minutes=7))

    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(0), "the same row"
    assert coordinator._plan.admitted_at == plan_admitted_at, "the same admission"
    assert coordinator._quarter_battery_kwh > before, "it resumed charging"


#: PV large enough to cover the 2.0 kW house and leave only a sliver of surplus.
SITE_COVERS_THE_HOUSE = 2050.0


async def test_a_genuine_hazard_still_aborts_unsuppressed(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S12, the negative control for S10, S13 and the whole inhibit partition.**

    A stale battery-power reading while a dispatch of ours is running is a hazard: the
    controller cannot say what the pack is doing. That must still abort, still
    unsuppressably, still with authority holding -- and it must still latch the
    admission. If this ever passes by holding instead, the release has traded one
    silent failure for a worse one.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)
    key = coordinator._plan.admission_key
    assert coordinator._plan_authority_holds(opens_at(1)) is True

    # A reading the controller cannot interpret, injected the way the suite does.
    hass.states.async_set(
        BATTERY_POWER,
        "unavailable",
        {"unit_of_measurement": "W", "device_class": "power"},
    )
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    boundary = boundary_of(report)

    assert boundary.get("stop_reason") == EXECUTION_STOP_SAFETY, boundary
    assert (boundary.get("authority") or {}).get("withheld_stop_reason") is None
    assert coordinator._plan is None, "a hazard is a total teardown"
    assert key in coordinator._abandoned_admissions
    terminal = coordinator._closed_campaign or {}
    assert terminal.get("reason") == EXECUTION_STOP_SAFETY
    # The rule the Live watch list turns on: a safety terminal must have a hazard.
    safety = report.get("safety") or {}
    assert safety.get("inhibit_reason") in INHIBIT_HAZARD_REASONS, safety


# ===========================================================================
# S6 -- the terminal counts the quarter it closed on
# ===========================================================================


async def test_the_terminal_counts_the_row_it_closed_on(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S6, asserted from the public payload and as equalities.**

    ``_async_end_quarter`` stopped at the physical layer -- which reaches
    ``_close_campaign``, which nulled ``_campaign_id`` -- and only *then* recorded the
    row, so ``_accrue_campaign_progress`` returned early and the quarter that caused
    the terminal was missing from it. Measured on 2026-08-30: 0.27 kWh reported
    against 0.548 kWh delivered by the rows, and ``quarters_admitted: 2`` against
    three.

    ``_close_campaign`` also read ``_campaign_realized_now()`` *after* nulling the
    identity, and ``_open_quarter_objective_kwh`` compares the row's campaign against
    it -- so the open-quarter term was structurally always ``0.0`` while the live
    figure beside it included the row. The two disagreed by exactly the closing
    quarter, from the public payload alone.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    for index in (0, 1, 2):
        await drive_quarter(hass, coordinator, live_surface, index, ticks=14)
        await step_once(hass, coordinator, live_surface, **step_clock(index + 1))

    # **A row still open and not yet accrued, which is the only state in which the
    # open-quarter term is non-zero.** ``_close_campaign`` promised to include it and
    # nulled ``_campaign_id`` first, so ``_open_quarter_objective_kwh`` compared the
    # row's campaign against ``None`` and returned ``0.0`` every time -- while the
    # live ``open_campaign`` figure beside it used the same helper with the campaign
    # still open and *did* include it. The two published figures disagreed by exactly
    # the closing quarter.
    # Row 4, not row 3: row 3 is the ``serve_load`` interval and is never derived,
    # so it cannot be an open row and the term would stay zero for the wrong reason.
    await step_once(hass, coordinator, live_surface, **step_clock(4))
    await drive_quarter(hass, coordinator, live_surface, 4, ticks=5)
    open_row = coordinator._quarter
    assert open_row is not None, "an executable row must be open for this to bite"
    assert open_row.quarter_start == opens_at(4)
    assert coordinator._campaign_accrued_row != open_row.quarter_start
    assert coordinator._quarter_battery_kwh > 0.0, "and it must have moved energy"

    live_before_close = coordinator._campaign_realized_now()
    accrued = coordinator._campaign_realized_kwh
    rows_realised = sum(
        entry.get("realized_battery_kwh") or 0.0
        for entry in coordinator._completed_quarters
        if entry.get("campaign_id") == CAMPAIGN_ID
    )
    assert rows_realised > 0.0, "the witness: rows measured something"

    coordinator._close_campaign(
        opens_at(4) + timedelta(minutes=6), EXECUTION_STOP_CAMPAIGN_COMPLETE
    )
    terminal = coordinator._closed_campaign or {}

    assert terminal, "a started campaign files a terminal"
    # ``abs`` is the terminal's own three-decimal rounding. The defect it catches
    # drops the whole open row -- 0.117 kWh here, two orders of magnitude larger.
    assert terminal["objective_realized_kwh"] == pytest.approx(
        live_before_close, abs=1e-3
    ), "the terminal and the live figure may not disagree"
    # The *accrued* total is the sum of the completed rows, exactly. The terminal is
    # that plus the open row it closed on, which is why the two are asserted apart:
    # collapsing them would need one of the three figures to be wrong.
    assert accrued == pytest.approx(rows_realised, abs=TICK_BATTERY_KWH)
    assert terminal["objective_realized_kwh"] > accrued, (
        "the open row's energy is in the terminal, not dropped on the way out"
    )
    assert terminal["reason_vocabulary"] == REASON_VOCABULARY_CAMPAIGN_END
    assert terminal["campaign_instance_id"] == coordinator._closed_campaign.get(
        "campaign_instance_id"
    )
    assert terminal["planned_end"] == campaign_end().isoformat()
    assert terminal["rows_completed"] == terminal["quarters_admitted"]


# ===========================================================================
# S7 -- a quarter that moved nothing says why
# ===========================================================================


async def test_a_row_that_moved_nothing_publishes_a_refusal(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S7, the reporting invariant, and it claims nothing about the live cause.**

    The 0.56 kWh row of 2026-08-30 was admitted, derived, ticked against fifteen
    times and moved nothing, and its whole published trace was
    ``binding_clamps: ["quarter_expired"]`` -- which is also exactly what a mid-row
    teardown writes. No tick reason, no authorisation refusal and no write-boundary
    refusal could reach that record: ``binding_clamps`` is written only by
    ``_note_quarter_clamp``, whose callers pass a clamp or a shortfall.

    This asserts only that an executable row **either armed or said why**. It makes
    no claim about what happened at 06:15Z on the installation; that is not
    determinable from the capture and is not guessed here.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=13)
    await step_once(hass, coordinator, live_surface, **step_clock(1))

    entries = [
        entry
        for entry in coordinator._completed_quarters
        if entry.get("quarter_start") == opens_at(0).isoformat()
    ]
    assert entries, "the row was recorded"
    row = entries[0]

    assert row["armed"] or row["refusals"], (
        "an executable row either armed or refused and said why"
    )
    assert set(row["binding_clamps"]) != {"quarter_expired"} or row["armed"], (
        "expiry alone cannot be the whole account of a row"
    )
    # ``write_count`` counts *power* writes, so zero is a legitimate answer: a row
    # that armed and then held steady inside the dispatch deadband corrects nothing
    # for fifteen minutes, which is the common case rather than a fault. That is
    # exactly why it cannot be the invariant -- ``armed or refusals`` is.
    assert isinstance(row["write_count"], int)
    assert isinstance(row["arm_attempts"], int)
    assert row["armed"] is True, "this row was armed by production"


# ===========================================================================
# S14 / S15 -- the instance is the lifecycle key, and it may not be silent
# ===========================================================================


async def test_no_refresh_narrates_an_accumulating_run_without_a_lifecycle(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S15. The A9 third state, with the exemption that hid it removed.**

    At 11:30 on 2026-08-31 the payload showed an affirmed carried run,
    ``execution.state: "armed"``, ``battery_realized_kwh: 0.432`` still accumulating,
    ``forward_authorised_kwh: 9.44`` and ``binding_cap: "frozen"`` -- beside
    ``admitted_plan: null``, no campaign, dispatch inactive and ownership ``none``.
    Every field was locally honest and the composite was impossible.

    The one invariant test that could have caught it exempted exactly this shape with
    ``if not owned: continue``. That guard is **not** present here: a run whose
    progress is advancing must have a plan, or the payload must say why it does not.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)

    # Force the shape: the run survives, the plan is taken away.
    coordinator._plan = None
    report = await step_once(hass, coordinator, live_surface, **step_clock(1))
    admission = admission_of(report)

    assert admission, "the admission block must exist on every refresh"
    assert admission["admitted"] is False
    assert admission["refused"], (
        "a refresh with no admitted plan must name the clause that refused it"
    )
    # And the two layers agree about whether the attempt is alive.
    assert admission["run_carried"] is (coordinator._carried is not None)


async def test_an_aborted_instance_dies_and_a_new_one_may_live(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**S14. The approved asymmetry, and both halves of it.**

    An identity-scoped closure was the beta.35 rule and it is what killed both
    campaigns: ``campaign_identity`` is a digest of the campaign's end, so one
    teardown barred the identity for the session. An identity-scoped *closed* list
    would have done exactly the same thing one release later.

    A hazard abort ends one physical attempt. A genuinely new admission afterwards is
    a second attempt with its own instance, its own frozen objective and its own
    zeroed accounting -- and the first attempt's measured energy is never touched.
    A campaign that *finished* may not open another instance at all, or Stage A
    republishing it would loop for ever.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=4)
    first_instance = coordinator._campaign_instance_id
    first_opened = coordinator._campaign_opened_at
    first_key = coordinator._plan.admission_key

    coordinator._abandon_execution(opens_at(0) + timedelta(minutes=5), "safety")
    first_terminal = dict(coordinator._closed_campaign or {})
    assert first_terminal
    assert first_key in coordinator._abandoned_admissions

    # **The re-admission goes through production, not around it.** Setting
    # ``_plan`` by hand would bypass ``carry_forward`` and ``carry_plan`` -- the two
    # layers the latch actually acts on, and the two that disagreed for a whole
    # session on 2026-08-31 -- so a campaign-keyed latch or a missing
    # ``carry_forward`` guard would leave no trace here at all.
    report = await step_once(hass, coordinator, live_surface, **step_clock(PLAN_B[1]))
    admission = admission_of(report)

    assert coordinator._plan is not None, (
        f"a new admission was refused: {admission.get('refused')!r}"
    )
    assert coordinator._plan.plan_id == PLAN_B[0]
    assert coordinator._plan.admission_key != first_key
    assert admission["refused"] is None
    assert coordinator._carried is not None, "and the run layer agrees"
    assert coordinator._quarter is not None

    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_instance_id not in (None, first_instance)
    assert coordinator._campaign_opened_at != first_opened
    assert coordinator._campaign_realized_kwh == 0.0
    assert coordinator._closed_campaign == first_terminal, "the first is immutable"


async def test_the_head_state_survives_the_loss_of_the_row(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**D10. The bookkeeping stopped lying about the physics.**

    ``_head_run_state`` read the admitted row and nothing else, so whenever
    ``self._plan`` went missing -- which on 2026-08-30 was every refresh for five and
    a half hours -- it reported ``IDLE`` while the inverter was demonstrably charging
    under a live claim. Every Stage-A solve then paid a fresh run-start fee to
    continue a charge it was already running, silently reverting beta.35's own R9
    correction. ``test_beta36_counterfactual`` prices that fee.

    The carried run is the fallback because it is a fact of the same kind: a run
    Stage B is carrying, whose window covers this instant, under an ownership record
    Alpha EMS wrote itself. It is **not** a licence to invent a direction -- a
    genuinely torn-down execution still seeds ``IDLE``, which is the second half of
    this test.

    *Mutation: remove the carried-run fallback and this fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=4)
    at = opens_at(0) + timedelta(minutes=5)
    assert coordinator._head_run_state(at) == RUN_STATE_CHARGE, "the witness"

    # The row goes, the run stays, the inverter keeps charging.
    coordinator._plan = None
    coordinator._quarter = None
    assert coordinator._carried is not None, "the fallback needs something to read"
    assert coordinator._head_run_state(at) == RUN_STATE_CHARGE, (
        "a charge that is physically running is not idle"
    )

    # And nothing is invented once the execution is genuinely gone.
    coordinator._carried = None
    assert coordinator._head_run_state(at) == RUN_STATE_IDLE


async def test_ending_a_quarter_at_campaign_scope_counts_that_quarter(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Close-before-accrue, at the site that actually did it.**

    ``_async_end_quarter`` stopped the dispatch first, and the physical stop reaches
    ``_close_campaign``, which nulled ``_campaign_id``. Only *then* was the row
    recorded, so ``_accrue_campaign_progress`` returned early on its
    ``quarter.campaign_id != self._campaign_id`` guard and the quarter that **caused**
    the terminal was missing from the total. And ``_close_campaign`` read
    ``_campaign_realized_now()`` after nulling the identity, so the open-quarter term
    it promised to include was structurally always ``0.0``.

    Measured on 2026-08-30: 0.27 kWh reported against 0.548 delivered by the rows, and
    ``quarters_admitted: 2`` against three.

    Driven through the real helper, with a row carrying measured energy that has not
    been accrued yet -- which is the only state in which either defect is visible.

    *Mutations: move the accrual back after the stop, or null the identity before
    reading the total, and this fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=6)

    row = coordinator._quarter
    assert row is not None
    delivered = coordinator._quarter_battery_kwh
    assert delivered > 0.0, "the witness: this row moved energy"
    assert coordinator._campaign_accrued_row != row.quarter_start, (
        "and it has not been accrued yet, which is the whole point"
    )
    before = coordinator._campaign_realized_kwh
    rows_before = coordinator._campaign_quarters_admitted

    snapshot = coordinator._pending_snapshot
    await coordinator._async_end_quarter(
        opens_at(0) + timedelta(minutes=7),
        snapshot,
        QUARTER_END_TARGET_REACHED,
        SHORTFALL_TARGET_REACHED,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
        scope=STOP_SCOPE_CAMPAIGN,
    )
    terminal = coordinator._closed_campaign or {}

    assert terminal, "a started campaign files a terminal"
    assert terminal["quarters_admitted"] == rows_before + 1, (
        "the quarter that caused the terminal is counted in it"
    )
    # ``abs`` is the terminal's own three-decimal rounding and nothing more: the
    # defect this catches drops a whole row, two orders of magnitude larger.
    assert terminal["objective_realized_kwh"] == pytest.approx(
        before + delivered, abs=1e-3
    ), "and its energy is in the total, not dropped on the way out"
    assert terminal["rows_completed"] == terminal["quarters_admitted"]


def test_the_run_layer_refuses_an_admission_the_plan_layer_killed() -> None:
    """**D13. The one place the two layers have to agree.**

    ``carry_forward`` had no abandoned check at all, so with ``_carried`` cleared by an
    abort the next refresh minted a *fresh* run from a target still carrying the dead
    campaign, admitted 14.9 minutes early through ``ACTIONABLE_LEAD_MINUTES``. The run
    layer resurrected the attempt every fifteen minutes while the plan layer destroyed
    its plan on the same refresh -- self-sustaining until the session ended, and
    visible in the 2026-08-31 capture as ``abandoned_campaigns: 2`` beside an affirmed
    run whose progress was still accumulating.

    Asserted on the pure function, because that is where the guard lives and a
    coordinator-level test would prove it only for one arrangement of the two layers.

    *Mutation: drop the guard and this fails.*
    """
    target = parse_target(published_target(PLAN_A))
    assert target is not None
    carried = admit(target, opens_at(0) - QUARTER)
    assert carried is not None

    alive = carry_forward(
        carried,
        (published_target(PLAN_A),),
        opens_at(0) + timedelta(minutes=1),
        executable_intents=(EXECUTION_INTENT_GRID_CHARGE,),
    )
    assert alive.carried is not None, "the witness: it carries when nothing is dead"

    dead = carry_forward(
        carried,
        (published_target(PLAN_A),),
        opens_at(0) + timedelta(minutes=1),
        executable_intents=(EXECUTION_INTENT_GRID_CHARGE,),
        abandoned_admissions=frozenset({carried.admission_key}),
    )

    assert dead.carried is None, "a dead attempt is not carried, whatever is published"
    assert dead.refused == CARRY_REFUSED_ADMISSION_ABANDONED


# ===========================================================================
# the 2026-09-01 hardware contract: production feeds an unfinished row
# ===========================================================================


async def test_an_unfinished_row_absorbs_production_when_the_budget_is_spent(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The behavioural contract the hardware measurement demanded.**

    Measured on the reference inverter: Mode 2 at 0 kW is a *total* hold, so it
    suppresses battery charging too, and 1.3 kW of free production went to the meter.
    On an unfinished charge row that is indefensible -- battery energy is the
    objective, grid import is only a ceiling, and production may satisfy the
    objective.

    Asking why the controller ever wanted 0 kW there found the cause: the grid
    authorisation was applied twice, once as a term added to the surplus and again as
    a bare clamp on the battery. So this row now **commands the surplus** rather than
    holding at zero, and no forced export is created at all.

    Given an unfinished row with its objective remaining, its grid ceiling spent and
    production above house load, then: the battery absorbs, realised battery energy
    increases, the grid ceiling stays unspent because production paid for it, and the
    plan, campaign, admission and lifecycle are untouched.

    *Mutation: put the authorisation back into the battery clamp pass and this fails.*
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan_admitted_at = coordinator._plan.admitted_at
    instance = coordinator._campaign_instance_id
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=2)

    row = coordinator._quarter
    assert row is not None
    # The 2026-08-31 state: budget spent, objective still outstanding.
    coordinator._quarter_grid_import_kwh = row.grid_authorised_kwh
    battery_before = coordinator._quarter_battery_kwh
    import_before = coordinator._quarter_grid_import_kwh
    assert row.battery_allowance_kwh() - battery_before > 0.0, (
        "the witness: this row is unfinished"
    )

    # And the measured site: PV 2.8 kW against a 1.5 kW house.
    charging_site(hass, pv_w=2800.0, house_w=1500.0, battery_w=-1300.0)

    at = opens_at(0) + timedelta(minutes=4)
    setpoint = coordinator._dispatch_setpoint(at)
    assert setpoint is not None
    # **A commandable charge, not a hold.** This is the whole contract.
    assert abs(setpoint.applied_kw) >= CONTROL_MIN_POWER_KW, setpoint.as_dict()
    assert setpoint.applied_kw < 0.0, "and it is a charge"
    # No forced export: absorbing the surplus keeps the meter at or above zero.
    assert setpoint.desired_grid_kw >= -0.05, setpoint.as_dict()

    await tick_at(hass, coordinator, live_surface, at)
    await tick_at(hass, coordinator, live_surface, at + timedelta(minutes=1))

    assert coordinator._last_tick_reason != TICK_HELD_RATE_BELOW_RESOLUTION
    assert coordinator._hold_reason is None
    assert coordinator._quarter_battery_kwh > battery_before, (
        "realised battery energy must increase: production satisfies the objective"
    )
    # Production paid for it, so the ceiling is not consumed further.
    assert coordinator._quarter_grid_import_kwh == pytest.approx(
        import_before, abs=1e-6
    ), "a PV-sourced charge must not spend grid authorisation"

    # And nothing about the lifecycle moved.
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(0)
    assert coordinator._plan is not None
    assert coordinator._plan.admitted_at == plan_admitted_at
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._campaign_instance_id == instance
    assert coordinator._closed_campaign is None
    assert not coordinator._abandoned_admissions


async def test_the_same_row_switches_back_to_grid_charging_when_production_goes(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**Same-row recovery, and it needs no re-arm because nothing was released.**

    Conditions change mid-row: production collapses, so an executable active command
    is needed again and it must come from the grid authorisation that remains. The row
    commands it on the very next sixty-second tick -- the same row, the same
    admission, the same campaign instance -- because the dispatch was never stopped.

    That is why this release does **not** release Mode 2 mid-row. Ownership in this
    design is defined by a running dispatch: ``ownership_of`` answers ``none`` the
    instant ``dispatch_active`` is false, and a marker on with no dispatch behind it
    is by definition stale and gets released on the next refresh. Pausing the
    dispatch to let the inverter fall back to its own behaviour would therefore mean
    giving up the claim, the per-row grid ceiling and the frozen objective, and
    re-acquiring them by claiming the marker again from the tick. Correcting the
    domain error instead keeps the battery under our command for the whole row, so
    none of that is needed -- and natural fallback discharge, whose interaction with
    the frozen objective could not have been guaranteed, never arises.

    Realised energy is continuous across the change, the grid ceiling still binds, and
    nothing is carried anywhere.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    plan_admitted_at = coordinator._plan.admitted_at
    instance = coordinator._campaign_instance_id
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=2)

    row = coordinator._quarter
    assert row is not None
    # Half the budget spent, so an active grid-fed command is still available later.
    coordinator._quarter_grid_import_kwh = row.grid_authorised_kwh / 2.0

    # Phase one: production covers the charge.
    charging_site(hass, pv_w=2800.0, house_w=1500.0, battery_w=-1300.0)
    at = opens_at(0) + timedelta(minutes=4)
    await tick_at(hass, coordinator, live_surface, at)
    await tick_at(hass, coordinator, live_surface, at + timedelta(minutes=1))
    after_pv = coordinator._quarter_battery_kwh
    import_after_pv = coordinator._quarter_grid_import_kwh
    assert after_pv > 0.0, "the witness: phase one charged"

    # Phase two: production goes. The grid authorisation is what is left.
    charging_site(hass, pv_w=0.0, house_w=1500.0, battery_w=-1400.0)
    later = at + timedelta(minutes=2)
    setpoint = coordinator._dispatch_setpoint(later)
    assert setpoint is not None
    assert abs(setpoint.applied_kw) >= CONTROL_MIN_POWER_KW, setpoint.as_dict()

    await tick_at(hass, coordinator, live_surface, later)
    await tick_at(hass, coordinator, live_surface, later + timedelta(minutes=1))

    # Same row, same admission, same instance -- no re-arm, no reopen.
    assert coordinator._quarter is not None
    assert coordinator._quarter.quarter_start == opens_at(0)
    assert coordinator._plan is not None
    assert coordinator._plan.admitted_at == plan_admitted_at
    assert coordinator._campaign_instance_id == instance
    assert coordinator._closed_campaign is None
    assert not coordinator._abandoned_admissions

    # Realised energy is continuous, and the grid is now paying for it.
    assert coordinator._quarter_battery_kwh > after_pv
    assert coordinator._quarter_grid_import_kwh > import_after_pv
    # The row's own ceiling still binds, and nothing was carried into it.
    assert coordinator._quarter_grid_import_kwh <= row.grid_authorised_kwh + 1e-6


async def test_a_satisfied_row_is_held_by_a_total_stop(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface: LiveSurface,
    monkeypatch,
) -> None:
    """**The one case Mode 2 at 0 kW is right for, and the measurement says so.**

    Measured 2026-09-01: dispatch on, mode 2, command 0.0 kW, SoC ~75 %, PV 2.8 kW,
    house 1.5 kW -- **battery power exactly 0 W**, and the 1.3 kW surplus exported.
    A total hold, suppressing charge as well as discharge.

    For a row whose frozen objective is already met that is the correct command, and
    the only one on this surface that cannot overshoot it: production must not push
    the pack past an objective Stage A authorised, and there is no "charge from PV
    only" primitive among the modes this release may command.

    Kept as its own test, apart from the unfinished-row contract above, because the
    two cases have opposite semantics and one generic hold is what got this wrong.
    """
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0)
    await step_once(hass, coordinator, live_surface, **step_clock(1))

    # Row 1's objective is 0.28 kWh and the site delivers 0.0233 a tick.
    reasons = await drive_quarter(hass, coordinator, live_surface, 1)
    assert TICK_HELD_QUARTER_SATISFIED in reasons, reasons
    assert coordinator._quarter_target_reached_at is not None

    # Production now arrives in quantity. The row is finished, so it stays finished.
    charging_site(hass, pv_w=2800.0, house_w=1500.0, battery_w=0.0)
    await tick_at(hass, coordinator, live_surface, opens_at(1) + timedelta(minutes=14))

    assert coordinator._hold_reason == HOLD_REASON_QUARTER_SATISFIED
    assert coordinator._applied_setpoint_kw == 0.0, (
        "a met objective is held at zero, whatever production is doing"
    )
    # **And the refresh reaches the same conclusion by its own route.** The tick
    # decides from the measured objective; the write boundary decides from
    # ``_quarter_is_satisfied``. If those two ever disagree, a satisfied row is held
    # for the *rate* reason instead -- which is the recoverable one, and would mean
    # the row resumed charging past an objective Stage A authorised.
    resting = await step_once(hass, coordinator, live_surface, **step_clock(2))
    assert boundary_of(resting).get("hold_reason") != HOLD_REASON_RATE_BELOW_RESOLUTION

    # And a satisfied row is never mistaken for a rate-limited one.
    assert coordinator._last_tick_reason != TICK_HELD_RATE_BELOW_RESOLUTION
    assert coordinator._plan is not None
    assert coordinator._campaign_id == CAMPAIGN_ID
    assert coordinator._closed_campaign is None
