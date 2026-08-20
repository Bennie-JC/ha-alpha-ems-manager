"""The captured live price contract, and the synthetic shapes derived from it.

CAPTURED ARTEFACT -- DO NOT EDIT TO MAKE A TEST PASS
====================================================

    source integration : ha-frank-quarter-prices
    version            : v0.1.7
    commit             : 67f1bc3
    captured           : 2026-08-20 21:45 local, Europe/Amsterdam
    Home Assistant     : 2026.8.2
    HA timezone        : Europe/Amsterdam
    entry country      : NL

Everything below the ``CAPTURED`` banner is a verbatim transcription of state read
from a running installation. It records **what was observed**, not what this
package would like to receive. On a source upgrade it is **re-captured and
diffed** -- never adjusted until a test goes green, because a fixture edited to
match the parser stops being evidence and becomes a restatement of the parser.

Why this file exists at all
---------------------------

The previous phase shipped a defect that its own tests could not catch: the fake
was written from a human-readable transcription of what the parser expected, so
it encoded the same wrong assumption and could only ever agree. A fixture derived
from the code's own expectations tests nothing.

So the fields this package deliberately does **not** read are kept here --
``duration_minutes`` and ``per_unit`` -- and a test asserts they stay unread. A
fixture holding only what the parser wants cannot catch a parser reading the
wrong thing.

What this artefact proves, and what it does not
-----------------------------------------------

It proves this package reads *the shape that was observed*. It does **not** prove
this package reads the source, because the test suite here cannot see the source
repository -- only the live cross-check on a real installation can do that. The
distinction is recorded rather than blurred; blurring it is how the last defect
shipped.

The capture is also a partial sample: six of the day's ninety-six blocks plus the
active one. The missing ninety are **not** invented here. Synthetic days below
take their key set, ordering and precision from the capture and say so.

A value collision the live data creates
---------------------------------------

On the captured installation ``feed_in_adjustment`` and ``sourcing_markup_price``
are both ``0.01815``. So code that reached for the markup where it meant the
adjustment -- an easy slip, two small per-kWh addends sitting side by side --
would reconstruct the export price *correctly* and pass every check, including
the live cross-check.

The capture keeps the real values, because its job is to record reality. Every
**synthetic** shape below therefore uses an adjustment distinct from both
components, and a test asserts the three stay distinct.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# --- CAPTURED -----------------------------------------------------------------

#: The block key set **and order**, exactly as observed.
CAPTURED_BLOCK_KEYS: tuple[str, ...] = (
    "from",
    "till",
    "duration_minutes",
    "market_price",
    "market_price_tax",
    "sourcing_markup_price",
    "energy_tax_price",
    "total_price_eur_kwh",
    "per_unit",
)

#: Fields this package reads out of a block. The two absent from it --
#: ``duration_minutes`` and ``per_unit`` -- are ignored deliberately: the
#: interval length is *measured* from ``from``/``till``, because a reported
#: summary can disagree with the instants it summarises.
CONSUMED_BLOCK_KEYS: frozenset[str] = frozenset(
    {
        "from",
        "till",
        "market_price",
        "market_price_tax",
        "sourcing_markup_price",
        "energy_tax_price",
        "total_price_eur_kwh",
    }
)

#: First three blocks of the captured day, verbatim.
CAPTURED_TODAY_FIRST: tuple[dict[str, Any], ...] = (
    {
        "from": "2026-08-20T00:00:00+02:00",
        "till": "2026-08-20T00:15:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.185,
        "market_price_tax": 0.03885,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.35285,
        "per_unit": "KWH",
    },
    {
        "from": "2026-08-20T00:15:00+02:00",
        "till": "2026-08-20T00:30:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.17823,
        "market_price_tax": 0.03743,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.34466,
        "per_unit": "KWH",
    },
    {
        "from": "2026-08-20T00:30:00+02:00",
        "till": "2026-08-20T00:45:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.16862,
        "market_price_tax": 0.03541,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.33303,
        "per_unit": "KWH",
    },
)

#: Last three blocks of the captured day, verbatim. The final ``till`` lands on
#: the **next civil date**, which is why "one civil date per array" applies to
#: ``from`` only.
CAPTURED_TODAY_LAST: tuple[dict[str, Any], ...] = (
    {
        "from": "2026-08-20T23:15:00+02:00",
        "till": "2026-08-20T23:30:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.176,
        "market_price_tax": 0.03696,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.34196,
        "per_unit": "KWH",
    },
    {
        "from": "2026-08-20T23:30:00+02:00",
        "till": "2026-08-20T23:45:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.17093,
        "market_price_tax": 0.0359,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.33583,
        "per_unit": "KWH",
    },
    {
        "from": "2026-08-20T23:45:00+02:00",
        "till": "2026-08-21T00:00:00+02:00",
        "duration_minutes": 15,
        "market_price": 0.16504,
        "market_price_tax": 0.03466,
        "sourcing_markup_price": 0.01815,
        "energy_tax_price": 0.11085,
        "total_price_eur_kwh": 0.3287,
        "per_unit": "KWH",
    },
)

#: The block covering the capture instant, verbatim. Matching the full series
#: against the current-price sensor's interval returned exactly one block, which
#: is the live evidence that instant-based alignment is right.
CAPTURED_ACTIVE_BLOCK: dict[str, Any] = {
    "from": "2026-08-20T21:45:00+02:00",
    "till": "2026-08-20T22:00:00+02:00",
    "duration_minutes": 15,
    "market_price": 0.18915,
    "market_price_tax": 0.03972,
    "sourcing_markup_price": 0.01815,
    "energy_tax_price": 0.11085,
    "total_price_eur_kwh": 0.35787,
    "per_unit": "KWH",
}

#: Every captured block, in chronological order. Six of ninety-six plus the
#: active one -- a sample, and treated as one.
CAPTURED_BLOCKS: tuple[dict[str, Any], ...] = (
    *CAPTURED_TODAY_FIRST,
    CAPTURED_ACTIVE_BLOCK,
    *CAPTURED_TODAY_LAST,
)

#: Attribute keys observed on the day sensors.
CAPTURED_TODAY_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "prices",
    "resolution_minutes",
    "cheapest_block",
    "most_expensive_block",
    "average_price",
    "min_price",
    "max_price",
    "unit_of_measurement",
    "friendly_name",
)

#: The next-day sensor carries two more, and **only while it is available**.
CAPTURED_TOMORROW_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "prices",
    "available",
    "resolution_minutes",
    "cheapest_block",
    "most_expensive_block",
    "average_price",
    "min_price",
    "max_price",
    "last_attempt",
    "unit_of_measurement",
    "friendly_name",
)

#: State observed on each entity at capture time. The next day was **published**,
#: so the unpublished shape is not evidenced here and is not claimed to be.
CAPTURED_STATES: dict[str, Any] = {
    "sensor.frank_prices_today": "96",
    "sensor.frank_prices_tomorrow": "96",
    "binary_sensor.frank_tomorrow_prices_available": "on",
    "sensor.frank_current_price": "0.35787",
    "sensor.frank_current_return_price": "0.2073",
}

CAPTURED_RESOLUTION_MINUTES = 15
CAPTURED_BLOCK_COUNT = 96
CAPTURED_TOMORROW_LAST_ATTEMPT = "2026-08-20 21:45:03.392783+02:00"

#: The return sensor's own attributes. ``calculation_method`` is the label this
#: package mirrors verbatim as an export basis, and the reason it does: the
#: figure is *reconstructed* from configuration, and must never be presented as a
#: published price.
CAPTURED_RETURN_ATTRIBUTES: dict[str, Any] = {
    "market_price_source": "market_price",
    "market_price": 0.18915,
    "feed_in_adjustment": 0.01815,
    "apply_vat": False,
    "vat_rate": 0.21,
    "calculation_method": "market_price_plus_adjustment",
}

#: The source config entry, from its diagnostics download. Unauthenticated, so
#: there is nothing secret in it -- the redaction set upstream is empty. The
#: derived-feature options are captured to show they exist and are asymmetric
#: (15.0 against 20.0): another integration's thresholds, which is precisely why
#: this package refuses to inherit them as input.
CAPTURED_ENTRY: dict[str, Any] = {
    "title": "Frank Quarter Prices (NL)",
    "data": {"country": "NL"},
    "options": {
        "apply_feed_in_vat": False,
        "cheap_zone_margin_percent": 15.0,
        "expensive_zone_margin_percent": 20.0,
        "feed_in_adjustment": 0.01815,
        "optimal_cheap_duration_minutes": 180.0,
        "optimal_cheap_enabled": True,
        "optimal_expensive_duration_minutes": 180.0,
        "optimal_expensive_enabled": True,
    },
}

#: The two live figures the runtime cross-check compares against.
CAPTURED_CURRENT_IMPORT_EUR_KWH = 0.35787
CAPTURED_CURRENT_EXPORT_EUR_KWH = 0.2073

#: The fixed import floor: markup plus energy tax, exact on every captured
#: block. It is why a negative wholesale interval still costs money to import
#: while earning a negative amount to export.
CAPTURED_IMPORT_FLOOR_EUR_KWH = 0.129


# --- SYNTHETIC, shaped by the capture ----------------------------------------

#: Chosen distinct from both captured components so a mix-up cannot pass. The
#: live collision (adjustment == markup == 0.01815) would hide exactly that.
SYNTHETIC_FEED_IN_ADJUSTMENT = 0.00723
SYNTHETIC_SOURCING_MARKUP = 0.021
SYNTHETIC_ENERGY_TAX = 0.105

#: The source publishes five decimal places. Six is the precision of its
#: *computed* feed-in output, not of the fields it receives -- so synthetic
#: blocks round to five, and look like the source rather than like our own model.
SYNTHETIC_PRICE_DECIMALS = 5

#: VAT rate observed in the return sensor's attributes.
SYNTHETIC_VAT_RATE = 0.21


def synthetic_block(
    start_iso: str,
    till_iso: str,
    market_price: float,
    *,
    duration_minutes: int = 15,
    markup: float = SYNTHETIC_SOURCING_MARKUP,
    energy_tax: float = SYNTHETIC_ENERGY_TAX,
    market_price_tax: float | None = None,
    per_unit: str = "KWH",
) -> dict[str, Any]:
    """Return one block in the captured shape.

    Key set, key order, five-decimal precision and the offset-aware timestamp
    style all come from the capture. ``duration_minutes`` is emitted because the
    source emits it -- and a test drives it deliberately out of step with the
    instants, to prove the mapping measures rather than trusts it.

    ``market_price_tax`` defaults to the VAT relation observed on every captured
    block, and is overridable precisely so a test can break the relation and
    watch it be flagged instead of silently repaired.
    """
    market = round(market_price, SYNTHETIC_PRICE_DECIMALS)
    tax = (
        round(market * SYNTHETIC_VAT_RATE, SYNTHETIC_PRICE_DECIMALS)
        if market_price_tax is None
        else market_price_tax
    )
    total = round(market + tax + markup + energy_tax, SYNTHETIC_PRICE_DECIMALS)
    return {
        "from": start_iso,
        "till": till_iso,
        "duration_minutes": duration_minutes,
        "market_price": market,
        "market_price_tax": tax,
        "sourcing_markup_price": markup,
        "energy_tax_price": energy_tax,
        "total_price_eur_kwh": total,
        "per_unit": per_unit,
    }


def synthetic_day(
    day: date,
    tz_key: str = "Europe/Amsterdam",
    *,
    period_minutes: int = 15,
    price_at: Callable[[int, datetime], float] | None = None,
    **block_kwargs: Any,
) -> list[dict[str, Any]]:
    """Return a whole market day of blocks, midnight to midnight in ``tz_key``.

    Built by **walking instants**, not by counting to 96. That is what makes it
    usable for the spring-forward and fall-back days, where a market day holds
    92 or 100 quarter-hours -- and it is why no assertion here may assume 96.

    ``period_minutes`` covers the hourly fallback the source supports: an hourly
    block is one block carrying one rate, never four interpolated ones, because a
    price is a rate rather than a quantity.
    """
    zone = ZoneInfo(tz_key)
    # Walked in UTC and rendered back into the market zone. Wall-clock arithmetic
    # would step *through* a transition rather than across it -- adding fifteen
    # minutes to 01:45 on a spring-forward day yields a local time that does not
    # exist -- so it produces ninety-six blocks on a ninety-two-quarter day and
    # silently duplicates instants on a hundred-quarter one.
    start = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
    end = end.astimezone(UTC)
    step = timedelta(minutes=period_minutes)

    blocks: list[dict[str, Any]] = []
    moment, index = start, 0
    while moment < end:
        following = moment + step
        market = (
            0.1 + 0.01 * (index % 7) if price_at is None else price_at(index, moment)
        )
        blocks.append(
            synthetic_block(
                moment.astimezone(zone).isoformat(),
                following.astimezone(zone).isoformat(),
                market,
                duration_minutes=period_minutes,
                **block_kwargs,
            )
        )
        moment, index = following, index + 1
    return blocks
