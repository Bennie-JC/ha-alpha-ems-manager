"""beta.43: what the public surface promises, and what it is judged against.

Two corrections, from the same live capture.

**The frozen target could under-state the campaign it belonged to.** A campaign opened
with a single published row, froze a 0.25 kWh target, and went on to run three rows
whose meter objectives summed to 1.50 kWh. It filed ``success`` at ``0.233 of 0.25``
beside 1.206 kWh of recorded export -- a true statement about a promise that had
stopped being the promise. The freeze exists so Stage A changing its mind cannot make
a shortfall retroactively successful; it was never meant to cap a campaign at whatever
fraction of itself happened to be published when it started.

**And nothing published the schedule ahead.** ``execution_targets`` has carried
absolute instants, a stable campaign identity and the executability verdict for
several releases, on no entity at all -- so a dashboard could only get at them by
parsing the diagnostics download.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_BATTERY,
    CAMPAIGN_BOUNDARY_METER,
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    MAX_UPCOMING_CAMPAIGNS_PUBLISHED,
    QUARTER_END_TARGET_REACHED,
    QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE,
    QUARTER_NOT_EXECUTABLE_INTENT,
    SHORTFALL_TARGET_REACHED,
    STOP_SCOPE_CAMPAIGN,
)
from custom_components.alpha_ems_manager.sensor import _upcoming_campaigns

from .beta36_trace import drive_quarter, opens_at
from .test_beta36_lifecycle import (  # noqa: F401
    live_surface,
    start_the_charge_campaign,
)

pytestmark = pytest.mark.usefixtures("control_surface")

START = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)


# ===========================================================================
# 1. the target may grow, and may never shrink
# ===========================================================================


async def _started(hass, config_data, frank, live_surface, monkeypatch):  # noqa: F811
    coordinator = await start_the_charge_campaign(
        hass, config_data, frank, live_surface, monkeypatch
    )
    await drive_quarter(hass, coordinator, live_surface, 0, ticks=6)
    assert coordinator._campaign_started_at is not None, "the witness: it started"
    assert coordinator._campaign_frozen_target_kwh is not None
    return coordinator


async def test_the_target_grows_when_the_campaign_does(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """More rows of the same campaign are published, and the promise follows them.

    *Mutation: drop the ``_grow_campaign_target`` call, or replace the ``max`` with
    an assignment guarded on ``is None``, and this fails.*
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    frozen = coordinator._campaign_frozen_target_kwh
    grown = frozen + 1.25

    monkeypatch.setattr(coordinator, "_campaign_objective_kwh", lambda _cid: grown)
    coordinator._grow_campaign_target()

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(grown)


async def test_the_target_never_shrinks(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """Stage A wanting less does not make a shortfall retroactively successful.

    This is the property the freeze was built for, and growth must not cost it.
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    frozen = coordinator._campaign_frozen_target_kwh
    assert frozen > 0.1, "the witness: there is something to shrink"

    monkeypatch.setattr(
        coordinator, "_campaign_objective_kwh", lambda _cid: frozen * 0.25
    )
    coordinator._grow_campaign_target()

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen)


async def test_an_unpublished_campaign_cannot_move_the_target(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """``None`` means nobody published this campaign, and is not a number.

    ``_campaign_objective_kwh`` goes to some trouble to distinguish "this campaign
    sells nothing" from "nobody published it", and growth must not collapse the two.
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    frozen = coordinator._campaign_frozen_target_kwh

    monkeypatch.setattr(coordinator, "_campaign_objective_kwh", lambda _cid: None)
    coordinator._grow_campaign_target()

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen)


async def test_growth_is_asked_only_of_the_open_started_instance(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """No identity, no growth -- which is what keeps a restart from inheriting one.

    ``_campaign_objective_kwh`` matches ``campaign_id`` on both of its branches, so
    another campaign's energy is not reachable from here at all; these two guards are
    what stop a *closed* or *unstarted* one being grown by a live publication.
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    frozen = coordinator._campaign_frozen_target_kwh
    monkeypatch.setattr(coordinator, "_campaign_objective_kwh", lambda _cid: 99.0)

    coordinator._campaign_instance_id = None
    coordinator._grow_campaign_target()
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen), (
        "an attempt with no instance identity is not grown"
    )

    coordinator._campaign_instance_id = "restored"
    coordinator._campaign_started_at = None
    coordinator._grow_campaign_target()
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(frozen), (
        "and neither is one that has not started: that is the opening read's job"
    )


async def test_the_terminal_is_judged_against_the_grown_target(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """The live shape: judged against the whole promise, not the opening fragment.

    Against the un-grown target this campaign reports success; against the campaign
    it actually became, it is partial -- and partial is the true answer.
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    delivered = coordinator._quarter_battery_kwh
    assert delivered > 0.0, "the witness: the row moved energy"

    frozen = coordinator._campaign_frozen_target_kwh
    grown = frozen + delivered * 6.0
    assert grown > frozen, "the witness: the campaign really did grow"
    monkeypatch.setattr(coordinator, "_campaign_objective_kwh", lambda _cid: grown)
    coordinator._grow_campaign_target()

    await coordinator._async_end_quarter(
        opens_at(0) + timedelta(minutes=7),
        coordinator._pending_snapshot,
        QUARTER_END_TARGET_REACHED,
        SHORTFALL_TARGET_REACHED,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
        scope=STOP_SCOPE_CAMPAIGN,
    )
    terminal = coordinator._closed_campaign or {}
    assert terminal["objective_target_kwh"] == pytest.approx(grown, abs=1e-3)
    assert terminal["outcome"] == "partial", (
        "delivering a sixth of the promise is not a success"
    )


# ===========================================================================
# 2. the open campaign carries its join keys
# ===========================================================================


async def test_the_open_campaign_can_be_joined_to_its_own_events(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """``campaign_id`` is stable across attempts by design, so it cannot be the key."""
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    block = coordinator._open_campaign_block()

    assert block is not None
    assert block["campaign_id"] == coordinator._campaign_id
    assert block["campaign_instance_id"] == coordinator._campaign_instance_id
    assert block["campaign_instance_id"] is not None
    assert "campaign_end" in block


# ===========================================================================
# 3. the upcoming schedule
# ===========================================================================


def _target(
    campaign: str,
    *,
    minute: int,
    intent: str = EXECUTION_INTENT_NET_EXPORT,
    export: float = 0.5,
    refusal: str | None = None,
) -> dict:
    start = START + timedelta(minutes=minute)
    return {
        "campaign_id": campaign,
        "intent": intent,
        "purpose": "export" if intent == EXECUTION_INTENT_NET_EXPORT else "charge",
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(minutes=15)).isoformat(),
        "expected_value_eur": 0.25,
        "quarter_schedule": [
            {
                "grid_export_target_kwh": export,
                "battery_kwh": export,
                "not_executable": refusal,
            }
        ],
    }


def _coordinator(*targets: dict) -> SimpleNamespace:
    return SimpleNamespace(execution_targets=tuple(targets))


def test_the_upcoming_schedule_is_one_entry_per_campaign() -> None:
    """A campaign is one decision, whatever number of rows it runs through."""
    upcoming = _upcoming_campaigns(
        _coordinator(
            _target("aa", minute=0),
            _target("aa", minute=15),
            _target("bb", minute=45),
        )
    )

    assert [entry["campaign_id"] for entry in upcoming] == ["aa", "bb"]
    assert upcoming[0]["objective_kwh"] == pytest.approx(1.0), (
        "the campaign's own objective, summed over its executable rows"
    )
    assert upcoming[0]["objective_boundary"] == CAMPAIGN_BOUNDARY_METER
    assert upcoming[0]["will_execute"] is True
    assert upcoming[0]["skip_reason"] is None
    assert upcoming[0]["starts_at"] == START.isoformat()
    assert upcoming[0]["ends_at"] == (START + timedelta(minutes=30)).isoformat()


def test_the_upcoming_schedule_is_ordered_and_capped() -> None:
    """At most eight, by start. This is an entity attribute, not a diagnostics dump."""
    targets = [
        _target(f"c{index:02d}", minute=15 * (20 - index)) for index in range(20)
    ]
    upcoming = _upcoming_campaigns(_coordinator(*targets))

    assert len(upcoming) == MAX_UPCOMING_CAMPAIGNS_PUBLISHED
    starts = [entry["starts_at"] for entry in upcoming]
    assert starts == sorted(starts)


def test_a_skipped_campaign_says_so_and_why() -> None:
    """**The public half of the controllability floor.**

    A dashboard has to be able to say *"kleine verkoopactie overgeslagen"* without
    reading the diagnostics download, and it has to be able to say which rule
    declined -- the actuator could not express it, or the loop could not hold it.
    """
    upcoming = _upcoming_campaigns(
        _coordinator(
            _target(
                "tiny",
                minute=0,
                export=0.04,
                refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE,
            )
        )
    )

    assert len(upcoming) == 1
    assert upcoming[0]["will_execute"] is False
    assert upcoming[0]["skip_reason"] == QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE
    assert upcoming[0]["objective_kwh"] == pytest.approx(0.0), (
        "nothing will be armed, so nothing is promised"
    )


def test_one_skipped_row_does_not_skip_the_campaign() -> None:
    """A campaign with one refused row and one good one still executes."""
    upcoming = _upcoming_campaigns(
        _coordinator(
            _target(
                "mixed",
                minute=0,
                export=0.04,
                refusal=QUARTER_NOT_EXECUTABLE_BELOW_CONTROLLABLE,
            ),
            _target("mixed", minute=15, export=0.9),
        )
    )

    assert upcoming[0]["will_execute"] is True
    assert upcoming[0]["skip_reason"] is None
    assert upcoming[0]["objective_kwh"] == pytest.approx(0.9)


def test_serve_load_is_not_an_announced_action() -> None:
    """It carries the identity across a gap and commands nothing."""
    upcoming = _upcoming_campaigns(
        _coordinator(
            {
                "campaign_id": "gap",
                "intent": "serve_load",
                "purpose": "discharge",
                "window_start": START.isoformat(),
                "window_end": (START + timedelta(minutes=15)).isoformat(),
                "quarter_schedule": [
                    {
                        "battery_kwh": 0.25,
                        "not_executable": QUARTER_NOT_EXECUTABLE_INTENT,
                    }
                ],
            }
        )
    )

    assert upcoming == []


def test_a_charge_campaign_is_published_at_the_battery_boundary() -> None:
    """The meter for a sale, the battery for a purchase. The boundary is published."""
    upcoming = _upcoming_campaigns(
        _coordinator(
            _target("buy", minute=0, intent=EXECUTION_INTENT_GRID_CHARGE, export=2.5)
        )
    )

    assert upcoming[0]["objective_boundary"] == CAMPAIGN_BOUNDARY_BATTERY
    assert upcoming[0]["objective_kwh"] == pytest.approx(2.5)
    assert upcoming[0]["purpose"] == "charge"


async def test_growth_happens_on_the_refresh_that_republishes_the_campaign(
    hass: HomeAssistant,
    config_data: dict,
    source_entities: None,
    frank,
    live_surface,  # noqa: F811
    monkeypatch,
) -> None:
    """**The wiring, not just the rule.**

    ``_grow_campaign_target`` is correct in isolation and worth nothing unless the
    refresh actually calls it. This drives ``_note_campaign_progress`` -- the one
    site that advances the campaign lifecycle, once per refresh -- so removing the
    call is visible here even though the helper itself still works.

    *Mutation: replace the ``else`` branch with ``pass`` and this fails.*
    """
    coordinator = await _started(hass, config_data, frank, live_surface, monkeypatch)
    frozen = coordinator._campaign_frozen_target_kwh
    grown = frozen + 2.5

    monkeypatch.setattr(coordinator, "_campaign_objective_kwh", lambda _cid: grown)
    coordinator._note_campaign_progress(opens_at(0) + timedelta(minutes=8), None)

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(grown), (
        "the refresh that republished a larger campaign grew its promise"
    )
