"""What the battery *should* do, kept apart from what it *can* do.

A policy proposes; :func:`battery.apply_request` decides. Nothing in this module
compares against a power limit, a capacity or a floor -- it may ask for four
kilowatts from an empty battery and be told it gets nothing, and that is the
intended division of labour. A policy that clamped its own requests would be a
second copy of the safety rules, which is the one thing this design refuses.

What Phase 3's policy honestly is
---------------------------------

Not an optimisation. With no photovoltaic forecast and no prices, "reduce grid
import" collapses to "discharge to cover load", which is what the inverter
already does by itself in self-consumption mode. Describing
:class:`ReserveGuardPolicy` as clever would be a lie, and the roadmap has four
later phases whose whole job is the cleverness.

What it does add is the reserve boundary made explicit and computable: where the
floor bites, how much energy is genuinely available above it, and a decision path
that provably cannot cross it. That is the thing Phase 4 needs before it is
allowed to touch an inverter, and it is what Phase 7 needs before a reserve can
be sized dynamically.

Why nothing here ever charges
-----------------------------

Every reason to put energy *into* a battery needs information Phase 3 does not
have. Surplus production is Phase 5. A cheap half-hour is Phase 6. A storm
warning is Phase 7. An arbitrage spread is Phase 8. Charging without one of those
reasons spends the user's money to no known end, and the inverter is already
absorbing photovoltaic surplus on its own.

So ``ACTION_CHARGE`` and ``MODE_CHARGE`` exist, are fully clamped and fully
simulated -- what-if comparison needs them, and Phases 5 to 8 need somewhere to
land -- but no policy shipped in Phase 3 emits one.
``test_phase_three_boundaries.py`` asserts that over every shipped policy rather
than trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .battery import BatteryRequest, BatteryState
from .const import (
    BATTERY_POLICY_VERSION,
    MODE_CHARGE,
    REASON_AT_RESERVE,
    REASON_BELOW_RESERVE,
    REASON_COVER_FORECAST_LOAD,
    REASON_FORECAST_UNAVAILABLE,
    REASON_NO_FLEXIBILITY,
    REASON_POLICY_HOLD,
)
from .simulation import IntervalDemand

#: Identifiers of the policies this release ships. Recorded on every decision
#: alongside ``BATTERY_POLICY_VERSION``, so a later phase cannot pool decisions
#: taken under different objectives into one series -- the same role
#: ``FORECAST_MODEL_VERSION`` plays for predictions.
POLICY_HOLD = "hold"
POLICY_RESERVE_GUARD = "reserve_guard"


@dataclass(frozen=True, slots=True)
class PolicyProposal:
    """One interval's proposal: what to ask for, and why.

    The reason travels with the request because a recommendation nobody can
    explain is not usable by a person or by a later phase. It is a constant from
    the bounded ``REASON_*`` vocabulary, never free text.
    """

    request: BatteryRequest
    reason: str


class BatteryPolicy:
    """The objective interface later phases replace.

    Deliberately tiny. A policy is a pure function of the battery's state and one
    interval's expected demand, plus an identity and a version. Phase 6 will pass
    a price through the demand side, Phase 7 a dynamically raised reserve through
    the state, and Phase 8 will supply an entirely different implementation --
    none of which requires the simulator or the clamp to change.
    """

    #: Stable identifier, recorded on every decision.
    identity: str = "abstract"
    #: Bumped when the same identity starts producing different requests.
    version: int = BATTERY_POLICY_VERSION

    def propose(self, state: BatteryState, demand: IntervalDemand) -> PolicyProposal:
        """Return what to ask of the battery for one interval."""
        raise NotImplementedError

    # -- convenience for the simulator ------------------------------------

    def provider(self):
        """Return a request provider the simulator can walk a day with.

        The simulator asks for requests and knows nothing about policies; this is
        the adapter, and it lives here rather than there for that reason.
        """

        def provide(state: BatteryState, demand: IntervalDemand) -> BatteryRequest:
            return self.propose(state, demand).request

        return provide


class HoldPolicy(BatteryPolicy):
    """Do nothing, ever.

    Not a placeholder. This is the reference trajectory the what-if comparison
    measures against -- the counterfactual of leaving the battery alone -- and it
    is what Phase 8 will price a candidate against. It also happens to be the
    safe answer whenever anything is missing.
    """

    identity = POLICY_HOLD

    def propose(self, state: BatteryState, demand: IntervalDemand) -> PolicyProposal:
        """Return an idle request, always."""
        return PolicyProposal(request=BatteryRequest.idle(), reason=REASON_POLICY_HOLD)


class ReserveGuardPolicy(BatteryPolicy):
    """Cover the predicted baseline load from the battery, down to the reserve.

    Three rules, in order, and each of them is a refusal rather than a
    calculation:

    * **No predicted demand, no request.** A withheld interval is not a
      prediction of an idle house. Asking for zero would be honest here, but
      asking for anything derived from a number that does not exist would not be.
    * **At or below the reserve, hold.** Below it the policy does not attempt to
      recover either: charging back up is a decision needing a justification
      Phase 3 cannot produce, and the hard floor in the clamp would refuse the
      discharge anyway. Reporting the distinction is the useful part.
    * **Otherwise ask for the load.** The magnitude is the interval's predicted
      demand expressed as an average power. Whether the battery can actually
      deliver it is not this module's business.

    The reserve consulted here is the **effective** minimum -- the policy target,
    which Phase 7 may raise above the user's configured floor. The clamp
    separately enforces the configured floor, and in Phase 3 the two are equal,
    so the user-visible promise holds exactly while the later flexibility is
    already expressible.
    """

    identity = POLICY_RESERVE_GUARD

    def propose(self, state: BatteryState, demand: IntervalDemand) -> PolicyProposal:
        """Return the discharge that covers this interval, or a hold."""
        power = demand.power_kw
        if power is None:
            return PolicyProposal(
                request=BatteryRequest.idle(), reason=REASON_FORECAST_UNAVAILABLE
            )

        if state.below_floor:
            return PolicyProposal(
                request=BatteryRequest.idle(), reason=REASON_BELOW_RESERVE
            )

        # The policy target, which is the effective reserve rather than the hard
        # floor. Identical in Phase 3; see the class docstring.
        target_energy = state.limits.energy_for_soc(
            state.reserve.effective_min_soc_percent
        )
        if state.energy_kwh <= target_energy:
            return PolicyProposal(
                request=BatteryRequest.idle(), reason=REASON_AT_RESERVE
            )

        if power <= 0.0:
            return PolicyProposal(
                request=BatteryRequest.idle(), reason=REASON_NO_FLEXIBILITY
            )

        return PolicyProposal(
            request=BatteryRequest.discharge(power),
            reason=REASON_COVER_FORECAST_LOAD,
        )


#: Every policy this release ships, in the order they are reported.
#:
#: Enumerated so the no-charging rule can be asserted over the real set rather
#: than over whichever policies a test happened to remember.
SHIPPED_POLICIES: tuple[type[BatteryPolicy], ...] = (HoldPolicy, ReserveGuardPolicy)

#: The policy whose decision Phase 3 publishes. The hold policy is still run
#: every refresh, as the reference trajectory.
DEFAULT_POLICY: type[BatteryPolicy] = ReserveGuardPolicy


def emits_charge(policy: BatteryPolicy, state: BatteryState) -> bool:
    """Return whether a policy would ever ask to charge in a given state.

    Used by the boundary test. A helper rather than an inline check because the
    rule it supports -- no Phase-3 policy charges -- is a phase boundary, and a
    boundary deserves to be named in the code it constrains.
    """
    for baseline in (None, 0.0, 0.001, 0.125, 1.0, 100.0):
        proposal = policy.propose(state, IntervalDemand(index=0, baseline_kwh=baseline))
        if proposal.request.mode == MODE_CHARGE:
            return True
    return False
