"""Recording measured PV generation, and the promise that it changes nothing.

A PV forecast is worth nothing without something to check it against, and
generation actually observed at 13:15 last Tuesday cannot be reconstructed from
anything else. So it is recorded -- on exactly the terms the state-of-charge
array is, and for exactly the same reason.

The invariant the whole file is organised around: measured PV is additive
evidence, never a learning input. Adding it, removing it or corrupting it must
not move a single Phase-1 or Phase-2 figure. ``test_pv_independence.py`` states
the deeper form of the same rule -- if a sunny day taught the model that the house
consumes less because the panels supplied the energy, every later decision would
be built on that lie -- and it passes unmodified beside this file.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    MAX_PLAUSIBLE_PV_W,
    PV_NEGATIVE_NOISE_FLOOR_W,
)
from custom_components.alpha_ems_manager.quarter import (
    interpretable_pv_w,
    sanitize_pv_w,
)
from custom_components.alpha_ems_manager.storage import DayRecord

from .forecast_helpers import NORMAL
from .synthetic import empty_day, flat_day

TOMORROW = NORMAL + timedelta(days=1)


def record_with_pv(day: date, samples: dict[int, float]) -> DayRecord:
    """Return a fully measured day carrying PV samples."""
    record = flat_day(day, 12.0)
    for index, value in samples.items():
        record.pv[index] = value
    return record


# -- the two sanitizers, and why they differ ---------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0.0, 0.0),
        (1.0, 1.0),
        (3132.0, 3132.0),
        (MAX_PLAUSIBLE_PV_W, MAX_PLAUSIBLE_PV_W),
        # Above the ceiling PV never used to have at all.
        (MAX_PLAUSIBLE_PV_W + 1.0, None),
        (1.0e6, None),
        # Inside the noise band: an inverter's standby draw after dark.
        (-0.5, 0.0),
        (PV_NEGATIVE_NOISE_FLOOR_W, 0.0),
        # Outside it: a sign-inverted sensor, refused rather than clamped.
        (PV_NEGATIVE_NOISE_FLOOR_W - 1.0, None),
        (-3000.0, None),
    ],
)
def test_the_accumulation_sanitizer_is_swept(
    value: float | None, expected: float | None
) -> None:
    """Ceiling refuses, narrow negative band clamps, inversion refuses."""
    assert sanitize_pv_w(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0.0, 0.0),
        (3132.0, 3132.0),
        (MAX_PLAUSIBLE_PV_W + 1.0, None),
        # The one deliberate difference: *any* negative is refused here.
        (-0.5, None),
        (-3000.0, None),
    ],
)
def test_the_balance_path_rule_refuses_every_negative(
    value: float | None, expected: float | None
) -> None:
    """Stricter than the accumulation rule, and for a stated reason.

    The energy-balance path's only freshness exemption applies to a PV reading of
    *exactly* zero. Clamping a small negative up to zero would manufacture an
    exactly-zero reading nobody published and hand it that exemption, so the
    balance path refuses what the accumulator clamps.
    """
    assert interpretable_pv_w(value) == expected


def test_the_two_rules_agree_on_everything_except_the_negative_band() -> None:
    """The difference is confined to exactly one interval of the input range."""
    for value in (0.0, 1.0, 500.0, 49_999.0, 50_001.0, -3000.0, None):
        assert sanitize_pv_w(value) == interpretable_pv_w(value), value

    for value in (-0.5, -10.0, PV_NEGATIVE_NOISE_FLOOR_W):
        assert sanitize_pv_w(value) == 0.0
        assert interpretable_pv_w(value) is None


# -- the shape ---------------------------------------------------------------


def test_a_fresh_record_has_an_empty_array_of_the_right_length() -> None:
    """Sized like every other parallel array, and never zero-filled."""
    record = empty_day(NORMAL)

    assert len(record.pv) == record.interval_count == 96
    assert record.pv == [None] * 96
    assert record.pv_sample_count == 0
    assert record.pv_total_kwh == 0.0


@pytest.mark.parametrize(
    ("day", "count"),
    [(date(2026, 3, 29), 92), (NORMAL, 96), (date(2026, 10, 25), 100)],
)
def test_the_array_matches_the_real_length_of_the_civil_day(
    day: date, count: int
) -> None:
    """92, 96 or 100, like the arrays beside it."""
    record = empty_day(day)

    assert len(record.pv) == record.interval_count == count


def test_missing_is_none_and_never_zero() -> None:
    """The distinction between "produced nothing" and "nobody looked"."""
    record = record_with_pv(NORMAL, {40: 0.9, 41: 0.0})

    assert record.pv_at(40) == 0.9
    # A genuine zero is stored as a zero and counts as a sample.
    assert record.pv_at(41) == 0.0
    assert record.pv_at(42) is None
    assert record.pv_sample_count == 2


def test_an_out_of_range_index_reads_none_rather_than_raising() -> None:
    """Same contract as ``soc_at``."""
    record = empty_day(NORMAL)

    assert record.pv_at(-1) is None
    assert record.pv_at(96) is None


def test_the_total_sums_only_what_was_observed() -> None:
    """A partial total, and honestly so."""
    record = record_with_pv(NORMAL, {40: 0.9, 44: 1.1, 48: 0.25})

    assert record.pv_total_kwh == pytest.approx(2.25)
    assert record.pv_sample_count == 3


# -- serialisation -----------------------------------------------------------


def test_the_array_is_omitted_when_there_is_nothing_to_say() -> None:
    """An installation without PV must not pay for this field."""
    payload = flat_day(NORMAL, 12.0).to_dict()

    assert "p" not in payload


def test_the_array_round_trips_under_its_own_key() -> None:
    """Key ``p``, beside ``m``, ``e``, ``x`` and ``s``."""
    record = record_with_pv(NORMAL, {0: 0.0, 40: 0.9, 95: 0.125})
    payload = record.to_dict()

    assert payload["p"][40] == 0.9

    restored = DayRecord.from_dict(NORMAL, payload, "Europe/Amsterdam")

    assert restored is not None
    assert restored.pv == record.pv


def test_a_document_written_before_this_field_existed_reads_as_no_samples() -> None:
    """Absent means missing, not zero. This is the upgrade path from beta.8."""
    beta8 = {"tz": "Europe/Amsterdam", "n": 96, "m": [0.125] * 96}

    restored = DayRecord.from_dict(NORMAL, beta8, "Europe/Amsterdam")

    assert restored is not None
    assert restored.pv == [None] * 96
    assert restored.pv_sample_count == 0
    # And the day is exactly as learnable as it was before the field existed.
    assert restored.is_learned is True
    assert restored.completeness == 1.0


def test_a_damaged_array_degrades_to_missing_samples() -> None:
    """Never to plausible-looking numbers."""
    payload = {
        "tz": "Europe/Amsterdam",
        "n": 96,
        "m": [0.125] * 96,
        "p": ["nonsense", None, float("nan"), 0.9],
    }

    restored = DayRecord.from_dict(NORMAL, payload, "Europe/Amsterdam")

    assert restored is not None
    assert restored.pv[0] is None
    assert restored.pv[2] is None
    assert restored.pv[3] == 0.9


def test_a_short_array_is_padded_and_a_long_one_trimmed() -> None:
    """Sized to the day, like every array beside it."""
    record = DayRecord(day=NORMAL, tz_key="Europe/Amsterdam", interval_count=96)
    record.pv = [0.5]
    record._resize()

    assert len(record.pv) == 96
    assert record.pv[0] == 0.5
    assert record.pv[1] is None


# -- THE invariant: it changes nothing ---------------------------------------


#: Every Phase-1 and Phase-2 figure that measured PV must not be able to move.
INVARIANTS = (
    "measured_total_kwh",
    "ev_total_kwh",
    "baseline_total_kwh",
    "measured_valid_count",
    "baseline_valid_count",
    "measured_completeness",
    "completeness",
    "is_learned",
)


def figures(record: DayRecord) -> dict[str, object]:
    """Return every figure PV must not be able to move."""
    values: dict[str, object] = {name: getattr(record, name) for name in INVARIANTS}
    values["baselines"] = [
        record.baseline_at(index) for index in range(record.interval_count)
    ]
    return values


def test_adding_pv_evidence_moves_no_learning_figure() -> None:
    """The whole justification for touching the learning store at all."""
    without = flat_day(NORMAL, 12.0)
    before = figures(without)

    with_pv = flat_day(NORMAL, 12.0)
    for index in range(96):
        with_pv.pv[index] = 0.4

    assert figures(with_pv) == before


def test_a_wildly_wrong_pv_array_moves_no_learning_figure() -> None:
    """Not merely a plausible array: a corrupt one must also be inert."""
    baseline = figures(flat_day(NORMAL, 12.0))

    corrupt = flat_day(NORMAL, 12.0)
    corrupt.pv = [1.0e6] * 96

    assert figures(corrupt) == baseline


def test_removing_pv_evidence_moves_no_learning_figure() -> None:
    """Inert in both directions, so a lost array costs only PV evidence."""
    with_pv = record_with_pv(NORMAL, dict.fromkeys(range(96), 0.4))
    before = figures(with_pv)

    with_pv.pv = [None] * 96

    assert figures(with_pv) == before


def test_baseline_never_reads_pv() -> None:
    """Asserted structurally: the derivation cannot mention it.

    A value-based test would keep passing if PV were folded into the baseline
    with a coefficient that happened to be small.
    """
    import inspect

    source = inspect.getsource(DayRecord.baseline_at)

    assert "pv" not in source.lower().replace("expected", "")


# -- accumulation on the real ingest path ------------------------------------


async def test_generation_is_integrated_into_the_interval_it_belongs_to(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """Driven through the real sampling path, not by calling the store.

    4 kW held across the 10:00 quarter is 1 kWh, in chronological index 40. Held
    rather than averaged, because integration is left-handed: the most recent
    reading stands until the next one arrives, which is the only correct reading
    of a change-driven sensor.
    """
    from .conftest import PV_POWER, set_sensor
    from .test_init import START, advance, setup_at

    set_sensor(hass, PV_POWER, 4000, "W", "power")
    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, PV_POWER, 4000, "W", "power")
    await advance(hass, freezer, 960)

    record = mock_config_entry.runtime_data.store.days[START.date()]

    assert record.pv[40] == pytest.approx(1.0, rel=1e-3)
    assert record.pv_sample_count == 1
    # And the house-load figure for the same interval is untouched.
    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)


async def test_a_missing_pv_source_costs_pv_evidence_and_nothing_else(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """The independence promise, on the live path rather than in the dataclass."""
    from .conftest import PV_POWER
    from .test_init import START, advance, setup_at

    await setup_at(hass, freezer, mock_config_entry, START)
    hass.states.async_remove(PV_POWER)
    await advance(hass, freezer, 960)

    record = mock_config_entry.runtime_data.store.days[START.date()]

    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)
    assert record.measured_valid_count == 1
    assert record.pv[40] is None
    assert mock_config_entry.runtime_data.rejected_quarters == 0


async def test_an_implausible_pv_spike_records_no_sample_and_rejects_nothing(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A glitch costs the PV reading for that interval. It costs nothing else."""
    from .conftest import PV_POWER, set_sensor
    from .test_init import START, advance, setup_at

    set_sensor(hass, PV_POWER, 1.0e6, "W", "power")
    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, PV_POWER, 1.0e6, "W", "power")
    await advance(hass, freezer, 960)

    record = mock_config_entry.runtime_data.store.days[START.date()]

    assert record.measured[40] == pytest.approx(0.5, rel=1e-3)
    assert record.pv[40] is None
    assert mock_config_entry.runtime_data.rejected_quarters == 0


async def test_a_night_time_zero_is_recorded_as_zero_not_as_missing(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """After dark the panels produced nothing, and nothing is a number.

    The 60-second safety sample keeps the accumulator advancing while a
    change-driven PV template stays silent, so the interval reaches full coverage
    and stores 0.0 rather than falling below the threshold and storing ``None``.
    """
    from .conftest import PV_POWER, set_sensor
    from .test_init import START, advance, setup_at

    set_sensor(hass, PV_POWER, 0, "W", "power")
    await setup_at(hass, freezer, mock_config_entry, START)
    set_sensor(hass, PV_POWER, 0, "W", "power")
    await advance(hass, freezer, 960)

    record = mock_config_entry.runtime_data.store.days[START.date()]

    assert record.pv[40] == 0.0
    assert record.pv_sample_count == 1


async def test_a_restart_gap_drops_the_in_flight_quarter(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """A partially observed quarter cannot reach the threshold, and must not.

    Guessing at the unobserved remainder would fabricate generation across
    downtime, which is the one thing a forecast-versus-actual comparison cannot
    survive.
    """
    from .conftest import PV_POWER, set_sensor
    from .test_init import START, advance, setup_at

    set_sensor(hass, PV_POWER, 4000, "W", "power")
    # Set up seven minutes into the quarter, so it can never be fully covered.
    await setup_at(
        hass, freezer, mock_config_entry, START.replace(minute=7, second=30)
    )
    set_sensor(hass, PV_POWER, 4000, "W", "power")
    await advance(hass, freezer, 600)

    coordinator = mock_config_entry.runtime_data

    # The 10:00 quarter was entered seven and a half minutes late, so it can never
    # reach the coverage threshold -- and because nothing was accepted, no day
    # record was created at all. That is the honest outcome: not a day of zeros,
    # and not a short day that looks fully covered.
    assert START.date() not in coordinator.store.days
    assert coordinator.rejected_quarters == 1
    # PV is subject to the same coverage rule as house load, not a looser one.
    assert coordinator.open_pv_coverage is not None
    assert coordinator.open_pv_coverage < 1.0


async def test_a_pv_less_installation_builds_no_accumulator_at_all(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Nothing is integrated, and nothing is stored, when there is no array."""
    from .test_battery_entities import reconfigure

    coordinator = setup_integration.runtime_data
    reconfigure(setup_integration, hass, has_pv=False)
    coordinator.async_start()

    assert coordinator.open_pv_coverage is None
