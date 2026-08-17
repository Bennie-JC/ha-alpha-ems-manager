"""The physical tolerance model behind the energy-balance verdict.

Phase 1A stopped the balance check blaming the configuration for asynchronous
source updates. What remained was a *coherent*, sustained residual on the live
system::

    supply 740 W vs demand 586 W, 21% off

That is not a timing artefact, so no amount of gating or debouncing addresses
it. Either the residual is physically explicable -- in which case the verdict
rule was wrong -- or it is real, in which case it should keep failing.

These tests pin down the answer to both halves. The allowance is built from the
three physical causes a residual actually has (see ``evaluate_balance``), and
the observed sample is measured against it rather than the reverse: the
thresholds are derived from inverter efficiency and meter accuracy, and the
740/586 case is then simply reported as whatever it turns out to be.

Spoiler, asserted below: it still fails, under every flow decomposition that
could have produced it. Explaining it away would need a conversion-loss
allowance of more than 12 %, which no hybrid inverter justifies.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    BALANCE_BASE_ALLOWANCE_W,
    BALANCE_CONVERSION_LOSS_FRACTION,
    BALANCE_METERING_TOLERANCE,
    BALANCE_SUSTAINED_FAILURES,
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
)
from custom_components.alpha_ems_manager.energy_balance import (
    MODE_IDLE,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    REASON_BASE,
    REASON_CONVERSION,
    REASON_METERING,
    BalanceMonitor,
    evaluate_balance,
    infer_balance_mode,
)
from custom_components.alpha_ems_manager.normalization import (
    PowerFlows,
    split_battery_power,
    split_grid_power,
)

#: The relative tolerance the previous rule applied, and the floor it divided by.
#: Kept here, not in ``const``, precisely because the production code no longer
#: uses them -- the comparison tests need the old rule to compare against.
OLD_RELATIVE_TOLERANCE = 0.15
OLD_ABSOLUTE_FLOOR_W = 250.0


def flows(
    house: float,
    pv: float = 0.0,
    battery_w: float = 0.0,
    grid_w: float = 0.0,
    battery_sign: str = SIGN_BATTERY_NEGATIVE_IS_CHARGE,
) -> PowerFlows:
    """Build a normalised snapshot from raw, signed source readings."""
    charge, discharge = split_battery_power(battery_w, battery_sign)
    imported, exported = split_grid_power(grid_w)
    return PowerFlows(
        house_load_w=house,
        pv_w=pv,
        battery_charge_w=charge,
        battery_discharge_w=discharge,
        grid_import_w=imported,
        grid_export_w=exported,
    )


def sample(fl: PowerFlows):
    """Evaluate a snapshot with no timing information attached."""
    result = evaluate_balance(fl)
    assert result is not None
    return result


def old_rule_passes(supply_w: float, demand_w: float) -> bool:
    """Return the verdict the pre-Phase-1B relative rule would have given."""
    residual = abs(supply_w - demand_w)
    denominator = max(supply_w, demand_w, OLD_ABSOLUTE_FLOOR_W)
    return residual / denominator <= OLD_RELATIVE_TOLERANCE


# -- the allowance formula ----------------------------------------------------


def test_the_allowance_is_the_sum_of_its_three_terms() -> None:
    """The verdict is a documented formula, not a fitted number."""
    result = sample(flows(house=2850.0, battery_w=3000.0))

    assert result.dc_power_w == pytest.approx(3000.0)
    assert result.ac_power_w == pytest.approx(3000.0)
    assert result.allowed_residual_w == pytest.approx(
        BALANCE_BASE_ALLOWANCE_W
        + BALANCE_CONVERSION_LOSS_FRACTION * 3000.0
        + BALANCE_METERING_TOLERANCE * 3000.0
    )


def test_dc_power_sums_pv_and_battery_without_netting_them() -> None:
    """PV charging a battery is converted twice, so both terms count.

    The MPPT stage and the battery DC-DC stage each take their cut. Netting them
    would credit the inverter with a free transfer it does not perform.
    """
    result = sample(flows(house=60.0, pv=2000.0, battery_w=-1900.0))

    assert result.dc_power_w == pytest.approx(3900.0)


def test_a_purely_ac_snapshot_gets_no_conversion_allowance() -> None:
    """Grid straight to house converts nothing, so nothing is forgiven for it."""
    result = sample(flows(house=740.0, grid_w=740.0))

    assert result.dc_power_w == pytest.approx(0.0)
    assert result.allowed_residual_w == pytest.approx(
        BALANCE_BASE_ALLOWANCE_W + BALANCE_METERING_TOLERANCE * 740.0
    )


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w", "expected"),
    [
        # Nothing flowing: only the fixed term exists.
        ("idle", 8.0, 0.0, 0.0, 8.0, REASON_BASE),
        # Battery converting: the DC term outruns the AC one.
        ("battery discharge", 2850.0, 0.0, 3000.0, 0.0, REASON_CONVERSION),
        # Pure grid import at high power: only the metering term grows.
        ("grid import", 8000.0, 0.0, 0.0, 8000.0, REASON_METERING),
    ],
)
def test_the_dominant_allowance_term_is_reported(
    label: str,
    house: float,
    pv: float,
    battery_w: float,
    grid_w: float,
    expected: str,
) -> None:
    """Diagnostics say *why* a sample was allowed the slack it got."""
    result = sample(flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w))

    assert result.tolerance_reason == expected, label


# -- 11. the real observed case -----------------------------------------------

#: Every flow decomposition that produces supply 740 W and demand 586 W. The
#: live warning reported only the two totals, so the cause has to be evaluated
#: against all of them rather than one assumed split.
OBSERVED_DECOMPOSITIONS = [
    # Night, battery idle: grid import covering the house. No conversion at all.
    ("grid to house", 586.0, 0.0, 0.0, 740.0),
    # Evening: battery alone carrying the house through the inverter.
    ("battery to house", 586.0, 0.0, 740.0, 0.0),
    # Evening with a little sun left.
    ("pv and battery to house", 586.0, 200.0, 540.0, 0.0),
    # Low irradiance: PV feeding the house and trickling into the battery.
    ("pv to house and battery", 386.0, 740.0, -200.0, 0.0),
]


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w"), OBSERVED_DECOMPOSITIONS
)
def test_the_observed_case_is_the_reported_740_586(
    label: str, house: float, pv: float, battery_w: float, grid_w: float
) -> None:
    """Each decomposition really does reproduce the logged totals."""
    result = sample(flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w))

    assert result.supply_w == pytest.approx(740.0), label
    assert result.demand_w == pytest.approx(586.0), label
    assert result.residual_w == pytest.approx(154.0), label
    assert result.relative_error == pytest.approx(154.0 / 740.0, rel=1e-3), label


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w"), OBSERVED_DECOMPOSITIONS
)
def test_the_observed_case_still_fails_under_the_physical_model(
    label: str, house: float, pv: float, battery_w: float, grid_w: float
) -> None:
    """154 W is more than conversion and metering can account for.

    The most forgiving decomposition is PV feeding the house while charging the
    battery, which converts 940 W of DC and so earns the largest allowance of
    the four. Even that lands at about 109 W, well short of 154 W. The residual
    is therefore a genuine sustained physical inconsistency, and is reported as
    one rather than tuned away.
    """
    result = sample(flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w))

    assert result.outcome == OUTCOME_FAILED, (
        f"{label}: residual {result.residual_w:.0f} W vs allowance "
        f"{result.allowed_residual_w:.0f} W"
    )
    assert result.allowed_residual_w < 154.0, label


def test_the_observed_case_fails_the_old_rule_too() -> None:
    """The new model is not what makes this sample fail.

    21 % was already outside the old flat 15 %. Phase 1B does not change this
    verdict; it changes how much is known about why.
    """
    assert not old_rule_passes(740.0, 586.0)
    assert not sample(flows(house=586.0, battery_w=740.0)).within_tolerance


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "minimum_fraction"),
    [
        # Most forgiving split: 940 W of DC crosses a conversion stage.
        ("pv to house and battery", 386.0, 740.0, -200.0, 0.09),
        # Battery alone: only 740 W of DC to spread the loss over.
        ("battery to house", 586.0, 0.0, 740.0, 0.12),
    ],
)
def test_explaining_the_observed_case_would_need_an_indefensible_loss(
    label: str, house: float, pv: float, battery_w: float, minimum_fraction: float
) -> None:
    """Quantifies exactly how far the tolerance would have to be stretched.

    Forgiving 154 W needs the DC conversion-loss fraction raised to somewhere
    between 10 % and 13 %, depending on how much DC power the split actually
    converts. That is roughly double the 5 % the model allows and three to four
    times the ~3 % a hybrid inverter loses near its rated point.

    It is also why widening it is the wrong move rather than merely a generous
    one: at 8 kW of DC throughput a 10 % fraction would hand out 800 W of slack,
    comfortably enough to swallow a mis-selected entity.
    """
    result = sample(flows(house=house, pv=pv, battery_w=battery_w))
    fixed_terms = (
        BALANCE_BASE_ALLOWANCE_W + BALANCE_METERING_TOLERANCE * result.ac_power_w
    )
    required_fraction = (154.0 - fixed_terms) / result.dc_power_w

    assert required_fraction > minimum_fraction, label
    assert required_fraction > BALANCE_CONVERSION_LOSS_FRACTION * 1.9, label


def test_the_observed_case_reads_as_moderate_not_as_a_gross_fault() -> None:
    """It is over the allowance, but nowhere near sign-inversion territory.

    This is what lets the warning avoid telling the user to re-check entities
    that may be perfectly correct.
    """
    result = sample(flows(house=586.0, battery_w=740.0))

    assert not result.within_tolerance
    assert not result.gross_fault_suspected
    assert result.excess_w == pytest.approx(154.0 - result.allowed_residual_w)


# -- 12. true faults must still fail ------------------------------------------


@pytest.mark.parametrize(
    ("label", "supply_w", "demand_w"),
    [("20 vs 1200", 20.0, 1200.0), ("100 vs 1500", 100.0, 1500.0)],
)
def test_gross_supply_shortfalls_still_fail(
    label: str, supply_w: float, demand_w: float
) -> None:
    """Energy arriving from nowhere remains a fault at any tolerance."""
    result = sample(flows(house=demand_w, pv=supply_w))

    assert result.outcome == OUTCOME_FAILED, label
    assert result.gross_fault_suspected, label


def test_an_inverted_battery_sign_is_still_a_gross_fault() -> None:
    """1.3 kW of discharge read as charge is a 2.6 kW error, every sample."""
    result = sample(
        flows(
            house=1524.0,
            pv=201.0,
            battery_w=1316.0,
            grid_w=-4.0,
            battery_sign=SIGN_BATTERY_POSITIVE_IS_CHARGE,
        )
    )

    assert result.outcome == OUTCOME_FAILED
    assert result.gross_fault_suspected
    # Comfortably clear of the allowance, not marginally over it.
    assert abs(result.residual_w) > result.allowed_residual_w * 8


def test_an_inverted_grid_sign_is_still_a_gross_fault() -> None:
    """1.2 kW of import read as export doubles the demand side."""
    imported, exported = split_grid_power(1200.0, "negative_is_import")
    result = evaluate_balance(
        PowerFlows(
            house_load_w=1200.0,
            pv_w=0.0,
            battery_charge_w=0.0,
            battery_discharge_w=0.0,
            grid_import_w=imported,
            grid_export_w=exported,
        )
    )

    assert result is not None
    assert result.outcome == OUTCOME_FAILED
    assert result.gross_fault_suspected


def test_a_sustained_gross_fault_still_warns_exactly_once() -> None:
    """The debounce is untouched: three coherent failures, one warning."""
    monitor = BalanceMonitor()
    warnings = 0

    for _ in range(BALANCE_SUSTAINED_FAILURES + 10):
        monitor.record(sample(flows(house=1200.0, pv=20.0)))
        if monitor.should_warn():
            warnings += 1

    assert warnings == 1
    assert monitor.pass_rate == pytest.approx(0.0)


def test_the_new_model_catches_a_fault_the_old_rule_let_through() -> None:
    """The point of tightening: 15 % of 8 kW was a 1.2 kW blind spot.

    A whole kilowatt of house load missing from an 8 kW import passed the old
    relative rule exactly at its boundary. Nothing is being converted here, so
    the physical model allows a few hundred watts and catches it.
    """
    assert old_rule_passes(8000.0, 6800.0)

    result = sample(flows(house=6800.0, grid_w=8000.0))

    assert result.outcome == OUTCOME_FAILED
    assert result.allowed_residual_w < 1200.0


# -- 13. low, medium and high power -------------------------------------------


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w"),
    [
        # ~100 W: standby, the fixed allowance carries it.
        ("low, grid", 100.0, 0.0, 0.0, 130.0),
        ("low, battery", 96.0, 0.0, 120.0, 0.0),
        # ~500-1000 W: the region the live warning came from, but healthy.
        ("medium, battery discharge", 700.0, 0.0, 740.0, 0.0),
        ("medium, pv to house", 620.0, 650.0, 0.0, -10.0),
        # 3-10 kW: conversion loss dominates and scales with throughput.
        ("high, battery discharge", 2850.0, 0.0, 3000.0, 0.0),
        ("high, pv to battery and house", 1280.0, 9500.0, -8000.0, 0.0),
    ],
)
def test_healthy_samples_pass_at_every_power_level(
    label: str, house: float, pv: float, battery_w: float, grid_w: float
) -> None:
    """Realistic conversion residuals stay inside the allowance throughout."""
    result = sample(flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w))

    assert result.outcome == OUTCOME_PASSED, (
        f"{label}: residual {result.residual_w:.0f} W vs allowance "
        f"{result.allowed_residual_w:.0f} W"
    )


def test_the_allowance_grows_with_conversion_not_with_bare_throughput() -> None:
    """Two 8 kW snapshots, one converting and one not, are judged differently.

    This is the substantive difference from a flat relative rule, which would
    hand both the same 1.2 kW of slack.
    """
    converting = sample(flows(house=200.0, pv=8000.0, battery_w=-7700.0))
    passthrough = sample(flows(house=8000.0, grid_w=8000.0))

    assert converting.allowed_residual_w > passthrough.allowed_residual_w * 3
    assert passthrough.allowed_residual_w < OLD_RELATIVE_TOLERANCE * 8000.0


def test_low_power_behaviour_matches_the_previous_rule() -> None:
    """Overnight and standby behaviour is known-good and must not change.

    Below the old 250 W floor the old rule was effectively an absolute 37.5 W
    test. The fixed allowance is 40 W, so every verdict down there agrees.
    """
    for supply_w, demand_w in ((8.0, 12.0), (12.0, 8.0), (0.0, 30.0), (25.0, 0.0)):
        result = sample(flows(house=demand_w, pv=supply_w))

        assert result.within_tolerance is old_rule_passes(supply_w, demand_w)
        assert result.outcome == OUTCOME_PASSED


def test_a_poor_but_credible_low_load_efficiency_passes() -> None:
    """300 W DC delivering 240 W AC is 80 % -- bad, and entirely real.

    Inverter efficiency curves fall away steeply below roughly a tenth of rated
    power, which is where a domestic battery spends much of the night. The fixed
    allowance exists to cover exactly this.
    """
    result = sample(flows(house=240.0, battery_w=300.0))

    assert result.outcome == OUTCOME_PASSED


def test_an_incredible_low_load_efficiency_still_fails() -> None:
    """300 W DC delivering 200 W AC is 67 %, which no inverter does.

    The fixed allowance forgives tens of watts, not a third of the throughput.
    """
    result = sample(flows(house=200.0, battery_w=300.0))

    assert result.outcome == OUTCOME_FAILED


# -- 14. mode-specific behaviour ----------------------------------------------


@pytest.mark.parametrize(
    ("house", "pv", "battery_w", "grid_w", "expected"),
    [
        (0.0, 0.0, 0.0, 0.0, MODE_IDLE),
        (586.0, 0.0, 0.0, 740.0, "grid->house"),
        (586.0, 0.0, 740.0, 0.0, "battery->house"),
        (586.0, 200.0, 540.0, 0.0, "pv+battery->house"),
        (386.0, 740.0, -200.0, 0.0, "pv->house+battery"),
        (500.0, 4000.0, 0.0, -3300.0, "pv->house+grid"),
        (100.0, 0.0, -2820.0, 3000.0, "grid->house+battery"),
        # House below the activity threshold, so this is PV to battery alone.
        (20.0, 2000.0, -1900.0, 0.0, "pv->battery"),
    ],
)
def test_the_operating_mode_is_labelled_from_the_flows(
    house: float, pv: float, battery_w: float, grid_w: float, expected: str
) -> None:
    """Every mode the task calls out gets a distinct, readable label."""
    assert infer_balance_mode(flows(house, pv, battery_w, grid_w)) == expected


def test_an_idle_system_is_not_given_a_spurious_mode() -> None:
    """A few watts of standby noise is idle, not a grid-to-house transfer."""
    assert infer_balance_mode(flows(house=8.0, grid_w=10.0)) == MODE_IDLE


@pytest.mark.parametrize(
    ("label", "house", "pv", "battery_w", "grid_w"),
    [
        # Battery discharging, ~4 % DC-AC loss.
        ("battery discharging", 1920.0, 0.0, 2000.0, 0.0),
        # Battery charging from the grid, ~4 % loss on the way in.
        ("battery charging", 100.0, 0.0, -2820.0, 3000.0),
        # PV surplus: house plus battery, two conversion stages.
        ("pv surplus", 900.0, 5000.0, -3900.0, -50.0),
        # Grid import only: no conversion, so a tight verdict.
        ("grid import", 1180.0, 0.0, 0.0, 1200.0),
        # PV export: DC in, AC out to the meter.
        ("grid export", 500.0, 4000.0, 0.0, -3300.0),
    ],
)
def test_each_operating_mode_tolerates_its_realistic_residual(
    label: str, house: float, pv: float, battery_w: float, grid_w: float
) -> None:
    """No single mode is systematically flagged by the model."""
    result = sample(flows(house=house, pv=pv, battery_w=battery_w, grid_w=grid_w))

    assert result.outcome == OUTCOME_PASSED, (
        f"{label}: mode {result.mode}, residual {result.residual_w:.0f} W vs "
        f"allowance {result.allowed_residual_w:.0f} W"
    )


def test_the_mode_and_flows_reach_diagnostics() -> None:
    """A future warning must be diagnosable from the payload alone.

    The live 740/586 report contained only the two totals, which is why its
    cause could not be settled from the log. Every term is now recorded.
    """
    payload = sample(flows(house=586.0, pv=200.0, battery_w=540.0)).as_dict()

    assert payload["mode"] == "pv+battery->house"
    assert payload["residual_w"] == pytest.approx(154.0)
    assert payload["allowed_residual_w"] == pytest.approx(99.2, abs=0.1)
    assert payload["gross_fault_suspected"] is False
    assert payload["flows_w"] == {
        "house_load": 586.0,
        "pv": 200.0,
        "battery_charge": 0.0,
        "battery_discharge": 540.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
    }


# -- 10. learning independence ------------------------------------------------


async def test_a_sustained_coherent_mismatch_never_stops_learning(
    hass, freezer, mock_config_entry
) -> None:
    """The decisive guarantee: balance is a quality signal and nothing more.

    Every source reports promptly and stays put, so the samples are coherent and
    genuinely fail -- exactly the live 740/586 situation, not the incoherent one
    ``test_balance_robustness`` already covers. The quarter must still be learned
    from the house-load sensor, untouched.
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
    freezer.move_to(START)

    def publish() -> None:
        # supply 740 W (grid) vs demand 586 W (house): 154 W unaccounted for,
        # every source fresh, so the sample is coherent and fails on its merits.
        set_sensor(hass, HOUSE_LOAD, 586, "W", "power")
        set_sensor(hass, PV_POWER, 0, "W", "power")
        set_sensor(hass, BATTERY_POWER, 0, "W", "power")
        set_sensor(hass, GRID_POWER, 740, "W", "power")

    publish()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(16):
        freezer.tick(timedelta(seconds=60))
        publish()
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data

    # The mismatch was real, sustained and reported.
    assert coordinator.balance.failed_samples >= BALANCE_SUSTAINED_FAILURES
    assert coordinator.balance.sustained_failure
    assert coordinator.balance.pass_rate == pytest.approx(0.0)

    # And the 10:00 quarter was learned anyway: 586 W for 15 minutes.
    record = coordinator.store.days[START.date()]
    assert record.measured[40] == pytest.approx(0.586 * 0.25, rel=1e-3)
    assert record.measured_valid_count == 1


async def test_a_moderate_mismatch_warns_without_blaming_the_configuration(
    hass, freezer, mock_config_entry
) -> None:
    """The 740/586 wording must not send the user hunting a correct entity."""
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

    def publish() -> None:
        set_sensor(hass, HOUSE_LOAD, 586, "W", "power")
        set_sensor(hass, PV_POWER, 0, "W", "power")
        set_sensor(hass, BATTERY_POWER, 740, "W", "power")
        set_sensor(hass, GRID_POWER, 0, "W", "power")

    publish()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(BALANCE_SUSTAINED_FAILURES + 5):
            publish()
            coordinator._sample_balance()

    messages = [call.args[0] % call.args[1:] for call in logged.call_args_list]
    balance = [text for text in messages if "energy-balance" in text]

    assert len(balance) == 1, "the debounce still allows exactly one warning"
    text = balance[0]
    assert "Sustained energy-balance mismatch" in text
    assert "battery->house" in text  # the mode is named
    assert "154 W" in text  # the residual, not just the two totals
    assert "different electrical boundaries" in text
    assert "Learning is unaffected" in text
    # The stronger wording is reserved for residuals that really are gross.
    assert "sign conventions" not in text


async def test_a_gross_fault_keeps_the_stronger_wording(
    hass, freezer, mock_config_entry
) -> None:
    """A genuinely impossible steady state must still say what to check."""
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

    def publish() -> None:
        # 1.2 kW consumed with nothing supplying it.
        for entity, value in (
            (HOUSE_LOAD, 1200),
            (PV_POWER, 0),
            (BATTERY_POWER, 0),
            (GRID_POWER, 0),
        ):
            set_sensor(hass, entity, value, "W", "power")

    publish()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    with patch.object(coordinator_module._LOGGER, "warning") as logged:
        for _ in range(BALANCE_SUSTAINED_FAILURES + 5):
            publish()
            coordinator._sample_balance()

    balance = [
        call.args[0] % call.args[1:]
        for call in logged.call_args_list
        if "energy-balance" in (call.args[0] % call.args[1:])
    ]

    assert len(balance) == 1
    text = balance[0]
    assert "one term of the identity is wrong" in text
    assert "selected source entities" in text
    assert "sign conventions" in text
