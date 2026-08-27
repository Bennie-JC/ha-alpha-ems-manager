"""beta.27.1: the two defects real hardware found, and the gates they exposed.

beta.27 shipped every mechanism this file exercises. It shipped **none of them
wired to the code that selects them**, and the hardware said so within one
quarter:

    "quarter_schedule": []          on every run, after a full 19:00 refresh
    quarter_start = null            intent = null
    physical tick reason           = no_admitted_quarter
    inhibit_reason                 = would_export
    authorized                     = false

Four omissions, all the same shape -- a new mechanism added beside an old gate
that still answered the old question:

======  =========================================================  =============
defect  omission                                                    symptom
======  =========================================================  =============
A       the ``execution_target`` call site never passed the rows    empty schedule
B.1     ``carry_forward`` kept its charge-only default              no export run
B.2     so the Phase-3 reserve-guard fallback took the wheel        would_export
B.3     the action gates were unconditionally charge-only           authorized=false
======  =========================================================  =============

B.2 is worth stating plainly: the reported ``would_export`` inhibition was
**correct**. ``evaluate`` was refusing a genuine reserve-guard discharge into the
house. What was wrong is that the reserve guard was asked at all, on a refresh
where Stage A wanted to export -- and it was asked because no export run was
carried, which is B.1. Nothing about ``evaluate`` needed to change.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    CONTROL_EXECUTABLE_ACTIONS,
    CONTROL_EXECUTABLE_ACTIONS_BY_INTENT,
    CONTROL_LIVE_DISPATCH_INTENTS,
    CONTROL_MODE_ACTIVE,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    INHIBIT_WOULD_EXPORT,
    OWNERSHIP_OWNED,
    REFUSE_LIVE_ACTION_NOT_PERMITTED,
    REFUSE_UNSAFE,
)
from custom_components.alpha_ems_manager.safety import (
    ControlContext,
    SafetyVerdict,
    authorize_reset,
    authorize_start,
    direction_permitted,
)

from .forecast_helpers import NORMAL, local
from .test_beta24_live_charge import LiveSurface, owned_live_charge

pytestmark = pytest.mark.usefixtures("control_surface")


@pytest.fixture
def live_surface(hass: HomeAssistant, control_surface: None) -> LiveSurface:
    """Return a control surface that responds to writes."""
    return LiveSurface(hass)


def safe() -> SafetyVerdict:
    """Return a passing verdict, so only the action gate is under test."""
    return SafetyVerdict(True, None, ())


def live_context() -> ControlContext:
    """Return a context that clears every stage before the action gate."""
    return ControlContext(
        mode=CONTROL_MODE_ACTIVE,
        execution_enabled=True,
        seconds_since_last_write=None,
    )


# ===========================================================================
# BUG A -- the schedule is published, from the solved rows
# ===========================================================================


async def test_a_live_run_publishes_a_non_empty_quarter_schedule(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The exact hardware symptom, on the real publication path.**

    Every field in the contract existed and the rule string was published beside
    the list, which is why the diagnostics looked almost right: the schema was
    there and only the rows were missing.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    targets = coordinator.execution_targets
    assert targets, "the fixture should publish at least one run"

    for target in targets:
        schedule = target["quarter_schedule"]
        assert schedule, f"{target['intent']} published an empty schedule"
        assert target["quarter_schedule_source"] == "solved_intervals"


async def test_every_published_row_carries_the_approved_fields(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The seven approved fields, on every row of every run."""
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    for target in coordinator.execution_targets:
        for row in target["quarter_schedule"]:
            for field in (
                "start",
                "end",
                "battery_kwh",
                "grid_authorised_kwh",
                "grid_export_target_kwh",
                "grid_export_caused_kwh",
                "desired_grid_kw",
            ):
                assert field in row, (target["intent"], field)


async def test_the_rows_are_one_per_solved_interval_and_chronological(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """One row per interval the run covers, each exactly one quarter long.

    Reconstructing rows from a run's aggregate totals was explicitly ruled out, and
    this is what tells the two apart: an aggregate could not produce a row count
    that matches the window length.
    """
    from custom_components.alpha_ems_manager.execution import parse_target

    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    for raw in coordinator.execution_targets:
        target = parse_target(raw)
        assert target is not None
        rows = target.quarter_schedule
        span = target.window_end - target.window_start
        assert len(rows) == round(span.total_seconds() / 900), (
            len(rows),
            span,
            target.intent,
        )
        for row in rows:
            assert row.end - row.start == timedelta(minutes=15)
        starts = [row.start for row in rows]
        assert starts == sorted(starts)
        for earlier, later in pairwise(rows):
            assert earlier.end == later.start


async def test_a_quarter_is_actually_admitted_from_the_published_schedule(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """**The downstream half of the symptom.**

    An empty schedule meant ``next_quarter_row`` had nothing to return, so no
    quarter was admitted, so the tick reported ``no_admitted_quarter`` on every
    single tick of an economically active period.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    quarter = coordinator._quarter
    assert quarter is not None, "a quarter should be admitted once rows are published"
    assert quarter.intent in CONTROL_LIVE_DISPATCH_INTENTS
    # Admitted before it opened, which is the R1 rule.
    assert quarter.admitted_at < quarter.quarter_start
    assert quarter.battery_target_kwh > 0.0 or quarter.grid_export_target_kwh > 0.0


def test_a_two_quarter_export_run_publishes_exactly_two_rows() -> None:
    """The required result, built from real ``EconomicInterval`` rows.

    For an export the objective is the **actual** meter export and the battery
    figure is the ceiling; for a charge it is the other way round. Both are read
    off the solved rows, never recomputed.
    """
    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    class Interval:
        """A stand-in carrying only the fields the builder reads."""

        def __init__(self, index: int) -> None:
            self.index = index
            self.battery_charge_ac_kwh = 0.0
            self.battery_discharge_ac_kwh = 1.0 + index
            self.marginal_grid_import_kwh = 0.0
            # Actual and marginal deliberately differ, which is case 26.
            self.grid_export_kwh = 2.0 + index
            self.marginal_grid_export_kwh = 1.0 + index
            self.grid_import_kwh = 0.0

    base = local(NORMAL, 19, 0)

    def moment(index: int):
        return base + timedelta(minutes=15 * index)

    rows = quarter_schedule_for(
        (Interval(0), Interval(1), Interval(2)),
        start_index=0,
        end_index=1,
        intent=EXECUTION_INTENT_NET_EXPORT,
        moment=moment,
    )

    assert len(rows) == 2, rows
    assert rows[0]["start"] == base.isoformat()
    assert rows[0]["end"] == (base + timedelta(minutes=15)).isoformat()
    assert rows[1]["start"] == (base + timedelta(minutes=15)).isoformat()
    # The objective is the actual meter export, not the marginal figure.
    assert rows[0]["grid_export_target_kwh"] == pytest.approx(2.0)
    assert rows[1]["grid_export_target_kwh"] == pytest.approx(3.0)
    assert rows[0]["grid_export_caused_kwh"] == pytest.approx(1.0)
    # And the battery figure is the discharge ceiling for an export.
    assert rows[0]["battery_kwh"] == pytest.approx(1.0)
    assert rows[1]["battery_kwh"] == pytest.approx(2.0)


def test_a_charge_run_takes_its_objective_and_ceiling_from_the_other_fields() -> None:
    """The charge half of the same builder, so the asymmetry is pinned here too."""
    from custom_components.alpha_ems_manager.economic import quarter_schedule_for

    class Interval:
        def __init__(self, index: int) -> None:
            self.index = index
            self.battery_charge_ac_kwh = 1.5 + index
            self.battery_discharge_ac_kwh = 0.0
            self.marginal_grid_import_kwh = 1.2 + index
            self.grid_export_kwh = 0.0
            self.marginal_grid_export_kwh = 0.0
            self.grid_import_kwh = 1.4 + index

    base = local(NORMAL, 19, 0)
    rows = quarter_schedule_for(
        (Interval(0), Interval(1)),
        start_index=0,
        end_index=1,
        intent=EXECUTION_INTENT_GRID_CHARGE,
        moment=lambda i: base + timedelta(minutes=15 * i),
    )

    assert len(rows) == 2
    assert rows[0]["battery_kwh"] == pytest.approx(1.5)
    assert rows[0]["grid_authorised_kwh"] == pytest.approx(1.2)
    assert rows[0]["desired_grid_kw"] == pytest.approx(1.4 / 0.25)


def test_execution_target_cannot_silently_publish_an_empty_schedule() -> None:
    """**The structural guard against this exact defect returning.**

    The old shape took a prebuilt list, so forgetting it was indistinguishable from
    a run with nothing to publish -- which is how it reached hardware. The rows are
    the input now, and the source is published, so the two cases are always
    distinguishable in a download.
    """
    import inspect

    from custom_components.alpha_ems_manager import economic

    signature = inspect.signature(economic.execution_target)

    # The rows go in; the assembled list does not.
    assert "intervals" in signature.parameters
    assert "moment" in signature.parameters
    assert "quarter_schedule" not in signature.parameters

    source = inspect.getsource(economic.execution_target)
    assert '"quarter_schedule": quarter_rows' in source
    assert "quarter_schedule_source" in source


def test_the_production_call_site_passes_the_solved_rows() -> None:
    """Asserted structurally, because the omission *was* the defect.

    Every behavioural test above would pass again if this call site dropped the
    argument -- they would simply be testing the builder rather than the pipeline.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert "intervals=outcome.desired.intervals" in source
    assert "moment=moment," in source


# ===========================================================================
# BUG B -- would_export must not preempt an admitted export
# ===========================================================================


def test_the_unconditional_action_set_is_still_charge_only() -> None:
    """**The set that must not have been widened.**

    Widening it would authorise every discharge, the Phase-3 reserve guard's
    included -- and for that path energy reaching the meter is an accident.
    """
    assert frozenset({ACTION_CHARGE}) == CONTROL_EXECUTABLE_ACTIONS


def test_only_net_export_unlocks_the_discharge_direction() -> None:
    """The second set, keyed on the intent, and it contains exactly one entry."""
    assert {
        EXECUTION_INTENT_NET_EXPORT: frozenset({ACTION_DISCHARGE})
    } == CONTROL_EXECUTABLE_ACTIONS_BY_INTENT


@pytest.mark.parametrize(
    ("action", "intent", "permitted"),
    [
        # A charge is permitted unconditionally, as it always was.
        (ACTION_CHARGE, EXECUTION_INTENT_GRID_CHARGE, True),
        (ACTION_CHARGE, None, True),
        (ACTION_CHARGE, "serve_load", True),
        # A discharge is permitted only under an export intent.
        (ACTION_DISCHARGE, EXECUTION_INTENT_NET_EXPORT, True),
        # And under nothing else -- which is the reserve guard's case.
        (ACTION_DISCHARGE, None, False),
        (ACTION_DISCHARGE, EXECUTION_INTENT_GRID_CHARGE, False),
        (ACTION_DISCHARGE, "serve_load", False),
        (ACTION_DISCHARGE, "anything_at_all", False),
        # A missing action is refused whatever the intent claims.
        (None, EXECUTION_INTENT_NET_EXPORT, False),
        (None, None, False),
    ],
)
def test_the_direction_gate_is_intent_aware_and_fails_closed(
    action: str | None, intent: str | None, permitted: bool
) -> None:
    """One table, and it is the only place the question is answered."""
    assert direction_permitted(action, intent) is permitted


def test_a_reserve_guard_discharge_is_still_refused_at_authorisation() -> None:
    """**Direction 1: the existing path is unchanged.**

    The reserve guard's discharge carries no export intent, so the action gate
    refuses it exactly as the charge-only set did before beta.27.1.
    """
    decision = authorize_start(
        safe(),
        live_context(),
        commands_planned=6,
        starts_or_increases=True,
        action=ACTION_DISCHARGE,
        intent=None,
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_LIVE_ACTION_NOT_PERMITTED


def test_serve_load_is_still_refused_at_authorisation() -> None:
    """**Direction 2.** It has no published meter target to be measured against."""
    decision = authorize_start(
        safe(),
        live_context(),
        commands_planned=6,
        starts_or_increases=True,
        action=ACTION_DISCHARGE,
        intent="serve_load",
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_LIVE_ACTION_NOT_PERMITTED


def test_an_admitted_export_is_authorised_at_the_action_gate() -> None:
    """**Direction 3: the fix.**

    ``authorized = false`` with ``live_charge_only`` was the second half of the
    hardware symptom, and it would have blocked every export even after the
    reserve-guard preemption was solved.
    """
    decision = authorize_start(
        safe(),
        live_context(),
        commands_planned=6,
        starts_or_increases=True,
        action=ACTION_DISCHARGE,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )

    assert decision.authorized is True, decision.refusal


def test_a_generic_unsafe_verdict_still_refuses_an_export() -> None:
    """**Direction 4: fail closed.** The gate is not a bypass.

    An export is authorised against the *export* verdict, which the coordinator
    substitutes for an admitted quarter. If what arrives is an unsafe verdict, the
    export is refused -- the action gate never overrides a hazard.
    """
    decision = authorize_start(
        SafetyVerdict(False, INHIBIT_WOULD_EXPORT, ()),
        live_context(),
        commands_planned=6,
        starts_or_increases=True,
        action=ACTION_DISCHARGE,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_UNSAFE
    assert decision.unsafe_reason == INHIBIT_WOULD_EXPORT


def test_an_export_can_also_be_stopped() -> None:
    """**The failure that would strand a running dispatch.**

    Left charge-only, the stop path would refuse to stop an export it had started,
    leaving the inverter running until the device dead-man expired. A start gate and
    a stop gate that disagree about a direction is the worst of the two.
    """
    stoppable = authorize_reset(
        ownership=OWNERSHIP_OWNED,
        stopping_action=ACTION_DISCHARGE,
        stop_reason="quarter_target_reached",
        steps_planned=2,
        intent=EXECUTION_INTENT_NET_EXPORT,
    )
    assert stoppable.authorized is True, stoppable.refusal

    # And without the intent it still fails closed, rather than defaulting open.
    refused = authorize_reset(
        ownership=OWNERSHIP_OWNED,
        stopping_action=ACTION_DISCHARGE,
        stop_reason="quarter_target_reached",
        steps_planned=2,
        intent=None,
    )
    assert refused.authorized is False
    assert refused.refusal == REFUSE_LIVE_ACTION_NOT_PERMITTED


def test_a_charge_stop_is_unaffected_by_the_new_parameter() -> None:
    """The parameter defaults, so every existing caller keeps its behaviour."""
    decision = authorize_reset(
        ownership=OWNERSHIP_OWNED,
        stopping_action=ACTION_CHARGE,
        stop_reason="target_reached",
        steps_planned=2,
    )

    assert decision.authorized is True, decision.refusal


# ===========================================================================
# BUG B.1 -- the export run is carried, so the reserve guard is not asked
# ===========================================================================


def test_carry_forward_is_called_with_both_executable_intents() -> None:
    """**The root cause of the ``would_export`` report, asserted structurally.**

    ``carry_forward`` defaults to charge-only. Left at the default, a ``net_export``
    run was never carried -- and because nothing was carried,
    ``stage_b_holds_the_run`` stayed false and the Phase-3 reserve-guard fallback
    was asked for a command instead. That layer only ever discharges into the
    house, so it produced one, and ``evaluate`` correctly refused it with
    ``would_export``.

    The inhibition was real. The command it described was never Stage B's.
    """
    import inspect

    from custom_components.alpha_ems_manager import coordinator as module

    source = inspect.getsource(module)

    assert "executable_intents=CONTROL_LIVE_DISPATCH_INTENTS," in source
    # And the bare three-argument call must not have survived anywhere.
    assert "carry_forward(self._carried, self.execution_targets, now)" not in source


async def test_an_export_run_is_carried_and_suppresses_the_reserve_guard(
    hass: HomeAssistant, config_data: dict, source_entities: None, frank, live_surface
) -> None:
    """The behavioural half: with a run carried, no second opinion is taken.

    ``stage_b_holds_the_run`` is what suppresses the fallback, and it is true
    whenever a run is carried -- whichever of the two intents it is.
    """
    coordinator = await owned_live_charge(hass, config_data, frank, live_surface)

    assert coordinator._carried is not None
    assert coordinator._carried.intent in CONTROL_LIVE_DISPATCH_INTENTS


def test_carry_forward_still_refuses_an_intent_this_release_cannot_execute() -> None:
    """Widening the set to two did not widen it to anything else."""
    from custom_components.alpha_ems_manager.execution import carry_forward

    base = local(NORMAL, 19, 0)
    published = {
        "plan_id": "plan-serve",
        "revision": 1,
        "intent": "serve_load",
        "purpose": "serve_load",
        "window_start": base.isoformat(),
        "window_end": (base + timedelta(minutes=15)).isoformat(),
        "battery_target_kwh": 1.0,
        "average_power_kw": 4.0,
        "quarter_schedule": [],
    }

    outcome = carry_forward(
        None,
        [published],
        base + timedelta(minutes=1),
        executable_intents=CONTROL_LIVE_DISPATCH_INTENTS,
    )

    assert outcome.carried is None


# ===========================================================================
# the diagnostic strings a hardware download is read against
# ===========================================================================


def test_the_execution_scope_names_both_executable_intents() -> None:
    """Stale wording here makes hardware debugging ambiguous, which is why it went.

    The old string said only a grid charge could execute, on a release that also
    exports -- so a reader watching an export refuse had no way to tell a bug from
    the documented design.
    """
    from custom_components.alpha_ems_manager.coordinator import _EXECUTION_SCOPE

    assert "grid_charge" in _EXECUTION_SCOPE
    assert "net_export inside an admitted quarter" in _EXECUTION_SCOPE
    assert "serve_load" in _EXECUTION_SCOPE
    assert "still cannot export" in _EXECUTION_SCOPE
    assert "modes 6 and 7" in _EXECUTION_SCOPE
    assert "force charging and force discharging" in _EXECUTION_SCOPE
    assert "only a stage-b grid charge" not in _EXECUTION_SCOPE


def test_the_refusal_reason_no_longer_says_charge_only() -> None:
    """It refuses a *direction* under an *intent*, which is what it now says."""
    assert REFUSE_LIVE_ACTION_NOT_PERMITTED == "live_direction_not_permitted"


def test_no_shipped_string_still_claims_export_cannot_execute() -> None:
    """A sweep over every string the package can publish, because the two known
    stale claims were found by reading a hardware download rather than the source.

    **String literals only, via the syntax tree.** A text search would match the
    comments that explain what the old wording was and why it went -- those are for
    a maintainer and never reach a download. What matters is what a reader of the
    diagnostics can be told.
    """
    import ast
    import pathlib as _pathlib

    from custom_components.alpha_ems_manager import const

    package = _pathlib.Path(const.__file__).parent
    stale = (
        "only a stage-b grid charge",
        "and only for a grid charge",
        "every other direction -- discharge, export",
        "live_charge_only",
    )
    offences: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            lowered = node.value.lower()
            for claim in stale:
                if claim in lowered:
                    offences.append(f"{path.name}:{node.lineno} {claim}")
    assert not offences, offences
