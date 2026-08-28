"""The architectural boundaries Phase 7 must not cross, enforced statically.

Behavioural tests prove the requirement is right today. These prove it stays
right: they read the real source files, so a well-meaning refactor that gives the
reserve a second copy of a limit, teaches it to convert an efficiency, lets it
see a price or a dispatch state, or has it write the floor the planner obeys
fails here rather than in a year's time.

Modelled on ``test_phase_three_boundaries.py`` and ``test_price_neutrality.py``,
which enforce the other boundaries this project cares about the same way.

The blindness contract, in three layers
---------------------------------------

The live installation settled why this matters. ``pv_absorption.modelled``
flipped from true to false inside fifteen minutes because a dispatch began, while
both forecasts stood still. A requirement that consulted it would have jumped
from roughly fifteen kilowatt-hours to twenty-one for no physical reason, and an
earlier belief would not be reproducible. So:

1. the **signature** of ``build_reserve`` is pinned, so no control-state or price
   argument can be threaded in without a visible decision;
2. the **identifiers** those facts travel under appear nowhere in the module;
3. and ``test_reserve_model`` covers the arithmetic that signature produces.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from custom_components.alpha_ems_manager import reserve as reserve_module

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: The Phase-7 module. One file, pure: no Home Assistant, no calendar, no source.
RESERVE_MODULE = "reserve"

#: Identifiers only the clamp may test a value against, borrowed verbatim from
#: the Phase-3 boundary test. A second copy of a safety limit is a second thing
#: to keep in step, and the first time the two disagreed it would be the copy
#: that got believed.
CLAMP_ONLY_NAMES = (
    "max_charge_kw",
    "max_discharge_kw",
    "headroom_energy_kwh",
    "usable_energy_kwh",
)

#: The two floors. Phase 7 computes a requirement *above* the configured floor
#: and writes neither: the configured one is the clamp's, the effective one is
#: the policy's, and this phase is allowed to move nothing.
CONFIGURED_FLOOR = "configured_min_soc_percent"
EFFECTIVE_FLOOR = "effective_min_soc_percent"

#: Facts about the live installation that must not reach the requirement. Every
#: one of them can change while both forecasts stand still, so a figure that
#: consulted any of them would not be reproducible from stored evidence.
LIVE_STATE_NAMES = (
    "pv_absorption",
    "absorption",
    "modelled",
    "dispatch",
    "dispatch_active",
    "excess_export",
    "peak_shaving",
    "control_mode",
    "execution_enabled",
)

#: The economic vocabulary the price layer is held away from every decision by.
#: Phase 7 is held to the same list, so the reserve cannot acquire an economic
#: term even with a zero coefficient.
ECONOMIC_NAMES = (
    "price",
    "tariff",
    "cost",
    "arbitrage",
    "cheap",
    "expensive",
    "eur",
    "spread",
)


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


def identifiers(tree: ast.Module) -> set[str]:
    """Return every name, attribute, argument and definition in a file.

    Deliberately **not** the raw text. Prose has to be able to explain that this
    phase ignores prices and dispatch states, and a test that fired on its own
    documentation would be silenced rather than fixed.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
    return found


#: The parts of the module that actually compute a requirement.
#:
#: Scoped deliberately. ``ReserveSnapshot`` has to carry the absorption pair --
#: recording it is the whole point, so it can be checked afterwards that the
#: figure did not move with it -- so a rule applied to the file as a whole would
#: fire on the evidence that exists to make the rule verifiable. These are the
#: nodes where seeing a live fact would actually change an answer.
CALCULATION_NODES = (
    "_build",
    "_probe_states",
    "_withdrawal",
    "_credit",
    "build_reserve",
    "build_reserve_same_interval_only",
    "build_reserve_pv_blind",
    "ReserveProjection",
    "ReserveInterval",
)


def calculation_identifiers(tree: ast.Module) -> set[str]:
    """Return every identifier used inside the calculation, and nowhere else."""
    found: set[str] = set()
    seen: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and node.name in CALCULATION_NODES
        ):
            seen.add(node.name)
            found |= identifiers(ast.Module(body=[node], type_ignores=[]))
    assert seen == set(CALCULATION_NODES), sorted(set(CALCULATION_NODES) - seen)
    return found


def constraining_references(tree: ast.Module, names: tuple[str, ...]) -> set[str]:
    """Return which of ``names`` are used to *constrain* a value.

    Narrower than "is the name mentioned": reporting a limit is not enforcing
    one, so this looks only at the two shapes a limit can be applied in -- a
    comparison, and a ``min`` or ``max`` call.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("min", "max")
        ):
            found |= {
                inner.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Attribute)
            } & set(names)
    return found


def test_the_boundary_check_sees_the_real_module() -> None:
    """Guard the path against silently matching nothing."""
    assert (COMPONENT_DIR / f"{RESERVE_MODULE}.py").exists()
    assert len(module_source(RESERVE_MODULE)) > 5000


# -- purity ------------------------------------------------------------------


def test_the_reserve_module_imports_no_home_assistant() -> None:
    """Every rule in it has to be testable against synthetic state.

    The same standard the four Phase-3 modules are held to, so the arithmetic can
    be exercised without a running core and a failure points at the model rather
    than at a fixture.
    """
    assert "homeassistant" not in imported_modules(module_tree(RESERVE_MODULE))
    assert "homeassistant" not in module_source(RESERVE_MODULE)


def test_the_reserve_module_reaches_into_no_source_and_no_store() -> None:
    """It consumes limits and demands. Nothing else is any of its business."""
    forbidden = {
        "history_store",
        "forecast_recorder",
        "coordinator",
        "diagnostics",
        "solcast_source",
        "frank_source",
        "alphaess_adapter",
        "alphaess_device",
        "safety",
        "control",
    }

    assert not imported_modules(module_tree(RESERVE_MODULE)) & forbidden


def test_the_reserve_module_controls_nothing() -> None:
    """Observation only. No service call, no write, no command."""
    tree = module_tree(RESERVE_MODULE)

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
    }


# -- one clamp, and one conversion -------------------------------------------


def test_the_reserve_never_constrains_against_a_hardware_limit() -> None:
    """It asks the clamp what an interval can move; it never decides itself.

    The structural half of the single-clamp rule for this phase. The behavioural
    half is in ``test_reserve_model``, where the power-limited interval is
    asserted at an exact value -- together they mean a requirement can only be
    built out of energies the clamp produced.
    """
    leaked = constraining_references(module_tree(RESERVE_MODULE), CLAMP_ONLY_NAMES)

    assert not leaked, f"reserve constrains by {sorted(leaked)}; go through the clamp"


def test_the_reserve_performs_no_efficiency_arithmetic_at_all() -> None:
    """The strongest guarantee in the phase, and the cheapest to keep.

    Every AC-to-DC crossing is read as the difference the clamp produced, so the
    direction cannot be inverted, a round trip cannot be used where one crossing
    belongs, and four of the mutation shapes this phase was warned about are not
    representable rather than merely tested.
    """
    source = module_source(RESERVE_MODULE)
    names = attribute_names(module_tree(RESERVE_MODULE))

    assert "charge_efficiency" not in names
    assert "discharge_efficiency" not in names
    assert "round_trip_efficiency" not in names
    assert "/ 100.0" not in source


def test_the_reserve_defines_no_interval_duration() -> None:
    """A second literal 0.25 or 15/60 anywhere would be a unit bug waiting.

    It imports ``INTERVAL_HOURS`` to turn an energy into the power a request
    carries, and defines nothing.
    """
    source = module_source(RESERVE_MODULE)

    assert "INTERVAL_HOURS: float =" not in source
    assert "INTERVAL_HOURS =" not in source
    assert "0.25" not in source
    assert "INTERVAL_HOURS" in source


def test_the_soc_conversion_goes_through_the_one_helper() -> None:
    """A percentage is derived from an energy, and only in the one place.

    ``soc_for_energy`` and ``energy_for_soc`` are the only conversions in the
    project, so a requirement in percent is a reading of a requirement in
    kilowatt-hours rather than a second quantity that could drift from it.
    """
    names = attribute_names(module_tree(RESERVE_MODULE))

    assert "soc_for_energy" in names
    assert "energy_for_soc" in names


# -- the two floors ----------------------------------------------------------


def test_the_reserve_names_neither_floor() -> None:
    """It receives a floor energy and computes above it. It moves nothing.

    Reading ``configured_min_soc_percent`` here would put a third module on a
    name the Phase-3 boundary test holds to two, and reading
    ``effective_min_soc_percent`` would be the first step towards writing it.
    Taking a plain float instead makes both unrepresentable.
    """
    names = attribute_names(module_tree(RESERVE_MODULE))

    assert CONFIGURED_FLOOR not in names
    assert EFFECTIVE_FLOOR not in names


def test_phase_seven_adds_no_dynamic_reserve_factory() -> None:
    """The Phase-3 tripwire stays green, and that is the phase boundary.

    A factory nothing calls is exactly the shape this project rejected for
    prices: "a field with no consumer is an invitation". Phase 7 computes and
    reports; Phase 8 writes ``dynamic_reserve`` beside ``static_reserve``,
    computes ``max(configured, dynamic)`` inside it, and updates the assertion
    below in the same commit that starts using it.
    """
    battery = module_source("battery")

    assert "def static_reserve(" in battery
    assert "def dynamic_reserve(" not in battery


def test_the_plan_still_builds_its_reserve_from_the_static_factory() -> None:
    """Compute-and-report-only, read off the real source.

    If this ever became ``dynamic_reserve``, the published recommendation would
    change -- the policy reads the effective floor -- and Phase 7 would have
    started deciding. ``test_release_regressions`` pins the published figures
    themselves; this pins the reason they are unchanged.
    """
    plan = module_source("plan")

    assert "static_reserve(configured_min_soc_percent)" in plan
    assert "dynamic_reserve(" not in plan


def test_no_module_writes_the_effective_floor() -> None:
    """The policy target is still equal to the user's setting, everywhere.

    Asserted over the whole package rather than over a remembered list, because
    the interesting failure is a module nobody thought to check.
    """
    for path in COMPONENT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == EFFECTIVE_FLOOR:
                # Only the factory may supply it, and only from the floor it was
                # handed. ``battery`` is that factory.
                assert path.stem == "battery", path.stem


# -- blindness, layer one: the signature -------------------------------------


def test_build_reserve_takes_exactly_limits_a_floor_and_demands() -> None:
    """Three arguments, so nothing else can be consulted without a visible change.

    This is the load-bearing half of the blindness contract. A price, a control
    mode, a dispatch state or an absorption flag cannot reach the requirement
    because there is nowhere to put it, and widening this signature is a decision
    someone has to make on purpose.
    """
    signature = inspect.signature(reserve_module.build_reserve)

    assert set(signature.parameters) == {"limits", "floor_energy_kwh", "demands"}
    for parameter in signature.parameters.values():
        assert parameter.kind is parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "name",
    ["build_reserve_same_interval_only", "build_reserve_pv_blind"],
)
def test_the_counterfactuals_take_the_same_three_arguments(name: str) -> None:
    """So a counterfactual cannot become the one figure that *is* state-aware."""
    signature = inspect.signature(getattr(reserve_module, name))

    assert set(signature.parameters) == {"limits", "floor_energy_kwh", "demands"}


def test_only_the_shortfall_is_allowed_to_see_the_battery() -> None:
    """One function takes a state, and it computes a difference rather than a need.

    Keeping it separate is what makes the requirement reproducible: the figure
    stored as evidence does not depend on what the pack happened to hold when it
    was computed.
    """
    signature = inspect.signature(reserve_module.shortfall)

    assert list(signature.parameters) == ["projection", "state"]


# -- blindness, layer two: the identifiers -----------------------------------


@pytest.mark.parametrize("name", LIVE_STATE_NAMES)
def test_no_live_installation_fact_reaches_the_calculation(name: str) -> None:
    """Checked as an identifier, not as text, so the prose may explain the rule.

    Each of these can change while the load and production forecasts stand still.
    The live installation proved it: absorption flipped from true to false inside
    fifteen minutes because a dispatch began. A requirement that moved with it
    would not be reproducible from the evidence recorded beside it.
    """
    found = {
        identifier
        for identifier in calculation_identifiers(module_tree(RESERVE_MODULE))
        if name in identifier.lower()
    }

    assert not found, f"{sorted(found)} lets a live installation fact in"


def test_the_snapshot_records_the_absorption_pair_the_calculation_ignores() -> None:
    """Both halves, because either alone would be the wrong rule.

    Recording it is what makes the blindness checkable afterwards: two snapshots
    differing only in the flag must carry the same requirement, and that
    comparison is impossible if the flag was never written down. Dropping the
    fields would make the phase unfalsifiable; reading them in the calculation
    would make it irreproducible.
    """
    fields = {
        field.name for field in dataclasses.fields(reserve_module.ReserveSnapshot)
    }

    assert "pv_absorption_modelled" in fields
    assert "pv_absorption_reason" in fields
    assert "replenishment_assumption" in fields
    # And none of them is a field of the thing that computes the figure.
    computed = {
        field.name for field in dataclasses.fields(reserve_module.ReserveProjection)
    }
    assert not {name for name in computed if "absorption" in name}


@pytest.mark.parametrize("name", ECONOMIC_NAMES)
def test_no_economic_term_is_named_in_the_reserve(name: str) -> None:
    """The same guard the decision layer has passed since before prices existed.

    An economic term added with a zero coefficient would behave identically today
    and be load-bearing tomorrow, which is why this is a name check rather than a
    behavioural comparison.
    """
    found = {
        identifier
        for identifier in calculation_identifiers(module_tree(RESERVE_MODULE))
        if name in identifier.lower()
    }

    assert not found, f"{sorted(found)} names an economic term"


def test_the_reserve_imports_no_part_of_the_price_layer() -> None:
    """Directly, and the converse is asserted in ``test_price_neutrality``."""
    assert not imported_modules(module_tree(RESERVE_MODULE)) & {
        "price_forecast",
        "frank_source",
    }


def test_the_headroom_flag_cannot_select_a_model() -> None:
    """It is derived after the walk, never read inside it.

    ``headroom_bound`` says the published figure understates. If the recursion
    consulted it, the phase would be choosing between reserve models on the
    strength of its own output -- which is the one thing the single-authoritative
    -figure rule forbids.
    """
    tree = module_tree(RESERVE_MODULE)
    walk_function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build"
    )
    inside = {
        node.attr for node in ast.walk(walk_function) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(walk_function) if isinstance(node, ast.Name)}

    assert "headroom_bound" not in inside
    assert "lower_bound_reason" not in inside
    assert "reachable" not in inside


def test_the_authoritative_figure_is_named_once_and_the_rest_are_labelled() -> None:
    """Two builders that answer different questions, and two counterfactuals.

    Names carry the contract here, and beta.31 made the contract explicit rather
    than implied. ``build_reserve`` answers *"how much if we never buy again?"* --
    an **autonomy** question -- and for six releases that figure was also the hard
    floor the economic solver obeyed, which immobilised 96.9 % of the usable pack
    on the reference installation against a 20 % physical floor.

    ``build_reserve_reachable`` answers the question a hard bound should ask: *can
    the pack hold the floor given replenishment that is physically possible and
    actionable?* It is the one the production solver consumes. The autonomy figure
    remains, published and consumed by diagnostics alone -- pinned by
    ``test_the_autonomy_requirement_reaches_no_production_solve``.
    """
    public = {name for name in vars(reserve_module) if name.startswith("build_reserve")}

    assert public == {
        "build_reserve",
        "build_reserve_reachable",
        "build_reserve_same_interval_only",
        "build_reserve_pv_blind",
        "build_reserve_snapshot",
    }
