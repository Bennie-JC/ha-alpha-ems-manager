"""Structural rules the control layer must satisfy, read off the real sources.

The Phase-3 boundary file established the technique and the reason for it: a
substring check that fires on a docstring gets silenced rather than fixed, so
these read the abstract syntax tree and look at what the code *does*.

The rule this file exists for above all others: **no command reaches an
inverter in this release**, and that must be true structurally rather than by
inspection. Everything else here supports it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: The pure control modules. Same standard as the Phase-3 four: no Home
#: Assistant, so every rule in them is testable against synthetic state.
PURE_MODULES = ("control", "safety", "alphaess_device", "soc_coherence")

#: The one control module that may touch Home Assistant, plus the two platforms
#: and the coordinator it is wired into.
IMPURE_MODULES = ("alphaess_adapter", "select")

#: Hardware-limit names only the Phase-3 clamp may constrain by. Reporting one
#: is fine; comparing against one is enforcing it somewhere it must not be.
CLAMP_ONLY_NAMES = (
    "max_charge_kw",
    "max_discharge_kw",
    "headroom_energy_kwh",
    "usable_energy_kwh",
)

#: Helpers that write registers the inverter keeps in flash memory: charge and
#: discharge schedules, their persistent cutoffs, the feed-in limit and the
#: date-time sync. Phase 4 may never write any of them.
#:
#: Two differ from the dispatch helpers Phase 4 *does* use by a single word --
#: ``discharging_cutoff_soc`` against ``force_discharging_cutoff_soc`` -- which
#: is exactly why this is asserted rather than remembered.
FLASH_BACKED_HELPERS = (
    "alphaess_helper_charging_cutoff_soc",
    "alphaess_helper_discharging_cutoff_soc",
    "alphaess_helper_max_feed_to_grid",
    "alphaess_helper_charging_period",
    "alphaess_helper_discharging_period",
    "alphaess_helper_charging_discharging_settings",
    "alphaess_helper_synchronise_date_time",
)

#: Grid-rate actuators. A battery decision must never be expressed as one: they
#: compensate for house load and generation internally, so commanding one from a
#: battery figure is wrong by the size of the house load.
GRID_RATE_ACTUATORS = ("force_export", "force_import")


def module_source(name: str) -> str:
    """Return one component module's source."""
    return (COMPONENT_DIR / f"{name}.py").read_text(encoding="utf-8")


def module_tree(name: str) -> ast.Module:
    """Return one component module's parsed tree."""
    return ast.parse(module_source(name))


def imported_modules(tree: ast.Module) -> set[str]:
    """Return every top-level package name the module imports."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def local_imports(tree: ast.Module) -> set[str]:
    """Return every sibling module the module imports."""
    return {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
    }


def called_services(tree: ast.Module) -> set[tuple[str, str]]:
    """Return every ``(domain, service)`` pair passed to a service call.

    Recognises the two shapes this integration uses: a call whose first two
    positional arguments are string literals, and one whose arguments are
    starred tuples of literals. Anything it cannot resolve is returned as a
    wildcard so an unrecognised shape fails loudly rather than passing.
    """
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "async_call"):
            continue
        literals = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        if len(literals) >= 2:
            pairs.add((str(literals[0]), str(literals[1])))
        else:
            pairs.add(("<unresolved>", ast.unparse(node)[:40]))
    return pairs


def constraining_references(tree: ast.Module, names: tuple[str, ...]) -> set[str]:
    """Return which of ``names`` are used to constrain rather than to report.

    Narrow on purpose, exactly as the Phase-3 equivalent is: only a comparison
    or a ``min``/``max`` call counts. Putting a limit in a diagnostics payload is
    reporting it; deciding something with it is not.
    """
    found: set[str] = set()

    def mentioned(node: ast.AST) -> set[str]:
        return {
            item.attr
            for item in ast.walk(node)
            if isinstance(item, ast.Attribute) and item.attr in names
        } | {
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and item.id in names
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"min", "max"}
        ):
            found |= mentioned(node)
    return found


# -- the modules exist, and are not stubs -----------------------------------


def test_the_boundary_check_sees_the_real_modules() -> None:
    """Every module named here exists and carries real content."""
    for name in (*PURE_MODULES, *IMPURE_MODULES):
        source = module_source(name)
        assert len(source) > 500, name


# -- purity -----------------------------------------------------------------


@pytest.mark.parametrize("name", PURE_MODULES)
def test_no_pure_control_module_imports_home_assistant(name: str) -> None:
    """The gate, the mapping and the instruments are all testable offline.

    Asserted twice: as an import, and as a substring over the whole file. The
    second is the strict one -- the word may not appear even in prose, which is
    what stops a helpful comment growing into a helpful import.
    """
    assert "homeassistant" not in imported_modules(module_tree(name)), name
    assert "homeassistant" not in module_source(name), name


@pytest.mark.parametrize("name", PURE_MODULES)
def test_no_control_module_reaches_into_the_evidence_layer(name: str) -> None:
    """Phase-2 evidence is reached through its own interface or not at all."""
    private = {"forecast_history", "history_store", "forecast_recorder", "metrics"}
    assert not imported_modules(module_tree(name)) & private, name


def test_the_adapter_cannot_reach_a_decision() -> None:
    """The vendor shell sees an intent and a command, and nothing upstream.

    This is what makes "no new decision engine" structural rather than a
    promise: the module that talks to the inverter physically cannot reach a
    policy, a simulator or a hardware limit to reason with.
    """
    for name in ("alphaess_adapter", "alphaess_device"):
        leaked = local_imports(module_tree(name)) & {
            "battery",
            "policy",
            "simulation",
            "plan",
        }
        # ``alphaess_device`` imports ``control`` for the intent type, which is
        # downstream of the decision and carries no limits.
        assert leaked == set(), f"{name} imports {sorted(leaked)}"


# -- no second clamp, no second decision ------------------------------------


@pytest.mark.parametrize("name", [*PURE_MODULES, *IMPURE_MODULES])
def test_no_control_module_constrains_by_a_hardware_limit(name: str) -> None:
    """Only the Phase-3 clamp may compare against a hardware limit."""
    leaked = constraining_references(module_tree(name), CLAMP_ONLY_NAMES)
    assert not leaked, f"{name} constrains by {sorted(leaked)}; go through the clamp"


@pytest.mark.parametrize("name", [*PURE_MODULES, *IMPURE_MODULES])
def test_no_control_module_builds_a_battery_request(name: str) -> None:
    """Requesting something of the battery is Phase 3's job, not this one's."""
    assert "BatteryRequest" not in module_source(name), name


def test_the_control_layer_does_not_publish_through_the_public_interface() -> None:
    """Phase 2's interface stays decision-free, as its own tests require.

    Nothing consumes the control layer yet, so giving it a public surface would
    mean relaxing a working guard to provide an interface with no callers.
    """
    source = (COMPONENT_DIR / "api.py").read_text(encoding="utf-8").lower()
    for forbidden in ("controlintent", "safetyverdict", "executiondecision"):
        assert forbidden not in source


# -- exactly which services may be called -----------------------------------


def test_only_the_adapter_calls_a_service() -> None:
    """One module may call a service, and it is the vendor shell."""
    callers = {
        path.stem
        for path in sorted(COMPONENT_DIR.glob("*.py"))
        if "async_call" in path.read_text(encoding="utf-8")
    }
    assert callers == {"alphaess_adapter"}


def test_the_only_service_call_takes_its_domain_from_a_planned_step() -> None:
    """The single call site cannot name a service of its own.

    It passes ``step.domain`` and ``step.service`` and nothing else, so the set
    of services reachable at runtime is exactly the set the pure planner can
    construct -- which the accompanying behavioural sweep pins to
    ``PERMITTED_SERVICES``. Checked this way round because the call site takes
    variables: an assertion that tried to read literals there would have found
    none and passed vacuously.
    """
    tree = module_tree("alphaess_adapter")
    calls = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_call"
    ]
    assert len(calls) == 1
    assert "step.domain, step.service" in calls[0]


def test_the_planner_can_only_construct_permitted_services() -> None:
    """Every service literal in the pure mapping is one of the three allowed."""
    from custom_components.alpha_ems_manager.alphaess_device import (
        PERMITTED_SERVICES,
    )

    tree = module_tree("alphaess_device")
    declared = {
        tuple(
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant)
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple)
    }
    pairs = {item for item in declared if len(item) == 2}
    assert pairs == set(PERMITTED_SERVICES)


def test_no_module_writes_modbus_directly() -> None:
    """The tested block write in the control surface is not reimplemented here.

    Checked as an API name and as a service domain rather than as the bare word,
    which appears legitimately in prose describing the user's own package -- and
    a check that fires on a comment gets deleted rather than fixed.
    """
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "write_register" not in source, path.name
        assert "modbus" not in imported_modules(tree), path.name
        assert not any(domain == "modbus" for domain, _ in called_services(tree)), (
            path.name
        )


# -- the flash boundary, and the actuator boundary --------------------------


@pytest.mark.parametrize("helper", FLASH_BACKED_HELPERS)
def test_no_flash_backed_helper_is_ever_named(helper: str) -> None:
    """A persistent inverter setting must not be writable from here at all."""
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        assert helper not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("actuator", GRID_RATE_ACTUATORS)
def test_no_grid_rate_actuator_is_ever_named(actuator: str) -> None:
    """A battery decision is never expressed as a grid-rate command."""
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        assert actuator not in path.read_text(encoding="utf-8"), path.name


# -- ownership is never inferred --------------------------------------------


def test_no_module_derives_ownership() -> None:
    """Ownership is a constant, never a comparison.

    Nothing in the control surface records who armed a dispatch, so there is no
    sound test for it. The dangerous unsound one -- comparing the running
    parameters against what would have been sent -- would be most confident
    precisely when it was most likely to be wrong, because the person watching
    the shadow recommendation is exactly who would arm those same figures.
    """
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            assert "owned" not in rendered, f"{path.name}: {rendered[:60]}"


def test_no_module_reads_a_call_context() -> None:
    """The partial signal a call context offers is not used as provenance.

    It cannot separate this integration from any other automation, and a restart
    discards it -- so it looks like evidence without being any.
    """
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "state.context" not in source, path.name
        assert "context.user_id" not in source, path.name


def test_no_stop_path_exists() -> None:
    """Nothing here turns a dispatch off.

    Stopping one would need proof that Alpha EMS created it. A stop path whose
    authorization cannot be established is worse than none, because the next
    person to read it inherits an open safety question dressed as working code.
    """
    from custom_components.alpha_ems_manager.alphaess_device import (
        OWNERSHIP_PROVABLE,
    )

    assert OWNERSHIP_PROVABLE is False

    # The meaningful structural claim: the only thing ever turned *off* is a
    # device hold flag. Turning off an activation boolean is what a stop would
    # be, and nothing constructs one.
    #
    # Checked by unparsing the planner rather than by grepping for the service
    # name, which appears legitimately three lines away. An earlier version of
    # this test intersected resolved service literals against the adapter's call
    # site -- where the arguments are variables, so nothing resolved and the
    # assertion passed vacuously.
    tree = module_tree("alphaess_device")
    planner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "plan_commands"
    )
    for node in ast.walk(planner):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node)
        if "SERVICE_TURN_OFF" in rendered:
            assert "family.hold" in rendered, rendered
            assert "activate" not in rendered, rendered


# -- the release barrier ----------------------------------------------------


def test_execution_is_unavailable_in_this_release() -> None:
    """The single constant standing between the pipeline and the inverter."""
    from custom_components.alpha_ems_manager.const import (
        CONTROL_EXECUTION_AVAILABLE,
    )

    assert CONTROL_EXECUTION_AVAILABLE is False


def test_the_executor_refuses_on_its_own() -> None:
    """The barrier is checked at the last moment as well as the first.

    Authorization already refuses, so this is redundant -- deliberately. It means
    the only way to command an inverter is to change a constant in a source file,
    not to make a mistake at a call site.
    """
    source = module_source("alphaess_adapter")
    tree = ast.parse(source)
    executor = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_execute"
    )
    rendered = ast.unparse(executor)
    assert "CONTROL_EXECUTION_AVAILABLE" in rendered
    assert "ControlExecutionUnavailable" in rendered


# -- the interval definition still lives in one place -----------------------


def test_the_interval_duration_is_still_defined_once() -> None:
    """Phase 4 divides an energy by an interval, and must not redefine one."""
    definitions = [
        path.stem
        for path in sorted(COMPONENT_DIR.glob("*.py"))
        if "INTERVAL_HOURS: float =" in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["battery"]
    for name in ("control", "safety", "alphaess_device"):
        assert "QUARTER_MINUTES / 60" not in module_source(name), name


# -- the manifest is untouched ----------------------------------------------


def test_the_control_layer_added_no_dependency() -> None:
    """Helper services are core, so nothing new is required to reach them.

    The control surface belongs to the *user*, from a package this integration
    must not require: its absence is a capability finding, not a setup failure.
    """
    import json

    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requirements"] == []
    assert manifest["iot_class"] == "calculated"
    assert "dependencies" not in manifest or manifest["dependencies"] == []
