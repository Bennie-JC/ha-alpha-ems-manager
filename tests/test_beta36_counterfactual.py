"""beta.36: what the 2026-08-30 lifecycle failure cost, and what cannot be known.

**Gate 2 of the release, and the shorter half of it on purpose.** The approved plan
asked for an offline re-solve of the lost morning from persisted ``DayRecord`` and
``PriceSnapshot`` history, priced at the prices the solver actually saw. That
history lives in the installation's own store. It is **not** in this repository and
it is not in either diagnostics download: those carry the published payloads, and
``MAX_DECISION_RECORDS_PUBLISHED`` is sixteen, so the records that survive the
2026-08-30 capture cover 19:15-23:00 local and nothing of the morning.

So the EUR half of §8 is **NOT RECOVERABLE**, and it is recorded as that rather than
reconstructed. Solving invented prices and reporting the result in euros would be the
exact defect this suite spent three releases learning to distrust: a figure a test
can reach that the world never produced. The order-of-magnitude context in the plan
(€0.7-2.0 for the day, from the observed 0.13 / 0.215 spread) stands as an upper
bound on an upper bound and is quoted here as neither a result nor a test.

What **is** exact is the structural half, and it is the half that matters for a
merge decision: the lifecycle defect distorted the optimiser's own inputs, and that
distortion is provable from production code with no historical data at all.

Provenance vocabulary
---------------------

``EXACT``
    From the capture verbatim, or arithmetic over it.
``STRUCTURAL``
    A property of production code, proven by solving it. No measurement involved,
    and therefore nothing to reconstruct.
``NOT RECOVERABLE``
    Needs state the capture does not contain. Declared, never estimated.
"""

from __future__ import annotations

import pytest

from custom_components.alpha_ems_manager.economic import (
    RUN_STATE_CHARGE,
    RUN_STATE_IDLE,
    run_state_for_intent,
)

from .beta34_shape import solve_at

#: The 2026-08-30 figures, from the capture. ``EXACT``.
#:
#: The campaign objective as published, what the three recorded rows delivered, and
#: what the 12:45:06Z reset left unrealised. The last is the integration's own
#: arithmetic at the moment of the stop and is quoted from the payload, not derived.
FROZEN_OBJECTIVE_KWH = 16.11
ROWS_REALISED_KWH = 0.548
TERMINAL_REPORTED_KWH = 0.27
UNREALISED_AT_RESET_KWH = 9.889

#: What the payload said about scope. ``EXACT``.
#:
#: ``_campaign_end_utc`` is only ever assigned ``quarter.quarter_end``, so it is a
#: high-water mark of rows *observed* rather than the end Stage A planned -- and
#: derivation stopped at 07:00Z, so the recorded end froze there while the campaign
#: had five and a half hours to run.
OBSERVED_WINDOW_END = "07:00Z"
ROWS_RECORDED = 3
QUARTERS_ADMITTED_REPORTED = 2

#: Anything below this needs the installation's store. ``NOT RECOVERABLE``.
NOT_RECOVERABLE = (
    "the morning ForecastRisk",
    "the 07:15-12:45Z decision records",
    "the PriceSnapshot the live solver read before 07:15Z",
    "the frozen 33-row schedule of the 16.11 kWh run",
)


def test_the_capture_is_internally_inconsistent_by_exactly_one_row() -> None:
    """**EXACT. The close-before-accrue defect, from the published numbers alone.**

    ``_async_end_quarter`` stopped the dispatch first -- which reaches
    ``_close_campaign``, which nulled ``_campaign_id`` -- and only then recorded the
    row, so ``_accrue_campaign_progress`` returned early and the quarter that *caused*
    the terminal was missing from the total. The arithmetic is the proof: three
    recorded rows delivered 0.548 kWh and the terminal reported 0.27, which is the
    first two rows and not the third.

    No reconstruction is involved. This is the capture disagreeing with itself.
    """
    assert TERMINAL_REPORTED_KWH < ROWS_REALISED_KWH
    assert QUARTERS_ADMITTED_REPORTED == ROWS_RECORDED - 1
    # The missing energy is one row's worth, not a rounding difference.
    missing = ROWS_REALISED_KWH - TERMINAL_REPORTED_KWH
    assert missing > 0.2, missing


def test_the_reported_loss_is_most_of_the_objective() -> None:
    """**EXACT arithmetic, and the reason this is a severity finding.**

    9.889 kWh of 16.11 was unrealised at the reset -- 61 % of a campaign the
    optimiser had committed to and the plant was executing correctly. It must **not**
    be read as 9.889 kWh of lost cheap grid charge: the frozen schedule is not
    recoverable, so the split between ``battery_kwh`` (the objective) and
    ``grid_authorised_kwh`` (an import *ceiling*) cannot be made, and a ceiling is
    never an amount to consume.
    """
    lost_fraction = UNREALISED_AT_RESET_KWH / FROZEN_OBJECTIVE_KWH
    assert 0.6 < lost_fraction < 0.62, lost_fraction
    assert "the frozen 33-row schedule of the 16.11 kWh run" in NOT_RECOVERABLE


def test_the_head_run_state_distortion_is_real_and_is_not_free() -> None:
    """**STRUCTURAL. D10, and it silently reverted beta.35's own correction.**

    ``_head_run_state`` read the admitted row and nothing else, so with ``self._plan``
    gone it reported ``IDLE``. For five and a half hours **every Stage-A solve paid a
    fresh run-start fee to continue a charge it was already running** -- which is
    exactly the fee beta.35's R9 correction exists to stop charging.

    Two facts are proven, and neither needs a measurement.

    *The fee.* Solved at a head **inside** the charge, the truthful head is charged
    half the switching cost of the head told it is idle. That is the quantity R9 is
    about, read from the plan's own decomposition rather than inferred.

    *The plan.* At a head two intervals **before** the run opens, the two solves do
    not merely price differently, they choose differently -- so the flag reaches the
    objective and not only the report.

    **What is deliberately not asserted: a direction.** On this horizon the truthful
    head reports the *higher* cost at the earlier head, so "telling the solver the
    truth cannot make the plan worse" is **false** as a general property and is not
    claimed. Nor should it be surprising: the two solves are minimising over different
    feasible sets, and the reported total is not comparable across them the way it is
    across two starting energies (see ``test_more_stored_energy_never_costs_more``,
    where only the initial state differs and dominance genuinely holds).

    So the finding is that the optimiser's inputs were wrong for five and a half hours.
    The **sign and size** of what that cost on 2026-08-30 are NOT RECOVERABLE, and no
    figure for either appears here.
    """
    inside_idle = solve_at(head=40, end=96, stored=8.294, head_run_state=RUN_STATE_IDLE)
    inside_live = solve_at(
        head=40, end=96, stored=8.294, head_run_state=RUN_STATE_CHARGE
    )

    # The witness: the flag reached the fee, so this cannot pass on a no-op.
    assert inside_live.desired.switching_cost_eur < (
        inside_idle.desired.switching_cost_eur
    ), (inside_live.desired.switching_cost_eur, inside_idle.desired.switching_cost_eur)
    assert inside_live.desired.switching_cost_eur == pytest.approx(
        inside_idle.desired.switching_cost_eur / 2.0
    ), "one run start instead of two"

    before_idle = solve_at(head=36, end=96, stored=8.294, head_run_state=RUN_STATE_IDLE)
    before_live = solve_at(
        head=36, end=96, stored=8.294, head_run_state=RUN_STATE_CHARGE
    )

    # And it reaches the objective, not only the decomposition.
    assert before_live.desired.cost_eur != before_idle.desired.cost_eur


def test_the_head_state_the_coordinator_reports_is_a_fact_about_the_inverter() -> None:
    """**STRUCTURAL.** The mapping the fix depends on, pinned at its source.

    ``_head_run_state`` now falls back to the carried run when no row is admitted, and
    that is only sound because ``run_state_for_intent`` is a statement about which
    direction is physically running -- not a preference, not a plan. A ``serve_load``
    interval, a hold and nothing at all are all idle, because none of them is a run
    the switching fee was ever charged for.
    """
    assert run_state_for_intent("grid_charge") == RUN_STATE_CHARGE
    assert run_state_for_intent("serve_load") == RUN_STATE_IDLE
    assert run_state_for_intent(None) == RUN_STATE_IDLE


def test_more_stored_energy_never_costs_more() -> None:
    """**STRUCTURAL. The dominance property, and a failure here is a real finding.**

    If the morning charge had completed, the evening solve would have begun from more
    stored energy. Whether that is worth euros depends on prices nobody can recover --
    but whether it can ever be *worse* is a property of the objective, and it is
    checkable without them.

    Solved at the same head, over the same horizon, with the same prices and the same
    terminal policy: only the starting energy differs. A violation would mean the
    solver can be harmed by having more energy, which would be a defect in the DP
    rather than a fact about the incident.
    """
    measured = solve_at(head=76, end=96, stored=8.294)
    corrected = solve_at(head=76, end=96, stored=min(21.6, 8.294 + 4.0))

    assert corrected.desired.cost_eur <= measured.desired.cost_eur + 1e-9, (
        corrected.desired.cost_eur,
        measured.desired.cost_eur,
    )


def test_the_safety_buy_is_monotone_in_stored_energy() -> None:
    """**STRUCTURAL, and it deliberately claims nothing about the observed 0.83 kWh.**

    A Safety Buy is physical-reachability driven: it exists because the pack cannot
    reach the reserve curve from where it is. More stored energy can only make that
    easier, so a solve begun with more must never buy *more* for safety.

    That is the direction, and it is all that is asserted. The evening 0.83 kWh Safety
    Buy of 2026-08-30 had ``bridge_kwh_now == 0``, so it was horizon-attributable
    rather than an emergency at the head -- and **no claim is made here that the
    morning failure caused it**. Attribution would need the morning forecast state,
    which is NOT RECOVERABLE.
    """
    measured = solve_at(head=76, end=96, stored=8.294)
    corrected = solve_at(head=76, end=96, stored=min(21.6, 8.294 + 4.0))

    assert corrected.safety_buy_ac_kwh <= measured.safety_buy_ac_kwh + 1e-9, (
        corrected.safety_buy_ac_kwh,
        measured.safety_buy_ac_kwh,
    )
    assert "the morning ForecastRisk" in NOT_RECOVERABLE


@pytest.mark.parametrize("missing", NOT_RECOVERABLE)
def test_what_is_not_recoverable_stays_declared(missing: str) -> None:
    """The list is part of the finding, not an apology for it.

    A reader deciding whether to trust this release needs to know which questions were
    answered by proof and which were left open -- and a test is the only place in this
    repository that a reader cannot skip. Nothing on this list has a figure anywhere
    in this module.
    """
    assert missing
    assert isinstance(missing, str)
