"""The Solcast boundary is read-only, and structurally rather than by intention.

Phase 5 widened a working guard: before it, exactly one module in this package
could call a Home Assistant service. Widening a guard is where safety properties
get lost, so the widening comes with four rules that together say something
stronger than the original single-caller rule did.

1. Two named modules may call a service, as an exact set.
2. Every call site here passes **string literals** for the domain and the action.
3. **No function here takes a domain or an action as an argument**, so no generic
   ``_call(domain, service, data)`` can appear later and become the escape hatch.
4. Every mutating Solcast action is named once in ``const`` and appears nowhere
   else in the package.

The reason rule 2 is stated this way round: the Phase-4 adapter's single call site
passes *variables* from a planned command, and an assertion that looked for
literals there would have found none and passed vacuously. Here the opposite is
true, so the assertion is the opposite.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from custom_components.alpha_ems_manager import solcast_source
from custom_components.alpha_ems_manager.const import (
    SOLCAST_DOMAIN,
    SOLCAST_FORBIDDEN_SERVICES,
    SOLCAST_PERMITTED_SERVICES,
    SOLCAST_SERVICE_DIAGNOSTIC,
    SOLCAST_SERVICE_QUERY_FORECAST,
)

COMPONENT_DIR = Path("custom_components/alpha_ems_manager")

#: Words that would mean the user was being asked to classify electrical
#: topology. Phase 5 asks exactly one question -- which sites belong to this
#: installation -- and this is what proves it never asks the other one.
TOPOLOGY_WORDS = (
    "dc_coupled",
    "ac_coupled",
    "dc-coupled",
    "ac-coupled",
    "hybrid_side",
    "grid_inverter_side",
    "inverter_input",
    "electrical_coupling",
    "coupling_type",
)


def module_tree(name: str) -> ast.Module:
    """Return the parsed source of one module."""
    return ast.parse((COMPONENT_DIR / f"{name}.py").read_text(encoding="utf-8"))


def package_sources() -> dict[str, str]:
    """Return every module's source, keyed by module name."""
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(COMPONENT_DIR.glob("*.py"))
    }


def service_calls(name: str) -> list[ast.Call]:
    """Return every ``async_call`` invocation in one module."""
    return [
        node
        for node in ast.walk(module_tree(name))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_call"
    ]


# -- rule 2: literal domain and action ---------------------------------------


def _static_value(node: ast.expr) -> str:
    """Return the value a call argument statically resolves to.

    A bare string resolves to itself. A bare name resolves to the module-level
    constant it refers to -- which is a *stronger* guarantee than an inline
    string, because the constant is the same one the deny-list test reasons about.

    Anything else fails: a call, a subscript, an attribute or an f-string is a
    value computed at runtime, and a runtime value can name any action at all.
    """
    if isinstance(node, ast.Constant):
        assert isinstance(node.value, str), ast.dump(node)
        return node.value
    if isinstance(node, ast.Name):
        resolved = getattr(solcast_source, node.id, None)
        assert isinstance(resolved, str), f"{node.id} is not a module-level string"
        return resolved
    raise AssertionError(f"computed at runtime, not static: {ast.dump(node)}")


def test_every_call_site_names_its_domain_and_action_statically() -> None:
    """The reachable action set is visible in the source, not computed at runtime.

    Asserted against the *resolved* values rather than merely against "some
    string", so a static reference naming the wrong action still fails.
    """
    calls = service_calls("solcast_source")

    assert len(calls) == 2, "expected exactly one read of each permitted action"

    named = set()
    for call in calls:
        assert len(call.args) >= 2, ast.dump(call)
        named.add((_static_value(call.args[0]), _static_value(call.args[1])))

    assert named == {
        (SOLCAST_DOMAIN, SOLCAST_SERVICE_QUERY_FORECAST),
        (SOLCAST_DOMAIN, SOLCAST_SERVICE_DIAGNOSTIC),
    }


def test_a_computed_domain_would_be_rejected() -> None:
    """The guard above is only worth having if it can actually fail.

    Without this, a bug that made ``_static_value`` accept everything would leave
    the literal check passing vacuously -- the same failure mode that made one of
    the Phase-4 structural checks worthless until it was found.
    """
    computed = ast.parse("hass.services.async_call(pick(), name, {})").body[0]
    call = computed.value

    with pytest.raises(AssertionError):
        _static_value(call.args[0])


def test_the_literals_resolve_through_the_named_constants() -> None:
    """The call sites use the constants rather than open-coding the strings.

    A literal that happened to match today but drifted later would pass the check
    above; this one fails if the module stops going through ``const``.
    """
    source = (COMPONENT_DIR / "solcast_source.py").read_text(encoding="utf-8")

    assert "SOLCAST_DOMAIN," in source
    assert "SOLCAST_SERVICE_QUERY_FORECAST," in source
    assert "SOLCAST_SERVICE_DIAGNOSTIC," in source


def test_both_reads_ask_for_a_response() -> None:
    """Both actions are response-only and raise without it.

    Not a style point: omitting ``return_response`` makes the call fail at
    runtime, on an installation rather than here, and the failure would look like
    "Solcast is broken".
    """
    for call in service_calls("solcast_source"):
        keywords = {keyword.arg for keyword in call.keywords}
        assert "return_response" in keywords
        assert "blocking" in keywords


# -- rule 3: no parameterised call surface -----------------------------------


@pytest.mark.parametrize(
    "name",
    sorted(
        name
        for name, value in vars(solcast_source).items()
        if not name.startswith("__") and inspect.isfunction(value)
    ),
)
def test_no_function_accepts_a_domain_or_an_action(name: str) -> None:
    """This is the guard that stops a generic helper appearing later.

    A module that may call two named actions is a boundary. The same module with
    one ``_call(domain, service, data)`` in it is a way to call anything, and the
    difference is invisible in a review of the call sites alone.
    """
    signature = inspect.signature(getattr(solcast_source, name))
    forbidden = {"domain", "service", "action", "service_name", "domain_name"}
    offending = forbidden & set(signature.parameters)

    assert not offending, f"{name} takes {sorted(offending)}"


def test_the_site_argument_names_a_rooftop_and_not_a_service() -> None:
    """``site_id`` is the one caller-supplied value that reaches the payload.

    It lands in the action's data, never in its name, so it cannot redirect the
    call. Asserted because "one string the caller controls" is exactly the shape
    an escape hatch takes.
    """
    for call in service_calls("solcast_source"):
        for argument in call.args[:2]:
            assert "site" not in _static_value(argument)


# -- rule 4: the mutating actions are unreachable ----------------------------


@pytest.mark.parametrize("service", SOLCAST_FORBIDDEN_SERVICES)
def test_no_mutating_solcast_action_is_named_anywhere(service: str) -> None:
    """Quota-consuming and configuration-changing actions, all absent.

    The same technique as the Phase-4 flash-backed helper deny-list: the forbidden
    names live in exactly one tuple in ``const``, so a test can prove they appear
    nowhere else. ``set_options`` and ``set_dampening`` would silently rewrite the
    user's own Solcast configuration; the update actions would spend an allowance
    of ten calls a day that Alpha EMS does not own.
    """
    offenders = {
        name
        for name, source in package_sources().items()
        if service in source and name != "const"
    }

    assert not offenders, f"{service} appears in {sorted(offenders)}"


def test_the_permitted_and_forbidden_sets_do_not_overlap() -> None:
    """A name cannot be both, and a typo that made one both would hide the other."""
    assert not set(SOLCAST_PERMITTED_SERVICES) & set(SOLCAST_FORBIDDEN_SERVICES)


def test_the_forbidden_list_covers_every_documented_mutating_action() -> None:
    """Pinned so removing one from the deny-list is a visible change."""
    assert set(SOLCAST_FORBIDDEN_SERVICES) == {
        "update_forecasts",
        "force_update_forecasts",
        "force_update_estimates",
        "clear_all_solcast_data",
        "set_options",
        "set_dampening",
        "set_hard_limit",
        "remove_hard_limit",
    }


# -- the network boundary is unchanged ---------------------------------------


def test_the_solcast_reader_opens_no_connection_of_its_own() -> None:
    """Everything comes through Home Assistant. Nothing here is a client.

    ``test_no_external_polling.py`` enforces this across the package; repeated for
    the one module that has a reason to be tempted.
    """
    source = (COMPONENT_DIR / "solcast_source.py").read_text(encoding="utf-8").lower()

    # Deliberately not the word "requests": it occurs in ordinary prose in this
    # module's own docstring, and a substring guard that fires on documentation
    # gets silenced rather than fixed. Imports are covered package-wide by
    # ``test_no_external_polling.py``, which reads the import statements.
    for forbidden in ("://", "socket", "urllib", "aiohttp", "solcast.com"):
        assert forbidden not in source, forbidden

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(module_tree("solcast_source"))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [])
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(module_tree("solcast_source"))
        if isinstance(node, ast.ImportFrom)
    }

    assert not imported & {"socket", "urllib", "aiohttp", "requests", "http"}


# -- the topology question is never asked ------------------------------------


@pytest.mark.parametrize("word", TOPOLOGY_WORDS)
def test_no_configuration_field_asks_the_user_about_electrical_topology(
    word: str,
) -> None:
    """The one question Phase 5 must not ask.

    Which selected site corresponds to which AlphaESS subsystem is not reliably
    known to a user, and a guessed topology recorded as fact is worse than a
    declared unknown. Asserted over the sources *and* both translation files,
    because a translation string is where such a question would actually appear.
    """
    # Scoped to where a question can actually be put to a user: the config flow
    # that builds the forms, and the translation files that carry their wording.
    # Deliberately *not* every comment in the package -- ``const`` documents at
    # length that this question is never asked, and a substring guard that fires
    # on the documentation of its own rule is how guards get deleted.
    flow = (COMPONENT_DIR / "config_flow.py").read_text(encoding="utf-8").lower()

    assert word not in flow, f"{word} in config_flow"

    for path in sorted((COMPONENT_DIR / "translations").glob("*.json")):
        assert word not in path.read_text(encoding="utf-8").lower(), path.name


def test_electrical_correspondence_is_recorded_as_unknown() -> None:
    """Not merely unasked: positively recorded as not known."""
    from custom_components.alpha_ems_manager.pv_forecast import PvProvenance

    assert PvProvenance().electrical_correspondence == "unknown"
