"""Persisting what reserve was believed necessary, and why it is worth persisting.

Nothing in this release learns from a stored requirement. The record exists
because **the battery configuration a requirement was computed against is
irrecoverable afterwards.** Both forecasts are already persisted, so the
arithmetic is reproducible -- but capacity, the floor, the power limits and the
efficiency live in the config entry, which keeps no history. Raising a minimum
state of charge would otherwise make every earlier belief unverifiable, which is
exactly the hindsight bias the evidence layer exists to prevent.

Two storage decisions here are deliberate and both cost something:

* **scalars only.** The per-interval requirement is a hundred and ninety-two
  floats a refresh and is recomputable from the three fingerprints plus the model
  version, so it is not stored at all.
* **the digest is over the inputs, not over the answer.** The requirement is a
  function of the interval it is asked from, so it differs every quarter-hour
  even when nothing has changed. Digesting the figure stored ninety-six documents
  a day and broke the rule that an unchanged refresh costs no I/O -- which
  ``test_forecast_issuance`` caught, and which is pinned here from the other side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_STORAGE_MINOR_VERSION,
    PV_ABSORPTION_DISPATCH_ACTIVE,
    PV_ABSORPTION_SELF_CONSUMPTION,
    RESERVE_MODEL_VERSION,
    RESERVE_REPLENISHMENT_ASSUMPTION,
)
from custom_components.alpha_ems_manager.history_store import ForecastHistoryStore
from custom_components.alpha_ems_manager.reserve import (
    ReserveSnapshot,
    build_reserve_snapshot,
    fingerprint_battery_config,
)

from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .test_reserve_model import (
    floor_energy,
    reference_limits,
    required,
    scenario_a,
)

ISSUED = datetime(2026, 8, 19, 9, 5, tzinfo=UTC)
HORIZON_START = datetime(2026, 8, 19, 7, 15, tzinfo=UTC)
HORIZON_END = datetime(2026, 8, 20, 22, 0, tzinfo=UTC)

CONFIG = {
    "capacity_kwh": 22.0,
    "min_soc_percent": 20.0,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0,
    "round_trip_efficiency_percent": 90.0,
    "max_soc_percent": 100.0,
}


def snapshot_of(**kwargs) -> ReserveSnapshot:
    """Return the persistable record of the reference-shaped requirement."""
    limits = reference_limits()
    demands = scenario_a()
    projection = required(demands)
    same = None
    blind = None
    if kwargs.pop("with_counterfactuals", True):
        from custom_components.alpha_ems_manager.reserve import (
            build_reserve_pv_blind,
            build_reserve_same_interval_only,
        )

        floor = floor_energy(limits)
        same = build_reserve_same_interval_only(
            limits=limits, floor_energy_kwh=floor, demands=demands
        )
        blind = build_reserve_pv_blind(
            limits=limits, floor_energy_kwh=floor, demands=demands
        )
    defaults = {
        "issued_at": ISSUED,
        "target_day": NORMAL,
        "tz_key": "Europe/Amsterdam",
        "floor_soc_percent": 20.0,
        "config_fingerprint": fingerprint_battery_config(**CONFIG),
        "horizon_start": HORIZON_START,
        "horizon_end": HORIZON_END,
        "same_interval_only": same,
        "pv_blind": blind,
        "load_fingerprint": "0123456789abcdef",
        "pv_fingerprint": "fedcba9876543210",
    }
    defaults.update(kwargs)
    return build_reserve_snapshot(projection, **defaults)


# --- what is stored ---------------------------------------------------------


def test_the_scalars_round_trip_exactly() -> None:
    """Every field, through the compact form and back.

    The compact keys are what a year of these documents is made of, so an
    asymmetry between writing and reading would surface as evidence that quietly
    lost a field rather than as an error.
    """
    original = snapshot_of()

    restored = ReserveSnapshot.from_dict(NORMAL, original.to_dict())

    assert restored == original


def test_no_per_interval_array_is_written() -> None:
    """Scalars only, and the document says so by having no list in it.

    A hundred and ninety-two floats a refresh would be a megabyte a month for
    something recomputable from the three fingerprints beside it.
    """
    payload = snapshot_of().to_dict()

    for key, value in payload.items():
        assert not isinstance(value, (list, tuple, dict)), key


def test_the_three_fingerprints_and_the_model_version_are_all_present() -> None:
    """What makes the requirement recomputable rather than merely remembered.

    The configuration fingerprint is the one of the four that no other store
    holds, and it is the whole reason this family exists.
    """
    payload = snapshot_of().to_dict()

    assert payload["cf"] == fingerprint_battery_config(**CONFIG)
    assert payload["lf"] == "0123456789abcdef"
    assert payload["pf"] == "fedcba9876543210"
    assert payload["mv"] == RESERVE_MODEL_VERSION
    assert payload["ra"] == RESERVE_REPLENISHMENT_ASSUMPTION


def test_the_counterfactuals_are_stored_beside_the_requirement() -> None:
    """So the cost of the replenishment relaxation is recoverable, not argued.

    Without the same-interval figure a later reader could not tell how much of
    the requirement's reduction rested on forecast sunshine, which is the one
    question this phase most wants asked of it.
    """
    snapshot = snapshot_of()

    assert snapshot.required_dc_kwh is not None
    assert snapshot.required_same_interval_only_dc_kwh > snapshot.required_dc_kwh
    assert snapshot.required_pv_blind_dc_kwh >= (
        snapshot.required_same_interval_only_dc_kwh
    )
    assert snapshot.peak_required_dc_kwh >= snapshot.required_dc_kwh


def test_a_changed_battery_configuration_changes_the_fingerprint() -> None:
    """Each of the six fields, one at a time.

    A digest that ignored any of them would let a belief computed for a different
    battery be pooled with this one.
    """
    baseline = fingerprint_battery_config(**CONFIG)

    for field, altered in (
        ("capacity_kwh", 20.0),
        ("min_soc_percent", 25.0),
        ("max_charge_kw", 5.0),
        ("max_discharge_kw", 5.0),
        ("round_trip_efficiency_percent", 92.0),
        ("max_soc_percent", 95.0),
    ):
        changed = fingerprint_battery_config(**{**CONFIG, field: altered})
        assert changed != baseline, field


def test_a_malformed_document_is_refused_rather_than_guessed() -> None:
    """No instant, no record. Reading one would date the belief to nothing."""
    assert ReserveSnapshot.from_dict(NORMAL, None) is None
    assert ReserveSnapshot.from_dict(NORMAL, {"tz": "Europe/Amsterdam"}) is None
    assert ReserveSnapshot.from_dict(NORMAL, {"at": "not-a-date"}) is None
    # A naive instant is refused too: an evidence record has to be locatable in
    # absolute time, and assuming a zone would be inventing one.
    assert ReserveSnapshot.from_dict(NORMAL, {"at": "2026-08-19T09:05:00"}) is None


def test_a_document_without_the_reserve_keys_reads_as_no_evidence() -> None:
    """Which is what every document written before minor 4 looks like.

    Absent rather than zero, and absent rather than an error: the addition is
    backward compatible in both directions, exactly as the price and
    photovoltaic families were.
    """
    restored = ReserveSnapshot.from_dict(NORMAL, {"at": ISSUED.isoformat()})

    assert restored is not None
    assert restored.required_dc_kwh is None
    assert restored.config_fingerprint == ""
    assert restored.model_version == RESERVE_MODEL_VERSION
    assert restored.replenishment_assumption == RESERVE_REPLENISHMENT_ASSUMPTION


# --- the absorption pair is recorded and read by nothing --------------------


@pytest.mark.parametrize(
    ("modelled", "reason"),
    [
        (True, PV_ABSORPTION_SELF_CONSUMPTION),
        (False, PV_ABSORPTION_DISPATCH_ACTIVE),
    ],
)
def test_the_absorption_pair_is_recorded_verbatim(modelled: bool, reason: str) -> None:
    """Both live values, stored as given."""
    snapshot = snapshot_of(pv_absorption_modelled=modelled, pv_absorption_reason=reason)

    assert snapshot.pv_absorption_modelled is modelled
    assert snapshot.pv_absorption_reason == reason
    assert (
        ReserveSnapshot.from_dict(NORMAL, snapshot.to_dict()).pv_absorption_reason
        == reason
    )


def test_two_snapshots_differing_only_in_absorption_carry_one_requirement() -> None:
    """The property the live installation made urgent.

    Absorption flipped from ``self_consumption`` to ``dispatch_active`` inside
    fifteen minutes while both forecasts stood still. Every figure below is
    identical across that change, and the fingerprint is too -- so the flip
    stores no second document and rewrites no belief.
    """
    absorbing = snapshot_of(
        pv_absorption_modelled=True, pv_absorption_reason=PV_ABSORPTION_SELF_CONSUMPTION
    )
    dispatching = snapshot_of(
        pv_absorption_modelled=False, pv_absorption_reason=PV_ABSORPTION_DISPATCH_ACTIVE
    )

    assert absorbing.required_dc_kwh == dispatching.required_dc_kwh
    assert absorbing.peak_required_dc_kwh == dispatching.peak_required_dc_kwh
    assert absorbing.lower_bound_reason == dispatching.lower_bound_reason
    assert absorbing.fingerprint == dispatching.fingerprint


# --- through the store ------------------------------------------------------


async def test_a_requirement_is_stored_once_and_then_recognised(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The second identical requirement is a duplicate, not a second document."""
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])
    snapshot = snapshot_of()

    assert store.add_reserve_snapshot(snapshot) is True
    assert store.add_reserve_snapshot(snapshot) is False
    assert store.has_reserve_fingerprint(NORMAL, snapshot.fingerprint) is True
    assert len(store.reserve_snapshots(NORMAL)) == 1
    assert store.latest_reserve_snapshot(NORMAL) == snapshot


async def test_the_stored_document_reloads_from_disk(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Read back through a fresh store, not from the in-memory view.

    The in-memory copy could hide a serialisation defect entirely, which is why
    this reloads rather than reading what it just wrote.
    """
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])
    snapshot = snapshot_of()
    store.add_reserve_snapshot(snapshot)
    await store.async_save_now()

    reloaded = ForecastHistoryStore(hass, setup_integration.entry_id)
    await reloaded.async_load()
    await reloaded.async_ensure_days([NORMAL])

    assert reloaded.reserve_snapshots(NORMAL) == [snapshot]
    assert reloaded.days[NORMAL].reserve_fingerprints == [snapshot.fingerprint]
    assert reloaded.corrupt is False
    assert reloaded.reset_by_migration is False
    assert FORECAST_STORAGE_MINOR_VERSION == 7


async def test_a_day_with_no_requirement_carries_no_reserve_keys(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Absent rather than present-and-empty.

    Same rule as the photovoltaic arrays on an installation without a forecast,
    so a document says nothing it cannot support -- and an installation without
    battery planning writes nothing here at all.
    """
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])
    partition = await store.async_partition("2026-08")

    payload = partition.to_dict()

    assert payload["days"] == {}
    assert store.reserve_snapshots(NORMAL) == []
    assert store.days.get(NORMAL) is None or (
        store.days[NORMAL].reserve_fingerprints == []
    )


# --- the coordinator writes it, and only when it must -----------------------


async def test_a_refresh_records_the_requirement_it_computed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One drive, one document, and the figures match the published plan."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    recorded = coordinator.history.latest_reserve_snapshot(NORMAL)
    plan = coordinator.battery_plan

    assert recorded is not None
    assert plan is not None and plan.reserve_projection is not None
    assert recorded.required_dc_kwh == pytest.approx(
        round(plan.reserve_projection.required_now_dc_kwh, 2)
    )
    assert recorded.floor_soc_percent == plan.reserve.configured_min_soc_percent
    assert recorded.config_fingerprint != ""
    assert recorded.horizon_start is not None
    assert recorded.horizon_end > recorded.horizon_start


async def test_advancing_the_clock_alone_records_no_second_document(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The invariant an earlier draft of this phase broke.

    The requirement genuinely differs at 10:20 from 10:05 -- the horizon is a
    quarter-hour shorter -- so a digest over the figure would store a document
    every quarter of every day. The digest is over the inputs, so eleven refreshes
    against one forecast leave one record.

    ``test_forecast_issuance`` asserts the same thing from the other side, as
    "ninety-four refreshes a day must be free". This asserts it about the reserve
    specifically, so the two cannot drift apart.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))
    first = coordinator.history.latest_reserve_snapshot(NORMAL)

    for step in range(1, 12):
        await refresh_at(
            coordinator, local(NORMAL, 10, 5) + timedelta(minutes=15 * step)
        )

    assert coordinator.history.reserve_snapshots(NORMAL) == [first]


async def test_a_refresh_that_touches_no_storage_still_touches_none(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The hot path, asserted against the store itself rather than by counting.

    If the reserve ever starts scheduling a write on an unchanged refresh, the
    evidence layer has begun charging rent on every quarter of every day for a
    figure it can already recompute.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    with (
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch("homeassistant.helpers.storage.Store.async_delay_save") as delay_save,
    ):
        await refresh_at(coordinator, local(NORMAL, 10, 20))

    save.assert_not_called()
    delay_save.assert_not_called()


async def test_a_changed_floor_does_record_a_second_document(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The counterpart: silence on the hot path must not mean silence always.

    Raising the minimum state of charge changes the belief while both forecasts
    stand still, and that is precisely the change no other store would have
    captured -- the load and production snapshots are identical either side of it.

    Exercised through the store rather than through an options update, because
    updating options reloads the entry and hands back a different coordinator;
    the property under test is the store's, and testing it here keeps the
    assertion about the store rather than about the reload.
    """
    limits = reference_limits()
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])

    twenty = snapshot_of(
        floor_soc_percent=20.0,
        config_fingerprint=fingerprint_battery_config(**CONFIG),
    )
    raised = build_reserve_snapshot(
        required(scenario_a(), percent=35.0),
        issued_at=ISSUED + timedelta(minutes=15),
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        floor_soc_percent=35.0,
        config_fingerprint=fingerprint_battery_config(
            **{**CONFIG, "min_soc_percent": 35.0}
        ),
        load_fingerprint="0123456789abcdef",
        pv_fingerprint="fedcba9876543210",
    )

    assert store.add_reserve_snapshot(twenty) is True
    assert store.add_reserve_snapshot(raised) is True

    snapshots = store.reserve_snapshots(NORMAL)
    assert len(snapshots) == 2
    assert snapshots[-1].floor_soc_percent == pytest.approx(35.0)
    assert snapshots[-1].required_dc_kwh > snapshots[0].required_dc_kwh
    assert snapshots[-1].required_dc_kwh - snapshots[0].required_dc_kwh == (
        pytest.approx(limits.energy_for_soc(15.0), abs=0.01)
    )
