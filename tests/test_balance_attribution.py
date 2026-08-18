"""Attributing energy-balance failures, and reporting them honestly.

A live installation reported 240 passed, 25 failed and 369 skipped samples with
a worst run of six consecutive coherent failures. The pass rate alone cannot say
which of three very different explanations that is:

* a residual confined to *converting* modes, which points at the inverter's
  DC/AC boundary;
* a residual confined to *low-power* modes, which points at a roughly constant
  offset between two instruments and is invisible at high power because the
  allowance grows;
* a residual spread evenly across every mode, which points at a genuine
  configuration error.

The counters in these tests exist so that question is answered from recorded
data rather than from argument. Nothing here widens a tolerance.

The second half pins the *reporting* defects found alongside it: a warning
timestamp that referred to a log line the throttle had discarded, and a
reassuring wording that silently rate-limited the escalated one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import custom_components.alpha_ems_manager.coordinator as coordinator_module
from custom_components.alpha_ems_manager.const import (
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    BALANCE_MAX_SOURCE_SKEW_SECONDS,
    BALANCE_SUSTAINED_FAILURES,
    LOG_THROTTLE_SECONDS,
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
)
from custom_components.alpha_ems_manager.energy_balance import (
    SKIP_SOURCE_SKEW,
    SKIP_STALE_SOURCE,
    BalanceMonitor,
    evaluate_balance,
    measure_coherence,
)
from custom_components.alpha_ems_manager.normalization import (
    PowerFlows,
    split_battery_power,
    split_grid_power,
)

from .conftest import (
    BATTERY_POWER,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    TEST_TIMEZONE,
    set_sensor,
)

NOW = datetime(2026, 8, 18, 5, 20, 0, tzinfo=UTC)


def coherence(*ages_seconds: float, entities: list[str] | None = None):
    """Return coherence for sources last reported ``ages_seconds`` ago."""
    return measure_coherence(
        [NOW - timedelta(seconds=age) for age in ages_seconds], NOW, entities
    )


def flows(
    house: float,
    pv: float = 0.0,
    battery_w: float | None = None,
    grid_w: float | None = None,
) -> PowerFlows:
    """Build a normalised snapshot from raw, signed source readings."""
    charge, discharge = split_battery_power(
        battery_w or 0.0, SIGN_BATTERY_NEGATIVE_IS_CHARGE
    )
    imported, exported = split_grid_power(grid_w or 0.0)
    return PowerFlows(
        house_load_w=house,
        pv_w=pv,
        battery_charge_w=charge,
        battery_discharge_w=discharge,
        grid_import_w=imported,
        grid_export_w=exported,
    )


def feed(
    monitor: BalanceMonitor,
    snapshot: PowerFlows,
    times: int = 1,
    ages: tuple[float, ...] = (),
    entities: list[str] | None = None,
):
    """Record ``times`` identical samples and return the last one."""
    sample = None
    for _ in range(times):
        timing = coherence(*ages, entities=entities) if ages else None
        sample = evaluate_balance(snapshot, timing)
        assert sample is not None
        monitor.record(sample)
    return sample


# -- the ten live operating modes --------------------------------------------
#
# Each case is the identity as the real system would report it, with a residual
# of the size that boundary genuinely produces. These are the modes the live
# failures must be attributed against, so their verdicts are pinned first.

REALISTIC_MODES = [
    # label, flows, expected verdict
    ("grid->house", flows(house=300, grid_w=305), True),
    ("grid->house+battery", flows(house=600, battery_w=-2000, grid_w=2680), True),
    ("pv->house", flows(house=1900, pv=2000), True),
    ("pv->house+battery", flows(house=1400, pv=3000, battery_w=-1500), True),
    # The live snapshot: PV and grid together charging a nearly empty battery.
    (
        "pv+grid->house+battery",
        flows(house=3134, pv=688, battery_w=-5122, grid_w=7654),
        True,
    ),
    ("battery->house", flows(house=760, battery_w=800), True),
    ("battery->house+grid", flows(house=500, battery_w=2000, grid_w=-1450), True),
    ("pv export", flows(house=900, pv=5000, grid_w=-4000), True),
    ("battery export", flows(house=200, battery_w=2500, grid_w=-2250), True),
    ("near-zero crossing", flows(house=12, grid_w=14), True),
]


@pytest.mark.parametrize(
    ("label", "snapshot", "expected"),
    REALISTIC_MODES,
    ids=[case[0] for case in REALISTIC_MODES],
)
def test_every_live_operating_mode_tolerates_its_own_residual(
    label: str, snapshot: PowerFlows, expected: bool
) -> None:
    """A correctly configured system passes in all ten modes it actually enters.

    If any of these failed, the 25 live failures would be the tolerance model's
    fault rather than the installation's, and the fix would be a tolerance
    change. They pass, so it is not.
    """
    sample = evaluate_balance(snapshot, None)

    assert sample is not None
    assert sample.within_tolerance is expected, (
        f"{label}: residual {sample.residual_w:.0f} W against an allowance of "
        f"{sample.allowed_residual_w:.0f} W"
    )


def test_a_constant_offset_fails_only_at_low_power() -> None:
    """The signature the live counters actually have, pinned as a measurement.

    A fixed ~150 W disagreement between the P1 meter and the inverter's own
    house-load figure is a boundary effect, not a sign error: it does not scale
    with any term. The allowance, however, does -- so the identical offset fails
    while the house is quiet and passes once enough power is flowing. That is what
    produces a high pass rate punctuated by short runs of failures at night, and
    it is why widening the tolerance to absorb it would blind the check at every
    power level instead of explaining this one.

    Two separate escapes exist, and the numbers matter because they decide how
    many failures a day produces. With nothing converting, the metering term only
    overtakes a 150 W offset at about 3.5 kW of load. Add conversion and the DC
    term closes the gap far sooner, which is why the same installation passes all
    afternoon with PV and the battery active.
    """
    offset_w = 150.0
    verdicts = {}
    for load in (300, 600, 1200, 3000, 4000, 8000):
        sample = evaluate_balance(flows(house=load, grid_w=load + offset_w), None)
        assert sample is not None
        verdicts[load] = sample.within_tolerance

    # Purely AC-side: the offset dominates the allowance well past 3 kW.
    assert verdicts[300] is False
    assert verdicts[600] is False
    assert verdicts[1200] is False
    assert verdicts[3000] is False
    # The crossover sits between 3 and 4 kW (40 + 0.03 * (L + 150) == 150).
    assert verdicts[4000] is True
    assert verdicts[8000] is True

    # The second escape: the same 150 W offset at a modest load passes as soon as
    # the battery is converting, because the DC term is added on top.
    converting = evaluate_balance(
        flows(house=1200, pv=1500, battery_w=-1000, grid_w=850), None
    )
    assert converting is not None
    assert converting.residual_w == pytest.approx(150.0)
    assert converting.within_tolerance is True

    # And the offset never reads as a gross fault, so the wording stays correct:
    # this is a measurement boundary, not a misconfiguration.
    quiet = evaluate_balance(flows(house=300, grid_w=450), None)
    assert quiet is not None
    assert quiet.gross_fault_suspected is False


# -- per-mode attribution ----------------------------------------------------


def test_passes_and_failures_are_counted_per_operating_mode() -> None:
    """The counters separate a converting residual from a low-power one."""
    monitor = BalanceMonitor()

    # Healthy while converting.
    feed(monitor, flows(house=1400, pv=3000, battery_w=-1500), times=4)
    # Failing on plain grid import at low power.
    feed(monitor, flows(house=300, grid_w=470), times=3)

    assert monitor.passed_by_mode == {"pv->house+battery": 4}
    assert monitor.failed_by_mode == {"grid->house": 3}
    assert monitor.eligible_samples == 7
    assert monitor.passed_samples == 4
    assert monitor.failed_samples == 3


def test_a_mode_can_both_pass_and_fail_without_the_counts_merging() -> None:
    """Attribution stays per-mode even when one mode does both."""
    monitor = BalanceMonitor()

    feed(monitor, flows(house=300, grid_w=305), times=5)
    feed(monitor, flows(house=300, grid_w=470), times=2)

    assert monitor.passed_by_mode["grid->house"] == 5
    assert monitor.failed_by_mode["grid->house"] == 2


def test_skipped_samples_are_not_attributed_to_a_mode() -> None:
    """An incoherent sample says nothing about the mode it was taken in."""
    monitor = BalanceMonitor()

    feed(monitor, flows(house=300, grid_w=470), times=3, ages=())
    monitor_skipped = BalanceMonitor()
    feed(
        monitor_skipped,
        flows(house=300, grid_w=470),
        times=3,
        # Two sources 200 s apart: well past the skew limit.
        ages=(0.0, 200.0),
    )

    assert monitor_skipped.failed_by_mode == {}
    assert monitor_skipped.passed_by_mode == {}
    assert monitor_skipped.skipped_incoherent_samples == 3
    assert monitor_skipped.eligible_samples == 0


# -- skip attribution --------------------------------------------------------


def test_a_skew_skip_and_a_stale_skip_are_told_apart() -> None:
    """Which of the two gates fired is the difference between two diagnoses.

    Skew means the sources describe different instants -- normal for Modbus
    registers on separate poll intervals. A stale source means one of them has
    stopped publishing, which is a fault. Reporting a single combined total left
    a 58 % skip rate unattributable.
    """
    skewed = coherence(0.0, BALANCE_MAX_SOURCE_SKEW_SECONDS + 10)
    stale = coherence(
        BALANCE_MAX_SOURCE_AGE_SECONDS + 10, BALANCE_MAX_SOURCE_AGE_SECONDS + 12
    )

    assert skewed.coherent is False
    assert skewed.skip_reason == SKIP_SOURCE_SKEW
    assert stale.coherent is False
    assert stale.skip_reason == SKIP_STALE_SOURCE
    # A usable sample has no skip reason at all.
    assert coherence(1.0, 3.0).skip_reason is None


def test_the_monitor_counts_the_two_skip_causes_separately() -> None:
    """Both totals reach the tally, and they sum to the combined count."""
    monitor = BalanceMonitor()
    snapshot = flows(house=300, grid_w=305)

    feed(monitor, snapshot, times=4, ages=(0.0, 200.0))
    feed(monitor, snapshot, times=2, ages=(400.0, 402.0))

    assert monitor.skipped_due_to_skew == 4
    assert monitor.skipped_due_to_stale_source == 2
    assert monitor.skipped_incoherent_samples == 6


def test_the_least_recently_reported_source_is_named() -> None:
    """A high skip rate must name the laggard, not just report a spread.

    Without this the user is told the sources disagree about when they are
    describing, and left to guess which of four it is.
    """
    monitor = BalanceMonitor()
    snapshot = flows(house=300, grid_w=305)

    feed(
        monitor,
        snapshot,
        times=3,
        ages=(1.0, 2.0, 200.0),
        entities=[HOUSE_LOAD, GRID_POWER, PV_POWER],
    )

    assert monitor.stale_source_counts == {PV_POWER: 3}
    assert monitor.last_sample is not None
    assert monitor.last_sample.coherence is not None
    assert monitor.last_sample.coherence.oldest_entity_id == PV_POWER


def test_naming_the_laggard_is_optional() -> None:
    """The timing arithmetic stays testable without a state machine."""
    without = coherence(1.0, 2.0)

    assert without.oldest_entity_id is None
    assert without.coherent is True
    # A mismatched label list is ignored rather than mispairing the names.
    mismatched = measure_coherence([NOW, NOW - timedelta(seconds=5)], NOW, ["only_one"])
    assert mismatched.oldest_entity_id is None


# -- worst-case retention ----------------------------------------------------


def test_the_worst_sample_is_the_largest_overshoot_not_the_largest_residual() -> None:
    """A residual is only meaningful against the allowance it broke.

    300 W at 10 kW is healthy; 300 W at 300 W is a fault. Retaining the biggest
    residual would keep the harmless high-power sample and discard the diagnostic
    one, which is the wrong way round.
    """
    monitor = BalanceMonitor()

    # Large residual, large allowance: only just over.
    feed(monitor, flows(house=8000, pv=2000, battery_w=-1000, grid_w=7600), times=1)
    big = monitor.last_failed_sample
    assert big is not None

    # Small residual, tiny allowance: far further past it.
    feed(monitor, flows(house=300, grid_w=700), times=1)

    assert monitor.worst_excess_sample is not None
    assert monitor.worst_excess_sample.mode == "grid->house"
    assert monitor.worst_excess_sample.excess_w > big.excess_w
    # The plain maxima are reported too, and they are the other quantity.
    assert monitor.worst_residual_w == pytest.approx(600.0)


def test_the_last_failure_is_retained_even_when_it_never_warned() -> None:
    """One failure below the debounce still has to be inspectable."""
    monitor = BalanceMonitor()

    feed(monitor, flows(house=300, grid_w=470), times=1)

    assert monitor.sustained_failure is False
    assert monitor.last_warning is None
    assert monitor.last_failed_sample is not None
    assert monitor.last_failed_sample.mode == "grid->house"
    assert monitor.last_failed_sample.residual_w == pytest.approx(170.0)


def test_the_worst_skew_is_tracked_across_skipped_and_eligible_samples() -> None:
    """Peak skew is the evidence for or against the 90 s gate itself."""
    monitor = BalanceMonitor()
    snapshot = flows(house=300, grid_w=305)

    feed(monitor, snapshot, times=1, ages=(0.0, 30.0))
    feed(monitor, snapshot, times=1, ages=(0.0, 240.0))

    assert monitor.worst_skew_seconds == pytest.approx(240.0)


def test_the_attribution_counters_start_empty() -> None:
    """A fresh session claims nothing, and the mappings are independent."""
    monitor = BalanceMonitor()

    assert monitor.passed_by_mode == {}
    assert monitor.failed_by_mode == {}
    assert monitor.stale_source_counts == {}
    assert monitor.worst_excess_sample is None
    assert monitor.last_failed_sample is None
    assert monitor.worst_residual_w == 0.0
    assert monitor.worst_skew_seconds == 0.0
    assert BalanceMonitor().passed_by_mode is not monitor.passed_by_mode


def test_the_mode_key_space_cannot_grow_with_runtime() -> None:
    """Mode labels come from a bounded set, so the mappings stay small.

    Diagnostics must not accumulate an unbounded mapping over a long session.
    """
    monitor = BalanceMonitor()

    for load in range(100, 4000, 37):
        feed(monitor, flows(house=load, pv=load / 3, grid_w=load), times=1)

    labels = set(monitor.passed_by_mode) | set(monitor.failed_by_mode)
    assert len(labels) <= 16, labels


# -- warning integrity -------------------------------------------------------


def test_the_two_wordings_do_not_rate_limit_each_other() -> None:
    """A moderate warning must not silence a gross fault for an hour.

    Both messages used one throttle key. A moderate residual warned, a passing
    sample re-armed the debounce, and the genuine fault that followed inside the
    throttle window was discarded -- permanently, because only a passing coherent
    sample re-arms the one-shot flag and a real fault never produces one. The
    user was left reading "learning is unaffected" for a broken configuration.
    """
    log = coordinator_module._ThrottledLogger()
    emitted = []

    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=NOW):
            emitted.append(
                log.warning(
                    coordinator_module._BALANCE_LOG_MODERATE, "moderate residual"
                )
            )
        # Twenty minutes later, well inside the throttle window.
        later = NOW + timedelta(seconds=LOG_THROTTLE_SECONDS // 3)
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=later):
            emitted.append(
                log.warning(coordinator_module._BALANCE_LOG_GROSS, "gross fault")
            )

    assert emitted == [True, True]
    messages = [call.args[0] for call in logged.call_args_list]
    assert "gross fault" in messages

    # The same key still throttles itself, which is the behaviour that exists.
    with (
        patch.object(coordinator_module._LOGGER, "warning"),
        patch.object(
            coordinator_module.dt_util,
            "utcnow",
            return_value=NOW + timedelta(seconds=60),
        ),
    ):
        repeat = log.warning(
            coordinator_module._BALANCE_LOG_MODERATE, "moderate residual"
        )
    assert repeat is False


def test_the_throttled_logger_reports_whether_it_emitted() -> None:
    """The return value is what keeps the timestamp honest."""
    log = coordinator_module._ThrottledLogger()

    with patch.object(coordinator_module._LOGGER, "warning"):
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=NOW):
            first = log.warning("k", "message")
            second = log.warning("k", "message")
        with patch.object(
            coordinator_module.dt_util,
            "utcnow",
            return_value=NOW + timedelta(seconds=LOG_THROTTLE_SECONDS + 1),
        ):
            third = log.warning("k", "message")

    assert first is True
    assert second is False
    assert third is True


async def test_the_warning_timestamp_only_records_a_warning_that_exists(
    hass, freezer, mock_config_entry
) -> None:
    """``last_warning`` must not point at a line the throttle discarded.

    This is the field that made the live evidence ambiguous: a timestamp was
    reported, so a warning was assumed to have been logged, and the log was
    searched for an entry that had never been written.
    """
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)

    def publish(house: float, grid: float) -> None:
        set_sensor(hass, HOUSE_LOAD, house, "W", "power")
        set_sensor(hass, PV_POWER, 0, "W", "power")
        set_sensor(hass, BATTERY_POWER, 0, "W", "power")
        set_sensor(hass, GRID_POWER, grid, "W", "power")

    publish(300, 305)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data

    # First episode: a moderate residual warns once and stamps the timestamp.
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(BALANCE_SUSTAINED_FAILURES):
            publish(300, 470)
            coordinator._sample_balance()
    assert logged.call_count == 1
    first_stamp = coordinator.balance.last_warning
    assert first_stamp is not None

    # A passing sample resolves the run, then the same moderate condition
    # returns inside the throttle window. Nothing is logged this time.
    publish(300, 305)
    coordinator._sample_balance()
    # The clock must advance, or a re-stamped timestamp would be byte-identical
    # to the first and this test would pass against the unfixed code.
    freezer.tick(timedelta(minutes=20))
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(BALANCE_SUSTAINED_FAILURES):
            publish(300, 470)
            coordinator._sample_balance()

    assert logged.call_count == 0, "the throttle window is still open"
    assert coordinator.balance.last_warning == first_stamp, (
        "the timestamp advanced for a warning that was never emitted"
    )


async def test_a_gross_fault_after_a_moderate_one_still_warns_end_to_end(
    hass, freezer, mock_config_entry
) -> None:
    """The escalated wording survives an earlier reassuring one."""
    from .test_pv_independence import START

    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)

    def publish(house: float, grid: float) -> None:
        set_sensor(hass, HOUSE_LOAD, house, "W", "power")
        set_sensor(hass, PV_POWER, 0, "W", "power")
        set_sensor(hass, BATTERY_POWER, 0, "W", "power")
        set_sensor(hass, GRID_POWER, grid, "W", "power")

    publish(300, 305)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data

    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        # Episode one: moderate.
        for _ in range(BALANCE_SUSTAINED_FAILURES):
            publish(300, 470)
            coordinator._sample_balance()
        # Resolved.
        publish(300, 305)
        coordinator._sample_balance()
        # Episode two: 8 kW consumed with nothing supplying it.
        for _ in range(BALANCE_SUSTAINED_FAILURES):
            publish(8000, 0)
            coordinator._sample_balance()

    texts = [call.args[0] % call.args[1:] for call in logged.call_args_list]
    balance = [text for text in texts if "energy-balance" in text]

    assert len(balance) == 2, balance
    assert "Learning is unaffected" in balance[0]
    assert "sign conventions" in balance[1]
    assert coordinator.balance.last_warning is not None
