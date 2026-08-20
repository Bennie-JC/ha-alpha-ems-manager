"""The architectural boundaries Phase 3 must not cross, enforced statically.

Behavioural tests prove the model is right today. These prove it stays right:
they read the real source files, so a well-meaning refactor that reintroduces a
second copy of a safety limit, reaches past the public interface, or teaches a
Phase-3 policy to charge fails here rather than in a year's time.

Modelled on ``tests/test_api_boundary.py`` and ``tests/test_no_external_polling``,
which enforce the other two boundaries this project cares about the same way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: The Phase-3 modules. Pure: no Home Assistant, and no reach into the evidence
#: layer.
PHASE_THREE_MODULES = ("battery", "simulation", "policy", "plan")

#: Where the hardware limits are allowed to be compared against. One module, and
#: one function inside it.
CLAMP_MODULE = "battery"

#: Identifiers that only the clamp may test a request against. A second copy
#: anywhere else is a second thing to keep in step, and the first time the two
#: disagreed it would be the copy that got believed.
CLAMP_ONLY_NAMES = (
    "max_charge_kw",
    "max_discharge_kw",
    "headroom_energy_kwh",
    "usable_energy_kwh",
)

#: The user's configured floor. Read by the clamp, and reported -- never used as
#: a policy target, which is what ``effective_min_soc_percent`` is for.
CONFIGURED_FLOOR = "configured_min_soc_percent"
EFFECTIVE_FLOOR = "effective_min_soc_percent"


def module_source(name: str) -> str:
    """Return one component module's source."""
    return (COMPONENT_DIR / f"{name}.py").read_text(encoding="utf-8")


def module_tree(name: str) -> ast.Module:
    """Return one component module's parsed syntax tree."""
    return ast.parse(module_source(name))


def imported_modules(tree: ast.Module) -> set[str]:
    """Return every module a file imports, sibling or absolute."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0] if node.level else node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
    return found


def attribute_names(tree: ast.Module) -> list[str]:
    """Return every attribute name accessed anywhere in a file."""
    return [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]


def _referenced(node: ast.AST) -> set[str]:
    """Return every attribute name referenced inside one syntax node."""
    return {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}


def constraining_references(tree: ast.Module, names: tuple[str, ...]) -> set[str]:
    """Return which of ``names`` are used to *constrain* a value.

    Deliberately narrower than "is the name mentioned anywhere". Reporting a
    limit is not enforcing one -- diagnostics has to be able to publish the
    capacity and the power limits, and a test that could not tell the two apart
    would be weakened the first time it fired on a report rather than fixed.

    So this looks only at the two shapes a limit can actually be applied in: a
    comparison, and a ``min`` or ``max`` call.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("min", "max")
        ):
            found |= _referenced(node) & set(names)
    return found


def function_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one named function, asserting it exists."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    """Return one named class, asserting it exists."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_the_boundary_check_sees_the_real_modules() -> None:
    """Guard the paths against silently matching nothing."""
    names = {path.stem for path in COMPONENT_DIR.glob("*.py")}

    assert set(PHASE_THREE_MODULES) <= names
    for name in PHASE_THREE_MODULES:
        assert len(module_source(name)) > 500, name


# -- purity ------------------------------------------------------------------


@pytest.mark.parametrize("name", PHASE_THREE_MODULES)
def test_no_phase_three_module_imports_home_assistant(name: str) -> None:
    """Every rule in these files has to be testable against synthetic state.

    The same standard ``forecast_history`` and ``metrics`` are held to: no
    *direct* Home Assistant import, so the arithmetic can be exercised without a
    running core and a failure points at the model rather than at a fixture.
    """
    assert "homeassistant" not in imported_modules(module_tree(name))
    assert "homeassistant" not in module_source(name)


@pytest.mark.parametrize("name", PHASE_THREE_MODULES)
def test_no_phase_three_module_reaches_into_the_evidence_layer(name: str) -> None:
    """Phase 3 consumes Phase 2 through ``api`` and nothing deeper.

    ``test_api_boundary`` already parametrises over every file in the package, so
    this is belt and braces -- but it names the phase, which is what makes the
    rule findable by someone adding a fifth module.
    """
    private = {"forecast_history", "history_store", "forecast_recorder", "metrics"}
    assert not imported_modules(module_tree(name)) & private


def test_the_public_interface_is_still_free_of_decisions() -> None:
    """``api.py`` reports what was predicted; it does not decide anything.

    Phase 3 deliberately does **not** publish its plan here. That would have
    meant deleting the guard in ``test_api_boundary`` that keeps this module
    descriptive, and the plan needs no public surface yet because nothing may
    consume it. When Phase 4 needs one it should get its own module.
    """
    source = module_source("api").lower()

    for forbidden in ("def charge", "def discharge", "def recommend", "def schedule"):
        assert forbidden not in source
    assert "batteryplan" not in source
    assert "batterydecision" not in source


def test_the_plan_module_is_the_only_phase_three_consumer_of_the_api() -> None:
    """One door, used once, so the coupling is visible in one place."""
    users = {
        name
        for name in PHASE_THREE_MODULES
        if "api" in imported_modules(module_tree(name))
    }

    assert users == {"plan"}


# -- one clamp ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", [module for module in PHASE_THREE_MODULES if module != CLAMP_MODULE]
)
def test_only_the_clamp_compares_against_a_hardware_limit(name: str) -> None:
    """No policy, simulator or plan may re-implement a safety limit.

    This is the structural half of the single-clamp rule. The behavioural half is
    in ``test_battery_model``; together they mean a request can only become an
    executable energy by going through ``apply_request``.
    """
    leaked = constraining_references(module_tree(name), CLAMP_ONLY_NAMES)

    assert not leaked, f"{name} constrains by {sorted(leaked)}; go through the clamp"


def test_the_clamp_lives_in_exactly_one_function() -> None:
    """``apply_request`` is the only thing that reduces a request."""
    tree = module_tree(CLAMP_MODULE)
    clamping = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            attribute in ast.dump(node)
            for attribute in ("max_charge_kw", "max_discharge_kw")
        )
    }

    assert clamping == {"apply_request", "build_limits"}


def test_the_simulator_never_enforces_anything_itself() -> None:
    """It steps and records; the limits come from the clamp it calls.

    ``simulate`` must contain no comparison and no bounding call at all: its only
    branch is "was this interval forecast", and every number it produces comes
    from ``apply_request`` or from arithmetic on the results.
    """
    tree = module_tree("simulation")
    assert "apply_request" in module_source("simulation")

    body = function_node(tree, "simulate")
    bounding = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("min", "max")
    ]

    assert bounding == []


# -- the two floors ----------------------------------------------------------


def test_the_configured_floor_is_read_by_the_clamp_and_the_report_only() -> None:
    """The user's setting is the hard floor and never a policy target.

    ``battery`` clamps at it, ``plan`` reports it, and nothing else touches it --
    so when Phase 7 starts raising the effective reserve it cannot accidentally
    move the floor the user chose.
    """
    readers = {
        name
        for name in PHASE_THREE_MODULES
        if CONFIGURED_FLOOR in attribute_names(module_tree(name))
    }

    assert readers <= {"battery", "plan"}


def test_the_clamp_never_reads_the_effective_floor() -> None:
    """The policy target must not leak into the hard limit.

    Checked against the two things that actually decide how much energy may
    leave the battery -- ``apply_request`` and the state's own energy properties
    -- rather than against the whole file, because ``BatteryReserve`` itself
    obviously has to hold both fields and compare them.

    If the clamp measured against the effective reserve, a later phase could not
    express "a price spike justifies dipping into the reserve, but never below
    the floor the user set" -- and merging the two names later is free while
    splitting a persisted one is not.
    """
    tree = module_tree("battery")

    assert EFFECTIVE_FLOOR not in _referenced(function_node(tree, "apply_request"))
    assert EFFECTIVE_FLOOR not in _referenced(class_node(tree, "BatteryState"))
    # And the floor the state does read is the configured one.
    assert CONFIGURED_FLOOR in _referenced(class_node(tree, "BatteryState"))


def test_the_simulator_never_reads_either_floor_directly() -> None:
    """It carries the reserve and hands it to the clamp; it never interprets it."""
    tree = module_tree("simulation")

    assert EFFECTIVE_FLOOR not in attribute_names(tree)
    assert CONFIGURED_FLOOR not in attribute_names(tree)


def test_the_policy_is_the_thing_that_reads_the_effective_floor() -> None:
    """Which is what makes the distinction real rather than decorative."""
    assert EFFECTIVE_FLOOR in attribute_names(module_tree("policy"))


def test_the_reserve_factory_is_the_only_way_to_build_a_reserve() -> None:
    """So the maximum that protects the user's floor cannot be forgotten.

    Phase 7 must add its factory beside ``static_reserve`` and compute
    ``max(configured, dynamic)`` inside it. This asserts the shape that makes
    that the obvious place.
    """
    source = module_source("battery")

    assert "def static_reserve(" in source
    assert "def dynamic_reserve(" not in source, (
        "a dynamic reserve belongs to Phase 7; it must clamp inside the factory"
    )


# -- the phase boundary ------------------------------------------------------


def test_no_phase_three_module_controls_anything() -> None:
    """Observation only. No service call, no write, no command.

    The whole promise of this phase in one assertion, read off the real sources.
    """
    for name in PHASE_THREE_MODULES:
        tree = module_tree(name)
        # No network or executor client, checked as an import rather than as a
        # substring: the word "requests" appears in this project's prose about
        # battery requests, and a test that fired on a docstring would be
        # silenced rather than fixed.
        assert not imported_modules(tree) & {"requests", "aiohttp", "httpx", "urllib"}

        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not called & {
            "async_call",
            "call",
            "async_add_executor_job",
            "async_create_task",
        }, name


def test_the_charge_path_exists_but_no_policy_uses_it() -> None:
    """Both halves of the rule, because either alone would be wrong.

    Removing the charge path would leave Phases 5 to 8 nowhere to land and make
    what-if comparison impossible. Letting a Phase-3 policy use it would spend
    the user's money for a reason this phase cannot state.
    """
    assert "MODE_CHARGE" in module_source("battery")
    assert "CONSTRAINT_MAX_CHARGE_POWER" in module_source("battery")

    policy_source = module_source("policy")
    assert "BatteryRequest.charge(" not in policy_source


def test_the_shipped_policy_list_is_what_the_charge_rule_is_asserted_over() -> None:
    """A rule asserted over a hand-written list is a rule with a hole in it."""
    from custom_components.alpha_ems_manager import policy as policy_module

    declared = {
        node.name
        for node in ast.walk(module_tree("policy"))
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "BatteryPolicy"
            for base in node.bases
        )
    }
    shipped = {cls.__name__ for cls in policy_module.SHIPPED_POLICIES}

    assert declared == shipped


# -- no duplicated conversions ----------------------------------------------


def test_the_interval_duration_is_defined_once() -> None:
    """A second literal 0.25 or 15/60 anywhere would be a unit bug waiting."""
    definitions = [
        name
        for name in PHASE_THREE_MODULES
        if "INTERVAL_HOURS: float =" in module_source(name)
        or "INTERVAL_HOURS =" in module_source(name)
    ]

    assert definitions == ["battery"]
    for name in ("simulation", "plan"):
        assert "0.25" not in module_source(name), name


def test_percentages_are_converted_in_one_place() -> None:
    """Dividing by a hundred in five files is how a fraction becomes a percent."""
    conversions = {
        name for name in PHASE_THREE_MODULES if "/ 100.0" in module_source(name)
    }

    assert conversions == {"battery"}


def test_the_existing_normalisation_infrastructure_is_reused() -> None:
    """A second numeric parser would drift from the tested one.

    The state-of-charge read path goes through ``normalize_percentage`` and then
    ``sanitize_soc_percent``: the first insists the unit really says percent, the
    second confines a noise band and refuses the rest.
    """
    coordinator = module_source("coordinator")

    assert "normalize_percentage(" in coordinator
    assert "sanitize_soc_percent(" in coordinator
    # And the plausibility band is not re-implemented in the coordinator.
    assert "SOC_NOISE_BAND_PERCENT" not in coordinator
