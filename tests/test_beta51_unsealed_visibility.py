"""Why a day did not seal, published rather than discarded.

beta.50 stopped the integration manufacturing measurement holes and let a past day
reach its own prices. What it did not do is say anything about the days already
holed: the sealing pass computed a reason for every refusal and threw it away, so an
operator watching a lifetime figure sit still had no way to learn why without reading
the source.

Three claims here, and they are separable:

* **The reason survives.** Every retained past day without a sealed figure is counted
  under a named refusal, and the counts are bounded -- a year of holed days must not
  become a year of attribute entries.

* **Terminal and retryable are different.** A day missing an interval can never gain
  one; a day whose price partition is merely not loaded gets it on the next pass.
  Reporting both as "unsealed" would be true and useless.

* **A completeness claim cannot outrun its evidence.** ``lifetime_history_complete``
  compared two endpoints and never looked inside the span, so it read ``true`` over a
  three-week hole. It is scoped to the accounting period on purpose: a hole before the
  battery was bought is published, but it cannot invalidate a claim about a period it
  falls outside of.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from custom_components.alpha_ems_manager.history_store import month_key

from .test_beta42_battery_return import TODAY, _sealed, invested  # noqa: F401
from .test_beta42_day_finalisation import _complete_day, sealable  # noqa: F401

# ===========================================================================
# the reason is kept
# ===========================================================================


async def test_a_day_that_never_seals_says_why_rather_than_being_skipped(
    sealable,  # noqa: F811
) -> None:
    """**The refusal was computed and discarded on every refresh.**

    The pass has always known exactly why each day was passed over -- it asks the
    predicate and then drops the answer on the floor. Publishing it is the whole of
    this change: a figure that stops moving must be able to say what it is waiting
    for.
    """
    coordinator, _plan, yesterday, today = sealable
    holed = yesterday - timedelta(days=2)
    record = _complete_day(holed)
    record.measured[7] = None
    coordinator.store.days[holed] = record

    reasons = coordinator.unsealed_day_reasons(today)

    assert reasons["unsealed_by_reason"]["intervals_missing"] >= 1
    assert reasons["first_unsealed_day"] is not None
    assert holed.isoformat() in {entry["d"] for entry in reasons["unsealed_recent"]}
    assert {entry["r"] for entry in reasons["unsealed_recent"]} <= {
        # A day can be unsealed simply because no pass has run since it completed.
        # That is a real state and a retryable one, and it is reported as what it is
        # rather than being folded into a refusal it did not suffer.
        "finalizable",
        "intervals_missing",
        "load_boundary_incomplete",
        "prices_never_stored",
        "price_partition_unloaded",
        "prices_lost",
        "no_day_record",
    }


async def test_the_published_reasons_are_bounded(sealable) -> None:  # noqa: F811
    """A year of holed days must not become a year of attribute entries.

    Home Assistant re-reads attributes on every state update, so an unbounded list
    here would be a performance defect shipped alongside a correctness fix. The
    counts stay complete; only the per-day detail is trimmed, and to the week an
    operator can actually act on.
    """
    coordinator, _plan, yesterday, today = sealable
    for offset in range(2, 40):
        day = yesterday - timedelta(days=offset)
        record = _complete_day(day)
        record.measured[3] = None
        coordinator.store.days[day] = record

    reasons = coordinator.unsealed_day_reasons(today)

    assert len(reasons["unsealed_recent"]) <= 7
    assert reasons["unsealed_past_days"] >= 38
    assert sum(reasons["unsealed_by_reason"].values()) == reasons["unsealed_past_days"]


async def test_a_missing_interval_is_terminal_and_an_unloaded_month_is_not(
    sealable,  # noqa: F811
) -> None:
    """**The distinction beta.50 made available, now acted on.**

    Nothing ever writes ``measured[i]`` retroactively, so a day short an interval is
    finished. A day whose month partition simply is not resident gets it on the next
    pass. Filing the second as permanently lost would be the same class of error the
    conflated price refusal was.
    """
    coordinator, _plan, yesterday, today = sealable
    holed = yesterday - timedelta(days=3)
    record = _complete_day(holed)
    record.measured[11] = None
    coordinator.store.days[holed] = record

    await coordinator.history.async_save_now()
    from custom_components.alpha_ems_manager.history_store import month_key

    del coordinator.history._partitions[month_key(yesterday)]

    reasons = coordinator.unsealed_day_reasons(today)

    assert reasons["terminally_unsealable_days"] >= 1
    assert reasons["retryable_unsealed_days"] >= 1
    assert (
        reasons["terminally_unsealable_days"] + reasons["retryable_unsealed_days"]
        == reasons["unsealed_past_days"]
    )


# ===========================================================================
# a completeness claim cannot outrun its evidence
# ===========================================================================


async def test_a_hole_inside_the_accounting_period_makes_history_incomplete(
    invested,  # noqa: F811
) -> None:
    """**The published falsehood this release removes.**

    The old test compared the first day of evidence against the purchase date and
    nothing else, so it could not see a gap in the middle. On the installation that
    prompted this work it read ``true`` while not one day inside the accounting period
    was sealed at all.
    """
    _sealed(invested, days=10, benefit_eur=1.0)
    invested.config = replace(
        invested.config,
        battery_investment_date=(TODAY - timedelta(days=10)).isoformat(),
    )
    gap = TODAY - timedelta(days=5)
    record = _complete_day(gap)
    record.measured[4] = None
    invested.store.days[gap] = record

    payload = invested.battery_return(TODAY)

    assert payload["unsealed_days_in_accounting_period"] >= 1
    assert payload["lifetime_history_complete"] is False
    assert payload["lifetime_history_incomplete_reason"] == "days_missing_inside_period"


async def test_a_hole_before_the_purchase_is_published_but_does_not_invalidate(
    invested,  # noqa: F811
) -> None:
    """**Scoped deliberately, and the count is published either way.**

    Anyone who ran this integration before buying the battery has holed days that
    cannot reach a single published figure -- Stage 3 gives the accounting a hard
    lower bound at the purchase date. Letting those days pin the flag false forever
    would make it useless precisely on the installations that have the most history.

    So the flag is scoped and ``unresolved_holes_total`` carries the rest. Nothing is
    hidden; it simply is not counted against a period it falls outside of.
    """
    _sealed(invested, days=10, benefit_eur=1.0)
    invested.config = replace(
        invested.config,
        battery_investment_date=(TODAY - timedelta(days=5)).isoformat(),
    )
    old_hole = TODAY - timedelta(days=40)
    record = _complete_day(old_hole)
    record.measured[9] = None
    invested.store.days[old_hole] = record

    payload = invested.battery_return(TODAY)

    assert payload["unresolved_holes_total"] >= 1
    assert payload["unsealed_days_in_accounting_period"] == 0
    assert payload["lifetime_history_complete"] is True
    assert payload["lifetime_history_incomplete_reason"] is None


# ===========================================================================
# the clause the docstring already claimed
# ===========================================================================


async def test_a_day_with_one_unpriced_interval_is_refused_not_sealed_short(
    sealable,  # noqa: F811
) -> None:
    """**A lifetime sum of quietly short days is biased, not noisy.**

    The predicate promised in prose that nothing was skipped for want of a price and
    never checked it: it confirmed only that a price *object* existed, while the lists
    inside it may carry gaps. An interval the realised window cannot value is dropped
    silently, always in the same direction.
    """
    coordinator, _plan, yesterday, today = sealable
    assert coordinator.day_finalizable(yesterday, today) == (True, "finalizable")

    # Holes are holes: the stored arrays are the length of the day and carry None
    # where an interval was never published. Re-store the day with one such gap.
    snapshot = coordinator.history.latest_price_snapshot(yesterday)
    holed = replace(
        snapshot,
        import_price=tuple(
            None if index == 12 else value
            for index, value in enumerate(snapshot.import_price)
        ),
    )
    partition = coordinator.history._partitions[month_key(yesterday)]
    partition.price_snapshots[yesterday] = [holed]

    assert coordinator.day_finalizable(yesterday, today) == (False, "price_hole")


async def test_a_day_missing_a_grid_interval_is_refused_not_sealed_short(
    sealable,  # noqa: F811
) -> None:
    """The same shrink through the other leg.

    An interval with neither grid figure is skipped by the realised window entirely,
    so the day still produces a number -- one that covers fewer intervals than it
    claims to.
    """
    coordinator, _plan, yesterday, today = sealable
    record = coordinator.store.days[yesterday]
    record.grid_import[20] = None
    record.grid_export[20] = None

    assert coordinator.day_finalizable(yesterday, today) == (
        False,
        "grid_flows_incomplete",
    )


# ===========================================================================
# the memo key that never tracked a retained seal
# ===========================================================================


async def test_the_price_basis_is_recomputed_when_a_retained_day_seals(
    sealable,  # noqa: F811
) -> None:
    """**The cache key tracked the wrong count.**

    It keyed on the *evicted* sealed-day total, which moves only at the 365-day
    retention edge, while the body it guards walks the retained days. So an ordinary
    seal -- the common case, and the one the comment claimed it tracked -- left a
    stale basis served until the civil day changed.
    """
    coordinator, plan, _yesterday, today = sealable

    before = coordinator._roi_price_basis(today)
    assert coordinator._roi_basis_cache is not None
    assert await coordinator.async_seal_finalizable_days(plan, today) == 1

    cached_key = coordinator._roi_basis_cache[0]
    coordinator._roi_price_basis(today)
    assert coordinator._roi_basis_cache[0] != cached_key, (
        "sealing a retained day must invalidate the basis cache"
    )
    assert before is not None
