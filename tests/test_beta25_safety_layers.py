"""Three beta.25 safety layers, each pure and each asserted at its own boundary.

**Ownership loss.** Marker off means *not owned*, and that definition is not
weakened to make a stop reachable -- the degraded state is never called owned.
But letting a dispatch we can still prove we caused run until the device dead-man
expires is up to twenty minutes of uncommanded charging against one write, so the
stop gets its own strictly subtractive authority.

**Control-grade coherence.** The diagnostics source-age limit is 300 seconds and
was calibrated for comparing accumulated energy. Reused as an actuator threshold
it would accept a five-minute-old reading as the basis for a live setpoint. The
control bound is counted in sixty-second physical ticks and is materially shorter
than the dead-man it sits inside.

**Downward revision.** An admitted run's energy figures were immutable, so a
Safety Buy admitted conservatively while tomorrow's prices were unknown kept
delivering after cheaper prices arrived. Two caps, each on its own domain, fix it
without ever mixing origins or growing a run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.alpha_ems_manager.alphaess_device import (
    DISPATCH_CUTOFF_SOC,
    DISPATCH_DURATION,
    DISPATCH_ENABLE,
    DISPATCH_MODE_SELECT,
    DISPATCH_POWER,
    DISPATCH_PV_SWITCH,
)
from custom_components.alpha_ems_manager.const import (
    CAP_FORWARD,
    CAP_FROZEN,
    COHERENCE_ACTION_HOLD,
    COHERENCE_ACTION_NONE,
    COHERENCE_ACTION_STOP,
    COHERENCE_EXPIRED,
    COHERENCE_HOLDING,
    COHERENCE_OK,
    CONTROL_COHERENCE_GRACE_TICKS,
    CONTROL_MAX_SOURCE_AGE_SECONDS,
    DEFAULT_CONTROL_HORIZON_MINUTES,
    EMERGENCY_STOP_MAX_ATTEMPTS,
    REFUSE_EMERGENCY_ATTEMPTS_SPENT,
    REFUSE_EMERGENCY_NOT_AUTHORIZED,
    REFUSE_EMERGENCY_NOT_THE_STOP,
    SAFETY_SAMPLE_SECONDS,
)
from custom_components.alpha_ems_manager.energy_balance import (
    BALANCE_MAX_SOURCE_AGE_SECONDS,
    SourceCoherence,
    control_coherence,
)
from custom_components.alpha_ems_manager.execution import (
    ForwardAuthorisation,
    remaining_authorised_kwh,
)
from custom_components.alpha_ems_manager.safety import (
    authorize_emergency_self_stop,
    emergency_self_stop_authorized,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

#: Causation still provable, marker gone. The one state that grants the authority.
DEGRADED = {
    "dispatch_active": True,
    "marker_present_and_on": False,
    "record_matches_run": True,
    "readback_compatible": True,
    "contradicted": False,
}


def healthy(age: float = 12.1, skew: float = 18.6) -> SourceCoherence:
    """Return a reading like the installation's own: well inside every bound."""
    return SourceCoherence(skew_seconds=skew, oldest_age_seconds=age, source_count=3)


# == 1. ownership loss ========================================================


def test_a_marker_lost_with_causation_intact_grants_the_authority() -> None:
    """**Test A.** The record still matches and the readback still agrees."""
    assert emergency_self_stop_authorized(**DEGRADED) is True


def test_a_missing_record_is_foreign_and_grants_nothing() -> None:
    """**Test B.** Causation can no longer be proven, so nothing may be written."""
    assert (
        emergency_self_stop_authorized(**{**DEGRADED, "record_matches_run": False})
        is False
    )


def test_a_contradicting_readback_is_foreign_and_grants_nothing() -> None:
    """**Test C.** The mode or the sign disagrees, so this is not our dispatch."""
    assert (
        emergency_self_stop_authorized(**{**DEGRADED, "readback_compatible": False})
        is False
    )


def test_any_other_contradiction_grants_nothing() -> None:
    """The catch-all, so a new kind of doubt fails closed rather than open."""
    assert emergency_self_stop_authorized(**{**DEGRADED, "contradicted": True}) is False


def test_an_intact_marker_is_the_ordinary_stop_not_the_emergency_one() -> None:
    """The authority must not shadow the path that already exists.

    With the marker on, ownership is intact and ``authorize_reset`` applies. Two
    entitlements for one situation is how the narrower one gets used by mistake.
    """
    assert (
        emergency_self_stop_authorized(**{**DEGRADED, "marker_present_and_on": True})
        is False
    )


def test_nothing_running_grants_nothing() -> None:
    """There is no emergency in stopping something that already stopped."""
    assert (
        emergency_self_stop_authorized(**{**DEGRADED, "dispatch_active": False})
        is False
    )


def test_the_authority_permits_the_dispatch_off_and_nothing_else() -> None:
    """The envelope, checked against the steps rather than the intention."""
    decision = authorize_emergency_self_stop(
        authorized=True, steps=(DISPATCH_ENABLE,), attempts_made=0
    )

    assert decision.authorized is True


@pytest.mark.parametrize(
    "extra",
    [
        DISPATCH_POWER,
        DISPATCH_CUTOFF_SOC,
        DISPATCH_DURATION,
        DISPATCH_MODE_SELECT,
        DISPATCH_PV_SWITCH,
    ],
)
def test_the_authority_refuses_any_widening(extra: str) -> None:
    """**Test E, the mutation.** One operation means one operation.

    Each of these touches a dispatch that may still be running, and one of them --
    the duration -- restarts the vendor timer, so a stop that also "tidied up"
    would extend the run it was ending.
    """
    decision = authorize_emergency_self_stop(
        authorized=True, steps=(DISPATCH_ENABLE, extra), attempts_made=0
    )

    assert decision.authorized is False
    assert decision.refusal == REFUSE_EMERGENCY_NOT_THE_STOP


def test_the_authority_refuses_a_list_that_is_not_the_stop_at_all() -> None:
    """Order and content both matter: the list must be exactly the one step."""
    decision = authorize_emergency_self_stop(
        authorized=True, steps=(DISPATCH_POWER,), attempts_made=0
    )

    assert decision.refusal == REFUSE_EMERGENCY_NOT_THE_STOP


def test_the_retry_is_bounded_and_then_the_dead_man_finishes_the_job() -> None:
    """**Test D.** One attempt per physical tick, and three is the end of it.

    A write that has failed three times is not going to be persuaded by a fourth,
    and the device dead-man already exists as the backstop.
    """
    for attempt in range(EMERGENCY_STOP_MAX_ATTEMPTS):
        assert (
            authorize_emergency_self_stop(
                authorized=True, steps=(DISPATCH_ENABLE,), attempts_made=attempt
            ).authorized
            is True
        )

    spent = authorize_emergency_self_stop(
        authorized=True,
        steps=(DISPATCH_ENABLE,),
        attempts_made=EMERGENCY_STOP_MAX_ATTEMPTS,
    )
    assert spent.authorized is False
    assert spent.refusal == REFUSE_EMERGENCY_ATTEMPTS_SPENT


def test_an_ungranted_authority_authorises_nothing() -> None:
    """The grant and the envelope are separate checks, and both must pass."""
    decision = authorize_emergency_self_stop(
        authorized=False, steps=(DISPATCH_ENABLE,), attempts_made=0
    )

    assert decision.refusal == REFUSE_EMERGENCY_NOT_AUTHORIZED


# == 2. control-grade coherence ==============================================


def test_the_control_bound_is_tighter_than_the_diagnostics_one() -> None:
    """**The whole reason a second threshold exists.**

    300 seconds was calibrated for comparing accumulated energy over a quarter.
    Reused as an actuator threshold it accepts a five-minute-old photovoltaic
    reading as the basis for a live setpoint, which is not a bound at all on a
    controller that corrects every sixty seconds.
    """
    assert CONTROL_MAX_SOURCE_AGE_SECONDS < BALANCE_MAX_SOURCE_AGE_SECONDS
    assert BALANCE_MAX_SOURCE_AGE_SECONDS == 300.0
    assert CONTROL_MAX_SOURCE_AGE_SECONDS == 90.0


def test_the_grace_period_is_materially_shorter_than_the_dead_man() -> None:
    """**The invariant the bound exists to satisfy.**

    Two economic refreshes is about thirty minutes -- longer than the dead-man it
    is supposed to sit inside, so the device would end the run before the
    controller decided to. Three physical ticks is 180 seconds.
    """
    grace = CONTROL_COHERENCE_GRACE_TICKS * SAFETY_SAMPLE_SECONDS
    deadman = DEFAULT_CONTROL_HORIZON_MINUTES * 60

    assert grace == 180
    assert grace < deadman / 4, (grace, deadman)


def test_the_installations_own_readings_are_comfortably_usable() -> None:
    """Derived from measured behaviour, so it cannot fire on ordinary jitter."""
    state = control_coherence(
        previous=None, now=NOW, coherence=healthy(), sources_available=True
    )

    assert state.state == COHERENCE_OK
    assert state.usable is True
    assert state.action == COHERENCE_ACTION_NONE
    assert state.last_coherent_tick == NOW


def test_one_bad_tick_holds_rather_than_calculating() -> None:
    """**Test 1.** No new setpoint, no new write, and the target is untouched."""
    stale = SourceCoherence(skew_seconds=5.0, oldest_age_seconds=150.0, source_count=3)

    state = control_coherence(
        previous=None, now=NOW, coherence=stale, sources_available=True
    )

    assert state.state == COHERENCE_HOLDING
    assert state.usable is False
    assert state.action == COHERENCE_ACTION_HOLD
    assert state.bad_ticks == 1
    assert state.bad_since == NOW
    assert state.expired is False


def test_recovery_inside_the_grace_period_resumes_control() -> None:
    """**Test 2.** The hold clears and the counter resets to zero."""
    stale = SourceCoherence(skew_seconds=5.0, oldest_age_seconds=150.0, source_count=3)
    holding = control_coherence(
        previous=None, now=NOW, coherence=stale, sources_available=True
    )

    resumed = control_coherence(
        previous=holding,
        now=NOW + timedelta(seconds=60),
        coherence=healthy(),
        sources_available=True,
    )

    assert resumed.state == COHERENCE_OK
    assert resumed.bad_ticks == 0
    assert resumed.bad_since is None


def test_the_bound_expires_at_the_third_bad_tick() -> None:
    """**Test 3.** Three ticks of untrusted control is the bound, not four.

    Spending a fourth would be tolerating 240 seconds while claiming 180, and the
    stricter reading is the one a safety bound should take.
    """
    stale = SourceCoherence(skew_seconds=5.0, oldest_age_seconds=150.0, source_count=3)
    state = None
    seen = []
    for tick in range(CONTROL_COHERENCE_GRACE_TICKS):
        state = control_coherence(
            previous=state,
            now=NOW + timedelta(seconds=60 * tick),
            coherence=stale,
            sources_available=True,
        )
        seen.append(state.state)

    assert seen == [COHERENCE_HOLDING, COHERENCE_HOLDING, COHERENCE_EXPIRED]
    assert state is not None
    assert state.expired is True
    assert state.action == COHERENCE_ACTION_STOP
    assert state.bad_ticks == CONTROL_COHERENCE_GRACE_TICKS


def test_the_dead_man_is_never_rearmed_while_incoherent() -> None:
    """**Test 4.** Deterministic, not a judgement call.

    Re-arming on measurements the controller does not trust is exactly "keep the
    run alive indefinitely while blind", which is what the grace period exists to
    prevent. A scheduled re-arm falling due during a hold is refused.
    """
    stale = SourceCoherence(skew_seconds=5.0, oldest_age_seconds=150.0, source_count=3)
    state = None
    for tick in range(CONTROL_COHERENCE_GRACE_TICKS):
        state = control_coherence(
            previous=state,
            now=NOW + timedelta(seconds=60 * tick),
            coherence=stale,
            sources_available=True,
        )
        assert state.may_rearm_deadman is False, state

    healthy_again = control_coherence(
        previous=state, now=NOW, coherence=healthy(), sources_available=True
    )
    assert healthy_again.may_rearm_deadman is True


def test_a_gross_identity_contradiction_follows_the_same_path() -> None:
    """**Test 5.** Same or stricter, and same is what it is.

    It is tempting to treat a provably wrong reading more harshly than a merely
    late one, but the energy identity has its own tolerance and a single blip is
    not evidence that control has been lost -- and the grace period is already
    only 180 seconds.
    """
    state = control_coherence(
        previous=None,
        now=NOW,
        coherence=healthy(),
        sources_available=True,
        identity_ok=False,
    )

    assert state.state == COHERENCE_HOLDING
    assert state.reason == "energy_identity"


def test_an_unavailable_source_is_a_hold_and_never_a_fabricated_value() -> None:
    """No fallback photovoltaic, house or grid figure is ever invented."""
    state = control_coherence(
        previous=None, now=NOW, coherence=None, sources_available=False
    )

    assert state.state == COHERENCE_HOLDING
    assert state.reason == "source_unavailable"


def test_excessive_skew_is_its_own_reported_reason() -> None:
    """Four ways to be unusable, and diagnostics say which."""
    skewed = SourceCoherence(skew_seconds=400.0, oldest_age_seconds=5.0, source_count=3)

    state = control_coherence(
        previous=None, now=NOW, coherence=skewed, sources_available=True
    )

    assert state.reason == "skew"


def test_the_coherence_block_names_every_field_the_plan_requires() -> None:
    """Diagnostics completeness, asserted rather than hoped for."""
    payload = control_coherence(
        previous=None, now=NOW, coherence=healthy(), sources_available=True
    ).as_dict()

    for field in (
        "coherence_state",
        "coherence_bad_since",
        "coherence_bad_ticks",
        "coherence_grace_seconds",
        "coherence_action",
        "last_coherent_physical_tick",
    ):
        assert field in payload, field


# == 3. downward revision of an admitted run =================================

BOUNDARY = datetime(2026, 8, 26, 10, 15, tzinfo=UTC)
AFTER = BOUNDARY + timedelta(minutes=1)
BEFORE = BOUNDARY - timedelta(minutes=5)


def forward(authorised: float, delivered: float = 0.0) -> ForwardAuthorisation:
    """Return a forward cap starting at the shared boundary."""
    return ForwardAuthorisation(
        authorised_kwh=authorised,
        forward_from=BOUNDARY,
        delivered_since_kwh=delivered,
    )


def test_case_a_a_smaller_fresh_target_shrinks_the_run() -> None:
    """Admitted 6.0, delivered 2.0, and the fresh solve now wants only 1.5."""
    allowed, cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=4.0, forward=forward(1.5)
    )

    assert allowed == pytest.approx(1.5)
    assert cap == CAP_FORWARD


def test_case_c_an_unchanged_target_changes_nothing() -> None:
    """**The regression the naive single-min form would have caused.**

    Comparing the frozen remainder against a fresh figure measured from a
    different origin trims a healthy run by an interval every refresh. Here the
    plan is unchanged and the run is untouched.
    """
    allowed, cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=4.5, forward=forward(4.5)
    )

    assert allowed == pytest.approx(4.5)
    assert cap == CAP_FROZEN


def test_case_d_a_larger_fresh_target_never_expands_the_run() -> None:
    """Strictly subtractive. Growth goes through fresh admission, not here."""
    allowed, cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=2.0, forward=forward(4.0)
    )

    assert allowed == pytest.approx(2.0)
    assert cap == CAP_FROZEN


def test_case_e_an_over_delivered_run_is_clamped_at_zero() -> None:
    """Never negative, and Stage B never compensates somewhere else."""
    allowed, _cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=3.0, forward=forward(0.0)
    )

    assert allowed == pytest.approx(0.0)

    over = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=3.0, forward=forward(1.0, delivered=2.5)
    )
    assert over[0] == pytest.approx(0.0)


def test_the_elapsing_interval_runs_under_the_authorisation_it_was_issued() -> None:
    """**Obsolescence begins at the boundary, which is where the cap begins.**

    The interval in flight is the one Stage A authorised *for now*, under the
    economics that held when it began. Cutting it too would mean Stage B
    overriding an authorisation Stage A issued for an interval it had already
    solved.
    """
    allowed, cap = remaining_authorised_kwh(
        now=BEFORE, frozen_remaining_kwh=6.0, forward=forward(1.5)
    )

    assert allowed == pytest.approx(6.0)
    assert cap == CAP_FROZEN


def test_a_healthy_run_is_never_ratcheted_down() -> None:
    """Four refreshes of an unchanged 6.0 kWh run, with the cap renewed each time.

    The accumulator resets at every affirmation, so while a boundary is still in
    the future the forward cap is inactive -- and by the time it passes, a fresh
    affirmation has moved it on.
    """
    start = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    frozen = 6.0
    allowances: list[float] = []
    for quarter in range(4):
        refresh = start + timedelta(minutes=15 * quarter)
        cap = ForwardAuthorisation(
            authorised_kwh=6.0 - 1.5 * (quarter + 1),
            forward_from=refresh + timedelta(minutes=15),
        )
        allowed, _which = remaining_authorised_kwh(
            now=refresh + timedelta(minutes=1),
            frozen_remaining_kwh=frozen,
            forward=cap,
        )
        allowances.append(allowed)
        frozen -= 1.5

    assert allowances == pytest.approx([6.0, 4.5, 3.0, 1.5])


def test_no_forward_cap_leaves_the_frozen_behaviour_exactly_as_it_was() -> None:
    """Before any affirmation there is one cap, and it is the one beta.24 had."""
    allowed, cap = remaining_authorised_kwh(
        now=AFTER, frozen_remaining_kwh=6.0, forward=None
    )

    assert allowed == pytest.approx(6.0)
    assert cap == CAP_FROZEN


def test_the_result_is_never_greater_than_the_frozen_remainder() -> None:
    """Swept, because "strictly subtractive" is the invariant of the whole fix."""
    for frozen_kwh in (0.0, 0.5, 2.0, 6.0, 11.0):
        for authorised in (0.0, 0.5, 2.0, 6.0, 99.0):
            for delivered in (0.0, 1.0, 50.0):
                allowed, _cap = remaining_authorised_kwh(
                    now=AFTER,
                    frozen_remaining_kwh=frozen_kwh,
                    forward=forward(authorised, delivered),
                )
                assert allowed <= frozen_kwh + 1e-9, (frozen_kwh, authorised, delivered)
                assert allowed >= 0.0
