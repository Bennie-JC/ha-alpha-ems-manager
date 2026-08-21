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
    CONTROL_EXECUTION_AVAILABLE,
    ECONOMIC_ADVICE_EVENT_KINDS,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_KINDS,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EXECUTION_EVENT_KINDS,
)

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


def test_execution_is_still_unavailable_in_this_release() -> None:
    """The one flag Stage A rests on. Nothing below it can be relaxed alone."""
    assert CONTROL_EXECUTION_AVAILABLE is False


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


def test_the_permitted_service_set_did_not_grow() -> None:
    """Three services, unchanged by this phase."""
    assert len(PERMITTED_SERVICES) == 3
    assert (
        frozenset(
            {
                ("input_number", "set_value"),
                ("input_boolean", "turn_on"),
                ("input_boolean", "turn_off"),
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


def test_the_activity_surface_sees_only_the_outcome() -> None:
    """Strictly observational, enforced by the signature rather than by intent.

    ``next_activity`` takes the previous entry, the economic outcome and a
    preformatted window. It cannot see the plan, the control report, the safety
    state or the recovery machinery, because they are not arguments -- so a later
    phase that wants to log an execution event has to change this signature, which
    is a visible act.
    """
    parameters = inspect.signature(activity_module.next_activity).parameters

    assert set(parameters) == {"previous", "outcome", "window"}
    for parameter in parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


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
    assert execution == {ECONOMIC_EVENT_STARTED, ECONOMIC_EVENT_CANCELLED}


@pytest.mark.parametrize("kind", ECONOMIC_EXECUTION_EVENT_KINDS)
def test_an_execution_event_is_refused_while_the_barrier_stands(kind: str) -> None:
    """A line reading "charge started" on a release that sends nothing is a lie.

    A guard rather than an assumption. Nothing can produce one today; if a later
    change makes it possible, the refusal is what stops the claim.
    """
    entry = activity_module.ActivityEntry(
        kind=kind,
        message="anything",
        state=activity_module.ActivityState(fingerprint="x", action="charge"),
    )

    with pytest.raises(ValueError, match="executes nothing"):
        activity_module.logbook_payload(
            entry, domain="alpha_ems_manager", entity_id="sensor.x"
        )


@pytest.mark.parametrize("kind", ECONOMIC_ADVICE_EVENT_KINDS)
def test_an_advice_event_is_accepted_and_carries_the_advisory_qualifier(
    kind: str,
) -> None:
    """The four kinds Stage A can produce, and the entity they attach to."""
    entry = activity_module.ActivityEntry(
        kind=kind,
        message="plans to hold. Advisory only: this release sends no command.",
        state=activity_module.ActivityState(fingerprint="x", action="hold"),
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
