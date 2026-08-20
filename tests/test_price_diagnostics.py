"""The price block in a diagnostics download.

Bounded, and honest about what each figure is. Three distinctions the block is
built to preserve, because a support request that loses any of them is unreadable:

* a capability probed **now** against one recorded at the last refresh -- printing
  a stale capability beside live readings is what made an earlier defect look
  like a self-contradiction;
* the next day being unpublished against the next day being broken;
* a reconstructed export figure against a published one.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    PRICE_CROSS_CHECK_AGREES,
    PRICE_EXPORT_BASIS_ADJUSTMENT,
    PRICE_MAPPING_VERSION,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_ENTITY_MISSING,
)
from custom_components.alpha_ems_manager.diagnostics import (
    REASON_NOT_BUILT,
    async_get_config_entry_diagnostics,
)

from .conftest import FakeFrank
from .forecast_helpers import NORMAL, frozen, local
from .frank_capture import SYNTHETIC_FEED_IN_ADJUSTMENT, synthetic_day
from .test_price_capability import TOMORROW, drive


async def download(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Return the price block of a diagnostics download, on the driven day.

    The clock is pinned because the block reports *today*, and a download taken
    at the real wall clock would look up a civil day the refresh never built --
    reporting "not built" for a series that is sitting right there.
    """
    with frozen(local(NORMAL, 9, 6)):
        payload = await async_get_config_entry_diagnostics(hass, entry)
    return payload["price"]


async def test_the_block_reports_both_days_and_the_horizon(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Counts, coverage, edges and the horizon -- and no series."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["entry_selected"] is True
    assert price["today"]["available"] is True
    assert price["today"]["intervals_known"] == 96
    assert price["today"]["coverage"] == 1.0
    assert price["today"]["economic_price_horizon_end"] is not None
    assert price["tomorrow"]["available"] is True
    assert price["mapping"]["blocks_received"] == 192
    assert price["provenance"]["mapping_version"] == PRICE_MAPPING_VERSION


async def test_the_unpublished_next_day_is_not_shown_as_a_fault(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The whole reason the reason taxonomy exists, visible in the download.

    Somebody reading a support request at ten in the morning must be able to see
    that the next day being absent is the source working as designed.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["capability"]["usable"] is True
    assert price["capability"]["unavailable_reason"] is None
    assert price["today"]["available"] is True
    assert price["today"]["tomorrow_reason"] == PRICE_TOMORROW_NOT_PUBLISHED


async def test_the_capability_is_probed_now_and_also_recorded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Two instants, labelled as two instants.

    An earlier release printed one capability computed at download time beside a
    forecast block carrying a snapshot from a refresh fifteen minutes earlier, and
    the two disagreed. Both are still reported -- that is useful -- but they are
    now named for what they are.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)
    assert price["capability"]["usable"] is True
    assert price["capability_at_last_refresh"]["usable"] is True

    # The entities go away after the refresh. The live probe notices; the
    # recorded one still describes the instant it was taken.
    registry_ids = list(frank.entity_ids.values())
    for entity_id in registry_ids:
        hass.states.async_remove(entity_id)
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_id in registry_ids:
        registry.async_remove(entity_id)

    price = await download(hass, setup_integration)
    assert price["capability"]["usable"] is False
    assert price["capability"]["unavailable_reason"] == PRICE_UNAVAILABLE_ENTITY_MISSING
    assert price["capability_at_last_refresh"]["usable"] is True


async def test_the_options_are_reported_with_their_provenance(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The two figures the export reconstruction depends on, and their origin."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["options"]["readable"] is True
    assert price["options"]["feed_in_adjustment"] == SYNTHETIC_FEED_IN_ADJUSTMENT
    assert price["options"]["apply_feed_in_vat"] is False
    assert "never duplicated" in price["options"]["note"]


async def test_the_cross_check_result_is_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Agreement with the source's own two figures, and the figures themselves."""
    coordinator = setup_integration.runtime_data
    blocks = synthetic_day(NORMAL)
    active = blocks[9 * 4]
    frank.publish(
        today=blocks,
        tomorrow=None,
        current_price=active["total_price_eur_kwh"],
        current_return_price=round(
            active["market_price"] + SYNTHETIC_FEED_IN_ADJUSTMENT, 6
        ),
    )
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["provenance"]["import_cross_check"] == PRICE_CROSS_CHECK_AGREES
    assert price["provenance"]["export_cross_check"] == PRICE_CROSS_CHECK_AGREES
    derived = price["derived_source_data"]
    assert derived["current_import_eur_kwh"] == active["total_price_eur_kwh"]
    assert "never overrides" in derived["note"]


async def test_the_derived_source_entities_are_named_as_not_read(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Zones and optimal periods are reported as refused, not silently absent.

    They are configured by margins on the *source's* entry, so consuming them
    would make this integration's behaviour depend on somebody else's thresholds.
    Saying so in the download is cheaper than someone rediscovering it.
    """
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert "not read" in price["derived_source_data"]["zones_and_optimal_periods"]


async def test_the_export_basis_is_visible_beside_the_export_price(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A reconstruction must never be readable as a published figure."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["today"]["export_price_available"] is True
    assert "reconstructed" in price["provenance"]["export_note"]
    assert (
        coordinator.price_forecasts[NORMAL].intervals[0].export_basis
        == PRICE_EXPORT_BASIS_ADJUSTMENT
    )


async def test_the_evidence_counts_are_reported_without_the_arrays(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """How many issuances, when, and what was flagged. Never the series."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    evidence = (await download(hass, setup_integration))["evidence"]

    assert evidence["issuances_today"] == 1
    assert evidence["latest_intervals_known"] == 96
    assert evidence["latest_issued_at"] is not None
    assert evidence["latest_flags"] == []
    assert "no outcome half" in evidence["note"]


async def test_the_block_carries_no_price_series_and_no_long_list(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The sixteen-item list ceiling holds throughout, recursively.

    Ninety-six prices truncated to sixteen would be worse than absent: it would
    read as a short day rather than as a clipped payload.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    def walk(value: object, path: str = "price") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            assert len(value) <= 16, f"{path} holds {len(value)} entries"
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(price)

    # No key anywhere is the source's raw array. Checked by *key name*, not by
    # searching the rendered text: the resolved entity ids contain the word
    # "prices", so a substring scan fails on a correct payload.
    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for item in value.values() for k in keys(item)}
        if isinstance(value, (list, tuple)):
            return {k for item in value for k in keys(item)}
        return set()

    assert "prices" not in keys(price)
    assert "blocks" not in keys(price)


async def test_the_block_explains_that_prices_decide_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Readable on its own, without cross-referencing the architecture notes."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert "calls no service at all" in price["neutrality"]
    assert "market day" in price["boundaries"]
    assert "reaches no battery decision" in price["today"]["decides_nothing"]


async def test_an_installation_with_no_price_entities_still_reports_cleanly(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No source, no exception, and a named reason rather than an empty block."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)

    price = await download(hass, setup_integration)

    assert price["entry_selected"] is True
    assert price["capability"]["usable"] is False
    assert price["today"]["available"] is False
    assert price["today"]["today_reason"] == PRICE_UNAVAILABLE_ENTITY_MISSING
    assert price["evidence"]["issuances_today"] == 1


async def test_the_download_survives_a_day_with_no_forecast_built(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A day the coordinator never built reports as such rather than raising."""
    coordinator = setup_integration.runtime_data
    await drive(coordinator, hour=9)
    coordinator.price_forecasts.pop(NORMAL + timedelta(days=1), None)

    price = await download(hass, setup_integration)

    assert price["tomorrow"]["available"] is False
    assert price["tomorrow"]["unavailable_reason"] == REASON_NOT_BUILT
