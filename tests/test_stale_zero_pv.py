"""Night-time PV staleness must not block the energy-balance check.

A live beta.3 installation skipped 185 of 189 incoherent balance samples for a
single reason: ``sensor.alphaess_current_pv_production`` sat at 0 W from dusk and
stopped republishing, so its report age reached three hours while the identity it
was blocking closed to within 1 W.

The fix is narrow and stated in full in :func:`measure_coherence`: a PV source
whose current value is exactly zero contributes exactly zero to the identity, so
its report age cannot change the verdict and it takes no part in the timing
comparison. Everything else -- a stale positive PV, an unreadable PV, a stale
battery, grid or house-load source -- is judged exactly as before.

These tests pin both halves: that the exemption fires where it should, and that
it cannot be stretched to cover anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    CONF_HAS_PV,
    CONF_PV_POWER_ENTITY,
)
from custom_components.alpha_ems_manager.energy_balance import (
    OUTCOME_PASSED,
    OUTCOME_SKIPPED_INCOHERENT,
    SKIP_STALE_SOURCE,
    measure_coherence,
)

from .conftest import (
    BATTERY_POWER,
    GRID_POWER,
    HOUSE_LOAD,
    PV_POWER,
    TEST_TIMEZONE,
    TZ,
    set_sensor,
)

#: Comfortably past the freshness limit, and about the age the live system saw.
STALE_SECONDS = 3 * 60 * 60


def age_source(hass: HomeAssistant, entity_id: str, seconds: float) -> None:
    """Backdate an entity's report timestamps by ``seconds``.

    ``last_reported`` is what the coherence check reads, but ``last_updated`` is
    the documented fallback for a core that does not carry it, so both move.
    """
    state = hass.states.get(entity_id)
    assert state is not None
    shifted = state.last_updated - timedelta(seconds=seconds)
    for attribute in ("last_updated", "last_reported", "last_changed"):
        if hasattr(state, attribute):
            object.__setattr__(state, attribute, shifted)


def night_flows(hass: HomeAssistant, pv_state: object = 0) -> None:
    """Write a coherent night-time snapshot that balances almost exactly.

    Grid imports 963 W, the house draws 963 W, the battery is idle and PV is
    whatever the caller wants to test. These are the live figures that were
    being skipped.
    """
    set_sensor(hass, HOUSE_LOAD, 963, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 963, "W", "power")
    set_sensor(hass, PV_POWER, pv_state, "W", "power")


def sample_once(coordinator) -> str | None:
    """Take one balance sample and return the outcome, or ``None`` if unjudged."""
    before = coordinator.balance.unavailable_samples
    coordinator._sample_balance()
    if coordinator.balance.unavailable_samples > before:
        return None
    return coordinator.last_balance.outcome


# -- 1. the reported defect --------------------------------------------------


async def test_a_pv_sensor_asleep_at_zero_overnight_no_longer_blocks_the_check(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The live failure: PV at 0 W for three hours, everything else fresh.

    Fails on beta.3, where the sample is skipped as ``stale_source`` with the PV
    entity named as the laggard.
    """
    coordinator = setup_integration.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) == OUTCOME_PASSED
    assert coordinator.balance.skipped_due_to_stale_source == 0
    assert coordinator.balance.eligible_samples == 1

    coherence = coordinator.last_balance.coherence
    assert coherence.quiescent_entity_ids == (PV_POWER,)
    # The remaining three sources are compared against each other, so the age
    # reflects them rather than the sensor that correctly stopped talking.
    assert coherence.oldest_age_seconds < BALANCE_MAX_SOURCE_AGE_SECONDS
    assert coherence.source_count == 3


async def test_the_exemption_is_recorded_rather_than_silent(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A relaxation that leaves no trace is one nobody can audit."""
    coordinator = setup_integration.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    sample_once(coordinator)
    sample_once(coordinator)

    payload = coordinator.balance.as_dict()
    assert payload["quiescent_zero_source_counts"] == {PV_POWER: 2}
    assert "exactly 0 W" in payload["quiescent_zero_rule"]
    assert payload["last_sample"]["coherence"]["quiescent_entity_ids"] == [PV_POWER]


# -- 2, 3, 4. only an exact, readable zero qualifies --------------------------


@pytest.mark.parametrize("pv_state", [250, 10, 5, 2, 1])
async def test_a_stale_non_zero_pv_reading_is_still_skipped(
    hass: HomeAssistant, setup_integration: MockConfigEntry, pv_state: float
) -> None:
    """PV last seen producing must never be silently trusted three hours on.

    A stale positive value contributes a real term to the identity and can
    cancel a genuine fault in either direction. There is also deliberately no
    band around zero: 1 W and 5 W are as unexempt as 250 W, because the
    justification is "contributes exactly nothing", not "contributes little".
    """
    coordinator = setup_integration.runtime_data
    night_flows(hass, pv_state=pv_state)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) == OUTCOME_SKIPPED_INCOHERENT
    coherence = coordinator.last_balance.coherence
    assert coherence.skip_reason == SKIP_STALE_SOURCE
    assert coherence.oldest_entity_id == PV_POWER
    assert coherence.quiescent_entity_ids == ()


@pytest.mark.parametrize("pv_state", ["unknown", "unavailable", "", "not a number"])
async def test_an_unreadable_pv_source_is_never_treated_as_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry, pv_state: str
) -> None:
    """An entity that cannot be read is not a zero, however old its last value.

    This is the strongest case in the rule: a snapshot missing a component gets
    no verdict at all, so the exemption is never even consulted.
    """
    coordinator = setup_integration.runtime_data
    night_flows(hass, pv_state=pv_state)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) is None
    assert coordinator.balance.unavailable_samples >= 1
    assert coordinator.balance.eligible_samples == 0


async def test_a_pv_source_with_an_unusable_unit_is_not_a_zero(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A 0 that cannot be scaled to watts is not a zero either."""
    coordinator = setup_integration.runtime_data
    night_flows(hass)
    set_sensor(hass, PV_POWER, 0, "kWh", "energy")
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) is None


async def test_a_negative_pv_reading_is_refused_rather_than_exempted(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An inverted PV sensor is uninterpretable, not a small zero."""
    coordinator = setup_integration.runtime_data
    night_flows(hass, pv_state=-0.5)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) is None


# -- 5, 6. sunrise ------------------------------------------------------------


async def test_sunrise_restores_the_normal_freshness_rules(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The exemption is self-terminating.

    A PV sensor that starts generating publishes a new value by definition, so
    the moment it is non-zero it is also fresh -- and if it somehow is not, it
    is judged as stale like anything else.
    """
    coordinator = setup_integration.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)
    assert sample_once(coordinator) == OUTCOME_PASSED

    # Dawn: PV publishes 400 W and the house is now partly supplied by it.
    set_sensor(hass, HOUSE_LOAD, 963, "W", "power")
    set_sensor(hass, GRID_POWER, 563, "W", "power")
    set_sensor(hass, PV_POWER, 400, "W", "power")
    await hass.async_block_till_done()

    assert sample_once(coordinator) == OUTCOME_PASSED
    assert coordinator.last_balance.coherence.quiescent_entity_ids == ()
    assert coordinator.balance.eligible_samples == 2


async def test_pv_that_turns_positive_without_republishing_is_caught_not_hidden(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The residual risk fails loudly rather than passing quietly.

    If real generation started while the sensor stayed silent at 0, substituting
    zero makes supply short by exactly that generation -- so the sample fails by
    it. The exemption can never manufacture a pass, which is what makes the
    relaxation safe in the only direction that matters.
    """
    coordinator = setup_integration.runtime_data
    # The house draws 963 W supplied by 400 W of unreported PV and 563 W of grid.
    set_sensor(hass, HOUSE_LOAD, 963, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 563, "W", "power")
    set_sensor(hass, PV_POWER, 0, "W", "power")
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) != OUTCOME_PASSED
    assert coordinator.last_balance.residual_w == pytest.approx(-400.0)


# -- 7. every other source keeps the old rules --------------------------------


@pytest.mark.parametrize("entity_id", [HOUSE_LOAD, BATTERY_POWER, GRID_POWER])
async def test_a_stale_zero_on_any_other_source_is_still_skipped(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_id: str
) -> None:
    """Only PV is exempt, and only PV.

    House load is the learning target and its silence is information; battery
    idle and a null grid reading are transient states rather than a nightly
    regime, so neither buys coverage worth widening the rule for.
    """
    coordinator = setup_integration.runtime_data
    set_sensor(hass, HOUSE_LOAD, 0, "W", "power")
    set_sensor(hass, BATTERY_POWER, 0, "W", "power")
    set_sensor(hass, GRID_POWER, 0, "W", "power")
    set_sensor(hass, PV_POWER, 0, "W", "power")
    await hass.async_block_till_done()
    age_source(hass, entity_id, STALE_SECONDS)

    assert sample_once(coordinator) == OUTCOME_SKIPPED_INCOHERENT
    assert coordinator.last_balance.coherence.oldest_entity_id == entity_id
    assert coordinator.balance.skipped_due_to_stale_source == 1


# -- 8. DST night -------------------------------------------------------------


@pytest.mark.parametrize(
    "local_moment",
    [
        datetime(2026, 3, 29, 1, 30, tzinfo=TZ),
        datetime(2026, 3, 29, 3, 30, tzinfo=TZ),
        datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0),
        datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1),
    ],
    ids=["before_spring_gap", "after_spring_gap", "first_fold", "second_fold"],
)
def test_coherence_arithmetic_is_unaffected_by_a_daylight_saving_fold(
    local_moment: datetime,
) -> None:
    """Timing is absolute-time arithmetic, so a fold cannot alter it.

    Both occurrences of a repeated 02:30 are distinct instants, and a source
    reported one minute before either is one minute old at both. Nothing about
    the exemption -- or the gate it bypasses -- depends on the wall clock.
    """
    now = local_moment.astimezone(UTC)
    fresh = now - timedelta(seconds=60)
    stale = now - timedelta(seconds=STALE_SECONDS)

    judged = measure_coherence(
        [fresh, fresh, fresh], now, quiescent_entity_ids=(PV_POWER,)
    )
    assert judged.coherent
    assert judged.oldest_age_seconds == pytest.approx(60.0)
    assert judged.quiescent_entity_ids == (PV_POWER,)

    # And with the same instant included rather than exempted, it is still stale.
    assert not measure_coherence([fresh, fresh, stale], now).coherent


async def test_the_exemption_survives_a_dst_night_end_to_end(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The coordinator path, not just the arithmetic, holds across a fold."""
    coordinator = setup_integration.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert sample_once(coordinator) == OUTCOME_PASSED
    assert coordinator.last_balance.coherence.quiescent_entity_ids == (PV_POWER,)


# -- 9, 10. installations without PV -----------------------------------------


async def test_an_installation_without_pv_contributes_no_pv_timestamp(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
) -> None:
    """With no array the generation term is a known zero, not an unread sensor.

    Covers both "no PV hardware" and "the PV option was switched off": either
    way the entity is not a balance source, so no exemption is needed and none
    is claimed.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_HAS_PV: False, CONF_PV_POWER_ENTITY: None},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    # Even an ancient PV entity is irrelevant: it is not a balance source.
    age_source(hass, PV_POWER, STALE_SECONDS)

    assert PV_POWER not in coordinator.balance_source_entities
    assert sample_once(coordinator) == OUTCOME_PASSED
    assert coordinator.last_balance.coherence.quiescent_entity_ids == ()


async def test_a_pv_less_system_still_skips_a_genuinely_stale_source(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    source_entities: None,
) -> None:
    """Removing PV from the comparison must not remove the comparison."""
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_HAS_PV: False, CONF_PV_POWER_ENTITY: None},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    night_flows(hass)
    await hass.async_block_till_done()
    age_source(hass, GRID_POWER, STALE_SECONDS)

    assert sample_once(coordinator) == OUTCOME_SKIPPED_INCOHERENT
    assert coordinator.last_balance.coherence.oldest_entity_id == GRID_POWER
