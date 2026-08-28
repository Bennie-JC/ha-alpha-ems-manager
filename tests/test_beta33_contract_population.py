"""No field of the execution contract may be published null by every target.

**The guard for a failure this project has now had twice.** In beta.27
``quarter_schedule`` was an optional parameter the production call site never
passed, so every run published an empty list beside a rule string describing what
should have been in it. In beta.32 ``campaign_id`` was an optional parameter the
production call site never passed, so every target published ``null`` and the
whole campaign lifecycle sat inert. Both times the callee accepted a parameter the
caller never supplied; both times the suite stayed green because every test built
its own dict.

A unit test cannot catch that, because a unit test supplies the argument itself.
What catches it is asking the *production builder*, on a representative solve, a
question no hand-built fixture can answer: **is there any documented contract field
that is null for every single target?**

A field that is null everywhere is either not wired or not real. Either way it must
be named here deliberately, with a reason, rather than discovered on hardware.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
)

from .test_beta33_campaign_wiring import planning_coordinator

pytestmark = pytest.mark.usefixtures("control_surface")

#: Fields that are legitimately absent on *every* target of an ordinary plan, each
#: with the reason it is absent. Anything not listed here must be populated by at
#: least one target, or the contract is describing something production does not
#: produce.
#:
#: **Keep this list short and argued.** Every entry is a field a consumer can read
#: and will always find empty; that is a cost, and it should be paid knowingly.
_ALLOWED_ALWAYS_NULL: dict[str, str] = {
    # Set only when the plan constrains headroom, which an ordinary shape does not.
    # Absent means *unconstrained*, explicitly not zero.
    "required_headroom_kwh": "null means unconstrained, never zero",
    "max_end_energy_kwh": "same constraint as required_headroom_kwh",
    "headroom_until": "same constraint as required_headroom_kwh",
}

#: Fields whose whole purpose is to be present, checked by name as well as by the
#: sweep -- so deleting one from the contract fails loudly rather than silently
#: shrinking what this guard covers.
_MUST_BE_POPULATED = (
    "plan_id",
    "campaign_id",
    "campaign_end",
    "intent",
    "purpose",
    "window_start",
    "window_end",
    "issued_at",
    "stale_after",
    "battery_target_kwh",
    "quarter_schedule",
    "revision",
    "reserve_floor_kwh",
)


async def test_no_contract_field_is_null_across_every_published_target(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The sweep. A field null everywhere is unwired or unreal.

    This is the assertion that would have caught both `quarter_schedule` in
    beta.27 and `campaign_id` in beta.32, without either defect having to reach
    hardware first.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    targets = list(coordinator.execution_targets)
    assert targets, "the fixture must publish targets for the sweep to mean anything"

    keys: set[str] = set()
    for target in targets:
        keys.update(target)

    always_null = sorted(
        key
        for key in keys
        if key not in _ALLOWED_ALWAYS_NULL
        and all(target.get(key) is None for target in targets)
    )

    assert not always_null, (
        f"published by production as null on every target: {always_null}. "
        "Either the field is not wired at the call site -- the beta.27 "
        "quarter_schedule and beta.32 campaign_id defect -- or it is not real and "
        "should be removed. If it is genuinely optional, add it to "
        "_ALLOWED_ALWAYS_NULL with the reason."
    )


async def test_the_named_contract_fields_are_populated(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The same guarantee by name, so shrinking the contract is visible."""
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    targets = list(coordinator.execution_targets)

    for field in _MUST_BE_POPULATED:
        populated = [t for t in targets if t.get(field) is not None]
        assert populated, f"no published target carries {field!r}"

    # ``quarter_schedule`` is the beta.27 case specifically: an empty list is
    # populated-but-useless, so emptiness is checked as well as presence.
    for target in targets:
        assert target["quarter_schedule"], f"{target['intent']} published no rows"


async def test_the_allow_list_does_not_hide_a_wired_field(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """An allow-list nobody prunes stops being a record and becomes a blanket.

    A field listed as always-null that production *does* populate has been fixed,
    and the entry must go -- otherwise the sweep silently stops covering it.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    targets = list(coordinator.execution_targets)

    stale = sorted(
        key
        for key in _ALLOWED_ALWAYS_NULL
        if any(target.get(key) is not None for target in targets)
    )
    assert not stale, (
        f"these are on the always-null allow-list but production populates them: "
        f"{stale}. Remove the entries so the sweep covers them again."
    )


async def test_both_executable_intents_publish_their_own_objective_boundary(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The boundary asymmetry, checked on real targets rather than asserted.

    A charge is aimed at the battery and an export at the meter; the other figure
    is a ceiling. Publishing one where the other belongs is how 1.3 kW of intended
    export became a 1.3 kW battery command.
    """
    coordinator = await planning_coordinator(hass, setup_integration, frank)
    by_intent: dict[str, list[dict]] = {}
    for target in coordinator.execution_targets:
        by_intent.setdefault(target["intent"], []).append(target)

    for target in by_intent.get(EXECUTION_INTENT_GRID_CHARGE, []):
        assert target["battery_target_kwh"] > 0.0
        assert target["grid_target_kwh"] is None, (
            "a charge has no meter objective; a present one invites a consumer to "
            "aim at the wrong boundary"
        )

    for target in by_intent.get(EXECUTION_INTENT_NET_EXPORT, []):
        assert target["grid_target_kwh"] is not None
        assert target["battery_target_kwh"] > 0.0, "the battery figure is the ceiling"
