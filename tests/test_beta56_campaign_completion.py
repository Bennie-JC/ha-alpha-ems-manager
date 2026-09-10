"""beta.56: a charge that physically finished, reported as an abandoned one.

The live 2026-09-10 charge campaign ran from 09:00 to its own planned end at
16:30, took 15.648 kWh into a 21.6 kWh pack, and finished at 100 %. Home
Assistant recorded:

    Campagne geannuleerd — 14.22 kWh / 15.63 kWh        (canceled / plan_replaced)

Every figure in that line is correct. The sentence is wrong three times over, and
each fault is independent:

1. **The reason was invented.** ``_decide`` compared the ended run's *minted run
   id* against the selected publication's *plan id* -- two different identity
   namespaces -- so the inequality was structurally always true and
   ``plan_replaced`` was asserted with a withdrawal basis claiming "a different
   run was running". The carry machine had watched the run end and recorded
   ``window_ended``, which is what actually happened.
2. **The outcome had no rung for it.** ``plan_replaced`` lives in
   ``EXECUTION_WITHDRAWAL_STOP_REASONS``, which ``_close_campaign``'s ladder never
   branched on, so it fell to the generic ``elif stop_reason`` and became
   ``canceled``. The ``superseded`` remap below required ``partial``, which a
   withdrawal with a known target could never reach -- so beta.42's word was dead
   for exactly the case it was written for. ``_recovered_outcome`` had the rung all
   along, so identical evidence closed ``canceled`` live and ``superseded`` after
   a restart, under a docstring claiming the two agreed.
3. **The physical evidence never left diagnostics.** The shortfall *is* the
   absorbed production, by construction: the target sums each row's uncapped
   allowance while the realised figure sums ``min(measured, allowance)`` per row,
   with no redistribution. 15.648 measured less 14.217 objective = 1.431 kWh of free
   sun, against a 1.413 kWh "shortfall". A reader had no way to learn any of it.

The asymmetry in (3) is **not** fixed here and must not be: absorbed production is
not objective progress, and this suite pins that it still is not. What beta.56
fixes is that the surface can now say so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alpha_ems_manager import execution as execution_module
from custom_components.alpha_ems_manager.const import (
    CAMPAIGN_BOUNDARY_METER,
    EXECUTION_COMPLETION_STOP_REASONS,
    EXECUTION_FAILED_STOP_REASONS,
    EXECUTION_STOP_CAMPAIGN_COMPLETE,
    EXECUTION_STOP_EXECUTION_ERROR,
    EXECUTION_STOP_NO_BATTERY_PLAN,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_WITHDRAWAL_STOP_REASONS,
    OUTCOME_CANCELED,
    OUTCOME_FAILED,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOME_SUPERSEDED,
)
from custom_components.alpha_ems_manager.execution import (
    CarriedRun,
    decide,
    parse_target,
)

from .test_beta32_campaign_lifecycle import (
    CAMPAIGN_METER_KWH,
    NOW,
    QUARTER,
    feed,
    sell_run,
    view,
)
from .test_beta34_campaign_terminal import latched
from .test_stage_b_controller import (
    CLOSES,
    OPENS,
    owned_evidence,
    progress_of,
    raw_target,
)

pytestmark = pytest.mark.usefixtures("control_surface")


# ---------------------------------------------------------------------------
# the live shape, as figures
# ---------------------------------------------------------------------------

#: The frozen objective, its realised total and the pack's uncapped intake, from
#: the 2026-09-10 diagnostics. The third is not derived from the first two.
LIVE_TARGET_KWH = 15.630
LIVE_REALIZED_KWH = 14.217
LIVE_MEASURED_KWH = 15.648
LIVE_QUARTERS = 32


def _rows(coordinator, campaign_id: str, measured_total: float, count: int = 4):
    """Record completed rows whose battery energy sums to ``measured_total``.

    Only ``realized_battery_kwh`` matters to ``_campaign_measured_now``, which is
    a sum over the rows naming this campaign -- so this injects a *measurement*,
    exactly as ``latched`` injects the frozen target and the realised total, and
    leaves every derivation to production.
    """
    per_row = measured_total / count
    base = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
    coordinator._completed_quarters.clear()
    for index in range(count):
        coordinator._completed_quarters.append(
            {
                "quarter_start": (base + timedelta(minutes=15 * index)).isoformat(),
                "campaign_id": campaign_id,
                "realized_battery_kwh": per_row,
            }
        )


def _closed(coordinator, *, soc: float | None, measured: float, **kwargs):
    """Close one campaign with a stated pack reading and a stated intake."""
    campaign_id = "c" * 16
    _rows(coordinator, campaign_id, measured)
    coordinator._read_soc_percent = lambda: soc  # type: ignore[method-assign]
    return latched(coordinator, **kwargs)


# ===========================================================================
# 1. D1 -- the reason, which was compared across two identity namespaces
# ===========================================================================

#: Mid-window on the controller suite's own time base, so ownership evidence and
#: the published target agree without a second set of constants to keep in step.
_ENDING = OPENS + timedelta(minutes=30)


def _decide_at_the_ending_refresh(**overrides):
    """Return the decision for a refresh where a run ended and a target is live.

    The live shape: the charge's admitted window has just closed, the carry
    machine has ended the run and named ``window_ended``, and Stage A's next
    publication is actionable -- so the state machine selects a target and takes
    the branch that used to assert ``plan_replaced``. Ownership is genuinely
    established, because that branch is guarded on it and a decision taken with
    ownership unproven would prove nothing about this fix.
    """
    params = {
        "mode_executes": True,
        "mode_off": False,
        "targets": [raw_target(plan_id="charge-next-window")],
        "now": _ENDING,
        "evidence": owned_evidence(),
        "progress": progress_of(LIVE_REALIZED_KWH),
        # A minted run id: intent, window start *and* admission instant. It can
        # never equal a publication id, which is built from the first two.
        "running_run_id": "9f2c11ab",
        "carried": None,
        "carry_ended": EXECUTION_STOP_WINDOW_ENDED,
    }
    params.update(overrides)
    return decide(**params)


def test_the_branch_under_test_is_actually_reached() -> None:
    """**The guard on the guard.** With ownership unproven this proves nothing.

    The ``plan_replaced`` branch is gated on ``owned``, and every assertion below
    depends on entering it. If the evidence ever stopped establishing ownership,
    the reason would be filled in by ``decide``'s shell instead and the tests
    would pass for the wrong reason -- which is exactly the shape of the two
    hand-built green tests beta.31 shipped.
    """
    from custom_components.alpha_ems_manager.const import OWNERSHIP_OWNED

    assert _decide_at_the_ending_refresh().ownership == OWNERSHIP_OWNED
    # And the machine itself, not the shell, is what names the reason now.
    machine = execution_module._decide(
        mode_executes=True,
        mode_off=False,
        targets=[raw_target(plan_id="charge-next-window")],
        now=_ENDING,
        evidence=owned_evidence(),
        progress=progress_of(LIVE_REALIZED_KWH),
        running_run_id="9f2c11ab",
        carried=None,
        carry_ended=EXECUTION_STOP_WINDOW_ENDED,
    )
    assert machine.stop_reason == EXECUTION_STOP_WINDOW_ENDED


def test_a_run_whose_window_ended_is_not_reported_as_replaced() -> None:
    """**The live misattribution, in one assertion.**

    ``running_run_id`` is a minted run id and ``target.plan_id`` is a publication
    id, so their inequality carries no information at all -- and it was published
    as the reason a five-and-a-half-hour campaign ended. The carry machine
    watched the run end; its verdict is the one that describes the event.

    *Mutation: restore ``stop_reason=EXECUTION_STOP_PLAN_REPLACED``
    unconditionally in that branch and this fails.*
    """
    decision = _decide_at_the_ending_refresh()

    assert decision.stop_reason == EXECUTION_STOP_WINDOW_ENDED
    assert decision.stop_reason != EXECUTION_STOP_PLAN_REPLACED


def test_the_stop_and_the_reset_are_exactly_what_they_were() -> None:
    """**Only the reported string changed. Nothing about the dispatch did.**

    This is the guard that keeps beta.56 out of beta.54's execution fidelity: the
    old dispatch must still be stopped before a new intent starts, and the
    teardown must still be requested. A fix that quietly stopped stopping would
    leave an inverter armed under a superseded intent.
    """
    decision = _decide_at_the_ending_refresh()

    assert decision.reset_required is True
    assert decision.state == execution_module.EXECUTION_STATE_STOPPING
    assert decision.target is not None
    assert decision.target.plan_id == "charge-next-window"


def test_a_genuinely_different_carried_run_still_reports_plan_replaced() -> None:
    """The real case is untouched, and it is the one comparing **like** identities.

    A carried run has its own minted id, so ``running_run_id != carried.run_id``
    is a genuine statement that a different run is running. That must still stop
    and still say so -- the beta.56 change narrows the claim, it does not remove
    it.
    """
    parsed = parse_target(raw_target(plan_id="charge-next-window"))
    assert parsed is not None
    carried = CarriedRun(
        run_id="aaaa1111",
        plan_id="charge-previous-window",
        target=parsed,
        revision=1,
        admitted_at=OPENS - timedelta(minutes=15),
        affirmed_at=OPENS - timedelta(minutes=15),
        stale_after=CLOSES + timedelta(hours=8),
    )
    decision = _decide_at_the_ending_refresh(
        carried=carried,
        carry_ended=None,
        running_run_id="bbbb2222",
    )

    assert decision.stop_reason == EXECUTION_STOP_PLAN_REPLACED
    assert decision.reset_required is True


def test_a_foreign_run_with_nothing_ended_still_reports_plan_replaced() -> None:
    """Nothing ended, we own a dispatch, and the id does not match the plan.

    On the direct path the identity the caller holds *is* a publication id, so the
    comparison is like-for-like and the conclusion is sound. With no carry verdict
    to prefer, there is nothing better to say and ``plan_replaced`` stands.
    """
    decision = _decide_at_the_ending_refresh(
        carried=None,
        carry_ended=None,
        running_run_id="not-the-published-plan",
    )

    assert decision.stop_reason == EXECUTION_STOP_PLAN_REPLACED


def test_the_carry_verdict_is_preferred_for_every_withdrawal_reason() -> None:
    """Not special-cased to ``window_ended``. Whatever the carry machine saw.

    ``stale_plan``, ``stage_a_hold`` and ``no_battery_plan`` are the other
    withdrawal reasons, and each is a statement about what Stage A now intends.
    Overwriting any of them with ``plan_replaced`` would report a cause the
    controller had not established.
    """
    for reason in (
        EXECUTION_STOP_WINDOW_ENDED,
        EXECUTION_STOP_STALE_PLAN,
        EXECUTION_STOP_STAGE_A_HOLD,
        EXECUTION_STOP_NO_BATTERY_PLAN,
    ):
        decision = _decide_at_the_ending_refresh(carry_ended=reason)
        assert decision.stop_reason == reason, reason


# ===========================================================================
# 2. D2 -- the outcome ladder, and the parity it claimed and did not have
# ===========================================================================


async def test_a_replaced_campaign_is_superseded_and_not_canceled(
    hass: HomeAssistant, setup_integration
) -> None:
    """**beta.42's word, reachable for the first time.**

    A started campaign a newer authoritative plan displaced did not miss its
    objective -- it was overtaken. ``canceled`` reads as though somebody called it
    off, which is the same failure beta.34 fixed for ``window_ended``.

    *Mutation: delete the ``EXECUTION_STOP_PLAN_REPLACED`` rung and this fails
    with ``canceled``.*
    """
    coordinator = setup_integration.runtime_data
    terminal = latched(
        coordinator,
        target=8.0,
        realized=5.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_PLAN_REPLACED,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_SUPERSEDED
    assert terminal["reason"] == EXECUTION_STOP_PLAN_REPLACED
    # **And the terminal is where it was decided.** beta.55 left the terminal
    # reading ``canceled`` and remapped only the public result -- except the remap
    # was guarded on ``partial`` and never fired, so the word was unreachable from
    # here. One decision now, in the ladder, published unchanged by the event.
    assert terminal["outcome"] != OUTCOME_CANCELED


async def test_window_ended_still_yields_partial_and_never_canceled(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The beta.34 guarantee, which the new rung sits above and must not disturb.**

    ``window_ended`` is the ordinary terminal of a campaign that ran to the end of
    its schedule. The two reason classes are disjoint, so a rung keyed on
    ``plan_replaced`` cannot capture it -- but the ordering is worth pinning,
    because getting it wrong is how beta.33 filed a working Safety Buy as an
    abandonment.

    *Mutation: move the new rung below the generic ``elif stop_reason`` -- or key
    it on the whole withdrawal set -- and the completion path changes.*
    """
    coordinator = setup_integration.runtime_data
    assert EXECUTION_STOP_WINDOW_ENDED in EXECUTION_COMPLETION_STOP_REASONS
    assert EXECUTION_STOP_WINDOW_ENDED not in EXECUTION_WITHDRAWAL_STOP_REASONS

    terminal = latched(
        coordinator,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_PARTIAL
    assert terminal["outcome"] != OUTCOME_CANCELED


async def test_the_live_and_recovered_ladders_agree_rung_for_rung(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The defect D2 embodied: one precedence written twice, disagreeing.**

    ``_recovered_outcome``'s docstring claimed it applied "the same precedence
    ``_close_campaign`` applies". It did not, and the divergence was invisible
    because no test compared them. This one sweeps every stop reason plus the
    no-reason case, so a future edit to either ladder alone fails here rather than
    on hardware after a restart.
    """
    coordinator = setup_integration.runtime_data
    reasons: list[str | None] = [None]
    reasons.extend(EXECUTION_FAILED_STOP_REASONS)
    reasons.extend(EXECUTION_COMPLETION_STOP_REASONS)
    reasons.extend(EXECUTION_WITHDRAWAL_STOP_REASONS)

    for reason in reasons:
        tolerance = coordinator._completion_tolerance_kwh(8.0, 8)
        live = latched(
            coordinator,
            target=8.0,
            realized=5.0,
            quarters=8,
            stop_reason=reason,
        )
        recovered = coordinator._recovered_outcome(
            measurable=True,
            target_kwh=8.0,
            realised_kwh=5.0,
            tolerance_kwh=tolerance,
            stop_reason=reason,
        )
        assert live is not None
        assert live["outcome"] == recovered, reason


async def test_an_unmeasurable_campaign_still_outranks_a_withdrawal(
    hass: HomeAssistant, setup_integration
) -> None:
    """The precedence above the new rung is unchanged.

    A total that is not a measurement cannot be evidence of anything, including of
    having been superseded. ``failed`` still wins, and so does a met objective.
    """
    coordinator = setup_integration.runtime_data

    unmeasurable = latched(
        coordinator,
        target=8.0,
        realized=5.0,
        quarters=8,
        measurable=False,
        stop_reason=EXECUTION_STOP_PLAN_REPLACED,
    )
    assert unmeasurable is not None
    assert unmeasurable["outcome"] == OUTCOME_FAILED

    met = latched(
        coordinator,
        target=8.0,
        realized=8.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_PLAN_REPLACED,
    )
    assert met is not None
    assert met["outcome"] == OUTCOME_SUCCESS


# ===========================================================================
# 3. D3 -- the physical evidence, and the seven cases it makes distinguishable
# ===========================================================================


async def test_the_live_campaign_now_publishes_why_it_was_short(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The exact hardware campaign, and the four figures that explain it.**

    15.648 kWh into the pack, 14.217 credited to the objective, 1.431 kWh of free
    sun beyond the row allowances, and a battery at 100 %. The verdict stays
    ``partial`` -- the objective genuinely was not met, because absorbed
    production is not progress -- and it is now legible instead of alarming.
    """
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=100.0,
        measured=LIVE_MEASURED_KWH,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_PARTIAL
    assert terminal["reason"] == EXECUTION_STOP_WINDOW_ENDED
    assert terminal["objective_target_kwh"] == pytest.approx(LIVE_TARGET_KWH)
    assert terminal["objective_realized_kwh"] == pytest.approx(LIVE_REALIZED_KWH)
    assert terminal["battery_measured_total_kwh"] == pytest.approx(
        LIVE_MEASURED_KWH, abs=1e-3
    )
    assert terminal["battery_full_at_close"] is True
    assert terminal["headroom_at_close_kwh"] == pytest.approx(0.0)
    # **The shortfall *is* the absorption**, which is the fact the old line hid.
    shortfall = LIVE_TARGET_KWH - LIVE_REALIZED_KWH
    assert terminal["absorbed_extra_kwh"] == pytest.approx(
        LIVE_MEASURED_KWH - LIVE_REALIZED_KWH, abs=1e-3
    )
    assert terminal["absorbed_extra_kwh"] >= shortfall


async def test_the_absorbed_total_includes_the_row_that_caused_the_close(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The open-row trap, closed by construction rather than by a special case.**

    beta.35's stop-before-record rule means the closing row reaches
    ``_completed_quarters`` *after* this terminal is filed. Summing per-row
    absorbed figures would therefore be short by one row on every campaign. The
    published figure is derived from ``battery_measured_total_kwh``, which already
    adds the accrued-but-unrecorded row, so it needs no correction of its own.
    """
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=100.0,
        measured=LIVE_MEASURED_KWH,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["absorbed_extra_kwh"] == pytest.approx(
        terminal["battery_measured_total_kwh"] - terminal["objective_realized_kwh"],
        abs=1e-6,
    )


async def test_a_short_campaign_on_an_unfilled_pack_says_so(
    hass: HomeAssistant, setup_integration
) -> None:
    """Case 3: short, and the pack was **not** full. A different problem entirely.

    Here the shortfall is not explained by absorption, and nothing in the payload
    pretends it is -- which is the point of publishing the pair rather than a
    single "it was fine" flag.
    """
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=62.0,
        measured=5.0,
        target=8.0,
        realized=5.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_PARTIAL
    assert terminal["battery_full_at_close"] is False
    assert terminal["absorbed_extra_kwh"] == pytest.approx(0.0)
    assert terminal["headroom_at_close_kwh"] > 0.0


async def test_an_unreadable_pack_publishes_absence_and_not_a_denial(
    hass: HomeAssistant, setup_integration
) -> None:
    """Not knowing whether the battery is full is not knowing that it is not.

    ``False`` would be a claim, and on the one surface built to explain a
    shortfall it would be the wrong one. Absent throughout, on the same terms as
    every other unreadable figure in this integration.
    """
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=None,
        measured=LIVE_MEASURED_KWH,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["battery_full_at_close"] is None
    assert terminal["headroom_at_close_kwh"] is None
    # The intake is a row sum and needs no pack reading, so it survives.
    assert terminal["battery_measured_total_kwh"] == pytest.approx(
        LIVE_MEASURED_KWH, abs=1e-3
    )


async def test_a_meter_bound_objective_withholds_the_absorbed_figure(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The subtraction is only meaningful at one boundary, and it says which.**

    For a battery-bound charge, realised and measured are the same kind of
    quantity, so their difference is exactly the free production the per-row clamp
    left out. For a *meter*-bound export the realised figure is grid energy while
    the measured total is battery energy: their difference is house-load
    compensation. Publishing it there would invent a figure, so it is absent.
    """
    coordinator = setup_integration.runtime_data
    campaign_id = "c" * 16
    _rows(coordinator, campaign_id, 13.5)
    coordinator._read_soc_percent = lambda: 21.0  # type: ignore[method-assign]
    # Closed by hand rather than through ``latched``, which pins the boundary to
    # ``battery`` -- and a test that let it do so would assert nothing at all.
    from homeassistant.util import dt as dt_util

    instant = dt_util.utcnow()
    coordinator._campaign_id = campaign_id
    coordinator._campaign_boundary = CAMPAIGN_BOUNDARY_METER
    coordinator._campaign_started_at = instant
    coordinator._campaign_frozen_target_kwh = 11.91
    coordinator._campaign_realized_kwh = 11.86
    coordinator._campaign_measurable = True
    coordinator._campaign_quarters_admitted = 6
    coordinator._quarter = None
    coordinator._closed_campaign = None
    coordinator._close_campaign(instant, EXECUTION_STOP_WINDOW_ENDED)
    terminal = coordinator._closed_campaign

    assert terminal is not None
    assert terminal["objective_boundary"] == CAMPAIGN_BOUNDARY_METER
    # The battery moved 13.5 kWh against an 11.86 kWh meter objective. The
    # difference is house-load compensation, and calling it absorbed sun would be
    # an invention -- so it is withheld, while the intake itself is published.
    assert terminal["absorbed_extra_kwh"] is None
    assert terminal["battery_measured_total_kwh"] == pytest.approx(13.5, abs=1e-3)


async def test_a_completed_campaign_reads_as_a_success(
    hass: HomeAssistant, setup_integration
) -> None:
    """Cases 1 and 7: the objective was met, and nothing new contradicts it."""
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=97.0,
        measured=8.2,
        target=8.0,
        realized=8.0,
        quarters=8,
        stop_reason=EXECUTION_STOP_CAMPAIGN_COMPLETE,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_SUCCESS
    assert terminal["battery_full_at_close"] is False


async def test_a_real_failure_is_still_a_failure(
    hass: HomeAssistant, setup_integration
) -> None:
    """Case 6: a command that failed is not a campaign that filled up early."""
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=100.0,
        measured=LIVE_MEASURED_KWH,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_EXECUTION_ERROR,
    )

    assert terminal is not None
    assert terminal["outcome"] == OUTCOME_FAILED


# ===========================================================================
# 4. and the invariant none of the above may weaken
# ===========================================================================


async def test_absorbed_production_is_still_not_objective_progress(
    hass: HomeAssistant, setup_integration
) -> None:
    """**The deferred asymmetry stays deferred, and this is the guard.**

    It would have been easy to make the live campaign read ``success`` by adding
    the absorbed total into the realised figure. That would redefine what a charge
    campaign promises, move the Trading Log denominator and beta.54's shortfall
    basis together, and -- through ``_completion_scope`` -- let a sunny row end a
    campaign that had not finished buying. The verdict must stay ``partial`` on
    evidence that physically filled the pack.

    *Mutation: credit ``absorbed_extra_kwh`` into ``objective_realized_kwh`` and
    this fails.*
    """
    coordinator = setup_integration.runtime_data
    terminal = _closed(
        coordinator,
        soc=100.0,
        measured=LIVE_MEASURED_KWH,
        target=LIVE_TARGET_KWH,
        realized=LIVE_REALIZED_KWH,
        quarters=LIVE_QUARTERS,
        stop_reason=EXECUTION_STOP_WINDOW_ENDED,
    )

    assert terminal is not None
    assert terminal["objective_realized_kwh"] == pytest.approx(LIVE_REALIZED_KWH)
    assert terminal["objective_realized_kwh"] < terminal["objective_target_kwh"]
    assert terminal["outcome"] == OUTCOME_PARTIAL
    # The tolerance could never have covered a structural 1.41 kWh gap, and that
    # is a fact about the two definitions rather than about the plant.
    assert terminal["success_tolerance_kwh"] < (LIVE_TARGET_KWH - LIVE_REALIZED_KWH)


# ===========================================================================
# 5. the Activity line, which is where a person actually meets all of this
# ===========================================================================


def _terminal_view(**overrides):
    from custom_components.alpha_ems_manager.activity import TerminalView

    payload = {
        "campaign_id": "c" * 16,
        "outcome": OUTCOME_PARTIAL,
        "objective_target_kwh": LIVE_TARGET_KWH,
        "objective_realized_kwh": LIVE_REALIZED_KWH,
        "reason": EXECUTION_STOP_WINDOW_ENDED,
        "battery_measured_total_kwh": LIVE_MEASURED_KWH,
        "absorbed_extra_kwh": LIVE_MEASURED_KWH - LIVE_REALIZED_KWH,
        "battery_full_at_close": True,
    }
    payload.update(overrides)
    return TerminalView(**payload)


def test_a_physically_finished_campaign_is_recognised_as_such() -> None:
    """The reading Activity makes, and the three facts it needs to make it."""
    assert _terminal_view().filled_from_production is True
    # Not full: a shortfall with the same arithmetic means something else.
    assert _terminal_view(battery_full_at_close=False).filled_from_production is False
    # Full but nothing absorbed: the pack was already there when it started.
    assert _terminal_view(absorbed_extra_kwh=0.0).filled_from_production is False
    # Full, absorbed, and no target to be short of: nothing to explain.
    assert _terminal_view(objective_target_kwh=None).filled_from_production is False
    # Full and absorbing, but the objective was met -- there is no shortfall.
    assert (
        _terminal_view(objective_realized_kwh=LIVE_TARGET_KWH).filled_from_production
        is False
    )


def test_an_absent_pack_reading_never_reads_as_finished() -> None:
    """``None`` grants nothing, exactly as it does at the coordinator."""
    assert _terminal_view(battery_full_at_close=None).filled_from_production is False
    assert _terminal_view(absorbed_extra_kwh=None).filled_from_production is False


def test_the_default_terminal_view_claims_nothing() -> None:
    """A caller that supplies no physical context gets no physical claim.

    Every existing test in the suite builds a ``TerminalView`` without these three
    fields, and each must keep rendering exactly the line it did.
    """
    from custom_components.alpha_ems_manager.activity import TerminalView

    bare = TerminalView(campaign_id="c" * 16, outcome=OUTCOME_PARTIAL)
    assert bare.battery_measured_total_kwh is None
    assert bare.absorbed_extra_kwh is None
    assert bare.battery_full_at_close is None
    assert bare.filled_from_production is False


def test_the_partial_line_explains_a_pack_that_filled_from_the_sun() -> None:
    """**The sentence the user actually reads, and the one that started this.**

    Driven through ``next_activity`` rather than through the renderer directly,
    because a rendering test on a hand-built input is exactly what beta.31 shipped
    green twice: it proves the wording and says nothing about whether the pipeline
    can produce the state. Here the lifecycle is announced, started and then
    closed, and the terminal is the only thing supplied.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    realized=LIVE_REALIZED_KWH,
                    running=False,
                    terminal=_terminal_view(
                        objective_target_kwh=CAMPAIGN_METER_KWH,
                        objective_realized_kwh=CAMPAIGN_METER_KWH - 0.4,
                        absorbed_extra_kwh=0.9,
                    ),
                ),
            ),
        ]
    )

    assert "Partial" in lines[-1][1]
    assert "Battery Full" in lines[-1][1]
    assert "Extra Solar Absorbed" in lines[-1][1]
    assert "Canceled" not in lines[-1][1]


def test_an_ordinary_partial_line_gains_no_clause() -> None:
    """**Additive, and only where the evidence supports it.**

    A campaign that ran out of time on a pack that was not full says exactly what
    it said in beta.55. A clause that appeared on every partial would be
    decoration, and on this surface decoration is a claim.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    realized=0.4,
                    running=False,
                    terminal=_terminal_view(
                        objective_realized_kwh=0.4,
                        objective_target_kwh=CAMPAIGN_METER_KWH,
                        battery_full_at_close=False,
                        absorbed_extra_kwh=0.0,
                    ),
                ),
            ),
        ]
    )

    assert "Partial" in lines[-1][1]
    assert "Battery Full" not in lines[-1][1]


def test_a_replaced_campaign_no_longer_prints_canceled() -> None:
    """``superseded`` had no branch, so beta.42's word printed as a cancellation.

    Stage A revising the future and something happening *to* the dispatch are
    different events, and filing both under one word is what made a replan look
    like an incident in a history view.
    """
    run = sell_run()
    lines = feed(
        [
            (NOW - QUARTER, (run,), None),
            (NOW, (run,), view(activation_confirmed=True)),
            (
                NOW + QUARTER,
                (run,),
                view(
                    realized=0.4,
                    running=False,
                    terminal=_terminal_view(
                        outcome=OUTCOME_SUPERSEDED,
                        reason=EXECUTION_STOP_PLAN_REPLACED,
                        objective_target_kwh=CAMPAIGN_METER_KWH,
                        objective_realized_kwh=0.4,
                        battery_full_at_close=None,
                        absorbed_extra_kwh=None,
                    ),
                ),
            ),
        ]
    )

    assert "Superseded" in lines[-1][1]
    assert "Replaced By A Newer Plan" in lines[-1][1]
    assert "Canceled" not in lines[-1][1]
