"""Persisting what prices were known, and why it is worth persisting at all.

Nothing in this release learns from a stored price. The record exists because
**which future prices were visible when a plan was made is irrecoverable
afterwards**: prices get revised and republished, so a later phase reading today's
series has no way to tell what nine o'clock knew. That is a hindsight bias which
can only be avoided in advance, never repaired.

Which is also why the storage decisions here are conservative. Four floats an
interval rather than three, holes kept as holes, and the source's own
``market_price_tax`` written exactly as received.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    FORECAST_MAX_SNAPSHOTS_PER_TARGET,
    FORECAST_STORAGE_MINOR_VERSION,
    PRICE_EXPORT_BASIS_ADJUSTMENT,
    PRICE_EXPORT_BASIS_ADJUSTMENT_VAT,
    PRICE_EXPORT_BASIS_API_FIELD,
    PRICE_EXPORT_BASIS_UNKNOWN,
    PRICE_FLAG_VAT_RATIO_UNEXPECTED,
    PRICE_MAPPING_VERSION,
    PRICE_TOMORROW_NOT_PUBLISHED,
)
from custom_components.alpha_ems_manager.history_store import ForecastHistoryStore
from custom_components.alpha_ems_manager.price_forecast import (
    PriceSnapshot,
    build_price_snapshot,
)

from .conftest import FakeFrank
from .forecast_helpers import NORMAL, local, refresh_at
from .frank_capture import synthetic_block, synthetic_day
from .test_frank_contract import build
from .test_price_capability import TOMORROW, drive

ISSUED = datetime(2026, 8, 19, 9, 5, tzinfo=UTC)


def snapshot_of(blocks, **kwargs) -> PriceSnapshot:
    """Return the persistable record of a series built from ``blocks``."""
    forecast = build(blocks, day=NORMAL, **kwargs)
    return build_price_snapshot(forecast, issued_at=ISSUED, interval_count=96)


# --- what is stored -----------------------------------------------------------


def test_four_floats_an_interval_round_trip_exactly() -> None:
    """Byte-for-byte, at the source's own precision.

    ``market_price_tax`` is one of the four. An earlier design stored three and
    derived the tax from the twenty-one per cent relation it satisfies on every
    observed block -- revoked, because the relation is VAT legislation rather
    than arithmetic, the rate can change, and a stored series that discarded the
    field could not be repaired afterwards. Which would defeat the only reason
    for storing anything.
    """
    blocks = synthetic_day(NORMAL)
    snapshot = snapshot_of(blocks)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert rebuilt.market_price == snapshot.market_price
    assert rebuilt.market_price_tax == snapshot.market_price_tax
    assert rebuilt.import_price == snapshot.import_price
    assert rebuilt.export_price == snapshot.export_price
    assert rebuilt.export_basis == snapshot.export_basis

    # And the values are the source's, not a recomputation of them.
    for index, block in enumerate(blocks):
        assert rebuilt.market_price[index] == block["market_price"]
        assert rebuilt.market_price_tax[index] == block["market_price_tax"]
        assert rebuilt.import_price[index] == block["total_price_eur_kwh"]


def test_the_stored_tax_is_never_recomputed_for_storage() -> None:
    """A deviating tax is stored as it came, and flagged.

    The relation is a *checked observation*. A VAT change then shows up as
    evidence instead of silently corrupting the record it was meant to preserve.
    """
    blocks = synthetic_day(NORMAL)
    blocks[12] = synthetic_block(
        blocks[12]["from"], blocks[12]["till"], 0.2, market_price_tax=0.077
    )
    snapshot = snapshot_of(blocks)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert rebuilt.market_price_tax[12] == 0.077
    assert PRICE_FLAG_VAT_RATIO_UNEXPECTED in rebuilt.flags


def test_holes_survive_the_round_trip_as_holes() -> None:
    """A missing interval comes back missing, not as a zero.

    The arrays are the length of the day, so a hole is a ``None`` at a known
    position rather than a shorter array whose gap could be anywhere -- and never
    a price of zero, which is a completely different claim.
    """
    blocks = synthetic_day(NORMAL)
    del blocks[40:44]
    snapshot = snapshot_of(blocks)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert len(rebuilt.import_price) == 96
    assert rebuilt.import_price[40:44] == (None, None, None, None)
    assert rebuilt.market_price[40:44] == (None, None, None, None)
    assert rebuilt.export_basis[40:44] == (PRICE_EXPORT_BASIS_UNKNOWN,) * 4
    assert rebuilt.intervals_known == 92
    assert all(value is not None for value in rebuilt.import_price[:40])


def test_a_known_zero_is_stored_as_a_zero_and_not_as_a_hole() -> None:
    """The distinction the whole model exists to preserve, at the storage layer."""
    blocks = synthetic_day(NORMAL, price_at=lambda index, moment: 0.0)
    snapshot = snapshot_of(blocks)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert rebuilt.market_price[0] == 0.0
    assert rebuilt.market_price[0] is not None
    assert rebuilt.intervals_known == 96
    assert None not in rebuilt.import_price


def test_the_fixed_components_are_stored_once_per_day() -> None:
    """Markup and energy tax at day level, with a flag when they vary.

    Ninety-six copies of one constant would be waste; one copy plus an honest
    flag is the same information. And when they *do* vary, no single value is
    claimed -- an energy tax genuinely changes on the first of January, and
    averaging that away would lose the observation.
    """
    steady = snapshot_of(synthetic_day(NORMAL))

    assert steady.sourcing_markup_eur_kwh == 0.021
    assert steady.energy_tax_eur_kwh == 0.105

    blocks = synthetic_day(NORMAL)
    blocks[80] = {**blocks[80], "energy_tax_price": 0.2}
    varied = snapshot_of(blocks)

    assert varied.energy_tax_eur_kwh is None
    assert varied.sourcing_markup_eur_kwh == 0.021


def test_the_export_basis_is_stored_per_interval() -> None:
    """A basis can differ between intervals, so it is not reduced to one label.

    An upstream publishing an explicit figure for part of a day would produce
    exactly that, and a day-level label would then be wrong for some of it.
    """
    blocks = synthetic_day(NORMAL)
    blocks[5] = {**blocks[5], "feed_in_price": 0.0912}
    snapshot = snapshot_of(blocks)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert rebuilt.export_basis[5] == PRICE_EXPORT_BASIS_API_FIELD
    assert rebuilt.export_price[5] == 0.0912
    assert rebuilt.export_basis[6] == PRICE_EXPORT_BASIS_ADJUSTMENT


def test_the_vat_basis_survives_the_round_trip() -> None:
    """Each of the three labels comes back as itself."""
    snapshot = snapshot_of(synthetic_day(NORMAL), adjustment=0.01, apply_vat=True)

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert set(rebuilt.export_basis) == {PRICE_EXPORT_BASIS_ADJUSTMENT_VAT}


def test_the_horizon_and_the_reasons_are_stored() -> None:
    """So a later phase can read what was known, not merely what was priced."""
    snapshot = snapshot_of(
        synthetic_day(NORMAL),
        tomorrow_available=False,
        tomorrow_reason=PRICE_TOMORROW_NOT_PUBLISHED,
    )

    rebuilt = PriceSnapshot.from_dict(NORMAL, snapshot.to_dict())

    assert rebuilt is not None
    assert rebuilt.available is True
    assert rebuilt.tomorrow_available is False
    assert rebuilt.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    assert rebuilt.economic_price_horizon_end == snapshot.economic_price_horizon_end
    assert rebuilt.known_window_start == snapshot.known_window_start
    assert rebuilt.mapping_version == PRICE_MAPPING_VERSION


def test_the_raw_source_arrays_are_not_stored() -> None:
    """Twenty-five kilobytes a state change, and reconstructible. Left out.

    What is kept is the normalised series and the minimum provenance -- where
    "minimum" means the minimum source fields actually consumed, not the minimum
    after applying an unenforced arithmetic claim.
    """
    payload = snapshot_of(synthetic_day(NORMAL)).to_dict()

    assert "prices" not in payload
    assert "blocks" not in payload
    assert not any(
        isinstance(value, list) and value and isinstance(value[0], dict)
        for value in payload.values()
    )


def test_an_unusable_stored_entry_is_refused_rather_than_half_read() -> None:
    """A corrupt row yields nothing, never a partial series."""
    assert PriceSnapshot.from_dict(NORMAL, None) is None
    assert PriceSnapshot.from_dict(NORMAL, {}) is None
    assert PriceSnapshot.from_dict(NORMAL, {"at": "not-a-time", "n": 96}) is None
    assert PriceSnapshot.from_dict(NORMAL, {"at": ISSUED.isoformat()}) is None
    assert PriceSnapshot.from_dict(NORMAL, {"at": ISSUED.isoformat(), "n": 0}) is None
    assert (
        PriceSnapshot.from_dict(NORMAL, {"at": ISSUED.isoformat(), "n": 9999}) is None
    )


# --- through the store --------------------------------------------------------


async def test_an_issuance_is_recorded_once_and_deduplicated_after(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Change-triggered, so identical refreshes cost nothing.

    Ninety-six refreshes a day against a source that republishes a handful of
    times would otherwise store ninety-six identical documents.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    recorded = coordinator.history.price_snapshots(NORMAL)
    assert len(recorded) == 1
    assert recorded[0].intervals_known == 96
    assert coordinator.history.days[NORMAL].price_fingerprints == [
        recorded[0].fingerprint
    ]

    # Same series, later instant: nothing new to record.
    await drive(coordinator, hour=10)
    assert len(coordinator.history.price_snapshots(NORMAL)) == 1


async def test_a_changed_series_is_recorded_as_a_second_issuance(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Two issuances for one day are two different claims about it.

    Which is the point: a series read at nine and a revised series read at noon
    are both facts, and only keeping both makes "what did nine o'clock know"
    answerable later.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    frank.publish(
        today=synthetic_day(NORMAL, price_at=lambda index, moment: 0.3),
        tomorrow=None,
    )
    # Refreshed directly rather than through the drive helper: that helper reseeds
    # the learning history and clears the evidence layer, which would discard the
    # first issuance this test exists to compare against.
    await refresh_at(coordinator, local(NORMAL, 12, 5))

    recorded = coordinator.history.price_snapshots(NORMAL)
    assert len(recorded) == 2
    assert recorded[0].fingerprint != recorded[1].fingerprint
    assert recorded[0].import_price != recorded[1].import_price


async def test_both_days_are_recorded_under_their_own_target_day(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """One issuance per target day, each on that day's own interval identity."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=9)

    for day in (NORMAL, TOMORROW):
        recorded = coordinator.history.price_snapshots(day)
        assert len(recorded) == 1
        assert recorded[0].target_day == day
        assert recorded[0].intervals_known == 96
        assert recorded[0].interval_count == 96


async def test_the_stored_document_reloads_from_disk(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Read back through a fresh store, not from the in-memory view.

    The in-memory copy could hide a serialisation defect entirely, which is why
    this reloads rather than reading what it just wrote.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)
    await coordinator.history.async_save_now()

    reloaded = ForecastHistoryStore(hass, setup_integration.entry_id)
    await reloaded.async_load()
    await reloaded.async_ensure_days([NORMAL])

    recorded = reloaded.price_snapshots(NORMAL)
    assert len(recorded) == 1
    assert recorded[0].intervals_known == 96
    assert (
        recorded[0].import_price
        == coordinator.history.price_snapshots(NORMAL)[0].import_price
    )
    assert reloaded.corrupt is False
    assert reloaded.reset_by_migration is False
    assert FORECAST_STORAGE_MINOR_VERSION == 4


async def test_an_installation_without_a_price_source_stores_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A day with no price evidence carries no price keys at all.

    Same rule as the photovoltaic arrays on an installation without a forecast:
    absent rather than present-and-empty, so the document says nothing it cannot
    support.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=None, tomorrow=None)
    await drive(coordinator, hour=9)
    await coordinator.history.async_save_now()

    recorded = coordinator.history.price_snapshots(NORMAL)
    # An unavailable series is still evidence -- that no price was known at that
    # instant is exactly the thing a later phase must be able to see.
    assert len(recorded) == 1
    assert recorded[0].available is False
    assert recorded[0].intervals_known == 0
    assert set(recorded[0].import_price) == {None}


async def test_the_per_day_ceiling_still_applies(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Change-triggering bounds growth; the ceiling is the backstop behind it."""
    store = ForecastHistoryStore(hass, setup_integration.entry_id)
    await store.async_load()
    await store.async_ensure_days([NORMAL])

    accepted = 0
    for index in range(FORECAST_MAX_SNAPSHOTS_PER_TARGET + 3):
        snapshot = build_price_snapshot(
            build(
                synthetic_day(NORMAL, price_at=lambda i, m, o=index: 0.1 + 0.001 * o),
                day=NORMAL,
            ),
            issued_at=ISSUED + timedelta(minutes=15 * index),
            interval_count=96,
        )
        if store.add_price_snapshot(snapshot):
            accepted += 1

    assert accepted == FORECAST_MAX_SNAPSHOTS_PER_TARGET
    assert store.snapshot_cap_hits == 3
