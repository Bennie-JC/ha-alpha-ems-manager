"""Break each beta.35 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch.

Every mutation below is a *plausible* edit -- in most cases literally the beta.34
line this release replaced. A surviving mutation means the test is vacuous and the
test gets rewritten; it never means the mutation gets weakened.

Run with:  python tools/mutation/run.py b35 [-k substring]
"""

from __future__ import annotations

# (name, file, old, new, test node id)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "R1: bound the claim by the row again",
        "coordinator.py",
        "            stale_after=plan.ends_at,",
        "            stale_after=quarter.quarter_end,",
        "tests/test_beta35_campaign_continuity.py"
        "::test_the_claim_is_bounded_by_the_plan_not_by_the_row",
    ),
    (
        "R2: adoption always loses the quarter again",
        "coordinator.py",
        "            self._quarter_progress_unknown = not continuous",
        "            self._quarter_progress_unknown = True",
        "tests/test_beta35_campaign_continuity.py"
        "::test_the_campaign_survives_both_boundaries",
    ),
    (
        # **Re-pointed, not retired. beta.38.** F1 keeps the carried run alive
        # across the boundary that used to drop it, so the continuity replay no
        # longer produces a quarter-authority refresh and the call-site edit
        # stopped failing anything. The fallback still matters -- the arm and the
        # claim read the same two sources in the same order -- so the mutation
        # moved to the fallback itself, with a witness that asserts it directly.
        "R3: the authority read sees only the carried run",
        "coordinator.py",
        "        plan = self._plan\n"
        "        return None if plan is None else plan.run_id",
        "        return None",
        "tests/test_beta38_authority_does_not_shield.py"
        "::test_the_authority_read_falls_back_to_the_frozen_schedule[net_export]",
    ),
    (
        # **The layer, changed to the one that now decides this. beta.38.**
        # Deleting the coordinator suppression alone no longer ends the run: F1
        # keeps it a step earlier, so no withdrawal is filed to suppress. Removing
        # the *first* layer restores exactly the pre-beta.38 behaviour this witness
        # was written against. The suppression itself stays covered by b36 M1b,
        # through ``no_battery_plan`` -- a withdrawal F1 cannot intercept.
        "R4: the opened-row guard is deleted",
        "coordinator.py",
        "            row_open=self._opened_row_owns(now, self._carried),",
        "            row_open=False,",
        "tests/test_beta35_campaign_continuity.py"
        "::test_the_campaign_survives_both_boundaries",
    ),
    (
        "A2: safety becomes a withdrawal reason",
        "const.py",
        "EXECUTION_WITHDRAWAL_STOP_REASONS: Final = (\n    EXECUTION_STOP_STALE_PLAN,",
        "EXECUTION_WITHDRAWAL_STOP_REASONS: Final = (\n"
        "    EXECUTION_STOP_SAFETY,\n"
        "    EXECUTION_STOP_STALE_PLAN,",
        "tests/test_beta35_campaign_continuity.py"
        "::test_the_two_stop_vocabularies_do_not_overlap",
    ),
    (
        "R5a: the objective forgets the frozen schedule",
        "coordinator.py",
        "        plan = self._plan\n        if plan is not None and plan.campaign_id == campaign_id:",
        "        plan = None\n        if plan is not None and plan.campaign_id == campaign_id:",
        "tests/test_beta35_campaign_continuity.py"
        "::test_the_frozen_objective_is_non_null_and_comes_from_the_schedule",
    ),
    # **R5b is deliberately absent, and it is an *equivalent* mutation rather than
    # a surviving one.** Restoring ``live or self._campaign_opening_target_kwh``
    # changes no behaviour reachable today: the block immediately above the freeze
    # already assigns ``opening = live`` whenever ``live is not None``, so the two
    # expressions agree in every state. The beta.35 edit removes a latent trap --
    # if that assignment ever moves, the ``or`` starts discarding legitimate zeros
    # silently -- and a mutation that cannot fail a test is not evidence of a
    # vacuous test. ``test_a_campaign_that_sells_nothing_freezes_at_zero_and_not_at
    # _a_fallback`` still pins the property itself, which is what a reader needs.
    (
        "R6: realised energy is charge-only again",
        "coordinator.py",
        "            soc_delta = (\n"
        "                max(0.0, opening - stored)\n"
        "                if self._run_is_discharge()\n"
        "                else max(0.0, stored - opening)\n"
        "            )",
        "            soc_delta = max(0.0, stored - opening)",
        "tests/test_beta35_campaign_continuity.py"
        "::test_a_discharge_reports_the_energy_it_actually_moved",
    ),
    (
        "R7: a surface reads a key nothing writes",
        "sensor.py",
        '    objective_realized = float(progress.get("objective_realized_kwh") or 0.0)',
        '    objective_realized = float(progress.get("grid_export_realized_kwh") or 0.0)',
        "tests/test_beta35_payload_contract.py"
        "::test_every_key_a_surface_reads_is_a_key_the_payload_writes",
    ),
    (
        "R8: Activity retracts a started campaign again",
        "activity.py",
        "        if _still_executing(lifecycle, execution):",
        "        if False and _still_executing(lifecycle, execution):",
        "tests/test_beta24_live_charge.py::test_a_live_campaign_says_three_things",
    ),
    (
        "R9: the head is always idle again",
        "economic.py",
        '        "head_run_state": head_run_state,',
        '        "head_run_state": _RUN_IDLE,',
        "tests/test_beta35_stored_value.py"
        "::test_a_running_campaign_is_not_abandoned_for_a_fee_it_already_paid",
    ),
    (
        "R10: the flat edge credit comes back",
        "economic.py",
        "            terminal.credit_eur(",
        "            terminal.flat.credit_eur(",
        "tests/test_beta35_stored_value.py"
        "::test_the_flat_terminal_liquidates_the_pack_and_the_new_one_does_not",
    ),
    (
        "R11: the head value curve is thrown away again",
        "economic.py",
        "        head_value=tuple(tuple(row[0]) for row in value),",
        "        head_value=(),",
        "tests/test_beta35_stored_value.py::test_the_head_value_curve_survives_the_solve",
    ),
    (
        "the ledger totals a hurdle rate as cash",
        "realized.py",
        "            self.realized_load_avoidance_value_eur - self.realized_net_cash_flow_eur,",
        "            self.realized_load_avoidance_value_eur\n"
        "            - self.realized_net_cash_flow_eur\n"
        "            - (self.model_switching_cost_eur or 0.0),",
        "tests/test_beta35_ledger.py"
        "::test_the_model_terms_are_reported_and_never_totalled_as_cash",
    ),
    (
        # **A narrower anchor. beta.38.** F1 inserted the opened-row guard and a
        # long comment between the affirmation search and the staleness test, so
        # an anchor spanning both stopped matching. The claim is unchanged: hoist
        # the deadline ahead of the publication and an affirming publication can
        # no longer save a run whose ``stale_after`` has just passed.
        "R6g: staleness is judged before the publication again",
        "execution.py",
        "    affirming = next((entry for entry in published if affirms(carried, entry)), None)",
        "    if carried.stale_at(now):\n"
        "        return CarryOutcome(\n"
        "            carried=None, ended=EXECUTION_STOP_STALE_PLAN, ended_run=carried\n"
        "        )\n"
        "\n"
        "    affirming = next((entry for entry in published if affirms(carried, entry)), None)",
        "tests/test_beta35_campaign_continuity.py"
        "::test_an_affirming_publication_is_read_before_the_deadline_is_judged",
    ),
    (
        "R13a: the abort leaves the schedule alive",
        "coordinator.py",
        "        self._quarter = None\n"
        "        self._plan = None\n"
        "        self._reset_quarter_progress(None)",
        "        self._quarter = None\n        self._reset_quarter_progress(None)",
        "tests/test_beta35_campaign_continuity.py"
        "::test_a_safety_abort_stops_at_once_and_q3_never_rearms",
    ),
    (
        "R13b: the re-arm guard is removed",
        "coordinator.py",
        # **beta.36 re-anchored this.** The guard used to ask whether the campaign
        # had been *abandoned*, which conflated an abort with a completion and
        # barred the one case that must be allowed -- a fresh attempt after a
        # hazard. It now asks whether the campaign genuinely *finished*, and that
        # is the guard a mutation has to break.
        "        if self._campaign_is_final(current) and current != self._campaign_id:",
        "        if False and current != self._campaign_id:",
        "tests/test_beta35_campaign_continuity.py"
        "::test_a_completed_campaign_may_not_open_another_instance",
    ),
    (
        "R14: the abort files no terminal",
        "coordinator.py",
        # Re-anchored for beta.36: the close is now conditional, because a
        # campaign Stage A is still publishing is not over. An **abort** still
        # closes immediately and unconditionally, and that is what this breaks.
        "        if aborting or not self._campaign_still_published():",
        "        if False:",
        "tests/test_beta35_campaign_continuity.py"
        "::test_a_safety_abort_stops_at_once_and_q3_never_rearms",
    ),
    (
        "A9: the refresh's emergency stop tears nothing down",
        "coordinator.py",
        "            if self._pending_is_reset or self._pending_is_emergency:",
        "            if self._pending_is_reset:",
        "tests/test_beta35_upgrade_and_invariants.py"
        "::test_a_safety_condition_at_the_boundary_aborts_through_the_refresh",
    ),
    (
        "R12: Next Planned Action skips the run at the head",
        "sensor.py",
        "        if run.start_index < head:",
        "        if run.start_index <= head:",
        "tests/test_beta35_public_semantics.py"
        "::test_a_run_beginning_at_the_head_is_the_next_planned_action",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
