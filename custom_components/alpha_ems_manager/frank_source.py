"""Phase 6: the read-only price-source boundary.

The impure half of the price layer. Everything here reads published state and
hands it to :mod:`price_forecast`, which does the thinking. Nothing here decides
anything, and nothing here *asks* for anything.

Read-only in the strongest available sense
------------------------------------------

Alpha EMS calls **no service at all** to obtain prices. It reads entity state,
which the price integration publishes as ordinary attributes. So the permitted
service-caller set is untouched by this phase, and "Alpha EMS cannot make the
price source fetch" is a structural fact rather than a promise: there is no call
site to misuse.

That integration owns fetching, retry, caching, publication of the next day and
the midnight rollover. This module observes the result.

Capability is established from facts, never from another entry's lifecycle
-------------------------------------------------------------------------

The Phase-5 work established the rule the hard way: inferring whether another
integration can be used from the internal setup state of its config entry
produces false negatives on every restart. So capability here is:

* a config entry is selected -- configuration;
* that entry exists -- resolvable;
* the required entities resolve **through the entity registry, by unique id**;
* their state is readable.

Resolving by unique id rather than by a guessed ``sensor.`` name matters three
ways: it isolates the *selected* entry by construction, because the entry id is
part of the unique id; it survives a user renaming the entity; and it needs no
new configuration.

Nothing here reads ``state``, ``runtime_data`` or any other config-entry internal
of the source integration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN_FRANK,
    FRANK_DEFAULT_APPLY_FEED_IN_VAT,
    FRANK_DEFAULT_FEED_IN_ADJUSTMENT,
    FRANK_KEY_CURRENT_PRICE,
    FRANK_KEY_CURRENT_RETURN_PRICE,
    FRANK_KEY_PRICES_TODAY,
    FRANK_KEY_PRICES_TOMORROW,
    FRANK_KEY_TOMORROW_AVAILABLE,
    FRANK_MARKET_TIMEZONES,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_ATTRIBUTE_UNUSABLE,
    PRICE_UNAVAILABLE_EMPTY,
    PRICE_UNAVAILABLE_ENTITY_MISSING,
    PRICE_UNAVAILABLE_ENTRY_NOT_FOUND,
    PRICE_UNAVAILABLE_NOT_CONFIGURED,
    PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE,
)
from .price_forecast import apply_vat_of, feed_in_adjustment_of

#: States that mean an entity exists but cannot be read.
_UNUSABLE_STATES = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})

#: The block fields read out of the source. Nothing outside this set leaves the
#: boundary, which is how "no unexpected material is carried through" becomes a
#: property of the code. The source is unauthenticated and holds no credential,
#: so there is nothing secret to redact -- the discipline is about shape, not
#: secrecy.
_BLOCK_FIELDS = (
    "from",
    "till",
    "market_price",
    "market_price_tax",
    "sourcing_markup_price",
    "energy_tax_price",
    "total_price_eur_kwh",
    "feed_in_price",
)


# --- capability ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrankCapability:
    """What of the price boundary was found, named even when absent.

    Every field is demonstrable. There is deliberately **no config-entry state
    field**: that concept produced the Phase-5 startup false negative, and it is
    not needed to read state an integration has already published.
    """

    entry_selected: bool = False
    entry_found: bool = False
    country: str | None = None
    today_entity_id: str | None = None
    tomorrow_entity_id: str | None = None
    availability_entity_id: str | None = None
    current_price_entity_id: str | None = None
    current_return_entity_id: str | None = None

    @property
    def market_timezone(self) -> str | None:
        """Return the market timezone for the selected country, for provenance.

        Context only. It never decides availability: the source's own signal does
        that, because publication can be early or late and a clock comparison
        would be wrong in both directions.
        """
        if self.country is None:
            return None
        return FRANK_MARKET_TIMEZONES.get(self.country)

    @property
    def usable(self) -> bool:
        """Return whether today's series can be read at all."""
        return self.entry_selected and self.entry_found and bool(self.today_entity_id)

    @property
    def unavailable_reason(self) -> str | None:
        """Return why the boundary is unusable, most specific cause first."""
        if not self.entry_selected:
            return PRICE_UNAVAILABLE_NOT_CONFIGURED
        if not self.entry_found:
            return PRICE_UNAVAILABLE_ENTRY_NOT_FOUND
        if not self.today_entity_id:
            return PRICE_UNAVAILABLE_ENTITY_MISSING
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics form, naming what was looked for."""
        return {
            "entry_selected": self.entry_selected,
            "entry_found": self.entry_found,
            "country": self.country,
            "market_timezone": self.market_timezone,
            "today_entity": self.today_entity_id,
            "tomorrow_entity": self.tomorrow_entity_id,
            "availability_entity": self.availability_entity_id,
            "current_price_entity": self.current_price_entity_id,
            "current_return_entity": self.current_return_entity_id,
            "usable": self.usable,
            "unavailable_reason": self.unavailable_reason,
            "basis": (
                "entities are resolved through the entity registry by unique id, "
                "so the selected entry is isolated by construction and a renamed "
                "entity still resolves. capability never consults the source "
                "entry's setup state, and no service is called to obtain prices"
            ),
        }


def _resolve(hass: HomeAssistant, entry_id: str, key: str, platform: str) -> str | None:
    """Return the entity id for one source key, or ``None``.

    The source builds every unique id as ``f"{entry_id}_{key}"`` and documents
    that as a stable contract, so this is a lookup rather than a guess.
    """
    registry = er.async_get(hass)
    return registry.async_get_entity_id(platform, DOMAIN_FRANK, f"{entry_id}_{key}")


def discover(hass: HomeAssistant, entry_id: str | None) -> FrankCapability:
    """Return what of the price boundary is present right now.

    Exception-free: a missing integration is a fact to report, not an error to
    raise, and this runs on every refresh.
    """
    if not entry_id:
        return FrankCapability()

    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return FrankCapability(entry_selected=True, entry_found=False)

    country = entry.data.get("country")
    return FrankCapability(
        entry_selected=True,
        entry_found=True,
        country=country if isinstance(country, str) else None,
        today_entity_id=_resolve(hass, entry_id, FRANK_KEY_PRICES_TODAY, "sensor"),
        tomorrow_entity_id=_resolve(
            hass, entry_id, FRANK_KEY_PRICES_TOMORROW, "sensor"
        ),
        availability_entity_id=_resolve(
            hass, entry_id, FRANK_KEY_TOMORROW_AVAILABLE, "binary_sensor"
        ),
        current_price_entity_id=_resolve(
            hass, entry_id, FRANK_KEY_CURRENT_PRICE, "sensor"
        ),
        current_return_entity_id=_resolve(
            hass, entry_id, FRANK_KEY_CURRENT_RETURN_PRICE, "sensor"
        ),
    )


# --- reading a day ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayRead:
    """One day's blocks as published, plus why there are none.

    ``available`` and ``reason`` are separate on purpose. The next day being
    unpublished is **normal operation** and carries a normal reason; it is not the
    same fact as an entity that should be there and is not.
    """

    blocks: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    available: bool = False
    reason: str | None = None
    reported_resolution_minutes: int | None = None
    updated_at: datetime | None = None
    last_attempt: Any = None

    @property
    def block_count(self) -> int:
        """Return how many usable blocks were published."""
        return len(self.blocks)


def _read_blocks(raw: Any) -> tuple[Mapping[str, Any], ...] | None:
    """Return the published blocks, or ``None`` when the attribute is unusable.

    Only the named fields are copied out. A block carrying extra fields keeps
    them out of Alpha EMS entirely, which is what lets a fixture prove the
    boundary rather than merely exercise the parser.
    """
    if not isinstance(raw, (list, tuple)):
        return None
    blocks: list[Mapping[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        blocks.append({key: entry[key] for key in _BLOCK_FIELDS if key in entry})
    return tuple(blocks)


def _reported_resolution(attributes: Mapping[str, Any]) -> int | None:
    """Return the source's own resolution summary, for provenance only.

    Never the mapping basis. The source derives it from the *first* block alone
    and snaps it to one of two values, so a mixed or unexpected resolution can be
    mislabelled there. The mapping measures every block itself and reports a
    disagreement.
    """
    value = attributes.get("resolution_minutes")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def read_today(hass: HomeAssistant, capability: FrankCapability) -> DayRead:
    """Return today's published blocks.

    Today is not optional. An unreadable today entity is abnormal, whatever the
    next day's publication state happens to be.
    """
    if not capability.today_entity_id:
        return DayRead(reason=PRICE_UNAVAILABLE_ENTITY_MISSING)

    state = hass.states.get(capability.today_entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return DayRead(reason=PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE)

    blocks = _read_blocks(state.attributes.get("prices"))
    if blocks is None:
        return DayRead(reason=PRICE_UNAVAILABLE_ATTRIBUTE_UNUSABLE)
    if not blocks:
        # Available and empty. A real, distinct condition -- the source says it
        # has the day and there is nothing in it -- and emphatically not a day of
        # free electricity.
        return DayRead(reason=PRICE_UNAVAILABLE_EMPTY)

    return DayRead(
        blocks=blocks,
        available=True,
        reported_resolution_minutes=_reported_resolution(state.attributes),
        updated_at=state.last_updated,
    )


def tomorrow_is_published(
    hass: HomeAssistant, capability: FrankCapability
) -> bool | None:
    """Return the source's own answer to "is the next day published yet".

    **The authoritative signal**, and the reason it is this entity rather than the
    price entity's attributes: Home Assistant drops an entity's extra attributes
    while it is unavailable, and the next-day price entity marks itself
    unavailable precisely when the day is unpublished. Its ``available``
    attribute is therefore absent in exactly the case one would want to consult
    it. This binary entity has no such override and stays readable.

    ``None`` when the entity itself cannot be read, which is not the same as
    "not published".
    """
    if not capability.availability_entity_id:
        return None
    state = hass.states.get(capability.availability_entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return None
    if state.state == STATE_ON:
        return True
    if state.state == STATE_OFF:
        return False
    return None


def read_tomorrow(hass: HomeAssistant, capability: FrankCapability) -> DayRead:
    """Return the next day's published blocks, or why there are none.

    Three outcomes that must not be confused, and the binary signal is what
    separates them:

    * signal off and the entity unavailable -- **normal**, not published yet;
    * signal on and the entity unavailable -- abnormal, the source claims to have
      the day and the entity is not carrying it;
    * signal on and an empty array -- abnormal, claimed and empty.

    Collapsing the first into either of the others would report a healthy
    installation as degraded for the part of every day before publication.
    """
    published = tomorrow_is_published(hass, capability)

    if not capability.tomorrow_entity_id:
        return DayRead(reason=PRICE_UNAVAILABLE_ENTITY_MISSING)

    state = hass.states.get(capability.tomorrow_entity_id)
    unreadable = state is None or state.state in _UNUSABLE_STATES

    if unreadable:
        if published is False:
            return DayRead(reason=PRICE_TOMORROW_NOT_PUBLISHED)
        # Either the source claims the day and the entity is not carrying it, or
        # the signal itself is unreadable. Both are source problems, and neither
        # is guessed at as "not published".
        return DayRead(reason=PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE)

    blocks = _read_blocks(state.attributes.get("prices"))
    if blocks is None:
        return DayRead(reason=PRICE_UNAVAILABLE_ATTRIBUTE_UNUSABLE)
    if not blocks:
        # Readable and empty. If the signal says it is not published, that is the
        # honest reason; otherwise the source claims a day it has not got.
        if published is False:
            return DayRead(reason=PRICE_TOMORROW_NOT_PUBLISHED)
        return DayRead(reason=PRICE_UNAVAILABLE_EMPTY)

    return DayRead(
        blocks=blocks,
        available=True,
        reported_resolution_minutes=_reported_resolution(state.attributes),
        updated_at=state.last_updated,
        last_attempt=state.attributes.get("last_attempt"),
    )


# --- options, and the two cross-checks ---------------------------------------


@dataclass(frozen=True, slots=True)
class FrankOptions:
    """The source options the export reconstruction depends on.

    Read from the source's own config entry rather than duplicated as Alpha EMS
    settings: the user configured them once, and the return-price figure on their
    dashboard is derived from them. A second copy here would drift.
    """

    adjustment: float = FRANK_DEFAULT_FEED_IN_ADJUSTMENT
    apply_vat: bool = FRANK_DEFAULT_APPLY_FEED_IN_VAT
    readable: bool = True


def read_options(hass: HomeAssistant, entry_id: str | None) -> FrankOptions:
    """Return the source's feed-in options, replicating its own parsing exactly.

    Two accessors with **different** rules, mirrored deliberately rather than
    harmonised: the adjustment goes through a numeric check that rejects strings
    and booleans, while the VAT flag is plain truthiness -- so the string
    ``"false"`` enables VAT there. Matching the source matters more than being
    tidy, because the figure the user can see is the source's.

    An entry that cannot be read at all is a different fact from an option that is
    absent: absent means the source's documented default, unreadable means unknown.
    """
    if not entry_id:
        return FrankOptions(readable=False)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return FrankOptions(readable=False)
    options = entry.options
    return FrankOptions(
        adjustment=feed_in_adjustment_of(options, FRANK_DEFAULT_FEED_IN_ADJUSTMENT),
        apply_vat=apply_vat_of(options, FRANK_DEFAULT_APPLY_FEED_IN_VAT),
        readable=True,
    )


def read_current_prices(
    hass: HomeAssistant, capability: FrankCapability
) -> tuple[float | None, float | None]:
    """Return the source's own current import and export figures.

    Used **only** to cross-check the normalised series against the two entities
    the user can see. A disagreement is recorded as evidence of contract drift; it
    never overrides the series, and it never reaches a decision.
    """

    def numeric(entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None or state.state in _UNUSABLE_STATES:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    return (
        numeric(capability.current_price_entity_id),
        numeric(capability.current_return_entity_id),
    )


def watched_entities(capability: FrankCapability) -> list[str]:
    """Return the source entities worth watching for a state change.

    So the next day's publication is picked up when it happens rather than at the
    following quarter tick. Cheap, and it needs no schedule of our own -- which is
    the point: the source's state is the signal, not a clock.
    """
    return [
        entity_id
        for entity_id in (
            capability.today_entity_id,
            capability.tomorrow_entity_id,
            capability.availability_entity_id,
        )
        if entity_id
    ]
