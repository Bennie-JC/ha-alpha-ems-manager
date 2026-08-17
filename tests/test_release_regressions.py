"""Regressions found during the v1.0.0-beta.1 pre-release audit.

Each test here corresponds to a defect that was reproduced before it was fixed.
They are grouped by the failure they prevent rather than by module, because that
is how they will be read the next time one of them goes red.

The forecast-honesty defects found in the same audit have their own file,
``test_forecast_honesty.py``, because there are enough of them to warrant it.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    MAX_HISTORY_DAYS,
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
    SIGN_GRID_NEGATIVE_IS_IMPORT,
    SIGN_GRID_POSITIVE_IS_IMPORT,
)
from custom_components.alpha_ems_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.alpha_ems_manager.energy_balance import evaluate_balance
from custom_components.alpha_ems_manager.normalization import (
    PowerFlows,
    split_battery_power,
    split_grid_power,
)
from custom_components.alpha_ems_manager.storage import DayRecord, LearningStore

from .conftest import (
    BATTERY_POWER,
    EV_POWER,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    TEST_TIMEZONE,
    set_sensor,
)

TZ_KEY = "Europe/Amsterdam"


# -- corrupt storage must degrade, never disable the integration --------------


def test_an_unresolvable_stored_timezone_falls_back_instead_of_raising() -> None:
    """A zone key that no longer resolves must not disable all four sensors.

    ``DayRecord.tz`` builds a ``ZoneInfo`` on every forecast, so an unresolvable
    key raised ``ZoneInfoNotFoundError`` out of ``build_forecast``, failed every
    coordinator refresh, and left the entity set permanently unavailable with no
    recovery path -- from one bad string in a stored document.
    """
    record = DayRecord.from_dict(
        date(2026, 8, 17),
        {"tz": "Not/AZone", "n": 96, "m": [0.1] * 96},
        TZ_KEY,
    )

    assert record is not None
    assert record.tz_key == TZ_KEY
    # The property is what used to raise.
    assert record.tz == ZoneInfo(TZ_KEY)


def test_a_valid_stored_timezone_is_preserved() -> None:
    """The fallback must not overwrite a zone that is perfectly fine."""
    record = DayRecord.from_dict(
        date(2026, 8, 17),
        {"tz": "America/Santiago", "n": 96, "m": [0.1] * 96},
        TZ_KEY,
    )

    assert record is not None
    assert record.tz_key == "America/Santiago"


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        # `int(raw.get("n") or len(m))` fell through on 0, redefining the day's
        # length as the array length -- so this loaded as a one-interval day at
        # 100 % completeness, counted as learned, and inflated confidence.
        ("zero n", {"tz": TZ_KEY, "n": 0, "m": [0.1]}),
        ("missing n", {"tz": TZ_KEY, "m": [0.1] * 20}),
        ("negative n", {"tz": TZ_KEY, "n": -5, "m": [0.1] * 96}),
        ("non-integer n", {"tz": TZ_KEY, "n": "96", "m": [0.1] * 96}),
        ("float n", {"tz": TZ_KEY, "n": 96.0, "m": [0.1] * 96}),
        # No upper bound meant three lists of half a million floats.
        ("absurd n", {"tz": TZ_KEY, "n": 500_000, "m": [0.1]}),
    ],
)
def test_a_damaged_interval_count_discards_the_day(label: str, raw: dict) -> None:
    """A damaged document must be dropped, never silently reinterpreted.

    Reinterpreting it is worse than losing it: a short day reads as fully covered
    and therefore *raises* the learned-day count and the confidence score on the
    strength of corrupt data.
    """
    assert DayRecord.from_dict(date(2026, 8, 17), raw, TZ_KEY) is None, label


def test_a_plausible_interval_count_is_still_accepted() -> None:
    """The three real day lengths must all load."""
    for count in (92, 96, 100):
        record = DayRecord.from_dict(
            date(2026, 8, 17),
            {"tz": TZ_KEY, "n": count, "m": [0.1] * count},
            TZ_KEY,
        )
        assert record is not None
        assert record.interval_count == count


# -- a clock excursion must not delete a year of history ----------------------


def test_pruning_ignores_a_reference_far_beyond_the_stored_history() -> None:
    """A forward clock jump must not wipe every learned day.

    ``get_or_create`` prunes against the day being created, so a Pi that hands
    Home Assistant a date years ahead before NTP corrects it created a
    future-dated record and pruned the entire retention window against it.
    """
    store = LearningStore.__new__(LearningStore)
    store.days = {}
    start = date(2026, 8, 1)
    for offset in range(30):
        day = start + timedelta(days=offset)
        store.days[day] = DayRecord(day=day, tz_key=TZ_KEY, interval_count=96)

    removed = store.prune(reference=date(2030, 1, 1))

    assert removed == 0
    assert len(store.days) == 30


def test_pruning_still_drops_genuinely_old_days() -> None:
    """The clamp must not disable retention."""
    store = LearningStore.__new__(LearningStore)
    store.days = {}
    newest = date(2026, 8, 1)
    ancient = newest - timedelta(days=MAX_HISTORY_DAYS + 10)
    for day in (ancient, newest):
        store.days[day] = DayRecord(day=day, tz_key=TZ_KEY, interval_count=96)

    removed = store.prune(reference=newest)

    assert removed == 1
    assert list(store.days) == [newest]


# -- sign conventions must fail safe -----------------------------------------


@pytest.mark.parametrize("convention", ["", "typo", None, "negative_is_charge_x"])
def test_an_unrecognised_battery_convention_uses_the_shipped_default(
    convention,
) -> None:
    """An unknown value must not select the *inverse* of the default.

    Written as `if convention == NEGATIVE_IS_CHARGE ... else <inverted>`, any
    unexpected value -- a renamed constant, a hand-edited config entry -- reported
    664 W of charging as 664 W of discharging: silently the exact fault the
    energy-balance check exists to detect.
    """
    charge, discharge = split_battery_power(-664.0, convention)

    assert (charge, discharge) == split_battery_power(
        -664.0, SIGN_BATTERY_NEGATIVE_IS_CHARGE
    )
    assert charge == pytest.approx(664.0)
    assert discharge == pytest.approx(0.0)


@pytest.mark.parametrize("convention", ["", "typo", None])
def test_an_unrecognised_grid_convention_uses_the_shipped_default(convention) -> None:
    """The same guarantee for the grid convention."""
    imported, exported = split_grid_power(500.0, convention)

    assert (imported, exported) == split_grid_power(500.0, SIGN_GRID_POSITIVE_IS_IMPORT)
    assert imported == pytest.approx(500.0)


def test_both_explicit_conventions_still_work() -> None:
    """The fix must not break the non-default choice it now compares against."""
    assert split_battery_power(-664.0, SIGN_BATTERY_POSITIVE_IS_CHARGE) == (0.0, 664.0)
    assert split_grid_power(500.0, SIGN_GRID_NEGATIVE_IS_IMPORT) == (0.0, 500.0)


# -- the balance allowance can never be negative -----------------------------


def test_a_negative_flow_yields_no_verdict_rather_than_a_negative_allowance() -> None:
    """A negative PV or house-load reading must not produce nonsense.

    The allowance scales positively with DC and AC power, so a negative flow made
    it negative -- and a snapshot whose identity closed *exactly* was then logged
    as "residual 0 W against an allowance of -120 W".
    """
    sample = evaluate_balance(
        PowerFlows(
            house_load_w=None,  # what read_flows now yields for a negative reading
            pv_w=0.0,
            battery_charge_w=0.0,
            battery_discharge_w=0.0,
            grid_import_w=0.0,
            grid_export_w=0.0,
        )
    )

    assert sample is None


def test_every_realistic_snapshot_has_a_positive_allowance() -> None:
    """Sanity net over the allowance itself."""
    for pv, charge, discharge, imported in (
        (0.0, 0.0, 0.0, 0.0),
        (5000.0, 3000.0, 0.0, 0.0),
        (0.0, 0.0, 1500.0, 0.0),
        (0.0, 0.0, 0.0, 8000.0),
    ):
        sample = evaluate_balance(
            PowerFlows(
                house_load_w=100.0,
                pv_w=pv,
                battery_charge_w=charge,
                battery_discharge_w=discharge,
                grid_import_w=imported,
                grid_export_w=0.0,
            )
        )
        assert sample is not None
        assert sample.allowed_residual_w > 0
        assert sample.dc_power_w >= 0
        assert sample.ac_power_w >= 0


async def test_a_negative_source_reading_is_treated_as_missing(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """End-to-end: an inverted PV sensor produces no balance verdict."""
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 500, "W", "power")
    set_sensor(hass, PV_POWER, -3000, "W", "power")  # inverted sign
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 500, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    flows = coordinator.read_flows()

    assert flows.pv_w is None
    assert evaluate_balance(flows) is None


# -- warning throttling must survive a flapping source -----------------------


async def test_a_flapping_source_does_not_defeat_the_warning_throttle(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A charger alternating value/unavailable must not warn on every read.

    ``clear()`` used to drop the throttle timestamp on every *good* read, so a
    source that resolved and re-failed between reads warned every single time --
    at whatever rate the fastest source publishes, indefinitely. That is the log
    burial the throttle exists to prevent.
    """
    from unittest.mock import patch

    import custom_components.alpha_ems_manager.coordinator as coordinator_module

    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 500, "W", "power")
    set_sensor(hass, EV_POWER, 0, "W", "power")

    entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        title=mock_config_entry.title,
        data={**mock_config_entry.data, "ev_power_entity": EV_POWER},
        options={},
        version=mock_config_entry.version,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(40):
            # Flap: unusable, then usable, then unusable again.
            set_sensor(hass, EV_POWER, "unavailable", "W", "power")
            coordinator._read_ev_power_w()
            set_sensor(hass, EV_POWER, 1000, "W", "power")
            coordinator._read_ev_power_w()

    ev_warnings = [call for call in logged.call_args_list if "EV charger" in str(call)]
    # One warning for the episode, not one per flap.
    assert len(ev_warnings) <= 2, f"{len(ev_warnings)} warnings from 40 flaps"


# -- diagnostics must never 500 ----------------------------------------------


async def test_diagnostics_survive_an_entry_that_is_not_loaded(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """`runtime_data` is deleted on unload, and diagnostics stays available.

    The state that matters most is the one this release's migration guard
    produces: a legacy entry sits in MIGRATION_ERROR, and downloading diagnostics
    is the first thing such a user is asked for. It used to raise AttributeError
    and return HTTP 500.
    """
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert payload["integration"]["loaded"] is False
    assert "state" in payload["integration"]
    # No exception, and nothing pretends to be real data.
    assert "learning" not in payload


async def test_diagnostics_report_the_flexible_load_consistently(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """`available_now` must agree with `current_power_w`.

    An implausible reading (-3000 W) is rejected by the sanitiser, so the learning
    path counts the interval as invalid. Diagnostics used to report the source as
    available with a null power at the same time.
    """
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
    set_sensor(hass, EV_POWER, -3000, "W", "power")

    entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        title=mock_config_entry.title,
        data={**mock_config_entry.data, "ev_power_entity": EV_POWER},
        options={},
        version=mock_config_entry.version,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)
    flexible = payload["flexible_load"]

    assert flexible["current_power_w"] is None
    assert flexible["available_now"] is False


# -- the options flow must never lock the user out ---------------------------


async def test_the_options_flow_aborts_when_the_price_source_is_gone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank_config_entry
) -> None:
    """Removing Frank must not make every option permanently unsaveable.

    The price source is a required dropdown built from the entries that exist
    now. With none left, the stored id is not a valid choice, and voluptuous
    rejected the form on submit -- so the user could not change *any* setting,
    including repointing an unrelated sensor. The only escape was deleting the
    entry, which loses all learned history.
    """
    await hass.config_entries.async_remove(frank_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "frank_not_configured"


async def test_the_options_flow_still_opens_normally(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The abort must not trigger when the price source is present."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)

    assert result["type"] is FlowResultType.FORM


# -- removing an entry must not orphan its history ---------------------------


async def test_removing_the_entry_deletes_its_learning_history(
    hass: HomeAssistant, setup_integration: MockConfigEntry, hass_storage
) -> None:
    """Storage is keyed per entry, so without this every removal leaks a document.

    Up to a year of quarter-hour history per orphan, unreachable forever.
    """
    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data
    coordinator.store.days[date(2026, 8, 17)] = DayRecord(
        day=date(2026, 8, 17), tz_key=TZ_KEY, interval_count=96
    )
    await coordinator.store.async_save_now()

    store_key = f"alpha_ems_manager.{entry_id}.learning"
    assert store_key in hass_storage

    assert await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()

    assert store_key not in hass_storage
