"""beta.49: what a campaign start line is entitled to say.

A Trading Log renders one sentence when execution physically begins:

    Verkopen gestart —> 4.52 kWh —> economische verkoop

Neither figure in it existed. The ``started`` event carried ``planned_kwh``, which is
the *creation* snapshot -- frozen when the campaign opened, and immutable, so it is
the right number for the plan-created line and the wrong one here. The execution
target frozen at first activation, ``_campaign_frozen_target_kwh``, was published
nowhere at all: at terminal it is recoverable as realised plus shortfall, but at
``started`` there is no shortfall yet.

And no classification was captured at first execution. ``classification_at_creation``
is the wrong instant, ``final_classification`` is the terminal-time live value, and
the live value legitimately moves -- a campaign spanning two admitted plans reads one
category and then another under one unchanged instance id. Rendering either would
make a historical line change its own meaning afterwards.

Two additive fields, publication-only. Nothing else in this release.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import callback

from custom_components.alpha_ems_manager.const import (
    EVENT_CAMPAIGN_LIFECYCLE,
    LIFECYCLE_KIND_CREATED,
    LIFECYCLE_KIND_PLANNED,
    LIFECYCLE_KIND_STARTED,
)


@pytest.fixture
def lifecycle_events(hass) -> list[dict[str, Any]]:
    """Record every lifecycle event, in fire order."""
    seen: list[dict[str, Any]] = []

    @callback
    def _record(event: Any) -> None:
        seen.append(event.data)

    hass.bus.async_listen(EVENT_CAMPAIGN_LIFECYCLE, _record)
    return seen


def _started(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single ``started`` payload, insisting there is exactly one."""
    starts = [e for e in events if e.get("kind") == LIFECYCLE_KIND_STARTED]
    assert len(starts) == 1, [e.get("kind") for e in events]
    return starts[0]


async def _run_a_campaign(hass, setup_integration, frank, source_entities):
    """Drive a campaign to first physical execution."""
    from .test_beta33_campaign_wiring import planning_coordinator

    coordinator = await planning_coordinator(hass, setup_integration, frank)
    await hass.async_block_till_done()
    return coordinator


async def test_a_start_carries_the_target_it_actually_froze(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """**The execution target at first activation, which was published nowhere.**

    Distinct from ``planned_kwh`` on the same payload: that is the creation snapshot
    and never moves, while this is frozen when execution begins and may legitimately
    be larger, because ``_grow_campaign_target`` is monotonic non-decreasing.

    *Mutation: publish planned_kwh here and the two become indistinguishable.*
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_frozen_target_kwh = 4.52
    coordinator._campaign_classification_at_start = None
    coordinator.store.campaign_lifecycle = {
        "instance_id": "i1",
        "marks": [LIFECYCLE_KIND_CREATED],
        "campaign_id": "c1",
        "planned_kwh": 4.97,
    }
    lifecycle_events.clear()

    coordinator._campaign_classification_at_start = "economic_export"
    coordinator._lifecycle_started(coordinator.last_refresh_at)
    await hass.async_block_till_done()

    started = _started(lifecycle_events)
    assert started["frozen_target_kwh"] == pytest.approx(4.52)
    assert started["classification_at_start"] == "economic_export"


async def test_the_frozen_target_is_null_and_never_zero_when_unknown(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """A campaign whose objective could not be read has no target, not a zero one."""
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_frozen_target_kwh = None
    coordinator._campaign_classification_at_start = None
    coordinator.store.campaign_lifecycle = {
        "instance_id": "i2",
        "marks": [LIFECYCLE_KIND_CREATED],
        "campaign_id": "c2",
        "planned_kwh": 1.0,
    }
    lifecycle_events.clear()

    coordinator._lifecycle_started(coordinator.last_refresh_at)
    await hass.async_block_till_done()

    started = _started(lifecycle_events)
    assert started["frozen_target_kwh"] is None
    assert started["classification_at_start"] is None


async def test_a_start_line_cannot_change_its_meaning_afterwards(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """**The whole reason a start-time classification had to be captured.**

    The live classification moves. If the start line read it live -- or read
    ``final_classification`` -- a sentence already written in a user's history would
    describe a different campaign type later.

    *Mutation: read the live classification at fire time and this fails.*
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_frozen_target_kwh = 2.0
    coordinator._campaign_classification_at_start = "economic_export"
    coordinator.store.campaign_lifecycle = {
        "instance_id": "i3",
        "marks": [LIFECYCLE_KIND_CREATED],
        "campaign_id": "c3",
        "planned_kwh": 2.0,
    }
    lifecycle_events.clear()

    coordinator._lifecycle_started(coordinator.last_refresh_at)
    await hass.async_block_till_done()
    recorded = _started(lifecycle_events)["classification_at_start"]

    # The world moves on: the campaign is re-solved into a different category.
    coordinator._campaign_classifications = {"c3": {"classification": "serve_load"}}

    assert recorded == "economic_export"
    assert coordinator._campaign_classification_at_start == "economic_export"


async def test_started_still_fires_exactly_once_per_instance(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """The two new fields ride the existing guard; they do not weaken it.

    A multi-row campaign re-arms at every row boundary and at every serve_load gap.
    None of that is a start.
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_frozen_target_kwh = 3.0
    coordinator._campaign_classification_at_start = "economic_buy"
    coordinator.store.campaign_lifecycle = {
        "instance_id": "i4",
        "marks": [LIFECYCLE_KIND_CREATED],
        "campaign_id": "c4",
        "planned_kwh": 3.0,
    }
    lifecycle_events.clear()

    for _ in range(4):
        coordinator._lifecycle_started(coordinator.last_refresh_at)
    await hass.async_block_till_done()

    starts = [e for e in lifecycle_events if e.get("kind") == LIFECYCLE_KIND_STARTED]
    assert len(starts) == 1


async def test_an_announcement_never_carries_the_new_fields(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """**The null-instance rule is not weakened.**

    ``planned`` and ``plan_closed`` describe a campaign that has not been placed, so
    they carry no instance id -- and must carry no execution figures either. The
    codebase states the rule as: no announcement event uses a campaign kind, and no
    campaign event carries a null instance id.
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    # A start has happened, so both fields are live -- which is precisely when an
    # announcement leaking one would be plausible enough to go unnoticed.
    coordinator._campaign_frozen_target_kwh = 4.52
    coordinator._campaign_classification_at_start = "economic_export"

    payload = coordinator._announcement_payload(
        {
            "campaign_id": "c-announced",
            "purpose": "export",
            "objective_boundary": "meter",
            "planned_kwh": 4.97,
            "window_start": None,
            "window_end": None,
        }
    )

    assert payload["campaign_instance_id"] is None
    assert "frozen_target_kwh" not in payload, payload
    assert "classification_at_start" not in payload, payload
    assert "realised_kwh" not in payload, payload

    # And nothing on the bus contradicts that, for either announcement kind.
    for event in lifecycle_events:
        if event.get("kind") in (LIFECYCLE_KIND_PLANNED, "plan_closed"):
            assert "frozen_target_kwh" not in event, event
            assert "classification_at_start" not in event, event


def test_no_decision_path_reads_the_start_payload_fields() -> None:
    """**Structural.** Publication-only means publication-only."""
    import pathlib

    import custom_components.alpha_ems_manager.coordinator as module

    names = ("classification_at_start", "_campaign_classification_at_start")
    root = pathlib.Path(module.__file__).parent
    for path in root.glob("*.py"):
        if path.name == "coordinator.py":
            continue
        source = path.read_text(encoding="utf-8")
        for name in names:
            assert name not in source, (path.name, name)


async def test_the_start_classification_is_frozen_from_the_live_value(
    hass, setup_integration, source_entities, frank, lifecycle_events
) -> None:
    """**Frozen at first physical execution, and read from the live value there.**

    ``_note_campaign_started`` is the one place that knows execution has actually
    begun -- it is called from the dispatch send path once a write carrying an
    activation succeeded. That is the instant the classification has to be captured,
    because it is the instant the sentence describes.

    *Mutation: capture None instead of the live value and this fails.*
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_id = "c9"
    coordinator._campaign_classifications = {
        "c9": {"classification": "economic_export"}
    }
    coordinator._campaign_started_at = None
    coordinator._campaign_classification_at_start = None
    coordinator.store.campaign_lifecycle = {
        "instance_id": "i9",
        "marks": [LIFECYCLE_KIND_CREATED],
        "campaign_id": "c9",
        "planned_kwh": 4.97,
    }
    lifecycle_events.clear()

    coordinator._note_campaign_started(coordinator.last_refresh_at)
    await hass.async_block_till_done()

    assert coordinator._campaign_classification_at_start == "economic_export"
    assert _started(lifecycle_events)["classification_at_start"] == "economic_export"


async def test_a_closed_campaign_leaves_no_start_classification_behind(
    hass, setup_integration, source_entities, frank
) -> None:
    """**A later campaign must not inherit an earlier campaign's start line.**

    The field is frozen once per instance, so it has to be cleared with the rest of
    the campaign state -- otherwise a campaign that never reached execution could
    publish the previous campaign's category.

    *Mutation: drop the reset and this fails.*
    """
    coordinator = await _run_a_campaign(hass, setup_integration, frank, source_entities)
    coordinator._campaign_id = "c-old"
    coordinator._campaign_started_at = coordinator.last_refresh_at
    coordinator._campaign_classification_at_start = "economic_export"
    coordinator._campaign_frozen_target_kwh = 4.52

    coordinator._close_campaign(coordinator.last_refresh_at, None)

    assert coordinator._campaign_classification_at_start is None
    assert coordinator._campaign_frozen_target_kwh is None
