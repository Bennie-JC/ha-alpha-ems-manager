"""Reading the price source: capability, the publication boundary, the rollover.

The publication gap is the theme. Between market midnight and the next day's
publication a healthy installation reports **today complete and tomorrow absent**,
and treating that as a fault would mark every installation degraded for the
better part of every day. So the tests here spend most of their effort on
distinguishing normal absence from real absence.

Capability is established from demonstrable facts and never from the source
entry's setup state. Two releases ago the PV layer asked that question and
produced a live false negative on every restart, so the fixture here leaves the
source entry ``NOT_LOADED`` on purpose: if a lifecycle probe ever came back,
every test in this file would fail.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    CONF_FRANK_ENTRY_ID,
    DOMAIN_FRANK,
    FRANK_KEY_CURRENT_PRICE,
    FRANK_KEY_CURRENT_RETURN_PRICE,
    FRANK_KEY_PRICES_TODAY,
    FRANK_KEY_PRICES_TOMORROW,
    FRANK_KEY_TOMORROW_AVAILABLE,
    PRICE_CROSS_CHECK_AGREES,
    PRICE_CROSS_CHECK_DISAGREES,
    PRICE_CROSS_CHECK_NOT_COMPARABLE,
    PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED,
    PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_EMPTY,
    PRICE_UNAVAILABLE_ENTITY_MISSING,
    PRICE_UNAVAILABLE_ENTRY_NOT_FOUND,
    PRICE_UNAVAILABLE_NOT_CONFIGURED,
    PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE,
)
from custom_components.alpha_ems_manager.frank_source import (
    discover,
    read_options,
    read_today,
    read_tomorrow,
    tomorrow_is_published,
)

from .conftest import PRICE_DAY, FakeFrank
from .forecast_helpers import NORMAL, history_before, local, refresh_at, seed
from .frank_capture import (
    SYNTHETIC_FEED_IN_ADJUSTMENT,
    synthetic_block,
    synthetic_day,
)
from .live_capability import assert_charge_only_capability

TOMORROW = NORMAL + timedelta(days=1)


def test_the_price_fixture_day_matches_the_suite_canonical_day() -> None:
    """The fixture's day and the suite's day are the same day.

    ``conftest`` cannot import ``forecast_helpers`` -- that module imports
    ``conftest`` -- so the date is written twice. This is what stops the two
    copies drifting apart and quietly publishing prices for a day no test drives.
    """
    assert PRICE_DAY == NORMAL


async def drive(coordinator, *, day: date = NORMAL, hour: int = 9) -> None:
    """Give the model history and refresh at a fixed instant."""
    seed(coordinator, history_before(day))
    await refresh_at(coordinator, local(day, hour, 5))


def today_price(coordinator):
    """Return the price series for the driven day."""
    return coordinator.price_forecasts[NORMAL]


# --- the healthy morning ------------------------------------------------------


async def test_the_unpublished_next_day_is_normal_and_named_as_such(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Today complete, the next day absent, and nothing reported as broken.

    The state every installation is in between market midnight and publication.
    It must leave the source usable, today available, and the next day carrying
    the *normal* reason rather than a failure -- and it must invent nothing to
    fill the gap.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator)

    forecast = today_price(coordinator)

    assert coordinator.price_capability.usable is True
    assert coordinator.price_capability.unavailable_reason is None
    assert coordinator.frank_available is True

    assert forecast.available is True
    assert forecast.today_available is True
    assert forecast.tomorrow_available is False
    assert forecast.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    assert forecast.intervals_known == 96
    assert forecast.coverage == 1.0

    # The horizon ends at the last block's till, which lands on the next date.
    assert forecast.economic_price_horizon_end == local(TOMORROW, 0).astimezone(
        forecast.economic_price_horizon_end.tzinfo
    )

    # Nothing is fabricated beyond it: not a placeholder, not a zero.
    assert max(interval.index for interval in forecast.intervals) == 95
    assert coordinator.price_forecasts[TOMORROW].intervals == ()


async def test_reading_prices_calls_no_service_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Not "no forbidden service" -- no service.

    Prices are read from published state, so there is no call site that could be
    misused and no way for Alpha EMS to make the source fetch. That is why the
    permitted service-caller set is untouched by this phase: the guarantee is
    structural rather than a promise about which names are avoided.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", autospec=True
    ) as async_call:
        await drive(coordinator)

    assert async_call.await_count == 0
    assert today_price(coordinator).available is True
    assert_charge_only_capability()


# --- capability, from facts ----------------------------------------------------


async def test_capability_does_not_depend_on_the_source_entry_being_loaded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The defect that shipped twice, and must not ship a third time.

    The source entry is ``NOT_LOADED`` here -- the state a config entry is in
    while it is still setting up, which on a restart is exactly when Alpha EMS
    takes its first reading. Prices are readable regardless, because published
    state is published state.
    """
    coordinator = setup_integration.runtime_data
    assert frank.entry.state is not ConfigEntryState.LOADED

    await drive(coordinator)

    assert coordinator.frank_available is True
    assert today_price(coordinator).available is True


async def test_a_renamed_entity_still_resolves(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Resolution is by unique id, so the entity id is never load-bearing."""
    coordinator = setup_integration.runtime_data
    renamed = frank.rename(FRANK_KEY_PRICES_TODAY, "sensor.electricity_prices_today")
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)

    await drive(coordinator)

    assert coordinator.price_capability.today_entity_id == renamed
    assert renamed != "sensor.frank_prices_today"
    assert today_price(coordinator).available is True


async def test_two_source_entries_cannot_be_combined(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A second country's entry contributes nothing, by construction.

    The entry id is *inside* the unique id, so isolating the selected entry is
    not a filter that could be forgotten -- the other entry's entities simply do
    not resolve.
    """
    other_entry = MockConfigEntry(
        domain=DOMAIN_FRANK, title="Frank Quarter Prices (BE)", unique_id="BE"
    )
    other_entry.add_to_hass(hass)
    other = FakeFrank(hass, other_entry)
    other.register()
    other.publish(
        today=synthetic_day(NORMAL, price_at=lambda index, moment: 9.99),
        tomorrow=None,
    )

    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator)

    capability = coordinator.price_capability
    assert capability.today_entity_id == frank.entity_ids[FRANK_KEY_PRICES_TODAY]
    assert capability.today_entity_id != other.entity_ids[FRANK_KEY_PRICES_TODAY]
    assert capability.country == "NL"

    prices = {
        interval.import_price_eur_kwh for interval in today_price(coordinator).intervals
    }
    assert all(price < 1.0 for price in prices)


async def test_an_unselected_source_is_named_rather_than_assumed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """No entry selected is a configuration fact, with its own reason."""
    hass.config_entries.async_update_entry(
        setup_integration,
        data={**setup_integration.data, CONF_FRANK_ENTRY_ID: None},
    )
    # Changing the entry reloads the integration, so the coordinator under test
    # is the one that came back -- not the object captured before the change.
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    assert coordinator.frank_available is False
    assert coordinator.price_capability.entry_selected is False
    assert today_price(coordinator).today_reason == PRICE_UNAVAILABLE_NOT_CONFIGURED


async def test_a_selected_entry_that_no_longer_exists_is_provable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A stale id is distinguishable from no id at all."""
    hass.config_entries.async_update_entry(
        setup_integration,
        data={**setup_integration.data, CONF_FRANK_ENTRY_ID: "does-not-exist"},
    )
    await hass.async_block_till_done()
    coordinator = setup_integration.runtime_data
    await drive(coordinator)

    capability = coordinator.price_capability
    assert capability.entry_selected is True
    assert capability.entry_found is False
    assert today_price(coordinator).today_reason == PRICE_UNAVAILABLE_ENTRY_NOT_FOUND


async def test_a_missing_price_entity_is_reported_not_guessed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank_config_entry
) -> None:
    """An entity absent from the registry is a different fact from one unavailable.

    No fallback to a conventional entity id: guessing ``sensor.frank_prices_today``
    would read whatever happened to hold that name.
    """
    coordinator = setup_integration.runtime_data
    fake = FakeFrank(hass, frank_config_entry)
    fake.register(keys=(FRANK_KEY_PRICES_TOMORROW,))

    await drive(coordinator)

    assert coordinator.price_capability.today_entity_id is None
    assert coordinator.frank_available is False
    assert today_price(coordinator).today_reason == PRICE_UNAVAILABLE_ENTITY_MISSING


# --- today is not optional ----------------------------------------------------


async def test_an_unavailable_today_entity_is_abnormal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Whatever the next day's state is, today being unreadable is a fault."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=None, tomorrow=None)
    await drive(coordinator)

    forecast = today_price(coordinator)
    assert forecast.available is False
    assert forecast.today_reason == PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE
    # Capability is still usable: the entity exists and resolves. The *reading*
    # failed, and those are deliberately separate facts.
    assert coordinator.price_capability.usable is True


async def test_an_available_but_empty_day_is_never_a_day_of_free_electricity(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Its own reason, and no zero-priced intervals invented to fill it."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=[], tomorrow=None)
    await drive(coordinator)

    forecast = today_price(coordinator)
    assert forecast.available is False
    assert forecast.today_reason == PRICE_UNAVAILABLE_EMPTY
    assert forecast.intervals == ()
    assert forecast.intervals_known == 0


async def test_an_unusable_prices_attribute_is_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A ``prices`` attribute that is not a list is a contract change."""
    coordinator = setup_integration.runtime_data
    hass.states.async_set(
        frank.entity_ids[FRANK_KEY_PRICES_TODAY], "96", {"prices": "not-a-list"}
    )
    await drive(coordinator)

    assert today_price(coordinator).available is False


# --- the next day: the three outcomes that must stay apart --------------------


async def test_the_next_day_claimed_but_not_carried_is_abnormal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Signal on, entity unavailable: the source says it has a day it has not.

    Distinguished from the normal unpublished case *only* by the signal, which is
    why the signal is read from the binary entity rather than from the price
    entity's attributes -- those are absent exactly when the entity is unavailable.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None, tomorrow_published=True)
    await drive(coordinator)

    forecast = today_price(coordinator)
    assert forecast.today_available is True
    assert forecast.tomorrow_available is False
    assert forecast.tomorrow_reason == PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE
    assert forecast.tomorrow_reason != PRICE_TOMORROW_NOT_PUBLISHED


async def test_the_next_day_claimed_and_empty_is_not_the_unpublished_reason(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Claimed and empty is a fault; not published yet is a routine.

    Collapsing them would hide the first behind the second for half of every day.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=[], tomorrow_published=True)
    await drive(coordinator)

    assert today_price(coordinator).tomorrow_reason == PRICE_UNAVAILABLE_EMPTY


async def test_an_unreadable_availability_signal_concludes_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """An unreadable signal is not evidence that the day is unpublished."""
    coordinator = setup_integration.runtime_data
    frank.publish(
        today=synthetic_day(NORMAL), tomorrow=None, tomorrow_published=STATE_UNAVAILABLE
    )
    await drive(coordinator)

    assert tomorrow_is_published(hass, coordinator.price_capability) is None
    assert (
        today_price(coordinator).tomorrow_reason == PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE
    )


# --- state beats the clock, in both directions --------------------------------


async def test_a_late_publication_is_still_unpublished_at_three_in_the_afternoon(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Past the usual window and still absent: nothing is invented.

    Publication is normally observed between 13:00 and 14:00 market time, and it
    can be late. Concluding "it is past 14:00, so the day must exist" would
    fabricate a series out of a clock reading.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=15)

    forecast = today_price(coordinator)
    assert forecast.tomorrow_available is False
    assert forecast.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    assert coordinator.price_forecasts[TOMORROW].intervals == ()


async def test_an_early_publication_is_consumed_at_ten_in_the_morning(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Before the usual window and already present: consumed.

    The mirror of the test above, and the reason both exist: a hard-coded window
    would be wrong in one direction or the other, so there is no window.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=10)

    assert today_price(coordinator).tomorrow_available is True
    assert coordinator.price_forecasts[TOMORROW].intervals_known == 96
    assert coordinator.price_forecasts[TOMORROW].coverage == 1.0


async def test_the_publication_transition_needs_no_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Off to on, and the next ordinary refresh picks it up.

    No reload, no re-setup, and no call to induce a fetch -- the source publishes
    and the next refresh reads what is there.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=12)

    before = today_price(coordinator)
    assert before.tomorrow_available is False

    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", autospec=True
    ) as async_call:
        await refresh_at(coordinator, local(NORMAL, 14, 5))

    after = today_price(coordinator)
    assert async_call.await_count == 0
    assert after.tomorrow_available is True
    assert coordinator.price_forecasts[TOMORROW].intervals_known == 96
    assert after.economic_price_horizon_end == before.economic_price_horizon_end


async def test_the_midnight_rollover_is_the_sources_to_perform(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """After the rollover the new today is consumed and nothing is carried over.

    No old-tomorrow-becomes-new-today copy on this side, no retained stale day,
    no synthesised day after next. Both entities are re-read every refresh and
    whatever the source is carrying is what gets normalised.
    """
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=synthetic_day(TOMORROW))
    await drive(coordinator, hour=23)

    assert today_price(coordinator).tomorrow_available is True

    # The source rolls over on its own: yesterday's tomorrow is now today, and
    # the day after next has not been published.
    frank.publish(today=synthetic_day(TOMORROW), tomorrow=None)
    seed(coordinator, history_before(TOMORROW))
    await refresh_at(coordinator, local(TOMORROW, 1, 5))

    rolled = coordinator.price_forecasts[TOMORROW]
    assert rolled.today_available is True
    assert rolled.intervals_known == 96
    assert rolled.tomorrow_reason == PRICE_TOMORROW_NOT_PUBLISHED
    assert {interval.source_day for interval in rolled.intervals} == {TOMORROW}

    # The day after next is absent rather than synthesised.
    assert coordinator.price_forecasts[TOMORROW + timedelta(days=1)].intervals == ()


# --- the live cross-check -----------------------------------------------------


async def test_the_cross_check_agrees_with_the_sources_own_two_figures(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Agreement proves the reconstruction and the alignment at once.

    Against the running integration rather than against a fixture, which is the
    only check here that can fail when the *source* changes.
    """
    coordinator = setup_integration.runtime_data
    blocks = synthetic_day(NORMAL)
    active = blocks[9 * 4]  # the block covering 09:00-09:15
    frank.publish(
        today=blocks,
        tomorrow=None,
        current_price=active["total_price_eur_kwh"],
        current_return_price=round(
            active["market_price"] + SYNTHETIC_FEED_IN_ADJUSTMENT, 6
        ),
    )
    await drive(coordinator, hour=9)

    provenance = today_price(coordinator).provenance
    assert provenance.import_cross_check == PRICE_CROSS_CHECK_AGREES
    assert provenance.export_cross_check == PRICE_CROSS_CHECK_AGREES
    assert PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED not in today_price(coordinator).flags


async def test_a_drifted_reconstruction_fails_the_cross_check_loudly(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A changed source formula shows up as a named disagreement.

    Here the source reports an export figure computed with a different
    adjustment. The series is **not** overridden -- our figure stays what our
    configuration implies -- and the disagreement is recorded instead. Silently
    adopting their number would hide the drift.
    """
    coordinator = setup_integration.runtime_data
    blocks = synthetic_day(NORMAL)
    active = blocks[9 * 4]
    frank.publish(
        today=blocks,
        tomorrow=None,
        current_price=active["total_price_eur_kwh"],
        current_return_price=round(active["market_price"] + 0.25, 6),
    )
    await drive(coordinator, hour=9)

    forecast = today_price(coordinator)
    assert forecast.provenance.export_cross_check == PRICE_CROSS_CHECK_DISAGREES
    assert PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED in forecast.flags
    assert forecast.provenance.import_cross_check == PRICE_CROSS_CHECK_AGREES

    ours = forecast.interval_at(local(NORMAL, 9, 5))
    assert ours is not None
    assert ours.export_price_eur_kwh == round(
        active["market_price"] + SYNTHETIC_FEED_IN_ADJUSTMENT, 6
    )


async def test_an_unreadable_current_sensor_is_not_a_disagreement(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """Absence of evidence, recorded as such."""
    coordinator = setup_integration.runtime_data
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    provenance = today_price(coordinator).provenance
    assert provenance.import_cross_check == PRICE_CROSS_CHECK_NOT_COMPARABLE
    assert PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED not in today_price(coordinator).flags


# --- the source's own options -------------------------------------------------


async def test_the_export_series_follows_the_sources_configuration(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """The adjustment and the VAT flag are read from the source's entry.

    Not duplicated as Alpha EMS settings: the user configured them once, and the
    return-price figure on their dashboard is derived from them. A second copy
    would drift away from the number they can see.
    """
    coordinator = setup_integration.runtime_data
    frank.set_options(feed_in_adjustment=0.04, apply_feed_in_vat=True)
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)
    await drive(coordinator, hour=9)

    forecast = today_price(coordinator)
    assert forecast.provenance.feed_in_adjustment == 0.04
    assert forecast.provenance.apply_feed_in_vat is True

    interval = forecast.intervals[0]
    assert interval.market_price_eur_kwh is not None
    assert interval.export_price_eur_kwh == round(
        (interval.market_price_eur_kwh + 0.04) * 1.21, 6
    )


def test_an_absent_option_takes_the_sources_documented_default(
    hass: HomeAssistant, frank_config_entry: MockConfigEntry
) -> None:
    """Absent means the source's default, which is what its own sensor reports."""
    options = read_options(hass, frank_config_entry.entry_id)

    assert options.readable is True
    assert options.adjustment == 0.0
    assert options.apply_vat is False


def test_an_unreadable_entry_is_not_an_absent_option(hass: HomeAssistant) -> None:
    """Two different facts, and neither is a guessed adjustment.

    Absent is the source's documented default and therefore the truth about what
    the user's sensor reports. Unreadable is unknown, and produces no export
    figure at all.
    """
    assert read_options(hass, None).readable is False
    assert read_options(hass, "does-not-exist").readable is False


def test_the_option_accessors_replicate_the_sources_own_two_rules(
    hass: HomeAssistant, frank_config_entry: MockConfigEntry
) -> None:
    """Two accessors, two different tolerances, mirrored rather than harmonised.

    Both are surprising, in opposite directions, and both matter: a string
    adjustment makes the source fall back to its default, while the *string*
    ``"false"`` is truthy and switches VAT on. A tidier parser here would
    disagree with the sensor the user is looking at, which is the one thing this
    must never do.
    """
    cases = (
        ({"feed_in_adjustment": "0.05"}, 0.0),
        ({"feed_in_adjustment": True}, 0.0),
        ({"feed_in_adjustment": None}, 0.0),
        ({"feed_in_adjustment": ""}, 0.0),
        ({"feed_in_adjustment": 0.05}, 0.05),
        ({"feed_in_adjustment": 1}, 1.0),
        ({}, 0.0),
    )
    for options, expected in cases:
        hass.config_entries.async_update_entry(frank_config_entry, options=options)
        assert read_options(hass, frank_config_entry.entry_id).adjustment == expected

    vat_cases = (
        ({"apply_feed_in_vat": "false"}, True),
        ({"apply_feed_in_vat": ""}, False),
        ({"apply_feed_in_vat": 0}, False),
        ({"apply_feed_in_vat": 1}, True),
        ({"apply_feed_in_vat": True}, True),
        ({}, False),
    )
    for options, expected in vat_cases:
        hass.config_entries.async_update_entry(frank_config_entry, options=options)
        assert read_options(hass, frank_config_entry.entry_id).apply_vat is expected


# --- the boundary read directly -----------------------------------------------


def test_the_unpublished_next_day_carries_no_attributes_at_all(
    hass: HomeAssistant, frank: FakeFrank
) -> None:
    """Why the signal is a separate entity, demonstrated rather than asserted.

    Home Assistant writes an entity's attributes only while it is available, and
    the source's next-day sensor marks itself unavailable precisely when the day
    is unpublished. So its own ``available`` attribute is absent in exactly the
    case one would want to consult it -- and reading it would give ``None``
    forever.
    """
    capability = discover(hass, frank.entry.entry_id)
    state = hass.states.get(capability.tomorrow_entity_id)

    assert state.state == STATE_UNAVAILABLE
    assert "available" not in state.attributes
    assert "prices" not in state.attributes

    signal = hass.states.get(capability.availability_entity_id)
    assert signal.state == STATE_OFF
    assert tomorrow_is_published(hass, capability) is False
    assert read_tomorrow(hass, capability).reason == PRICE_TOMORROW_NOT_PUBLISHED


def test_the_boundary_reports_what_it_looked_for(
    hass: HomeAssistant, frank: FakeFrank
) -> None:
    """All five entities resolved, and named in the diagnostics form."""
    payload = discover(hass, frank.entry.entry_id).as_dict()

    assert payload["entry_selected"] is True
    assert payload["entry_found"] is True
    assert payload["country"] == "NL"
    assert payload["market_timezone"] == "Europe/Amsterdam"
    assert payload["usable"] is True
    for key in (
        "today_entity",
        "tomorrow_entity",
        "availability_entity",
        "current_price_entity",
        "current_return_entity",
    ):
        assert payload[key] is not None


def test_the_boundary_copies_no_unnamed_field_out_of_a_block(
    hass: HomeAssistant, frank: FakeFrank
) -> None:
    """A block carrying an unexpected field leaves it behind.

    The captured artefact keeps the fields Alpha EMS ignores so this can be
    checked rather than assumed. A reader that copied everything would carry
    whatever the source started publishing next, unreviewed.
    """
    block = synthetic_block(
        "2026-08-19T00:00:00+02:00", "2026-08-19T00:15:00+02:00", 0.2
    )
    frank.publish(today=[{**block, "surprise": "unread", "per_unit": "KWH"}])

    capability = discover(hass, frank.entry.entry_id)
    read = read_today(hass, capability)

    assert read.available is True
    assert "surprise" not in read.blocks[0]
    assert "per_unit" not in read.blocks[0]
    assert "duration_minutes" not in read.blocks[0]
    assert read.reported_resolution_minutes == 15


def test_the_observed_freshness_is_labelled_as_observed(
    hass: HomeAssistant, frank: FakeFrank
) -> None:
    """The source publishes no update instant, so this is when state was written.

    A different fact from "when the source last fetched", and recorded as the
    different fact it is rather than presented as the one nobody can see.
    """
    capability = discover(hass, frank.entry.entry_id)
    read = read_today(hass, capability)

    assert read.updated_at is not None
    assert read.block_count == 96


async def test_the_current_price_entities_are_read_but_never_relied_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank: FakeFrank
) -> None:
    """A missing pair of diagnostic sensors costs the cross-check and nothing else."""
    coordinator = setup_integration.runtime_data
    for key in (FRANK_KEY_CURRENT_PRICE, FRANK_KEY_CURRENT_RETURN_PRICE):
        hass.states.async_remove(frank.entity_ids[key])
    frank.publish(today=synthetic_day(NORMAL), tomorrow=None)

    await drive(coordinator, hour=9)

    forecast = today_price(coordinator)
    assert forecast.available is True
    assert forecast.intervals_known == 96
    assert forecast.provenance.import_cross_check == PRICE_CROSS_CHECK_NOT_COMPARABLE


async def test_the_availability_signal_being_absent_leaves_today_intact(
    hass: HomeAssistant, setup_integration: MockConfigEntry, frank_config_entry
) -> None:
    """Today does not depend on the next day's signal existing."""
    coordinator = setup_integration.runtime_data
    fake = FakeFrank(hass, frank_config_entry)
    fake.register(keys=(FRANK_KEY_PRICES_TODAY,))
    fake.publish_day(FRANK_KEY_PRICES_TODAY, synthetic_day(NORMAL))

    await drive(coordinator)

    forecast = today_price(coordinator)
    assert forecast.available is True
    assert forecast.tomorrow_reason == PRICE_UNAVAILABLE_ENTITY_MISSING
    assert coordinator.price_capability.availability_entity_id is None


def test_the_signal_states_map_only_to_on_and_off(
    hass: HomeAssistant, frank: FakeFrank
) -> None:
    """Anything else is unreadable rather than coerced to a false."""
    capability = discover(hass, frank.entry.entry_id)
    entity_id = frank.entity_ids[FRANK_KEY_TOMORROW_AVAILABLE]

    for state, expected in (
        (STATE_ON, True),
        (STATE_OFF, False),
        ("maybe", None),
        (STATE_UNAVAILABLE, None),
    ):
        hass.states.async_set(entity_id, state, {})
        assert tomorrow_is_published(hass, capability) is expected
