"""Break each beta.36 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch.

Every mutation below is a *plausible* edit, and most are literally the beta.35 line
this release replaced -- the line that destroyed a Live campaign on 2026-08-30 or
2026-08-31. A surviving mutation means the test is vacuous and **the test gets
rewritten**; it never means the mutation gets weakened.

The beta.35 table is a sibling and must still pass: a beta.36 correction that
resurrects a beta.35 defect is a regression, not a fix. Run both.

Run with:  python tools/mutation/run.py b36 [-k substring]
"""

from __future__ import annotations

LIFECYCLE = "tests/test_beta36_lifecycle.py"
PIPELINE = "tests/test_control_pipeline.py"
CONTINUITY = "tests/test_beta35_campaign_continuity.py"
LEDGER = "tests/test_beta35_ledger.py"
COUNTER = "tests/test_beta36_counterfactual.py"
DOMAINS = "tests/test_beta36_charge_domains.py"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production -- which is how the vacuity
# traps get tested rather than merely asserted.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------------------- the vocabularies
    (
        "M8a: a completion joins the abort family",
        "const.py",
        "EXECUTION_ABORT_STOP_REASONS: Final = (\n    EXECUTION_STOP_SAFETY,",
        "EXECUTION_ABORT_STOP_REASONS: Final = (\n"
        "    EXECUTION_STOP_QUARTER_TARGET_REACHED,\n"
        "    EXECUTION_STOP_SAFETY,",
        f"{LIFECYCLE}::test_every_stop_reason_belongs_to_exactly_one_vocabulary",
    ),
    (
        "M8b: a reason belongs to no vocabulary at all",
        "const.py",
        "EXECUTION_COMPLETION_STOP_REASONS: Final = (\n    EXECUTION_STOP_WINDOW_ENDED,",
        "EXECUTION_COMPLETION_STOP_REASONS: Final = (",
        f"{LIFECYCLE}::test_every_stop_reason_belongs_to_exactly_one_vocabulary",
    ),
    (
        "M20: the sub-resolution inhibit goes back to being a hazard",
        "const.py",
        "INHIBIT_NO_COMMAND_REASONS: Final = (\n    INHIBIT_NOTHING_TO_COMMAND,\n"
        "    INHIBIT_POWER_BELOW_DEVICE_MINIMUM,\n)",
        "INHIBIT_NO_COMMAND_REASONS: Final = (\n    INHIBIT_NOTHING_TO_COMMAND,\n)",
        f"{LIFECYCLE}::test_every_inhibit_belongs_to_exactly_one_class",
    ),
    (
        "M21: the hazard class becomes empty -- default permit",
        "const.py",
        "INHIBIT_HAZARD_REASONS: Final = tuple(",
        "INHIBIT_HAZARD_REASONS: Final = () and tuple(",
        f"{LIFECYCLE}::test_a_genuine_hazard_still_aborts_unsuppressed",
    ),
    (
        "M21b: and the partition notices it too",
        "const.py",
        "INHIBIT_HAZARD_REASONS: Final = tuple(",
        "INHIBIT_HAZARD_REASONS: Final = () and tuple(",
        f"{LIFECYCLE}::test_every_inhibit_belongs_to_exactly_one_class",
    ),
    (
        "M22: the two kinds of nothing share one token again",
        "safety.py",
        "    if not check(INHIBIT_NOTHING_TO_COMMAND, intent is not None):\n"
        "        return SafetyVerdict(False, INHIBIT_NOTHING_TO_COMMAND, tuple(checks))",
        "    if not check(INHIBIT_NO_BATTERY_PLAN, intent is not None):\n"
        "        return SafetyVerdict(False, INHIBIT_NO_BATTERY_PLAN, tuple(checks))",
        f"{PIPELINE}::test_the_two_kinds_of_nothing_are_told_apart",
    ),
    # -------------------------------------------------- a completion is not an abort
    (
        "M3: restore the unconditional stop when a row meets its target",
        "coordinator.py",
        # Re-anchored in beta.40: the satisfied-row body moved into
        # ``_async_finish_satisfied_row``, one indent level out. Same layer, same
        # defect, narrowest text that still identifies it.
        "        scope = self._completion_scope(now)\n        if scope is None:",
        "        scope = self._completion_scope(now)\n        if False:",
        f"{LIFECYCLE}::test_a_row_reaching_its_target_holds_and_the_campaign_survives",
    ),
    (
        "M3b: a target-reached row aborts unconditionally, as beta.35 did",
        "coordinator.py",
        # Re-anchored in beta.40, as M3 above.
        "        scope = self._completion_scope(now)\n        if scope is None:",
        '        scope = "abort"\n        if False:',
        f"{LIFECYCLE}::test_the_campaign_walks_every_row_of_both_plans",
    ),
    (
        "M4: latch the admission on any reason again",
        "coordinator.py",
        "        if aborting:\n            for key in (",
        "        if True:\n            for key in (",
        f"{LIFECYCLE}::test_a_completion_is_not_an_abandonment_but_a_hazard_still_is",
    ),
    (
        "M4b: and close the campaign on any reason again",
        "coordinator.py",
        "        if aborting or not self._campaign_still_published():",
        "        if True:",
        f"{LIFECYCLE}::test_the_campaign_walks_every_row_of_both_plans",
    ),
    (
        "M10: re-key the latch to the campaign identity",
        "coordinator.py",
        "                None if plan is None else plan.admission_key,",
        "                None if plan is None else plan.campaign_id,",
        f"{LIFECYCLE}::test_a_completion_is_not_an_abandonment_but_a_hazard_still_is",
    ),
    (
        "M23: a finished campaign may open another instance",
        "coordinator.py",
        "        if stop_reason in EXECUTION_COMPLETION_STOP_REASONS:\n"
        "            self._remember_final_campaign(campaign_id)",
        "        if False:\n            self._remember_final_campaign(campaign_id)",
        f"{CONTINUITY}::test_a_completed_campaign_may_not_open_another_instance",
    ),
    (
        "M23b: the campaign re-opens, and re-mints its instance, every refresh",
        "coordinator.py",
        "        if current is not None and current != self._campaign_id:",
        "        if current is not None:",
        f"{LIFECYCLE}::test_the_campaign_walks_every_row_of_both_plans",
    ),
    (
        "M24: drop the carry_forward abandoned-admission guard",
        "execution.py",
        "    if carried.admission_key in abandoned_admissions:",
        "    if False and carried.admission_key in abandoned_admissions:",
        f"{LIFECYCLE}::test_the_run_layer_refuses_an_admission_the_plan_layer_killed",
    ),
    # ----------------------------------------------------------------- the two holds
    (
        "M11: arm a sub-resolution row anyway",
        "coordinator.py",
        # Extended to a unique match. The shorter form matched the satisfied-row floor beneath it, and the
        # first match is the one that gets edited -- so the mutation could run
        # against code the named test does not exercise. Same site, same change.
        "            and abs(decision.applied_kw) < CONTROL_MIN_POWER_KW\n"
        "            and self._quarter_target_reached_at is None",
        "            and False\n"
        "            and self._quarter_target_reached_at is None",
        f"{LIFECYCLE}::test_a_rate_below_the_actuator_resolution_holds_and_recovers",
    ),
    (
        "M19: promote any unsafe verdict to a safety abort again",
        "coordinator.py",
        "        hazard = not verdict.safe and inhibit not in (",
        "        hazard = not verdict.safe or inhibit not in (",
        f"{LIFECYCLE}::test_stage_a_publishing_no_battery_plan_is_withheld_not_fatal",
    ),
    (
        "M19b: a hold intent is never produced for a satisfied row",
        "coordinator.py",
        "            holds = satisfied or rate_kw < CONTROL_MIN_POWER_KW",
        "            holds = False",
        f"{LIFECYCLE}::test_a_row_reaching_its_target_holds_and_the_campaign_survives",
    ),
    (
        # **The layer, changed to the one that now decides this. beta.38.**
        # Deleting the coordinator suppression alone no longer ends the executing
        # row: F1 keeps the run a step earlier, so there is no withdrawal left to
        # suppress. Removing the *first* layer restores the pre-beta.38 behaviour
        # this witness was written against. M1b still mutates the suppression
        # itself, through ``no_battery_plan`` -- a withdrawal F1 cannot intercept.
        "M1: delete the opened-row guard",
        "coordinator.py",
        "            row_open=self._opened_row_owns(now, self._carried),",
        "            row_open=False,",
        f"{LIFECYCLE}::test_a_stage_a_hold_is_withheld_while_the_row_is_executing",
    ),
    (
        "M1b: and the no-BatteryPlan withdrawal with it",
        "coordinator.py",
        "            plan_authority_holds\n"
        "            and stop_reason in EXECUTION_WITHDRAWAL_STOP_REASONS",
        "            False\n"
        "            and stop_reason in EXECUTION_WITHDRAWAL_STOP_REASONS",
        f"{LIFECYCLE}::test_stage_a_publishing_no_battery_plan_is_withheld_not_fatal",
    ),
    (
        "M2: plan authority holds unconditionally",
        "coordinator.py",
        "    def _plan_authority_holds(self, now: datetime) -> bool:",
        "    def _plan_authority_holds(self, now: datetime) -> bool:\n"
        "        if True:\n            return True",
        f"{LIFECYCLE}::test_stage_a_publishing_no_battery_plan_is_withheld_not_fatal",
    ),
    # ------------------------------------------------------------------- the ledger
    (
        "M6: null the identity before reading the realised total",
        "coordinator.py",
        "        realized = self._campaign_realized_now()\n"
        "        quarters = self._campaign_quarters_admitted",
        "        _identity, self._campaign_id = self._campaign_id, None\n"
        "        realized = self._campaign_realized_now()\n"
        "        self._campaign_id = _identity\n"
        "        quarters = self._campaign_quarters_admitted",
        f"{LIFECYCLE}::test_the_terminal_counts_the_row_it_closed_on",
    ),
    (
        "M7: accrue after the stop instead of before it",
        "coordinator.py",
        "        self._accrue_campaign_progress(finished, self._row_objective_kwh(finished))",
        "        pass",
        f"{LIFECYCLE}::test_ending_a_quarter_at_campaign_scope_counts_that_quarter",
    ),
    (
        "M7b: the exactly-once accrual guard never latches",
        "coordinator.py",
        "        self._campaign_accrued_row = quarter.quarter_start\n"
        "        self._campaign_realized_kwh += max(0.0, realised_kwh)",
        "        self._campaign_realized_kwh += max(0.0, realised_kwh)",
        f"{LEDGER}::test_a_row_is_accrued_exactly_once",
    ),
    # -------------------------------------------------------------- the provenance
    (
        "M12: a row that moved nothing reports only its expiry",
        "coordinator.py",
        "        if self._quarter is not None and self._quarter.open_at(now):\n"
        "            if commands and self._refresh_outcome.wrote:",
        "        if False:\n            if commands and self._refresh_outcome.wrote:",
        f"{LIFECYCLE}::test_a_row_that_moved_nothing_publishes_a_refusal",
    ),
    (
        "M25: the admission block stops naming the clause that refused",
        "coordinator.py",
        '            "refused": self._admission_refusal,',
        '            "refused": None,',
        f"{LIFECYCLE}"
        "::test_no_refresh_narrates_an_accumulating_run_without_a_lifecycle",
    ),
    # -------------------------------------------------------- the starved head state
    (
        "M15: the head run state is idle whenever no row is admitted",
        "coordinator.py",
        "        if carried is not None and carried.actionable_at(moment):\n"
        "            return run_state_for_intent(carried.target.intent)",
        "        if False:\n"
        "            return run_state_for_intent(carried.target.intent)",
        f"{LIFECYCLE}::test_the_head_state_survives_the_loss_of_the_row",
    ),
    (
        "M15b: and the fee it distorts",
        "economic.py",
        "    if intent == EXECUTION_INTENT_GRID_CHARGE:\n        return _RUN_CHARGE",
        "    if intent == EXECUTION_INTENT_GRID_CHARGE:\n        return _RUN_IDLE",
        f"{COUNTER}"
        "::test_the_head_state_the_coordinator_reports_is_a_fact_about_the_inverter",
    ),
    # ------------------------------------------------- the fixture's own vacuity gates
    (
        "V1: the fixture stops proving the campaign started",
        "tests/beta36_trace.py",
        "ROW_BATTERY_KWH: tuple[float, ...] = (0.56, 0.28, 0.56, 0.0, 0.56, 0.0, 0.56, 0.56)",
        "ROW_BATTERY_KWH: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)",
        f"{LIFECYCLE}::test_a_row_reaching_its_target_holds_and_the_campaign_survives",
    ),
    (
        "V2: the campaign identity is invented instead of derived",
        "tests/beta36_trace.py",
        "CAMPAIGN_ID = campaign_id()",
        'CAMPAIGN_ID = "not-a-digest"',
        f"{LIFECYCLE}::test_a_row_reaching_its_target_holds_and_the_campaign_survives",
    ),
    # ------------------------------- the 2026-09-01 hardware contract
    #
    # Mode 2 at 0 kW was measured to be a *total* hold -- it suppresses charging as
    # well as discharging -- which is right for a satisfied row and indefensible for
    # an unfinished one. These five protect both halves of that, and the domain error
    # that made the controller want 0 kW on an unfinished row at all.
    (
        "H1: charge the grid authorisation against the battery again",
        "dispatch.py",
        "    clamped_kw, clamp_reason = clamp_charge_kw(\n"
        "        applied_kw, replace(limits, remaining_grid_kw=None)\n"
        "    )",
        "    clamped_kw, clamp_reason = clamp_charge_kw(applied_kw, limits)",
        # **A malformed node id, fixed in beta.38 and not weakened.** The two module
        # constants were concatenated, so this resolved to
        # ``...lifecycle.pytests/...domains.py::...`` and pytest could collect
        # nothing -- the mutation reported as an anchor failure rather than as a
        # kill. The witness it always named is in the charge-domain file.
        f"{DOMAINS}"
        "::test_absorbing_production_causes_no_import_beyond_the_authorisation",
    ),
    (
        "H1b: and the arithmetic notices it too",
        "dispatch.py",
        "    clamped_kw, clamp_reason = clamp_charge_kw(\n"
        "        applied_kw, replace(limits, remaining_grid_kw=None)\n"
        "    )",
        "    clamped_kw, clamp_reason = clamp_charge_kw(applied_kw, limits)",
        f"{DOMAINS}::test_free_production_is_absorbed_once_the_grid_budget_is_spent",
    ),
    (
        "H2: lose the run-level downward revision when the clamp is dropped",
        "dispatch.py",
        "        grid_rate_cap_kw = min(grid_rate_cap_kw, max(0.0, limits.remaining_grid_kw))",
        "        grid_rate_cap_kw = grid_rate_cap_kw",
        f"{DOMAINS}::test_the_two_bounds_are_both_kept_and_the_tighter_one_binds",
    ),
    (
        "H3: a satisfied row stops being recognised as satisfied",
        "coordinator.py",
        # Re-anchored in beta.40: ``_quarter_is_satisfied`` became "satisfied AND
        # not absorbing", so the single-expression return is gone. The defect is
        # unchanged -- a satisfied row that never reports itself satisfied.
        "        return progress is None or not self._absorption_live(progress)",
        "        return False",
        f"{LIFECYCLE}::test_a_row_reaching_its_target_holds_and_the_campaign_survives",
    ),
    (
        "H4: every unfinished row is forced to a 0 kW hold",
        "coordinator.py",
        # Extended to a unique match. The shorter form matched the satisfied-row floor beneath it, and the
        # first match is the one that gets edited -- so the mutation could run
        # against code the named test does not exercise. Same site, same change.
        "            and abs(decision.applied_kw) < CONTROL_MIN_POWER_KW\n"
        "            and self._quarter_target_reached_at is None",
        "            and True\n            and self._quarter_target_reached_at is None",
        f"{LIFECYCLE}"
        "::test_an_unfinished_row_absorbs_production_when_the_budget_is_spent",
    ),
    (
        "H5: a genuine hazard stops aborting an owned live dispatch",
        "coordinator.py",
        "        unsafe_while_owned = owned and dispatch_active and hazard",
        "        unsafe_while_owned = owned and dispatch_active and hazard and False",
        f"{LIFECYCLE}::test_a_genuine_hazard_still_aborts_unsuppressed",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
