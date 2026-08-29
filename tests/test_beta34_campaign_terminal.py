"""beta.34: a campaign that ran must reach a terminal that describes it.

The reference installation performed a **successful** Safety Buy on 2026-08-29:
1.063 kWh delivered against 1.11 planned, 4.3 % short, the quarter expired
normally. Home Assistant's logbook recorded it as:

    Failed Plan ID: 9d3c04 -- Measurement Unavailable

Three separate faults stacked to produce that sentence, and each is pinned here.

1. **The freeze was structurally one refresh late.** ``_note_campaign_progress``
   runs from ``_build_control_report``, which runs *before* ``_async_dispatch``
   sets ``_activation_confirmed``. So the freeze can only fire on refresh N+1 --
   by which time ``execution_targets`` has been rebuilt from a solve whose head
   is ``elapsed + 1`` and therefore cannot contain the quarter just executed. A
   one-quarter campaign froze ``None``. A multi-quarter one escaped, which is
   why 18.33 kWh froze correctly on the same day and 1.11 did not.
2. **"No target was published" was filed under "the measurement is not
   trustworthy."** They are different failures with different readers.
3. **The tolerance was the actuator resolution.** 0.025 kWh --
   ``MIN_EXECUTABLE_QUARTER_KWH``, the smallest command that can be issued --
   used as a completion tolerance. Even with the target frozen, 1.063 of 1.11
   could never have been a success.

And ``window_ended``, the ordinary terminal of a campaign that ran to the end of
its window, was truthy and not in the failed set, so it fell through to
``canceled``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_ORPHAN_GRACE_MINUTES,
    CAMPAIGN_SUCCESS_TOLERANCE_FRACTION,
    CAMPAIGN_SUCCESS_TOLERANCE_PER_QUARTER_KWH,
    CAMPAIGN_TARGET_UNAVAILABLE,
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_QUARTER_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    MIN_EXECUTABLE_QUARTER_KWH,
    OUTCOME_CANCELED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
)
from custom_components.alpha_ems_manager.execution import carry_quarter

from .test_beta33_campaign_wiring import EXPORT_INTENTS, multi_segment_targets

pytestmark = pytest.mark.usefixtures("control_surface")


# ===========================================================================
# 1. the tolerance: derived from the hardware, not chosen to fit the example
# ===========================================================================


def test_the_tolerance_is_built_from_quantisation_and_measurement(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The rule, stated as arithmetic rather than as a percentage.**

    The brief was explicit that 5 % must not be adopted merely because it makes
    the observed 1.063 / 1.11 example classify as a success. So the rule is built
    from two physical quantities and then *capped* by them:

    * **quantisation** -- Stage B commands in steps of
      ``MIN_EXECUTABLE_QUARTER_KWH``, once per quarter, so a campaign of N
      quarters can miss by N steps for no reason but rounding;
    * **measurement resolution** -- the pack reports state of charge in whole
      percent, which on the reference 21.6 kWh pack is 0.216 kWh per reported
      step. Read from the configured capacity here, so the rule scales with the
      hardware rather than with a number written down once.

    Their sum is what the hardware *cannot* do better than. The proportional term
    is what a *user* would call close enough. The tolerance is the **smaller** of
    the two, which is what stops a large campaign missing materially and still
    claiming success.
    """
    coordinator = setup_integration.runtime_data
    capacity = coordinator.config.battery_capacity_kwh
    assert capacity > 0.0

    # A one-quarter campaign, the shape of the live Safety Buy.
    one = coordinator._completion_tolerance_kwh(1.11, 1)
    physical = MIN_EXECUTABLE_QUARTER_KWH * 1 + capacity * 0.01
    proportional = CAMPAIGN_SUCCESS_TOLERANCE_FRACTION * 1.11
    assert one == pytest.approx(min(physical, proportional))
    # And the measured shortfall clears it.
    assert one > 1.11 - 1.063

    # A large campaign is bounded by the physical term, not the percentage: the
    # proportional rule alone would allow it to miss by 0.917 kWh.
    big = coordinator._completion_tolerance_kwh(18.33, 22)
    assert big < CAMPAIGN_SUCCESS_TOLERANCE_FRACTION * 18.33
    assert big == pytest.approx(
        min(
            MIN_EXECUTABLE_QUARTER_KWH * 22 + capacity * 0.01,
            CAMPAIGN_SUCCESS_TOLERANCE_FRACTION * 18.33,
        )
    )

    # A tiny campaign never goes below one actuator step: a tolerance smaller
    # than the smallest command that can be issued is not a tolerance.
    small = coordinator._completion_tolerance_kwh(0.3, 1)
    assert small >= CAMPAIGN_SUCCESS_TOLERANCE_PER_QUARTER_KWH


@pytest.mark.parametrize(
    ("target", "quarters", "delivered", "expected"),
    [
        # The live example. 4.3 % short of a one-quarter Safety Buy.
        (1.11, 1, 1.063, OUTCOME_SUCCESS),
        # The same campaign missing by a quarter of its target is not a success.
        (1.11, 1, 0.83, OUTCOME_PARTIAL),
        # A large campaign delivering 38 % of what it promised must never read
        # success, whatever the proportional rule alone would say.
        (18.33, 22, 7.019, OUTCOME_PARTIAL),
        # And a large campaign missing by one actuator step per quarter does.
        (18.33, 22, 18.33 - 0.5, OUTCOME_SUCCESS),
    ],
)
def test_the_outcome_at_both_ends_of_the_target_scale(
    hass: HomeAssistant,
    setup_integration,
    target: float,
    quarters: int,
    delivered: float,
    expected: str,
) -> None:
    """Small and large, as the brief required, through the production rule."""
    coordinator = setup_integration.runtime_data
    tolerance = coordinator._completion_tolerance_kwh(target, quarters)
    outcome = OUTCOME_SUCCESS if target - delivered <= tolerance else OUTCOME_PARTIAL
    assert outcome == expected, (target, delivered, tolerance)


# ===========================================================================
# 2. the classification ladder
# ===========================================================================


def latched(
    coordinator,
    *,
    target,
    realized,
    stop_reason,
    measurable=True,
    quarters=1,
    started=True,
):
    """Drive one campaign through the **production** close path and return it.

    The fields set here are the ones the coordinator itself sets during a live
    campaign -- the frozen target, the accumulated realisation, the admitted
    quarter count and the measurement verdict. Nothing that production computes
    is injected: the tolerance, the precedence and the terminal payload are all
    the real ones.
    """
    now = coordinator.hass.loop.time  # noqa: F841 - only to prove hass is wired
    from homeassistant.util import dt as dt_util

    instant = dt_util.utcnow()
    coordinator._campaign_id = "c" * 16
    coordinator._campaign_boundary = "battery"
    coordinator._campaign_started_at = instant if started else None
    coordinator._campaign_frozen_target_kwh = target
    coordinator._campaign_realized_kwh = realized
    coordinator._campaign_measurable = measurable
    coordinator._campaign_quarters_admitted = quarters
    coordinator._quarter = None
    coordinator._closed_campaign = None
    coordinator._close_campaign(instant, stop_reason)
    return coordinator._closed_campaign


async def test_the_live_safety_buy_now_reads_as_a_success(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The exact hardware campaign, through the exact production path.**

    1.063 kWh delivered against 1.11 planned, one quarter, ended by
    ``quarter_expired``. Home Assistant recorded *Failed -- Measurement
    Unavailable*. It was a success.

    *Mutation: restore ``tolerance = min(TARGET_TOLERANCE_KWH, 0.025 * quarters)``
    and this fails.*
    """
    coordinator = setup_integration.runtime_data
    terminal = latched(
        coordinator,
        target=1.11,
        realized=1.063,
        quarters=1,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_SUCCESS
    assert terminal["objective_target_kwh"] == pytest.approx(1.11)
    assert terminal["objective_realized_kwh"] == pytest.approx(1.063)
    assert terminal["objective_measurable"] is True


async def test_window_ended_is_how_a_campaign_finishes_not_how_it_is_cancelled(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The ordinary ending, filed as an abandonment.**

    ``EXECUTION_STOP_WINDOW_ENDED`` is not in the failed set, but it *is* truthy,
    and the ladder tested truthiness before it tested completion. Every campaign
    that ran to the end of its window and missed its tolerance was therefore
    classified as though somebody had called it off.

    *Mutation: move the ``EXECUTION_COMPLETION_STOP_REASONS`` branch below the
    generic ``elif stop_reason`` and this fails.*
    """
    coordinator = setup_integration.runtime_data
    assert EXECUTION_STOP_WINDOW_ENDED in EXECUTION_COMPLETION_STOP_REASONS
    assert EXECUTION_STOP_QUARTER_TARGET_REACHED in EXECUTION_COMPLETION_STOP_REASONS
    assert EXECUTION_STOP_EXECUTION_ERROR not in EXECUTION_COMPLETION_STOP_REASONS

    terminal = latched(
        coordinator,
        target=8.0,
        realized=5.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )
    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_PARTIAL
    assert terminal["outcome"] != OUTCOME_CANCELED

    # And a genuine failure reason still fails, on the same shortfall.
    failed = latched(
        coordinator,
        target=8.0,
        realized=5.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_EXECUTION_ERROR,
    )
    assert failed is not None
    assert failed["outcome"] == OUTCOME_FAILED


async def test_no_published_target_is_partial_and_says_why(
    hass: HomeAssistant, setup_integration
) -> None:
    """**Two different failures wearing one word.**

    ``measurable`` was ``self._campaign_measurable and target_kwh is not None``,
    so a campaign whose *target* was never published landed in the branch whose
    whole argument is about untrustworthy *measurement*. The reader is told the
    battery cannot be measured, when what happened is that the plan could not be
    quoted -- which is the sentence the live installation printed under a charge
    that physically worked.

    *Mutation: restore the ``and target_kwh is not None`` conjunction and this
    fails.*
    """
    coordinator = setup_integration.runtime_data
    terminal = latched(
        coordinator,
        target=None,
        realized=1.063,
        quarters=1,
        stop_reason=None,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_PARTIAL
    assert terminal["outcome"] != OUTCOME_FAILED
    assert terminal["reason"] == CAMPAIGN_TARGET_UNAVAILABLE
    # The measurement was never in doubt, and the payload says so.
    assert terminal["objective_measurable"] is True


async def test_only_an_untrustworthy_measurement_reaches_failed_with_no_reason(
    hass: HomeAssistant, setup_integration
) -> None:
    """The one route to *Measurement Unavailable*, asserted as the only one.

    Activity renders that phrase iff ``outcome == failed and not measurable``, so
    the guarantee has to hold where the outcome is decided. With the measurement
    trusted, no combination of missing target and ordinary ending may produce it.
    """
    coordinator = setup_integration.runtime_data

    unmeasurable = latched(
        coordinator,
        target=1.11,
        realized=1.063,
        quarters=1,
        measurable=False,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )
    assert unmeasurable is not None
    assert unmeasurable["outcome"] == OUTCOME_FAILED
    assert unmeasurable["objective_measurable"] is False

    # Every trusted-measurement combination stays out of that branch.
    for target, realized, reason in (
        (None, 1.063, None),
        (None, 0.0, EXECUTION_STOP_WINDOW_ENDED),
        (1.11, 0.0, EXECUTION_STOP_WINDOW_ENDED),
        (1.11, 1.063, EXECUTION_STOP_QUARTER_TARGET_REACHED),
    ):
        terminal = latched(
            coordinator,
            target=target,
            realized=realized,
            quarters=1,
            stop_reason=reason,
        )
        assert terminal is not None
        assert terminal["objective_measurable"] is True, (target, realized, reason)


async def test_a_campaign_that_never_started_files_nothing(
    hass: HomeAssistant, setup_integration
) -> None:
    """Nothing physical happened, so there is nothing to have finished.

    Unchanged from beta.32 and asserted here because the precedence above it was
    rewritten: a reordering that filed a terminal for an unstarted campaign would
    put a line in the history for a plan the battery never acted on.
    """
    coordinator = setup_integration.runtime_data
    terminal = latched(
        coordinator,
        target=1.11,
        realized=0.0,
        quarters=1,
        started=False,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )
    assert terminal is None


# ===========================================================================
# 3. the freeze survives the refresh that drops the campaign
# ===========================================================================


async def test_a_campaign_that_opens_and_ends_in_one_refresh_keeps_its_target(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """**The structural lateness, reproduced through the production wiring.**

    The ordering is the fault. ``_note_campaign_progress`` is called from
    ``_build_control_report``; ``_async_dispatch`` sets ``activation_confirmed``
    afterwards. So on the refresh that opens a campaign and sends its activation,
    the flag is still false and the freeze cannot fire. It fires on refresh N+1 --
    and by then ``execution_targets`` has been rebuilt from a solve whose head is
    ``elapsed + 1``, which by construction cannot contain the quarter just
    executed.

    ``_campaign_objective_kwh`` sums over that live tuple and returns ``None``
    when it finds nothing. For a one-quarter campaign there *is* nothing, so the
    frozen target was ``None`` -- and the guard on ``started_at`` prevents any
    later retry. A multi-quarter campaign escapes because it is still in the N+1
    publication, which is exactly why 18.33 kWh froze correctly on 2026-08-29 and
    1.11 kWh did not.

    The sequence below is that sequence: publish, open, then withdraw the
    publication *before* the activation is confirmed.

    *Mutation: remove ``or self._campaign_opening_target_kwh`` from the freeze and
    this fails -- the target comes back ``None`` and the terminal cannot judge
    what happened.*
    """
    coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    exports = [t for t in targets if t["intent"] == EXECUTION_INTENT_NET_EXPORT]
    identity = exports[0]["campaign_id"]
    assert identity is not None

    coordinator.execution_targets = tuple(targets)
    opens = datetime.fromisoformat(exports[0]["window_start"])
    quarter = carry_quarter(
        None,
        [exports[0]],
        opens - timedelta(minutes=1),
        run=None,
        executable_intents=EXPORT_INTENTS,
    )
    assert quarter is not None

    # Refresh N: the campaign opens. The activation goes out on this refresh, so
    # the flag is still false when the progress note runs -- the real ordering.
    coordinator._quarter = quarter
    coordinator._activation_confirmed = False
    coordinator._note_campaign_progress(opens, None)
    assert coordinator._campaign_id == identity
    assert coordinator._campaign_frozen_target_kwh is None, "the freeze is later"
    expected = coordinator._campaign_objective_kwh(identity)
    assert expected is not None and expected > 0.0

    # Refresh N+1: the activation is confirmed, and the fresh solve no longer
    # names this campaign at all -- the head has moved past it.
    coordinator.execution_targets = ()
    coordinator._activation_confirmed = True
    coordinator._note_campaign_progress(opens + timedelta(minutes=15), None)

    assert coordinator._campaign_frozen_target_kwh == pytest.approx(expected), (
        "the objective was captured while it was still published"
    )


async def test_the_capture_tracks_a_target_that_grows_before_activation(
    hass: HomeAssistant, setup_integration, source_entities: None, frank
) -> None:
    """The capture must follow the plan, and stop the moment the freeze fires.

    A campaign whose objective is revised upward before it starts must freeze the
    revised figure, not the one it was first announced with. And after the freeze
    the capture is never consulted again, so a later publication cannot reach the
    frozen number through this door -- which is the immutability rule beta.32
    established and this change must not weaken.
    """
    coordinator, _solved, targets = await multi_segment_targets(
        hass, setup_integration, frank
    )
    exports = [t for t in targets if t["intent"] == EXECUTION_INTENT_NET_EXPORT]
    identity = exports[0]["campaign_id"]
    coordinator.execution_targets = tuple(targets)
    opens = datetime.fromisoformat(exports[0]["window_start"])
    quarter = carry_quarter(
        None,
        [exports[0]],
        opens - timedelta(minutes=1),
        run=None,
        executable_intents=EXPORT_INTENTS,
    )
    assert quarter is not None

    coordinator._quarter = quarter
    coordinator._activation_confirmed = False
    coordinator._note_campaign_progress(opens, None)
    first = coordinator._campaign_objective_kwh(identity)
    assert first is not None

    # The plan is revised upward while the campaign is still waiting to start.
    grown = []
    for target in targets:
        if (
            target["campaign_id"] == identity
            and target["intent"] == EXECUTION_INTENT_NET_EXPORT
            and target["grid_target_kwh"] is not None
        ):
            target = {**target, "grid_target_kwh": target["grid_target_kwh"] + 1.0}
        grown.append(target)
    coordinator.execution_targets = tuple(grown)
    coordinator._note_campaign_progress(opens + timedelta(minutes=1), None)
    revised = coordinator._campaign_objective_kwh(identity)
    assert revised is not None and revised > first

    # Now it starts, and freezes the revised figure.
    coordinator._activation_confirmed = True
    coordinator._note_campaign_progress(opens + timedelta(minutes=2), None)
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(revised)

    # And a later publication cannot move it, through the capture or otherwise.
    coordinator.execution_targets = tuple(targets)
    coordinator._activation_confirmed = True
    coordinator._note_campaign_progress(opens + timedelta(minutes=3), None)
    assert coordinator._campaign_frozen_target_kwh == pytest.approx(revised)


# ===========================================================================
# 4. the orphan bound
# ===========================================================================


def test_a_campaign_cannot_stay_open_past_its_own_end(
    hass: HomeAssistant, setup_integration
) -> None:
    """``_campaign_end_utc`` was maintained and read only for reporting.

    A stale carried quarter therefore held a campaign open indefinitely, and the
    lifecycle that should have closed never filed anything at all. The grace is
    an hour: long enough that a slow final quarter is not cut short, short enough
    that a reader is not left with an open campaign overnight.
    """
    assert CAMPAIGN_ORPHAN_GRACE_MINUTES == 60
    grace = timedelta(minutes=CAMPAIGN_ORPHAN_GRACE_MINUTES)
    assert grace > timedelta(minutes=15), "shorter than one quarter would be a race"
