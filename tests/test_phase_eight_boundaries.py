"""The architectural boundaries Phase 8 must not cross, enforced statically.

Phase 8 is the first layer that produces a *trading* decision, which makes it the
first layer where a well-meaning refactor could quietly turn advice into action.
So these tests read the real source files rather than exercising behaviour: a
future change that gives the optimizer a service call, a second copy of a hardware
limit, an inverter helper name, or the ability to see a dispatch state fails here
rather than on somebody's battery.

Modelled on the Phase-3, Phase-4 and Phase-7 boundary tests, which enforce the
other boundaries this project cares about the same way.

Stage A's promise, in four layers
---------------------------------

1. the **global barrier** stays false, and both the Phase-4 executor refusal and
   the entity's blocked reason are pinned to it;
2. the **module** is pure -- no Home Assistant, no source, no store, no control
   layer -- so every rule in it is testable against synthetic state;
3. the **vocabulary** the optimizer could express an action in contains no
   inverter helper, no grid-rate actuator and no service name;
4. and the **Activity surface** refuses the two event kinds that would claim the
   battery moved.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from custom_components.alpha_ems_manager import activity as activity_module
from custom_components.alpha_ems_manager import economic as economic_module
from custom_components.alpha_ems_manager.alphaess_device import (
    PERMITTED_SERVICES,
)
from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ADVICE_EVENT_KINDS,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_KINDS,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EVENT_STOPPED,
    ECONOMIC_EXECUTION_EVENT_KINDS,
)

from .live_capability import assert_charge_only_capability
from .test_phase_four_boundaries import (
    FLASH_BACKED_HELPERS,
    GRID_RATE_ACTUATORS,
)
from .test_phase_seven_boundaries import (
    CLAMP_ONLY_NAMES,
    LIVE_STATE_NAMES,
    constraining_references,
    identifiers,
    imported_modules,
    module_source,
    module_tree,
)

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: The two Phase-8 modules. Both pure: the optimizer, and the sentence-builder
#: for the logbook.
PHASE_EIGHT_MODULES = ("economic", "activity")

#: Inverter control-surface helpers. Phase 8 names none of them: it produces an
#: *action*, and turning an action into a register write is Phase 4's job and
#: nowhere else's.
HELPER_PREFIXES = (
    "input_boolean.alphaess",
    "input_number.alphaess",
    "alphaess_helper",
)

#: Features of the control surface that drive the battery on their own. Phase 8
#: may not read them, switch them, or plan around them.
SELF_DRIVING_FEATURES = ("excess_export", "peak_shaving")


def test_the_boundary_check_sees_the_real_modules() -> None:
    """Guard the paths against silently matching nothing."""
    for name in PHASE_EIGHT_MODULES:
        assert (COMPONENT_DIR / f"{name}.py").exists(), name
    assert len(module_source("economic")) > 30_000
    assert len(module_source("activity")) > 3_000


# -- the global barrier ------------------------------------------------------


def test_only_a_charge_is_executable_in_this_release() -> None:
    """The capability Stage A rests on. Nothing below it can be relaxed alone.

    Stage A plans charges, discharges, exports and curtailment. beta.24 executes
    the first of those and refuses the rest, so what Stage A rests on is no longer
    a flag but a set -- and every action it can recommend other than a charge must
    still be outside it.
    """
    assert_charge_only_capability()


def test_the_blocked_reason_is_the_barrier_and_nothing_finer() -> None:
    """While the barrier stands, no per-action reason may mask it.

    Load-bearing for honesty: reporting ``no_primitive_export`` on a release that
    sends nothing at all would tell a user the export was the only thing stopping
    them.
    """
    from custom_components.alpha_ems_manager.sensor import _economic_blocked_reason

    source = inspect.getsource(_economic_blocked_reason)
    tree = ast.parse(source.lstrip())
    first = next(node for node in ast.walk(tree) if isinstance(node, ast.If))

    assert "CONTROL_EXECUTION_AVAILABLE" in ast.dump(first.test)
    assert ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE == "execution_unavailable"


def test_no_phase_eight_module_calls_a_service() -> None:
    """Zero actuation, checked at the syntax level rather than promised."""
    for name in PHASE_EIGHT_MODULES:
        tree = module_tree(name)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert "async_call" not in called, name
        assert "call" not in called, name
        assert not imported_modules(tree) & {
            "alphaess_adapter",
            "alphaess_device",
            "control",
            "safety",
        }, name


def test_no_phase_eight_module_names_an_inverter_helper() -> None:
    """An action is a word. Turning it into a register write is Phase 4's job."""
    for name in PHASE_EIGHT_MODULES:
        source = module_source(name)
        for prefix in HELPER_PREFIXES:
            assert prefix not in source, (name, prefix)


@pytest.mark.parametrize("helper", FLASH_BACKED_HELPERS)
def test_no_phase_eight_module_names_a_flash_backed_helper(helper: str) -> None:
    """Schedules, persistent cutoffs and the feed-in limit stay out of reach."""
    for name in PHASE_EIGHT_MODULES:
        assert helper not in module_source(name), (name, helper)


@pytest.mark.parametrize("actuator", GRID_RATE_ACTUATORS)
def test_no_phase_eight_module_names_a_grid_rate_actuator(actuator: str) -> None:
    """``force_export`` and ``force_import`` are structurally out of the vocabulary.

    Phase 8 *models* an export, and that is exactly why this matters: the modelled
    action is a battery discharge whose grid consequence the inverter derives. A
    grid-rate actuator compensates for house load internally, so expressing the
    same intent through one would be wrong by the size of the house load.
    """
    for name in PHASE_EIGHT_MODULES:
        assert actuator not in module_source(name), (name, actuator)


def test_the_permitted_service_set_is_exactly_these_four() -> None:
    """The closed set, named member by member rather than counted.

    Three until beta.25, which adds ``input_select.select_option`` and nothing
    else: the dispatch mode is an ``input_select`` whose label the vendor package
    parses the mode number out of. Deliberately not ``input_button.press`` --
    turning the enable boolean off already triggers the package own reset, so a
    button would widen the surface and buy nothing.
    """
    assert len(PERMITTED_SERVICES) == 4
    assert (
        frozenset(
            {
                ("input_number", "set_value"),
                ("input_boolean", "turn_on"),
                ("input_boolean", "turn_off"),
                ("input_select", "select_option"),
            }
        )
        == PERMITTED_SERVICES
    )


# -- purity ------------------------------------------------------------------


@pytest.mark.parametrize("name", PHASE_EIGHT_MODULES)
def test_no_phase_eight_module_imports_home_assistant(name: str) -> None:
    """Every rule has to be testable against synthetic state.

    The same standard the Phase-3 four, the Phase-4 four and the reserve are held
    to, so the arithmetic can be exercised without a running core and a failure
    points at the model rather than at a fixture.
    """
    assert "homeassistant" not in imported_modules(module_tree(name))
    assert "homeassistant" not in module_source(name)


@pytest.mark.parametrize("name", PHASE_EIGHT_MODULES)
def test_no_phase_eight_module_reaches_into_a_source_or_a_store(name: str) -> None:
    """It consumes prices, demands, a reserve and limits. Nothing else."""
    forbidden = {
        "history_store",
        "forecast_recorder",
        "forecast_history",
        "coordinator",
        "diagnostics",
        "solcast_source",
        "frank_source",
        "price_forecast",
        "pv_forecast",
        "storage",
    }

    assert not imported_modules(module_tree(name)) & forbidden


def test_the_optimizer_cannot_read_the_price_layer() -> None:
    """Prices arrive as ``IntervalPrice``, never as a source it can query.

    The same rule the reserve is held to, for the same reason: a module that can
    reach a source can acquire a dependency on *when* it was read, and then the
    same stored evidence stops reproducing the same answer.
    """
    tree = module_tree("economic")

    assert "price_forecast" not in imported_modules(tree)
    assert "frank_source" not in imported_modules(tree)
    assert hasattr(economic_module, "IntervalPrice")


@pytest.mark.parametrize("name", PHASE_EIGHT_MODULES)
def test_no_phase_eight_module_performs_network_io(name: str) -> None:
    """No client, no socket, no polling."""
    assert not imported_modules(module_tree(name)) & {
        "requests",
        "aiohttp",
        "httpx",
        "urllib",
        "socket",
    }


# -- the clamp is the only clamp ---------------------------------------------


def test_the_optimizer_constrains_by_no_hardware_limit() -> None:
    """Every physical bound comes out of ``apply_request``, never from a compare.

    A second copy of a safety limit is a second thing to keep in step, and the
    first time the two disagreed it would be the copy that got believed. The
    physics table exists precisely so this module can be limit-free: it *asks*
    the clamp what is reachable and remembers the answer.
    """
    found = constraining_references(module_tree("economic"), CLAMP_ONLY_NAMES)

    assert found == set(), sorted(found)


def test_the_optimizer_performs_no_efficiency_arithmetic_of_its_own() -> None:
    """The conversion ratios are *measured* from the clamp, never computed.

    No square root, no percentage-to-fraction division: the two ratios come from
    probing a calibration state, which is why they match the simulator to
    fourteen decimal places rather than to within a modelling assumption.
    """
    source = module_source("economic")

    assert "math.sqrt" not in source
    assert "round_trip_efficiency_percent" not in source
    assert "_calibrate" in source


def test_the_grid_residual_has_exactly_one_source() -> None:
    """``split_grid_energy`` is the only thing that decides import and export.

    Two formulas for a residual is one formula too many, and the one that got it
    wrong here omitted production entirely -- a five-kilowatt discharge against a
    one-kilowatt load with five kilowatts of sun exports nine, not four.
    """
    tree = module_tree("economic")
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "split_grid_energy" in called
    assert "battery" in imported_modules(tree)


def test_the_interval_duration_is_still_defined_once() -> None:
    """No module re-derives a quarter of an hour."""
    for name in PHASE_EIGHT_MODULES:
        source = module_source(name)
        assert "/ 60.0" not in source, name
        assert "0.25  #" not in source, name


# -- blindness ---------------------------------------------------------------


@pytest.mark.parametrize("fact", LIVE_STATE_NAMES)
def test_no_live_installation_fact_reaches_the_optimizer(fact: str) -> None:
    """A plan must be reproducible from stored evidence, and these are not stored.

    ``pv_absorption.modelled`` flipped inside fifteen minutes on the live
    installation because a dispatch began, while both forecasts stood still. A
    plan that consulted it would not be recomputable from what was written down.
    """
    assert fact not in identifiers(module_tree("economic")), fact


@pytest.mark.parametrize("feature", SELF_DRIVING_FEATURES)
def test_the_optimizer_does_not_plan_around_a_self_driving_feature(
    feature: str,
) -> None:
    """If one is on, Alpha EMS stands down. It does not model its way past it."""
    assert feature not in module_source("economic"), feature


def test_the_activity_surface_sees_only_planned_runs_and_the_clock() -> None:
    """Strictly observational, enforced by the signature rather than by intent.

    ``next_activity`` takes what has already been said, the runs the plan now
    holds, the current instant and whether anything may be sent. It cannot see the
    plan object, the control report, the safety state or the recovery machinery,
    because they are not arguments -- so a later phase that wants to describe
    something new has to change this signature, which is a visible act.

    ``now`` arrived in beta.16 and is a *value*, not a clock: the module reads no
    time of its own, which is what keeps the whole lifecycle policy testable
    against fixed instants.
    """
    parameters = inspect.signature(activity_module.next_activity).parameters

    # ``execution`` since beta.19 and ``shadow`` since beta.31, and each arrival
    # is the design working as intended: this module could not describe execution
    # while execution was not an argument, and the docstring said changing that
    # would have to be "a visible act". Both were. The discipline they protected is
    # intact -- what is passed is a narrow summary and one boolean, not the
    # controller, so Activity still cannot reach the rolling setpoint it must never
    # report.
    assert set(parameters) == {"previous", "runs", "now", "execution", "shadow"}
    for parameter in parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    source = inspect.getsource(activity_module)
    for forbidden in ("utcnow", "dt_util", "datetime.now", "time.time"):
        assert forbidden not in source, forbidden


def test_nothing_in_the_integration_subscribes_to_an_activity_event() -> None:
    """Write-only. No figure is derived from a logbook entry.

    An installation with the recorder removed must produce identical numbers, and
    the way to guarantee that is for nothing to listen.
    """
    for path in COMPONENT_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        # Narrow deliberately: the coordinator does subscribe to
        # ``EVENT_CORE_CONFIG_UPDATE``, which is how a timezone change reaches it.
        # What must not exist is a subscription to the logbook, and a mention of
        # the event name anywhere but the one place that fires it.
        assert "async_listen(EVENT_LOGBOOK_ENTRY" not in source, path.name
        if "EVENT_LOGBOOK_ENTRY" in source:
            assert path.name == "sensor.py", path.name
            assert source.count("EVENT_LOGBOOK_ENTRY") == 2, path.name


# -- the Activity vocabulary -------------------------------------------------


def test_the_event_kinds_partition_into_advice_and_execution() -> None:
    """Six kinds, four about advice and two about execution, no overlap."""
    advice = set(ECONOMIC_ADVICE_EVENT_KINDS)
    execution = set(ECONOMIC_EXECUTION_EVENT_KINDS)

    assert advice | execution == set(ECONOMIC_EVENT_KINDS)
    assert not advice & execution
    # ``started`` is the only claim about the battery. ``cancelled`` moved to the
    # advice set in beta.16: withdrawing advice that never began is plainly an
    # advice event, and the one-message-per-run design has to be able to retract
    # an announcement or leave it standing as a lie.
    # Two execution kinds since beta.19, and shadow's ``would_start`` /
    # ``would_stop`` are deliberately on the *advice* side of the partition: a
    # shadow line is not a claim about the battery, and giving it an execution
    # kind would have forced the refusal below to be relaxed for the real case
    # too.
    #
    # beta.31 added ``finished`` and ``error`` to the execution side, because both
    # assert something about the battery: a success says energy moved and an error
    # says a command failed. Neither is reachable in Shadow, which emits no start
    # and therefore no ending of either kind -- a Shadow lifecycle is planned and
    # then cancelled, and both of those are advice.
    assert execution == {
        ECONOMIC_EVENT_STARTED,
        ECONOMIC_EVENT_STOPPED,
        ECONOMIC_EVENT_FINISHED,
        ECONOMIC_EVENT_ERROR,
    }
    assert ECONOMIC_EVENT_CANCELLED in advice


@pytest.mark.parametrize("kind", ECONOMIC_EXECUTION_EVENT_KINDS)
def test_an_execution_event_is_accepted_now_that_a_charge_executes(kind: str) -> None:
    """**The guard did not move; the world it guards against did.**

    A line reading "charge started" on a release that sends nothing is a lie, and
    through beta.23 the refusal was what stopped it. beta.24 sends a charge, so the
    kind is legitimate -- and the refusal still stands behind it, keyed on the
    barrier rather than on a version, so a release that goes back to sending
    nothing goes back to refusing it.

    Shadow is answered separately and structurally: it emits ``would_start`` and
    ``would_stop``, never these kinds.
    """
    entry = activity_module.ActivityEntry(
        kind=kind,
        message="anything",
        state=activity_module.ActivityState(),
    )

    payload = activity_module.logbook_payload(
        entry, domain="alpha_ems_manager", entity_id="sensor.x"
    )
    assert payload["message"] == "anything"
    assert payload["name"] == activity_module.ACTIVITY_NAME


@pytest.mark.parametrize("kind", ECONOMIC_ADVICE_EVENT_KINDS)
def test_an_advice_event_is_accepted_and_carries_the_advisory_qualifier(
    kind: str,
) -> None:
    """The five kinds Stage A can produce, and the entity they attach to."""
    entry = activity_module.ActivityEntry(
        kind=kind,
        message="plans to hold. Advisory only: this release sends no command.",
        state=activity_module.ActivityState(),
    )
    payload = activity_module.logbook_payload(
        entry, domain="alpha_ems_manager", entity_id="sensor.alpha_ems_economic_action"
    )

    assert payload["domain"] == "alpha_ems_manager"
    assert payload["entity_id"] == "sensor.alpha_ems_economic_action"
    assert payload["name"] == activity_module.ACTIVITY_NAME
    assert "Advisory only" in payload["message"]


# -- nothing was added to the manifest ---------------------------------------


def test_phase_eight_added_no_dependency() -> None:
    """Still no requirements, and no dependency on the logbook.

    Firing ``EVENT_LOGBOOK_ENTRY`` is harmless on an installation without the
    logbook -- the event simply goes unheard -- so declaring a dependency would
    make a decoration into a setup requirement.
    """
    import json

    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["requirements"] == []
    assert "dependencies" not in manifest
    assert manifest["iot_class"] == "calculated"


# ===========================================================================
# and the runtime half: the whole beta.16 surface, writing nothing
# ===========================================================================


@pytest.fixture
def economic_service_calls(hass) -> list:
    """Capture every call to a service this integration is permitted to make.

    Registered as **real handlers**, so a write attempt would succeed and be
    recorded rather than raising -- otherwise an attempted call could be mistaken
    for an absent service and the test would pass for the wrong reason.
    """
    calls: list = []

    async def record(call) -> None:
        calls.append(call)

    for domain, service in PERMITTED_SERVICES:
        hass.services.async_register(domain, service, record)
    assert len(set(PERMITTED_SERVICES)) == 4
    return calls


async def test_the_economic_surface_writes_nothing_in_active_mode(
    hass,
    setup_integration,
    control_surface: None,
    frank,
    economic_service_calls: list,
) -> None:
    """A day of quarter-hours, in the most permissive mode the release can reach.

    The static proofs above show that no Phase-8 module *names* a service. This
    is the other half: the integration actually running, with real prices so the
    optimizer produces a plan, in ``active`` mode, filing Activity lines -- and
    not one service call leaving it.

    Asserted positively as well as negatively. Silence while the plan was
    unavailable and the logbook empty would prove nothing, so the plan must be
    available with runs in it and at least one Activity line must have been filed
    before the zero below is worth anything -- and at least one of them must be a
    **sale**, since a sale is the line whose marker beta.32 changed.

    **The sweep is a full day since beta.32, and that is the point of the change.**
    Through beta.31 eight quarters sufficed because the plan put a discharge run in
    the near term and slid it forward one interval per refresh -- three
    ``Plan Replaced`` cancellations in two hours, which is the churn this release
    exists to remove. With the idle counterfactual corrected, an interval in which
    the inverter already serves the house from the battery no longer looks like a
    reason to pay a switching fee, so the sale concentrates at the single best
    price of the day and stops moving. The sweep therefore has to run until it
    *reaches* that sale, rather than relying on a run that was never going to
    happen where the old plan said it would.
    """
    from datetime import timedelta

    from homeassistant.const import EVENT_LOGBOOK_ENTRY

    from custom_components.alpha_ems_manager.const import (
        CONTROL_MODE_ACTIVE,
    )

    from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
    from .frank_capture import synthetic_day
    from .test_control_modes import set_mode
    from .test_economic_published import allow_trading

    logbook: list = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: logbook.append(event.data))

    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    # beta.31: the plan needs a reason to act now, and for *this* test the action
    # must be one that is not executable -- an export or a discharge. Only a charge
    # executes in this release, and ``_advisory_suffix`` is right to omit the
    # disclaimer from a charge line: calling an executable action advisory would be
    # the false claim. So a sell-shaped day, which is precisely what the advisory
    # half of the economic surface is made of.
    from .test_beta24_live_charge import sell_now_price

    day = synthetic_day(NORMAL, price_at=sell_now_price)
    # Tomorrow as well, so the horizon does not collapse late in the sweep: the
    # optimiser must stay *available* for the whole day, or the silence below is
    # silence from a plan that had nothing to say.
    frank.publish(today=day, tomorrow=day)
    # Both opt-ins on: the state that gives the optimizer something to advise,
    # and therefore the state in which a write would actually be tempting.
    allow_trading(coordinator, allow_grid_charging=True, allow_battery_export=True)
    await set_mode(hass, CONTROL_MODE_ACTIVE)

    outcomes = []
    started = local(NORMAL, 10, 15)
    # Stops at 23:30: the refresh after that crosses midnight, and a horizon whose
    # first interval belongs to a different day is a separate question from this
    # one.
    for quarter in range(54):
        await refresh_at(coordinator, started + timedelta(minutes=15 * quarter))
        outcomes.append(coordinator.data["economic"])

    # The surface was live, or the zero below means nothing.
    assert coordinator.control_mode == CONTROL_MODE_ACTIVE
    assert all(o is not None and o.available for o in outcomes)
    assert any(o.desired.runs for o in outcomes)
    assert logbook, "no Activity line was filed, so silence proves nothing"

    # And it wrote nothing.
    assert economic_service_calls == []
    assert_charge_only_capability()
    assert (coordinator.control_report or {}).get("last_write") is None
    sells = 0
    for entry in logbook:
        assert entry["name"] == activity_module.ACTIVITY_NAME
        # **No line is advisory in Active mode, and that inversion is the beta.32
        # correction.** Through beta.31 a Live sell carried ``— Advisory`` because
        # :data:`IMPLEMENTED_ACTIONS` said no actuator existed for an export --
        # while ``CONTROL_EXECUTABLE_ACTIONS_BY_INTENT`` had authorised an admitted
        # ``net_export`` since beta.27 and the hardware had performed one. So the
        # marker was appended to lines a command was about to be sent for, which is
        # the one claim a writing release must not get wrong.
        assert not entry["message"].endswith("— Advisory"), entry["message"]
        assert not entry["message"].endswith("— Shadow"), entry["message"]
        if " Sell " in entry["message"]:
            sells += 1
        # And the claim the disclaimer was only ever a proxy for: no line says the
        # battery did anything. A start, a success and an error are execution
        # kinds, and this whole test is the proof that nothing was sent.
        assert " Started " not in entry["message"]
        assert not entry["message"].startswith("Finished ")
    assert sells, "no sale was announced, so the marker rule proves nothing"

    # And it did not repeat itself. The live symptom was a near-identical line
    # every quarter of an hour about a run already under way, so a day of refreshes
    # producing a day of variations on one sentence is the failure this catches.
    messages = [entry["message"] for entry in logbook]
    assert len(messages) == len(set(messages)), messages
