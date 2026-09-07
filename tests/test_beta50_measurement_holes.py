"""Measurement holes: the two ways a completed day was permanently unsealable.

Both defects share a shape. Evidence that exists is made unreachable, the sealing
pass reads the absence as a fact about the day, and the day is refused forever
with no way back.

* **The open quarter is discarded on every setup.** ``QuarterAccumulator`` keeps
  the in-flight quarter in memory only, and ``async_start`` builds fresh
  accumulators on every reload, options change and restart. The quarter the reload
  landed in therefore closes on the observed remainder alone, fails the coverage
  threshold, and is never written -- so ``day_finalizable`` refuses the whole civil
  day on ``intervals_missing``, permanently, because nothing ever writes
  ``measured[i]`` retroactively.

* **A past day's prices go out of reach.** Price snapshots live in month
  partitions loaded on demand, and the sealing pass is synchronous so it cannot
  load one. A non-resident partition reads exactly like a month whose prices were
  never recorded, so a day that is complete and priced is refused for a reason
  that is not true.

Both are reproduced against real timelines rather than by poking records, because
both are produced by the lifecycle and would be invisible to a fixture that built
its records directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ems_manager.const import (
    MIN_QUARTER_COVERAGE,
    QUARTER_SECONDS,
)
from custom_components.alpha_ems_manager.quarter import QuarterAccumulator

from .conftest import HOUSE_LOAD, TEST_TIMEZONE, set_sensor
from .test_persistence import advance

TZ = ZoneInfo(TEST_TIMEZONE)

#: A Wednesday with no DST transition, so the day is a plain 96 quarters and a
#: chronological index equals its wall-clock slot.
START = datetime(2026, 8, 19, 10, 0, tzinfo=TZ)
DAY = START.date()

#: 10:15-10:30, the quarter the reload lands inside.
LOST = 41

#: Five minutes past the quarter boundary. The acceptance cliff is at
#: ``900 * (1 - 0.80) = 180`` seconds, so this is unambiguously past it.
RELOAD_OFFSET_SECONDS = 300


# ===========================================================================
# the mechanism, in isolation
# ===========================================================================


def _across_a_restart(offset_seconds: int, *, resume: bool, downtime: int = 0):
    """Return the dying accumulator and the quarter the replacement closes.

    Models the coordinator exactly. ``before`` is the process about to be replaced;
    ``after`` is built the way ``async_start`` builds one, and when ``resume`` is
    set it adopts the snapshot *before* the seeding sample -- which is the ordering
    the fix turns on, because ``restore`` refuses an accumulator that has already
    started.

    ``downtime`` is the gap between the two, so a test can ask what a genuinely
    unobserved stretch does to the coverage of a resumed quarter.
    """
    start = datetime(2026, 8, 19, 10, 15, tzinfo=TZ).astimezone(UTC)
    stopped = start + timedelta(seconds=offset_seconds)

    before = QuarterAccumulator(TZ)
    moment = start
    while moment < stopped:
        before.add_sample(moment, 2000.0)
        moment = min(moment + timedelta(seconds=60), stopped)
    before.add_sample(stopped, 2000.0)

    resumed_at = stopped + timedelta(seconds=downtime)
    after = QuarterAccumulator(TZ)
    if resume:
        state = before.snapshot()
        assert state is not None
        assert after.restore(
            slot_start_utc=before.slot_start_utc,
            cursor_utc=before.cursor_utc,
            energy_wh=state["wh"],
            valid_seconds=state["s"],
        )
    after.add_sample(resumed_at, 2000.0)
    closed: list = []
    moment = resumed_at
    limit = start + timedelta(seconds=960)
    while moment < limit:
        moment = min(moment + timedelta(seconds=60), limit)
        closed.extend(after.add_sample(moment, 2000.0))
    return before, closed[0]


def test_no_energy_that_was_measured_is_thrown_away_by_a_restart() -> None:
    """**The whole defect, as one equality.**

    At a steady 2 kW a whole quarter is 0.5 kWh. The five minutes before the
    restart are 1/6 kWh and the ten minutes after are 1/3 kWh, and they sum to
    exactly the quarter. So the energy was measured, it was held in memory, and
    the restart discarded it -- which is a different statement from the quarter
    being unobserved, and it is the one that makes the loss avoidable.
    """
    before, closed = _across_a_restart(RELOAD_OFFSET_SECONDS, resume=True)

    # The witness: the dying process really did hold the first third.
    assert before.open_coverage == pytest.approx(300 / QUARTER_SECONDS)
    assert before.open_energy_kwh == pytest.approx(1 / 6, abs=1e-6)

    assert closed.day == DAY
    assert closed.slot == LOST
    assert closed.energy_kwh == pytest.approx(0.5, abs=1e-6)
    assert closed.coverage == pytest.approx(1.0)
    assert closed.accepted


def test_a_restart_with_no_snapshot_still_loses_the_quarter() -> None:
    """The other side of the asymmetry, and it is deliberate.

    Only a graceful stop writes a snapshot, so a crash, a power cut or a SIGKILL
    leaves nothing to resume and the quarter is discarded exactly as it always was.
    That is the honest outcome -- nobody observed those seconds -- and pinning it
    here stops a later change from inventing a snapshot the process never took.
    """
    _before, closed = _across_a_restart(RELOAD_OFFSET_SECONDS, resume=False)

    assert closed.coverage == pytest.approx(600 / QUARTER_SECONDS)
    assert not closed.accepted


def test_downtime_inside_a_resumed_quarter_accrues_no_energy_and_no_coverage() -> None:
    """**The guard that stops the held value being helpfully restored later.**

    Resuming carries the seconds that were observed and nothing else. A four-minute
    outage sits under ``MAX_SAMPLE_GAP_SECONDS``, so had the pre-shutdown reading
    been restored alongside the integral, ``_advance_to`` would have integrated it
    straight across the gap and credited the quarter with time nobody watched.

    Measured: five minutes observed, four minutes down, six minutes observed. The
    quarter must close at 11/15 covered and 11 minutes of energy -- not at 15.
    """
    _before, closed = _across_a_restart(300, resume=True, downtime=240)

    assert closed.coverage == pytest.approx(660 / QUARTER_SECONDS)
    assert closed.energy_kwh == pytest.approx(2000.0 * 660 / 3600 / 1000, abs=1e-6)
    assert not closed.accepted, "11 of 15 minutes is below the threshold, and says so"


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(0, True), (180, True), (181, False), (300, False)],
)
def test_a_fresh_accumulator_survives_a_restart_only_in_the_first_three_minutes(
    offset: int, accepted: bool
) -> None:
    """Characterisation, kept so the arithmetic behind the cliff stays visible.

    Whether a quarter survives is decided by where in it the restart happened,
    which is not a property anyone chose. Unchanged by the fix for a *fresh*
    accumulator -- joining late is still joining late, and only a resumed snapshot
    escapes this.
    """
    assert pytest.approx(180.0) == QUARTER_SECONDS * (1.0 - MIN_QUARTER_COVERAGE)

    accumulator = QuarterAccumulator(TZ)
    start = datetime(2026, 8, 19, 10, 15, tzinfo=TZ).astimezone(UTC)
    joined = start + timedelta(seconds=offset)
    accumulator.add_sample(joined, 2000.0)
    closed: list = []
    moment = joined
    limit = start + timedelta(seconds=900)
    while moment < limit:
        moment = min(moment + timedelta(seconds=60), limit)
        closed.extend(accumulator.add_sample(moment, 2000.0))

    assert closed[0].coverage == pytest.approx(
        (QUARTER_SECONDS - offset) / QUARTER_SECONDS
    )
    assert closed[0].accepted is accepted


# ===========================================================================
# the consequence, through the real lifecycle
# ===========================================================================


def _fill_the_rest_of_the_day(record, *, skip: set[int]) -> None:
    """Record every interval the installation was up for, leaving the hole alone.

    The reduction the test rests on: the *only* interval this day is short of is
    the one the reload was inside. Everything else was observable and, on an
    installation that had not been reloaded, would have been observed.
    """
    for index in range(record.interval_count):
        if index in skip or record.measured[index] is not None:
            continue
        record.record_interval(
            index,
            measured_kwh=0.25,
            ev_kwh=None,
            ev_expected=False,
            pv_kwh=0.0,
            grid_import_kwh=0.1,
            grid_export_kwh=0.0,
            soc_percent=50.0,
        )


async def test_a_reload_mid_quarter_does_not_cost_the_day_its_seal_forever(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry
) -> None:
    """**A reload must not make an interval missing in the first place.**

    This does not contradict ``test_one_missing_interval_withholds_the_seal``,
    which pins that a *genuinely* missing interval withholds the seal and must
    keep passing. The claim here is upstream of it: the interval was measured, and
    only the lifecycle lost it.

    Permanence is asserted rather than assumed. The day is offered to the
    predicate again on thirty successive civil days, which is what the
    quarter-hour refresh really does, and the reason never changes.
    """
    await hass.config.async_set_time_zone(TEST_TIMEZONE)
    freezer.move_to(START)
    set_sensor(hass, HOUSE_LOAD, 2000, "W", "power")

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # 10:00 -> 10:20. Closes quarter 40 and leaves 41 open with five minutes in it.
    await advance(hass, freezer, 20 * 60)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.store.days[DAY].measured[40] is not None, (
        "the day must actually be being measured, or nothing below proves anything"
    )
    assert coordinator.store.days[DAY].measured[LOST] is None, "still open"

    # One options change, five minutes into the quarter.
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data
    assert coordinator.store.days[DAY].measured[40] is not None, "the store survived"

    # 10:20 -> 10:50. Closes 41 (the holed one) and 42 (fully observed).
    await advance(hass, freezer, 30 * 60)
    record = coordinator.store.days[DAY]
    assert record.measured[42] is not None, "measurement resumed after the reload"

    # (A) THE MECHANISM. The reload discarded five observed minutes, so the
    # quarter closed at 600/900 and was never written.
    assert record.measured[LOST] is not None

    # (B) THE CONSEQUENCE, and its permanence. Every other interval is present, so
    # the only thing that can withhold this day is the hole the reload made.
    _fill_the_rest_of_the_day(record, skip=set())
    for offset in range(1, 31):
        assert (
            coordinator.day_finalizable(DAY, DAY + timedelta(days=offset))[1]
            != "intervals_missing"
        ), f"still refused {offset} days later"


# ===========================================================================
# price evidence that exists but cannot be reached
# ===========================================================================

# The sealable coordinator is defined next door and reused verbatim rather than
# rebuilt, so the two files cannot drift about what a priceable past day is.
from .test_beta42_day_finalisation import _complete_day, sealable  # noqa: E402, F401


def _evict_partition(coordinator, day) -> None:
    """Drop one month partition from memory, leaving it on disk.

    Exactly what a restart does. Partitions are loaded on demand and only today,
    tomorrow and yesterday are ever asked for, so any older month is simply absent
    from ``_partitions`` while its file sits untouched beside the others.
    """
    from custom_components.alpha_ems_manager.history_store import month_key

    key = month_key(day)
    assert key in coordinator.history._partitions, "must be resident before eviction"
    del coordinator.history._partitions[key]


async def test_a_past_day_in_an_unloaded_month_is_retryable_not_terminal(
    sealable,  # noqa: F811
) -> None:
    """**"We did not load the file" is not a fact about the day.**

    A non-resident partition and a month whose prices were never recorded produce
    the identical empty list from ``price_snapshots``, so the pass reports the same
    refusal for both. One is transient and one is terminal, and any classification
    built on the conflated reason would mark a perfectly sealable day permanently
    lost.

    The index already carries the discriminator: ``price_fingerprints`` says the
    issuance was recorded, and it lives in the always-loaded index precisely so
    this question costs no partition load.
    """
    coordinator, plan, yesterday, today = sealable

    # Put the evidence on disk, then forget it -- which is what a restart does.
    await coordinator.history.async_save_now()
    _evict_partition(coordinator, yesterday)

    assert coordinator.history.row(yesterday).price_fingerprints, (
        "the index must still say the prices were recorded"
    )

    # The refusal must name the transient cause, not the terminal one.
    assert coordinator.day_finalizable(yesterday, today) == (
        False,
        "price_partition_unloaded",
    )

    # And the pass must resolve it rather than leaving the day refused forever.
    assert await coordinator.async_seal_finalizable_days(plan, today) == 1
    assert coordinator.store.days[yesterday].final_benefit is not None


async def test_a_day_whose_prices_were_never_recorded_stays_terminal(
    sealable,  # noqa: F811
) -> None:
    """The other half, so the split above is not vacuous.

    A day with no issuance in the index can never gain one -- the recording path
    only ever offers today and tomorrow -- so it is terminal, and it must not
    provoke a partition load on every refresh for the rest of the year.
    """
    coordinator, _plan, yesterday, today = sealable
    unpriced = yesterday - timedelta(days=40)
    coordinator.store.days[unpriced] = _complete_day(unpriced)

    assert coordinator.history.row(unpriced) is None
    assert coordinator.day_finalizable(unpriced, today) == (
        False,
        "prices_never_stored",
    )


async def test_a_day_still_seals_after_its_month_has_fallen_out_of_reach(
    sealable,  # noqa: F811
) -> None:
    """**The deadline, pinned.**

    Residency is only ever ``{today, tomorrow, yesterday}``, so once the civil
    clock moves two months past a day, nothing on the refresh path will ever load
    its partition again. A day left unsealed at that point acquires a second,
    independent permanent cause that no repair of its intervals could undo.
    """
    coordinator, plan, yesterday, today = sealable

    await coordinator.history.async_save_now()
    _evict_partition(coordinator, yesterday)

    much_later = today + timedelta(days=62)
    assert await coordinator.async_seal_finalizable_days(plan, much_later) == 1
    assert coordinator.store.days[yesterday].final_benefit is not None


async def test_making_a_partition_reachable_advances_the_published_figures(
    sealable,  # noqa: F811
) -> None:
    """**The intended euro-figure movement, pinned rather than discovered.**

    Stage 0 makes days sealable that were being refused for a reason that was not
    true, so the lifetime total moves on the first refresh after upgrade. That is a
    correction, not a regression -- but it is a visible jump in a published figure
    and it must be asserted here rather than met as a surprise on a dashboard.
    """
    from dataclasses import replace

    coordinator, plan, yesterday, today = sealable
    coordinator.config = replace(
        coordinator.config,
        battery_investment_eur=11000.0,
        battery_subsidy_eur=1000.0,
        other_one_time_credit_eur=0.0,
        battery_investment_date=(yesterday - timedelta(days=30)).isoformat(),
    )

    await coordinator.history.async_save_now()
    _evict_partition(coordinator, yesterday)

    before = coordinator.battery_return(today)
    # With its only sealable day out of reach the figure has no history at all --
    # which is exactly how this installation looks today, and it is wrong.
    assert before["available"] is False
    assert before["unavailable_reason"] == "no_finalised_days"

    assert await coordinator.async_seal_finalizable_days(plan, today) == 1
    after = coordinator.battery_return(today)

    sealed = coordinator.store.days[yesterday].benefit_eur_final
    assert sealed is not None

    assert after["available"] is True
    assert after["retained_sealed_days"] == 1
    assert after["sample_days"] == 1
    assert after["sealed_through"] == yesterday.isoformat()
    assert after["cumulative_realised_benefit_eur"] == pytest.approx(sealed)
    assert yesterday not in {
        day for day, rec in coordinator.store.days.items() if rec.final_benefit is None
    }
