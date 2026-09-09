"""The reason a day did not seal, visible when the return has nothing else to say.

beta.51 computed a refusal for every unsealed past day and published it. beta.52
then bounded the investment return to the accounting period -- and on an
installation whose sealed days all predate its purchase, that made ``sample_days``
zero and routed the payload into the one branch that omitted the refusals. So the
release that added the explanation and the release that needed it cancelled out.

``unsealed_day_reasons`` is reachable from production code through exactly one path,
``_roi_provenance``, which was spread into the *available* branch alone. On the
reference installation the answer therefore existed, was recomputed every refresh,
and could not be read from the entity or from the diagnostics download.

Three further things this file holds:

* **The pass itself is visible.** It opens with ``if plan is None: return 0`` and
  ``_build_battery_plan`` turns every exception into ``None``, so a site whose plan
  cannot be built sealed nothing on every refresh and published no reason for it.

* **A hole says which series and how big.** One lost quarter and a lost afternoon
  are both ``intervals_missing``, and only one of them is worth suspecting a sensor
  over.

* **Nothing historical is repaired.** An interval nobody measured cannot be
  recovered, and a day that is terminally incomplete stays unsealed. What changes is
  forward: an ungraceful restart no longer starts the open quarter from zero.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from custom_components.alpha_ems_manager.const import (
    ROI_UNAVAILABLE_BEFORE_INVESTMENT,
    SEAL_PASS_BLOCKED_NO_PLAN,
    SEAL_REFUSED_NO_DAY_RECORD,
    SEAL_TERMINAL_REFUSALS,
)

from .test_beta42_battery_return import TODAY, _sealed, invested  # noqa: F401
from .test_beta42_day_finalisation import _complete_day, sealable  # noqa: F401


def _invested_on(coordinator, day: date) -> None:
    """Point the configured purchase date at ``day``."""
    coordinator.config = replace(
        coordinator.config, battery_investment_date=day.isoformat()
    )


# ===========================================================================
# the branch that needed the reasons most
# ===========================================================================


async def test_an_unavailable_return_still_says_why_no_day_sealed(
    invested,  # noqa: F811
) -> None:
    """**The headline, and it reproduces the reference installation exactly.**

    Two sealed days, both before the purchase date, so the return has measured
    nothing about this investment and correctly reports itself unavailable. Until
    beta.54 that refusal arrived with the investment figures and no explanation --
    the block that says which days are unsealed and why was spread into the other
    branch.
    """
    _sealed(invested, days=2, benefit_eur=3.2445, end=TODAY - timedelta(days=6))
    _invested_on(invested, TODAY - timedelta(days=4))
    holed = TODAY - timedelta(days=3)
    record = _complete_day(holed)
    record.measured[7] = None
    invested.store.days[holed] = record

    payload = invested.battery_return(TODAY)

    assert payload["available"] is False
    assert payload["unavailable_reason"] == ROI_UNAVAILABLE_BEFORE_INVESTMENT
    # The provenance block is present, and it names the hole.
    assert payload["unsealed_past_days"] >= 1
    assert payload["unsealed_by_reason"]
    assert payload["unsealed_recent"]
    assert "terminally_unsealable_days" in payload
    assert "retryable_unsealed_days" in payload
    assert "lifetime_history_complete" in payload


async def test_the_available_branch_did_not_lose_anything(
    invested,  # noqa: F811
) -> None:
    """The branch that already worked keeps every key it had."""
    _sealed(invested, days=10, benefit_eur=2.0)
    _invested_on(invested, TODAY - timedelta(days=6))

    payload = invested.battery_return(TODAY)

    assert payload["available"] is True
    assert payload["unsealed_by_reason"] is not None
    assert "accounting_start_date" in payload
    assert "benefit_basis_version" in payload


# ===========================================================================
# the pass itself
# ===========================================================================


async def test_a_pass_that_never_ran_is_not_a_pass_that_sealed_nothing(
    invested,  # noqa: F811
) -> None:
    """``None`` and ``0`` are different claims, and both are real answers here.

    A fresh process has attempted nothing, which a reader must be able to tell from
    a process that attempted a pass and found nothing to move. The state is
    session-local for exactly that reason -- a stale claim inherited from a previous
    process would be worse than an absence.
    """
    _sealed(invested, days=2, benefit_eur=1.0)
    _invested_on(invested, TODAY - timedelta(days=40))

    # As a process that has not reached its first refresh.
    invested._last_seal_attempt_at = None
    invested._last_seal_count = None
    invested._last_seal_blocked_reason = None

    payload = invested.battery_return(TODAY)
    assert payload["last_seal_attempt_at"] is None
    assert payload["days_sealed_last_pass"] is None

    # And after a pass that had nothing to move, the same keys say so.
    await invested.async_seal_finalizable_days(invested.battery_plan, TODAY)
    payload = invested.battery_return(TODAY)
    assert payload["last_seal_attempt_at"] is not None
    assert payload["days_sealed_last_pass"] == 0


async def test_a_plan_that_cannot_be_built_publishes_a_blocked_seal_pass(
    invested,  # noqa: F811
) -> None:
    """**The silent path, and it could strand an installation indefinitely.**

    ``_build_battery_plan`` turns every exception into ``None`` and the pass returns
    zero on it, discarding even that. A site whose hardware facts are missing -- or
    whose plan raised -- therefore sealed nothing on every refresh for as long as the
    condition lasted, with the return unavailable and no reason anywhere that named
    the real cause.
    """
    _sealed(invested, days=2, benefit_eur=1.0)
    _invested_on(invested, TODAY - timedelta(days=40))

    sealed = await invested.async_seal_finalizable_days(None, TODAY)

    assert sealed == 0
    payload = invested.battery_return(TODAY)
    assert payload["seal_pass_blocked_reason"] == SEAL_PASS_BLOCKED_NO_PLAN
    assert payload["last_seal_attempt_at"] is not None
    assert payload["days_sealed_last_pass"] == 0


async def test_a_pass_that_ran_reports_what_it_moved(
    sealable,  # noqa: F811
) -> None:
    """And clears the blocked reason, because the pass is no longer blocked."""
    coordinator, plan, _yesterday, today = sealable

    sealed = await coordinator.async_seal_finalizable_days(plan, today)

    assert sealed >= 1
    report = coordinator._seal_pass_report()
    assert report["days_sealed_last_pass"] == sealed
    assert report["seal_pass_blocked_reason"] is None
    assert report["last_seal_attempt_at"] is not None


# ===========================================================================
# which series, and how much of it
# ===========================================================================


async def test_a_hole_names_its_series_and_counts_its_intervals(
    sealable,  # noqa: F811
) -> None:
    """**One lost quarter and a lost afternoon are different problems.**

    The gates short-circuit on the first ``None`` they find, so the reason alone
    cannot say how big the hole is -- and a reader deciding whether to suspect a
    sensor needs exactly that.
    """
    coordinator, _plan, yesterday, today = sealable
    holed = yesterday - timedelta(days=2)
    record = _complete_day(holed)
    for index in (7, 8, 9):
        record.measured[index] = None
    coordinator.store.days[holed] = record

    reasons = coordinator.unsealed_day_reasons(today)
    entry = next(
        row for row in reasons["unsealed_recent"] if row["d"] == holed.isoformat()
    )

    assert entry["missing_measured_intervals"] == 3
    assert entry["intervals"] == record.interval_count
    assert entry["terminal"] is True


async def test_a_grid_hole_is_counted_separately_from_the_house_series(
    sealable,  # noqa: F811
) -> None:
    """**They fail independently, which the single reason string hides.**

    An under-covered grid interval stores nothing and invalidates nothing, so the
    house series can be perfect while a grid leg is short -- the shape a brief meter
    outage leaves behind.
    """
    coordinator, _plan, yesterday, today = sealable
    holed = yesterday - timedelta(days=2)
    record = _complete_day(holed)
    record.grid_import[11] = None
    coordinator.store.days[holed] = record

    reasons = coordinator.unsealed_day_reasons(today)
    entry = next(
        row for row in reasons["unsealed_recent"] if row["d"] == holed.isoformat()
    )

    assert entry["missing_measured_intervals"] == 0
    assert entry["missing_grid_intervals"] == 1


async def test_a_day_the_integration_was_off_for_is_terminal(
    invested,  # noqa: F811
) -> None:
    """**It sat in neither classification set and in no published count.**

    ``unsealed_day_reasons`` iterates the records that exist, so a day with no record
    is invisible to it -- neither sealed nor unsealed. Inside the accounting period
    that absence is the whole story, and nothing can ever recover it: the only writer
    of an interval is the quarter that just closed.
    """
    assert SEAL_REFUSED_NO_DAY_RECORD in SEAL_TERMINAL_REFUSALS

    _sealed(invested, days=2, benefit_eur=1.0, end=TODAY - timedelta(days=1))
    _invested_on(invested, TODAY - timedelta(days=5))

    payload = invested.battery_return(TODAY)

    # Two records exist inside a five-day period, so three days have none at all.
    assert payload["days_with_no_record_in_accounting_period"] >= 1


# ===========================================================================
# nothing historical is repaired
# ===========================================================================


async def test_a_terminally_holed_day_stays_unsealed(
    sealable,  # noqa: F811
) -> None:
    """**And that is the correct outcome, not a residual bug.**

    An interval nobody measured cannot be recovered. Repeated passes must leave it
    exactly as it is rather than reaching for a substitute.
    """
    coordinator, plan, yesterday, today = sealable
    holed = yesterday - timedelta(days=2)
    record = _complete_day(holed)
    record.measured[7] = None
    coordinator.store.days[holed] = record

    for _ in range(3):
        await coordinator.async_seal_finalizable_days(plan, today)

    assert coordinator.store.days[holed].final_benefit is None
    usable, reason = coordinator.day_finalizable(holed, today)
    assert usable is False
    assert reason in SEAL_TERMINAL_REFUSALS


async def test_a_complete_day_still_seals_and_is_counted_once(
    sealable,  # noqa: F811
) -> None:
    """The forward-looking acceptance criterion, and idempotence with it."""
    coordinator, plan, yesterday, today = sealable

    first = await coordinator.async_seal_finalizable_days(plan, today)
    sealed_value = coordinator.store.days[yesterday].benefit_eur_final
    second = await coordinator.async_seal_finalizable_days(plan, today)

    assert first >= 1
    assert second == 0
    assert sealed_value is not None
    # Write-once: a second pass cannot restate or double it.
    assert coordinator.store.days[yesterday].benefit_eur_final == sealed_value


async def test_sealing_is_unaffected_by_the_accounting_bound(
    sealable,  # noqa: F811
) -> None:
    """The bound filters the read side and must never gate the write side.

    A day earlier than the purchase is still measured, still sealed and still on
    disk -- it is simply published under its own name rather than counted as
    recovery of a cost it predates.
    """
    coordinator, plan, yesterday, today = sealable
    coordinator.config = replace(
        coordinator.config,
        battery_investment_date=(today + timedelta(days=30)).isoformat(),
    )

    sealed = await coordinator.async_seal_finalizable_days(plan, today)

    assert sealed >= 1
    assert coordinator.store.days[yesterday].final_benefit is not None


# ===========================================================================
# the crash window
# ===========================================================================


def test_the_snapshot_schedules_no_write_of_its_own() -> None:
    """**Deliberate, and the reason is disk wear rather than tidiness.**

    The store serialises a year of quarter data -- measured at 1.31 MB on a mature
    installation -- so a per-minute write of it is roughly two gigabytes a day onto
    whatever card Home Assistant runs from. The snapshot is kept in memory and rides
    the next routine write instead, which narrows the crash window without buying it
    at that price.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    source = inspect.getsource(AlphaEmsCoordinator._snapshot_open_integration)

    assert "schedule_save" not in source
    assert "async_save_now" not in source


def test_an_outage_that_cannot_clear_coverage_is_refused_up_front() -> None:
    """**Refused now rather than adopted and dropped later.**

    A resumed quarter accrues nothing across the downtime, so its coverage is the
    seconds already banked plus the seconds left after the restart. The 24-hour
    staleness guard is far looser than that arithmetic, so a snapshot could be
    reported as successfully resumed and then lose its interval anyway -- the least
    useful pair of statements available.
    """
    import inspect

    from custom_components.alpha_ems_manager.coordinator import AlphaEmsCoordinator

    source = inspect.getsource(AlphaEmsCoordinator._restore_open_integration)

    assert "outage_exceeds_coverage" in source
    assert "MIN_QUARTER_COVERAGE" in source


@pytest.mark.parametrize("interval_count", [92, 96, 100])
def test_the_missing_counts_are_measured_against_the_real_day_length(
    interval_count: int,
) -> None:
    """92, 96 and 100 -- the civil day is not always 96 quarters long.

    A count taken against a nominal length would report a spring-forward day as
    four intervals short of a figure it never had.
    """
    from custom_components.alpha_ems_manager.storage import DayRecord

    record = DayRecord(
        day=date(2026, 3, 29), tz_key="Europe/Amsterdam", interval_count=interval_count
    )

    assert len(record.measured) == interval_count
    assert sum(1 for value in record.measured if value is None) == interval_count
