"""Energy-balance robustness against asynchronous source updates.

The live system produced warnings like ``supply 9 W vs demand 1197 W`` while its
overall pass rate sat around 95 %. The cause was timing, not configuration: the
house-load template published a fresh reading the moment a load switched on,
while the battery and grid meters still described the previous few seconds.

These tests pin down the two mechanisms that separate that from a real fault --
coherence gating and sustained-failure debounce -- and, just as importantly,
that a genuine misconfiguration is still caught.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    BALANCE_MAX_SOURCE_SKEW_SECONDS,
    BALANCE_SUSTAINED_FAILURES,
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
)
from custom_components.alpha_ems_manager.energy_balance import (
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED_INCOHERENT,
    BalanceMonitor,
    evaluate_balance,
    measure_coherence,
)
from custom_components.alpha_ems_manager.normalization import (
    PowerFlows,
    split_battery_power,
    split_grid_power,
)

NOW = datetime(2026, 8, 17, 20, 0, 0, tzinfo=UTC)


def coherence(*ages_seconds: float, now: datetime = NOW):
    """Return coherence for sources last reported ``ages_seconds`` ago."""
    return measure_coherence(
        [now - timedelta(seconds=age) for age in ages_seconds], now
    )


def flows(
    house: float,
    pv: float = 0.0,
    battery_w: float | None = None,
    grid_w: float | None = None,
    battery_sign: str = SIGN_BATTERY_NEGATIVE_IS_CHARGE,
) -> PowerFlows:
    """Build a normalised snapshot from raw, signed source readings."""
    charge, discharge = split_battery_power(battery_w or 0.0, battery_sign)
    imported, exported = split_grid_power(grid_w or 0.0)
    return PowerFlows(
        house_load_w=house,
        pv_w=pv,
        battery_charge_w=charge,
        battery_discharge_w=discharge,
        grid_import_w=imported,
        grid_export_w=exported,
    )


def sample(fl: PowerFlows, *ages: float):
    """Evaluate a snapshot with the given source report ages."""
    result = evaluate_balance(fl, coherence(*ages) if ages else None)
    assert result is not None
    return result


# -- coherence measurement ---------------------------------------------------


def test_simultaneous_sources_have_no_skew() -> None:
    """Four sources reporting together are perfectly coherent."""
    result = coherence(0.5, 0.5, 0.5, 0.5)

    assert result.skew_seconds == pytest.approx(0.0)
    assert result.source_count == 4
    assert result.coherent


def test_normal_asynchronous_updates_are_still_coherent() -> None:
    """A minute-polled register alongside a second-polled meter is fine.

    This is ordinary operation, not a fault, and must not cost a sample.
    """
    result = coherence(1.0, 2.0, 45.0, 60.0)

    assert result.skew_seconds == pytest.approx(59.0)
    assert result.skew_seconds <= BALANCE_MAX_SOURCE_SKEW_SECONDS
    assert result.coherent


def test_a_source_that_stopped_reporting_is_incoherent() -> None:
    """One source far behind the others fails the skew gate."""
    result = coherence(1.0, 1.0, 1.0, 200.0)

    assert result.skew_seconds > BALANCE_MAX_SOURCE_SKEW_SECONDS
    assert not result.coherent


def test_uniformly_ancient_sources_are_incoherent() -> None:
    """Zero skew is not enough if nothing has reported for ages."""
    age = BALANCE_MAX_SOURCE_AGE_SECONDS + 60
    result = coherence(age, age, age, age)

    assert result.skew_seconds == pytest.approx(0.0)
    assert not result.coherent


def test_no_timestamps_at_all_is_treated_as_coherent() -> None:
    """With nothing to compare, the sample is judged on its numbers alone."""
    result = measure_coherence([], NOW)

    assert result.source_count == 0
    assert result.coherent


# -- 15. asynchronous update sequence ----------------------------------------


def test_a_transient_lag_is_skipped_not_failed() -> None:
    """The exact live failure: fresh house load, stale battery and grid.

    House load reports 1197 W the instant a load starts; the battery and grid
    still describe the previous state. The identity reads 99 % off. With the
    other sources visibly far behind, the sample is skipped rather than blamed
    on the configuration.
    """
    monitor = BalanceMonitor()
    stale = sample(flows(house=1197.0, grid_w=9.0), 0.5, 200.0, 200.0, 0.5)

    assert stale.relative_error > 0.9  # the number really is that bad
    assert stale.outcome == OUTCOME_SKIPPED_INCOHERENT

    monitor.record(stale)
    assert monitor.skipped_incoherent_samples == 1
    assert monitor.failed_samples == 0
    assert monitor.passed_samples == 0
    assert monitor.consecutive_failures == 0
    assert not monitor.should_warn()


def test_the_catch_up_sample_passes_normally() -> None:
    """Once every source has caught up, the same situation balances."""
    monitor = BalanceMonitor()

    # T0..T3: house load leads, the rest lag behind.
    for lag in (200.0, 150.0, 120.0, 100.0):
        monitor.record(sample(flows(house=1197.0, grid_w=9.0), 0.5, lag, lag, 0.5))

    # T4: everything reports the settled state together.
    settled = sample(flows(house=1197.0, grid_w=1200.0), 1.0, 1.0, 1.0, 1.0)
    monitor.record(settled)

    assert settled.outcome == OUTCOME_PASSED
    assert monitor.passed_samples == 1
    assert monitor.failed_samples == 0
    assert monitor.skipped_incoherent_samples == 4
    assert monitor.pass_rate == pytest.approx(1.0)


def test_a_coherent_transient_needs_the_debounce_not_the_gate() -> None:
    """When every source reports promptly, only the debounce protects us.

    A load step can produce one bad sample while all four timestamps look fresh,
    because the lag is inside the sensors rather than in their publishing. The
    gate cannot see that, so the consecutive-failure rule is what keeps it
    quiet -- this is the mechanism that actually fixes the reported warnings.
    """
    monitor = BalanceMonitor()

    transient = sample(flows(house=1197.0, grid_w=9.0), 1.0, 1.0, 1.0, 1.0)
    assert transient.outcome == OUTCOME_FAILED  # the gate does not catch it

    monitor.record(transient)
    assert monitor.consecutive_failures == 1
    assert not monitor.should_warn()  # but no warning is produced

    monitor.record(sample(flows(house=1197.0, grid_w=1200.0), 1.0, 1.0, 1.0, 1.0))
    assert monitor.consecutive_failures == 0


# -- 16. sustained real mismatch ---------------------------------------------


def sustained_bad():
    """Return a coherent, genuinely unbalanced sample: 1200 W from nowhere."""
    return sample(flows(house=1200.0, pv=20.0), 1.0, 1.0, 1.0, 1.0)


def test_each_sustained_sample_fails() -> None:
    """The mismatch is real and every coherent sample says so."""
    bad = sustained_bad()

    assert bad.outcome == OUTCOME_FAILED
    assert bad.relative_error > 0.9


def test_no_warning_before_the_threshold() -> None:
    """Two consecutive failures are not yet enough to bother the user."""
    monitor = BalanceMonitor()

    for _ in range(BALANCE_SUSTAINED_FAILURES - 1):
        monitor.record(sustained_bad())
        assert not monitor.should_warn()

    assert monitor.consecutive_failures == BALANCE_SUSTAINED_FAILURES - 1
    assert not monitor.sustained_failure


def test_the_warning_fires_exactly_at_the_threshold() -> None:
    """The third consecutive coherent failure raises it, once."""
    monitor = BalanceMonitor()

    for _ in range(BALANCE_SUSTAINED_FAILURES - 1):
        monitor.record(sustained_bad())
        assert not monitor.should_warn()

    monitor.record(sustained_bad())
    assert monitor.sustained_failure
    assert monitor.should_warn()


def test_a_persistent_fault_warns_only_once_per_run() -> None:
    """Twenty more failures do not produce twenty more warnings."""
    monitor = BalanceMonitor()
    warnings = 0

    for _ in range(BALANCE_SUSTAINED_FAILURES + 20):
        monitor.record(sustained_bad())
        if monitor.should_warn():
            warnings += 1

    assert warnings == 1
    assert monitor.failed_samples == BALANCE_SUSTAINED_FAILURES + 20


# -- 17. recovery ------------------------------------------------------------


def good():
    """Return a coherent, healthy sample."""
    return sample(flows(house=1599.0, pv=1595.0), 1.0, 1.0, 1.0, 1.0)


def test_an_intermittent_pattern_never_warns() -> None:
    """fail, fail, pass, fail, fail, pass never reaches three in a row."""
    monitor = BalanceMonitor()
    warnings = 0

    for outcome in (False, False, True, False, False, True):
        monitor.record(sustained_bad() if not outcome else good())
        if monitor.should_warn():
            warnings += 1

    assert warnings == 0
    assert monitor.failed_samples == 4
    assert monitor.passed_samples == 2
    assert monitor.consecutive_failures == 0


def test_a_pass_resets_the_failure_run() -> None:
    """Recovery clears the counter and re-arms the warning."""
    monitor = BalanceMonitor()

    for _ in range(BALANCE_SUSTAINED_FAILURES):
        monitor.record(sustained_bad())
    assert monitor.should_warn()

    monitor.record(good())
    assert monitor.consecutive_failures == 0
    assert not monitor.sustained_failure

    # A fresh sustained run is reported again, rather than staying silent.
    for _ in range(BALANCE_SUSTAINED_FAILURES):
        monitor.record(sustained_bad())
    assert monitor.should_warn()


def test_a_skipped_sample_neither_advances_nor_resets_the_run() -> None:
    """An incoherent sample is not evidence either way."""
    monitor = BalanceMonitor()

    monitor.record(sustained_bad())
    monitor.record(sustained_bad())
    monitor.record(sample(flows(house=1200.0), 0.5, 200.0, 200.0, 0.5))

    assert monitor.consecutive_failures == 2
    assert not monitor.should_warn()

    monitor.record(sustained_bad())
    assert monitor.consecutive_failures == 3
    assert monitor.should_warn()


# -- 18. realistic live balances ---------------------------------------------


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w"),
    [
        # Evening: battery carrying the house, a trickle of export.
        ("evening discharge", 1524.0, 201.0, 1316.0, -4.0),
        # Daytime: solar covering the house and charging the battery.
        ("daytime charge", 847.0, 1138.0, -300.0, -6.0),
        # The maintainer's own logged healthy sample.
        ("logged healthy sample", 1599.0, 1595.0, 0.0, 0.0),
    ],
)
def test_realistic_balances_pass(
    label: str, house: float, pv: float, battery_w: float, grid_w: float
) -> None:
    """Real measurements with normal noise are comfortably within tolerance."""
    result = sample(
        flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w),
        1.0,
        2.0,
        3.0,
        4.0,
    )

    assert result.outcome == OUTCOME_PASSED, (
        f"{label}: supply {result.supply_w:.0f} W vs demand "
        f"{result.demand_w:.0f} W, {result.relative_error * 100:.1f}% off"
    )
    assert result.relative_error < 0.05


def test_a_realistic_balance_survives_small_timing_noise() -> None:
    """A few tens of watts of disagreement is normal and must pass."""
    result = sample(
        flows(house=1524.0, pv=201.0, battery_w=1316.0, grid_w=-4.0),
        1.0,
        20.0,
        40.0,
        60.0,
    )

    assert result.outcome == OUTCOME_PASSED


# -- 19. low load ------------------------------------------------------------


@pytest.mark.parametrize(
    ("supply_w", "demand_w"),
    [(8.0, 12.0), (12.0, 8.0), (0.0, 30.0), (25.0, 0.0), (100.0, 130.0)],
)
def test_low_power_states_are_not_catastrophic(
    supply_w: float, demand_w: float
) -> None:
    """A handful of watts apart at 3 a.m. is noise, not a 50 % fault.

    The absolute floor in the denominator is what makes this work: below it the
    test is effectively an absolute one of about 37 W.
    """
    result = sample(flows(house=demand_w, pv=supply_w), 1.0, 1.0, 1.0, 1.0)

    assert result.outcome == OUTCOME_PASSED
    assert result.relative_error < 0.2


def test_a_real_fault_at_low_power_is_still_caught() -> None:
    """The floor forgives watts, not hundreds of watts."""
    result = sample(flows(house=900.0, pv=5.0), 1.0, 1.0, 1.0, 1.0)

    assert result.outcome == OUTCOME_FAILED


# -- 20. genuine sign mistakes -------------------------------------------------


def test_an_inverted_battery_sign_still_produces_a_sustained_fault() -> None:
    """The robustness work must not hide a real misconfiguration.

    A battery discharging at 1300 W read under the wrong convention becomes
    1300 W of charging: 2.6 kW of error, on every single coherent sample.
    """
    monitor = BalanceMonitor()
    warnings = 0

    for _ in range(BALANCE_SUSTAINED_FAILURES):
        wrong = sample(
            flows(
                house=1524.0,
                pv=201.0,
                battery_w=1316.0,
                grid_w=-4.0,
                battery_sign=SIGN_BATTERY_POSITIVE_IS_CHARGE,
            ),
            1.0,
            1.0,
            1.0,
            1.0,
        )
        assert wrong.outcome == OUTCOME_FAILED
        monitor.record(wrong)
        if monitor.should_warn():
            warnings += 1

    assert warnings == 1
    assert monitor.pass_rate == pytest.approx(0.0)


def test_an_inverted_grid_sign_still_produces_a_sustained_fault() -> None:
    """The same holds for the grid convention."""
    monitor = BalanceMonitor()

    for _ in range(BALANCE_SUSTAINED_FAILURES):
        imported, exported = split_grid_power(1200.0, "negative_is_import")
        wrong = evaluate_balance(
            PowerFlows(
                house_load_w=1200.0,
                pv_w=0.0,
                battery_charge_w=0.0,
                battery_discharge_w=0.0,
                grid_import_w=imported,
                grid_export_w=exported,
            ),
            coherence(1.0, 1.0, 1.0, 1.0),
        )
        assert wrong is not None
        monitor.record(wrong)

    assert monitor.sustained_failure


def test_a_missing_pv_source_produces_a_sustained_fault() -> None:
    """Forgetting to select PV shows up as supply that never arrives."""
    monitor = BalanceMonitor()

    for _ in range(BALANCE_SUSTAINED_FAILURES):
        monitor.record(sample(flows(house=3000.0, pv=0.0), 1.0, 1.0, 1.0, 1.0))

    assert monitor.sustained_failure


# -- 21. pass-rate semantics --------------------------------------------------


def test_the_pass_rate_denominator_excludes_skipped_samples() -> None:
    """pass, pass, skip, fail -> 2/3, not 2/4."""
    monitor = BalanceMonitor()

    monitor.record(good())
    monitor.record(good())
    monitor.record(sample(flows(house=1200.0), 0.5, 200.0, 200.0, 0.5))
    monitor.record(sustained_bad())

    assert monitor.eligible_samples == 3
    assert monitor.passed_samples == 2
    assert monitor.failed_samples == 1
    assert monitor.skipped_incoherent_samples == 1
    assert monitor.pass_rate == pytest.approx(2 / 3)


def test_the_pass_rate_is_undefined_before_any_eligible_sample() -> None:
    """Skipped samples alone do not create a score."""
    monitor = BalanceMonitor()
    monitor.record(sample(flows(house=1200.0), 0.5, 200.0, 200.0, 0.5))

    assert monitor.eligible_samples == 0
    assert monitor.pass_rate is None


def test_unavailable_samples_are_tracked_separately() -> None:
    """A partial snapshot is neither eligible nor incoherent."""
    monitor = BalanceMonitor()
    monitor.record_unavailable()

    assert monitor.unavailable_samples == 1
    assert monitor.eligible_samples == 0
    assert monitor.skipped_incoherent_samples == 0
    assert monitor.pass_rate is None


def test_the_diagnostics_payload_reports_every_counter() -> None:
    """Everything needed to interpret tomorrow's live run is exposed."""
    monitor = BalanceMonitor()
    monitor.record(good())
    monitor.record(sustained_bad())
    monitor.record(sample(flows(house=1200.0), 0.5, 200.0, 200.0, 0.5))

    payload = monitor.as_dict()

    assert set(payload) >= {
        "eligible_samples",
        "passed_samples",
        "failed_samples",
        "skipped_incoherent_samples",
        "unavailable_samples",
        "pass_rate",
        "consecutive_failures",
        "max_allowed_skew_seconds",
        "last_sample",
        "last_coherent_sample",
        "last_warning",
    }
    assert payload["eligible_samples"] == 2
    assert payload["skipped_incoherent_samples"] == 1
    # The last sample was skipped, so the last *coherent* one is the failure.
    assert payload["last_sample"]["outcome"] == OUTCOME_SKIPPED_INCOHERENT
    assert payload["last_coherent_sample"]["outcome"] == OUTCOME_FAILED


# -- 22. reload and lifecycle -------------------------------------------------


async def test_a_reload_starts_the_failure_run_clean(
    hass, freezer, mock_config_entry
) -> None:
    """Reloading must not inherit a failure run that may already be over.

    The monitor is session state, deliberately not persisted: warning about a
    condition that was fixed before the restart would be worse than silence.
    """
    from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Force a failure run, as a genuine misconfiguration would.
    coordinator = mock_config_entry.runtime_data
    for _ in range(BALANCE_SUSTAINED_FAILURES):
        coordinator.balance.record(sustained_bad())
    assert coordinator.balance.sustained_failure

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    reloaded = mock_config_entry.runtime_data
    assert reloaded.balance.consecutive_failures == 0
    assert not reloaded.balance.sustained_failure
    assert reloaded.balance.eligible_samples == 0
    assert reloaded.balance.last_warning is None


async def test_startup_with_stale_sources_raises_no_warning(
    hass, freezer, mock_config_entry
) -> None:
    """Sources whose last report predates setup must not warn immediately."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    import custom_components.alpha_ems_manager.coordinator as coordinator_module

    from .conftest import (
        BATTERY_POWER,
        GRID_POWER,
        HOUSE_LOAD,
        PV_POWER,
        TEST_TIMEZONE,
        set_sensor,
    )
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    # Publish every source well before setup, then jump forward so all of them
    # look ancient the moment the integration starts.
    freezer.move_to(START - timedelta(minutes=30))
    set_sensor(hass, HOUSE_LOAD, 1197, "W", "power")
    set_sensor(hass, PV_POWER, 0, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 9, "W", "power")
    freezer.move_to(START)

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(6):
            freezer.tick(timedelta(seconds=60))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    balance_warnings = [
        call for call in logged.call_args_list if "energy-balance" in str(call)
    ]
    assert balance_warnings == []

    coordinator = mock_config_entry.runtime_data
    # The samples were recognised as stale rather than counted against us.
    assert coordinator.balance.skipped_incoherent_samples > 0
    assert coordinator.balance.failed_samples == 0


async def test_an_incoherent_sample_never_rejects_a_learning_interval(
    hass, freezer, mock_config_entry
) -> None:
    """Balance is a quality signal; it must not gate house-load learning.

    The balance sources are deliberately left stale and wildly unbalanced while
    the house-load sensor keeps reporting. The quarter must still be learned.
    """
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .conftest import (
        BATTERY_POWER,
        GRID_POWER,
        HOUSE_LOAD,
        PV_POWER,
        TEST_TIMEZONE,
        set_sensor,
    )
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START - timedelta(minutes=30))
    set_sensor(hass, PV_POWER, 0, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 9, "W", "power")
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(16):
        freezer.tick(timedelta(seconds=60))
        set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    record = coordinator.store.days[START.date()]

    # The 10:00 interval is measured and stored exactly as if balance were
    # perfect, despite every balance sample being unusable.
    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)
    assert record.measured_valid_count == 1
    assert coordinator.balance.skipped_incoherent_samples > 0


async def test_the_warning_wording_is_about_sustained_mismatch(
    hass, freezer, mock_config_entry
) -> None:
    """A real sustained fault warns, and says what to check without over-claiming."""
    from unittest.mock import patch

    import custom_components.alpha_ems_manager.coordinator as coordinator_module

    from .conftest import (
        BATTERY_POWER,
        GRID_POWER,
        HOUSE_LOAD,
        PV_POWER,
        TEST_TIMEZONE,
        set_sensor,
    )
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    # A physically impossible steady state: 1.2 kW consumed, nothing supplying.
    for entity, value in (
        (HOUSE_LOAD, 1200),
        (PV_POWER, 0),
        (BATTERY_POWER, 0),
        (GRID_POWER, 0),
    ):
        set_sensor(hass, entity, value, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(BALANCE_SUSTAINED_FAILURES + 5):
            # Republish so the sources stay coherent while staying wrong.
            for entity, value in (
                (HOUSE_LOAD, 1200),
                (PV_POWER, 0),
                (BATTERY_POWER, 0),
                (GRID_POWER, 0),
            ):
                set_sensor(hass, entity, value, "W", "power")
            coordinator._sample_balance()

    messages = [call.args[0] % call.args[1:] for call in logged.call_args_list]
    balance = [text for text in messages if "energy-balance" in text]

    assert len(balance) == 1, "a sustained fault should warn exactly once"
    text = balance[0]
    assert "Sustained energy-balance mismatch" in text
    assert "selected source entities" in text
    assert "sign conventions" in text
    assert "updates far more slowly" in text
    assert "1200" in text  # the demand figure is included
    assert coordinator.balance.last_warning is not None
