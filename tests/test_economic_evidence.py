"""Persisting what the optimizer believed, and why it is worth persisting.

Nothing in this release learns from a stored plan. The record exists because **the
economic settings and the actuator capability a plan was computed under are
irrecoverable afterwards.** Prices, load, production and the reserve are already
persisted, so the arithmetic is reproducible -- but a threshold the user changed,
or an opt-in they turned on, lives in the config entry, which keeps no history.
Turning grid charging off would otherwise make every earlier plan unverifiable,
which is exactly the hindsight bias the evidence layer exists to prevent.

Two storage decisions here are deliberate and both cost something:

* **scalars only.** The per-interval plan is a hundred and ninety-two rows a
  refresh and is recomputable from the fingerprints plus the model version, so it
  is not stored at all.
* **the digest is over the inputs, not over the answer.** A plan's horizon starts
  at the next boundary, so it differs every quarter-hour even when nothing has
  changed. Digesting the plan would store ninety-six documents a day and break the
  rule that an unchanged refresh costs no I/O -- the same defect Phase 7 shipped
  once and which is pinned here from the other side before it can happen again.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
    ECONOMIC_BLOCKED_NOT_ENABLED,
    ECONOMIC_MODEL_VERSION,
    ECONOMIC_REASON_NO_ACTION,
    ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
    FORECAST_STORAGE_MINOR_VERSION,
)
from custom_components.alpha_ems_manager.economic import (
    EconomicSnapshot,
    build_economic_snapshot,
    fingerprint_economic,
    fingerprint_settings,
)
from custom_components.alpha_ems_manager.history_store import ForecastHistoryStore
from custom_components.alpha_ems_manager.reserve import fingerprint_battery_config

from .conftest import FakeFrank
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import synthetic_day
from .test_economic_actions import outcome_for
from .test_economic_model import (
    START_KWH,
    eight_interval_horizon,
    reference_table,
)

ISSUED = datetime(2026, 8, 19, 9, 5, tzinfo=UTC)

CONFIG = {
    "capacity_kwh": 22.0,
    "min_soc_percent": 20.0,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0,
    "round_trip_efficiency_percent": 90.0,
    "max_soc_percent": 100.0,
}
SETTINGS = {
    "minimum_trade_gain_eur": 0.10,
    # beta.31: both per-kWh terms are part of the digest and have no defaults, so
    # a caller cannot silently omit the setting its plan actually rested on.
    "grid_charge_margin_eur_per_kwh": 0.0,
    "battery_throughput_cost_eur_per_kwh": 0.0,
    "allow_grid_charging": False,
    "allow_battery_export": False,
    "bucket_kwh": 0.25,
}


def snapshot_of(*, settings: dict | None = None, **overrides) -> EconomicSnapshot:
    """Return the persistable record of the reference-shaped plan."""
    table = reference_table()
    outcome = outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH)
    payload = {
        "issued_at": ISSUED,
        "target_day": NORMAL,
        "tz_key": "Europe/Amsterdam",
        "execution_blocked_reason": ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        "config_fingerprint": fingerprint_battery_config(**CONFIG),
        "settings_fingerprint": fingerprint_settings(**(settings or SETTINGS)),
        "price_fingerprint": "price0",
        "load_fingerprint": "load0",
        "pv_fingerprint": "pv0",
        "reserve_fingerprint": "res0",
    }
    payload.update(overrides)
    return build_economic_snapshot(outcome, **payload)


async def opened_store(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ForecastHistoryStore:
    """Return a loaded store with the target day's partition ready."""
    store = ForecastHistoryStore(hass, entry.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])
    return store


# --- the record itself ------------------------------------------------------


def test_the_scalars_round_trip_exactly() -> None:
    """Every field survives the compact form, unchanged."""
    snapshot = snapshot_of()

    assert EconomicSnapshot.from_dict(NORMAL, snapshot.to_dict()) == snapshot


def test_no_per_interval_array_is_written() -> None:
    """Scalars only. A hundred and ninety-two rows a refresh is not evidence."""
    payload = snapshot_of().to_dict()

    for key, value in payload.items():
        assert not isinstance(value, (list, tuple, dict)), key


def test_the_six_fingerprints_and_the_model_version_are_all_present() -> None:
    """Four inputs, the hardware, the settings -- and the version that combined them.

    Six rather than four, and the last two are the ones that earn the record: a
    plan is only reproducible if you know both the battery it was computed for and
    the thresholds it was computed under.
    """
    snapshot = snapshot_of()

    assert snapshot.price_fingerprint == "price0"
    assert snapshot.load_fingerprint == "load0"
    assert snapshot.pv_fingerprint == "pv0"
    assert snapshot.reserve_fingerprint == "res0"
    assert snapshot.config_fingerprint
    assert snapshot.settings_fingerprint
    assert snapshot.model_version == ECONOMIC_MODEL_VERSION


def test_both_plans_and_the_gap_are_stored_beside_the_action() -> None:
    """The desired value, the capability value, and what the difference cost.

    Storing only the desired figure would make the whole desired-versus-capability
    separation unverifiable after the fact, which is the one thing this phase most
    needs to be able to defend.
    """
    snapshot = snapshot_of()

    assert snapshot.desired_action == ECONOMIC_ACTION_CHARGE
    assert snapshot.desired_value_eur is not None
    assert snapshot.capability_value_eur is not None
    assert snapshot.value_forgone_eur == pytest.approx(
        round(snapshot.desired_value_eur - snapshot.capability_value_eur, 2), abs=0.01
    )


def test_the_terminal_and_reserve_figures_are_stored() -> None:
    """The bound that stopped a sale, and what protecting the reserve cost."""
    snapshot = snapshot_of()

    assert snapshot.terminal_floor_kwh == pytest.approx(START_KWH)
    assert snapshot.terminal_binding is True
    assert snapshot.violation_kwh == pytest.approx(0.0)
    assert snapshot.reserve_protection_cost_eur is not None
    assert snapshot.bucket_kwh == 0.25


def test_the_execution_barrier_is_recorded_with_every_plan() -> None:
    """So a later reader can tell an advisory plan from an executed one.

    This is the field that stops a Phase-9 scoring pass crediting Stage A with an
    outcome it never caused.
    """
    assert snapshot_of().execution_blocked_reason == (
        ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE
    )


def test_an_unavailable_plan_is_recorded_as_unavailable_not_as_hold() -> None:
    """No plan is a different fact from a plan to do nothing."""
    snapshot = build_economic_snapshot(
        None,
        issued_at=ISSUED,
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        config_fingerprint=fingerprint_battery_config(**CONFIG),
        settings_fingerprint=fingerprint_settings(**SETTINGS),
    )

    assert snapshot.available is False
    assert snapshot.unavailable_reason == ECONOMIC_UNAVAILABLE_HORIZON_EMPTY
    assert snapshot.desired_action == ECONOMIC_ACTION_HOLD
    assert snapshot.reason == ECONOMIC_REASON_NO_ACTION
    assert snapshot.desired_value_eur is None
    assert snapshot.capability_value_eur is None


# --- the fingerprint is over the inputs ------------------------------------


def test_the_digest_is_over_the_inputs_and_not_over_the_plan() -> None:
    """Two different plans from the same inputs share one fingerprint.

    The defect this prevents, stated as a property: the plan's horizon starts at
    the next boundary, so the *answer* changes every quarter-hour while the inputs
    stand still. A digest over the answer stores ninety-six documents a day.
    """
    table = reference_table()
    inputs = {
        "price_fingerprint": "p",
        "load_fingerprint": "l",
        "pv_fingerprint": "v",
        "reserve_fingerprint": "r",
        "config_fingerprint": fingerprint_battery_config(**CONFIG),
        "settings_fingerprint": fingerprint_settings(**SETTINGS),
    }
    one = build_economic_snapshot(
        outcome_for(table, eight_interval_horizon(table), start_kwh=START_KWH),
        issued_at=ISSUED,
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        **inputs,
    )
    other = build_economic_snapshot(
        outcome_for(
            table, eight_interval_horizon(table), start_kwh=20.0, terminal_kwh=4.4
        ),
        issued_at=ISSUED,
        target_day=NORMAL,
        tz_key="Europe/Amsterdam",
        execution_blocked_reason=ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE,
        **inputs,
    )

    assert one.desired_value_eur != other.desired_value_eur
    assert one.fingerprint == other.fingerprint
    assert one.fingerprint == fingerprint_economic(**inputs)


@pytest.mark.parametrize(
    "change",
    [
        {"minimum_trade_gain_eur": 0.25},
        {"allow_grid_charging": True},
        {"allow_battery_export": True},
        {"bucket_kwh": 0.5},
    ],
)
def test_a_changed_setting_changes_the_fingerprint(change: dict) -> None:
    """Each of the four economic settings is in the digest, individually.

    Asserted one at a time rather than all together, because a digest that
    happened to ignore one of them would still pass a combined test.
    """
    assert (
        snapshot_of().fingerprint
        != snapshot_of(settings={**SETTINGS, **change}).fingerprint
    )


def test_a_changed_battery_configuration_changes_the_fingerprint() -> None:
    """Raising the floor makes an earlier plan a different plan."""
    changed = fingerprint_battery_config(**{**CONFIG, "min_soc_percent": 30.0})

    assert (
        snapshot_of().fingerprint != snapshot_of(config_fingerprint=changed).fingerprint
    )


def test_a_malformed_document_is_refused_rather_than_guessed() -> None:
    """A missing or unparseable timestamp means no record, never a default one."""
    assert EconomicSnapshot.from_dict(NORMAL, None) is None
    assert EconomicSnapshot.from_dict(NORMAL, {}) is None
    assert EconomicSnapshot.from_dict(NORMAL, {"at": ""}) is None
    assert EconomicSnapshot.from_dict(NORMAL, {"at": "not-a-time"}) is None
    # A naive stamp is refused too: an instant without a zone is not an instant.
    assert EconomicSnapshot.from_dict(NORMAL, {"at": "2026-08-19T09:05:00"}) is None


# --- through the store ------------------------------------------------------


async def test_a_plan_is_stored_once_and_then_recognised(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The second identical plan is a duplicate, not a second document."""
    store = await opened_store(hass, setup_integration)
    snapshot = snapshot_of()

    assert store.add_economic_snapshot(snapshot) is True
    assert store.add_economic_snapshot(snapshot) is False
    assert store.has_economic_fingerprint(NORMAL, snapshot.fingerprint) is True
    assert len(store.economic_snapshots(NORMAL)) == 1
    assert store.latest_economic_snapshot(NORMAL) == snapshot


async def test_the_stored_document_reloads_from_disk(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Read back through a fresh store, not from the in-memory view.

    The in-memory copy could hide a serialisation defect entirely, which is why
    this reloads rather than reading what it just wrote.
    """
    store = await opened_store(hass, setup_integration)
    snapshot = snapshot_of()
    store.add_economic_snapshot(snapshot)
    await store.async_save_now()

    reloaded = await opened_store(hass, setup_integration)

    assert reloaded.economic_snapshots(NORMAL) == [snapshot]
    assert reloaded.days[NORMAL].economic_fingerprints == [snapshot.fingerprint]
    assert reloaded.corrupt is False
    assert reloaded.reset_by_migration is False
    assert FORECAST_STORAGE_MINOR_VERSION == 8


async def test_a_day_with_no_plan_carries_no_economic_keys(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Absent rather than present-and-empty.

    Same rule as the photovoltaic, price and reserve families, so a document says
    nothing it cannot support.
    """
    store = await opened_store(hass, setup_integration)
    partition = await store.async_partition("2026-08")

    payload = partition.to_dict()

    assert payload["days"] == {}
    assert store.economic_snapshots(NORMAL) == []
    assert store.days.get(NORMAL) is None or (
        store.days[NORMAL].economic_fingerprints == []
    )


# --- the coordinator writes it, and only when it must -----------------------


async def test_a_refresh_records_the_plan_it_computed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """One drive, one document, and the figures match the published plan."""
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    recorded = coordinator.history.latest_economic_snapshot(NORMAL)
    outcome = (coordinator.data or {}).get("economic")

    assert recorded is not None
    assert outcome is not None
    assert recorded.available is outcome.available
    assert recorded.desired_action == outcome.action
    assert recorded.capability_action == outcome.capability_action
    assert recorded.reason == outcome.reason
    assert recorded.horizon_intervals == outcome.horizon.intervals
    assert recorded.horizon_limited_by == outcome.horizon.limited_by
    # **The stored barrier is the live one.** Through beta.32 this field was
    # hardcoded to ``execution_unavailable`` on every snapshot -- a release-level
    # claim that no command reaches the battery, untrue since beta.24 -- so a later
    # scoring pass reading these documents could not tell an install that sent
    # nothing from one that was sending. It now records what actually stood in the
    # way, and on this fixture that is the user's own execution switch.
    assert recorded.execution_blocked_reason == coordinator.economic_blocked_reason
    assert recorded.execution_blocked_reason == ECONOMIC_BLOCKED_NOT_ENABLED
    assert recorded.execution_blocked_reason != ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE


async def test_advancing_the_clock_alone_records_no_second_document(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Ninety-four more refreshes a day must be free.

    The rule the whole change-triggered design exists for, and the one a digest
    over the plan would have broken silently: the plan itself moves every
    quarter-hour because its horizon starts at the next boundary.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await refresh_at(coordinator, local(NORMAL, 10, 5))
    before = len(coordinator.history.economic_snapshots(NORMAL))

    for minute in (20, 35, 50):
        await refresh_at(coordinator, local(NORMAL, 10, minute))

    assert before == 1
    assert len(coordinator.history.economic_snapshots(NORMAL)) == 1


async def test_a_changed_setting_does_record_a_second_document(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Turning an opt-in on is a new belief, so it earns a new record.

    The other half of the change-trigger: silence on an unchanged input is only
    correct if a changed one still speaks.
    """
    coordinator = setup_integration.runtime_data
    seed(coordinator, history_before(NORMAL))
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await refresh_at(coordinator, local(NORMAL, 10, 5))

    fields = {
        name: getattr(coordinator.config, name)
        for name in coordinator.config.__dataclass_fields__
    }
    coordinator.config = coordinator.config.__class__(
        **{**fields, "allow_grid_charging": True}
    )
    await refresh_at(coordinator, local(NORMAL, 10, 20))

    snapshots = coordinator.history.economic_snapshots(NORMAL)

    assert len(snapshots) == 2
    assert snapshots[0].settings_fingerprint != snapshots[1].settings_fingerprint


# --- beta.15 documents keep reading ----------------------------------------


def test_a_beta15_economic_document_is_read_not_discarded() -> None:
    """The storage bumps are additive, and this is the proof.

    A document written by beta.15 carries none of the terminal figures and no
    ``dc``. It must read back with every other field intact and the new ones at
    their documented absences -- never be rejected, and never invent a figure it
    does not have.
    """
    payload = snapshot_of().to_dict()
    for added in ("tplc", "tpli", "tfrc", "br", "mrc", "mrd", "dc"):
        assert added in payload, added
        del payload[added]

    restored = EconomicSnapshot.from_dict(NORMAL, payload)

    assert restored is not None
    assert restored.desired_action == ECONOMIC_ACTION_CHARGE
    assert restored.fingerprint == snapshot_of().fingerprint
    assert restored.reserve_protection_cost_eur is not None
    # Absent is absent. A zero cost would be a claim the document cannot support.
    assert restored.terminal_plan_cost_eur is None
    assert restored.terminal_plan_import_kwh is None
    assert restored.terminal_first_run_changed is None
    assert restored.bucket_rule is None
    assert restored.max_representable_charge_kw is None
    assert restored.max_representable_discharge_kw is None
    # A count, though, has an honest zero: no recorded changes.
    assert restored.direction_changes == 0


def test_a_beta16_document_is_read_through_the_rename() -> None:
    """beta.17 renamed two keys. A rename must not lose the figures behind them.

    beta.16 wrote ``tpc``/``tpi``; beta.17 writes ``tplc``/``tpli`` because the
    old names claimed the number was protection *cost* when it is a whole-horizon
    plan difference. The reader accepts both spellings, so the euro figure a
    beta.16 installation recorded yesterday is still readable today -- which is
    the whole point of an additive bump. ``tfrc`` did not exist then and must
    come back absent rather than as a false ``False``.
    """
    current = snapshot_of()
    payload = current.to_dict()
    payload["tpc"] = payload.pop("tplc")
    payload["tpi"] = payload.pop("tpli")
    for absent in ("tfrc", "br", "mrc", "mrd"):
        del payload[absent]

    restored = EconomicSnapshot.from_dict(NORMAL, payload)

    assert restored is not None
    assert restored.terminal_plan_cost_eur == current.terminal_plan_cost_eur
    assert restored.terminal_plan_import_kwh == current.terminal_plan_import_kwh
    assert restored.terminal_first_run_changed is None
    # The lattice provenance is absent rather than back-filled: a beta.16 plan
    # really was solved on the constant bucket, but this document does not say so
    # and inventing it would make old and new figures look continuous when they
    # are not.
    assert restored.bucket_rule is None
    assert restored.max_representable_charge_kw is None
    # The bucket size itself was always stored, and it is what actually explains
    # a one-bucket difference across the upgrade.
    assert restored.bucket_kwh == current.bucket_kwh
    assert restored.fingerprint == current.fingerprint
    assert restored.direction_changes == current.direction_changes


def test_a_recorded_terminal_figure_survives_the_beta18_removal() -> None:
    """beta.18 stopped computing these. It must not stop *reading* them.

    The rename test above compares a freshly built snapshot against itself, and
    since beta.18 writes ``None`` for the terminal figures it can no longer show
    that a recorded number survives -- both sides are absent. This does, by
    reading a document with the figures a beta.16 or beta.17 installation really
    wrote in it.

    That is the whole reason the fields were kept rather than deleted. Dropping
    them would silently rewrite history to look like beta.18 had always been the
    behaviour, and a stored figure is evidence of what an earlier release decided.
    """
    payload = snapshot_of().to_dict()
    # What beta.17 wrote on the live installation: a euro difference, the import
    # it forced, and a first run the bound did reach.
    payload["tplc"] = 3.9142
    payload["tpli"] = 9.4871
    payload["tfrc"] = True

    restored = EconomicSnapshot.from_dict(NORMAL, payload)

    assert restored is not None
    assert restored.terminal_plan_cost_eur == pytest.approx(3.9142)
    assert restored.terminal_plan_import_kwh == pytest.approx(9.4871)
    assert restored.terminal_first_run_changed is True
    # And it still round-trips, so reading an old document and writing it back
    # does not quietly erase the figures.
    assert EconomicSnapshot.from_dict(NORMAL, restored.to_dict()) == restored


def test_a_beta16_spelling_of_a_recorded_figure_also_survives() -> None:
    """The same, through the beta.17 rename, with a number rather than a ``None``.

    beta.16 wrote ``tpc``/``tpi``. Both spellings must still yield the figure.
    """
    payload = snapshot_of().to_dict()
    del payload["tplc"]
    del payload["tpli"]
    payload["tpc"] = 1.7700
    payload["tpi"] = 9.4900

    restored = EconomicSnapshot.from_dict(NORMAL, payload)

    assert restored is not None
    assert restored.terminal_plan_cost_eur == pytest.approx(1.77)
    assert restored.terminal_plan_import_kwh == pytest.approx(9.49)


def test_no_storage_migration_was_performed_for_the_removal() -> None:
    """Nothing left the schema, so nothing needed migrating.

    The honest treatment of an instrumentation field that stops being computed is
    to write ``None`` and keep reading what is there. Bumping the version to strip
    optional fields would rewrite records for no gain and lose the old figures.
    """
    from custom_components.alpha_ems_manager.const import (
        FORECAST_STORAGE_MINOR_VERSION,
    )

    assert FORECAST_STORAGE_MINOR_VERSION == 8
    # The keys are still written, and still absent-not-zero.
    payload = snapshot_of().to_dict()
    for key in ("tplc", "tpli", "tfrc"):
        assert key in payload, key
        assert payload[key] is None, key


def test_the_new_scalars_round_trip_like_the_rest() -> None:
    """Additive fields are still fields, and still have to survive the journey."""
    snapshot = snapshot_of()

    # Absent since beta.18: the comparison these priced no longer happens, and a
    # zero would say the constraint is free rather than gone.
    assert snapshot.terminal_plan_cost_eur is None
    assert snapshot.terminal_plan_import_kwh is None
    assert snapshot.terminal_first_run_changed is None
    # The lattice provenance is still recorded, and still round-trips.
    assert snapshot.bucket_rule is not None
    assert EconomicSnapshot.from_dict(NORMAL, snapshot.to_dict()) == snapshot
