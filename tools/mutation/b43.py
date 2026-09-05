"""Break each beta.43 claim on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out which
kind you have is to break the thing and watch. A surviving mutation means the test is
vacuous and **the test gets rewritten**; it never means the mutation gets weakened.

**Why this release needs a table of its own.** beta.43 changes no planner decision --
the DP objective, the reserve, the three purchase categories, the terminal value and
Stage A authority are frozen, and the neutrality digests plus the beta.40/41 anchors
are the proof. What it changes is whether a measurement survives long enough to be
recorded, and whether a state is named truthfully. Both fail silently.

Four families:

The ``A`` mutations attack the **boundary**. This is the release's root cause: the
executing slot advanced and the accumulators were rebased onto the successor before
the ended row was read, so a row with a successor recorded ``0.0`` and a row that
ended with nothing after it recorded the truth. The failure produced no error, no
warning and no gap -- a plausible zero, on every mid-campaign row.

The ``T`` mutations attack the **target**. Growth and the freeze pull in opposite
directions, and a mutation in either direction is a plausible-looking verdict: a
target that shrinks makes a shortfall retroactively successful, and one that cannot
grow judges a campaign against the fragment of itself that happened to be published
when it started.

The ``O`` mutations attack the **releasing state**. Every clause of
``own_release_draining`` is a refusal, and dropping any one of them lets a receipt
excuse a dispatch it cannot account for -- which is the one direction this state must
never fail in, because it is reached from ``foreign``.

The ``C`` mutations attack the **controllability floor**. It sits beside the
actuator-resolution floor and must stay a separate, export-only rule: collapsing the
two, or asking the export question of a charge row, would forfeit authorised PV
absorption for no dispatch saving at all.
"""

from __future__ import annotations

ACC = "tests/test_beta43_accounting.py"
MAT = "tests/test_beta43_materiality.py"
OWN = "tests/test_beta43_ownership.py"
PUB = "tests/test_beta43_public.py"
B36 = "tests/test_beta36_lifecycle.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # A -- the measurement has to survive the boundary
    # =====================================================================
    (
        "A1: the ended row's totals are captured after the slot has advanced",
        "coordinator.py",
        "        pending = self._capture_quarter_progress()\n"
        "        pending_clamps = set(self._quarter_clamps)",
        "        pending = None\n        pending_clamps = None",
        f"{ACC}::test_a_row_with_a_successor_keeps_its_own_measurement",
    ),
    (
        "A2: the captured totals are never put back, so the record reads the rebase",
        "coordinator.py",
        "        if measured is not None:\n"
        "            self._restore_quarter_progress(measured)\n"
        "            self._quarter_clamps = set(() if clamps is None else clamps)",
        "        if False:\n"
        "            self._restore_quarter_progress(measured)\n"
        "            self._quarter_clamps = set(() if clamps is None else clamps)",
        f"{ACC}::test_a_row_with_a_successor_keeps_its_own_measurement",
    ),
    (
        "A3: the row is accrued after the stop again, so the close nulls the id first",
        "coordinator.py",
        "        self._accrue_campaign_progress(row, self._row_objective_kwh(row))\n"
        "        if stop:",
        "        if stop:",
        f"{ACC}::test_the_campaign_total_is_the_sum_of_its_rows",
    ),
    (
        "A4: the stop rebases the row before the terminal reads it",
        "coordinator.py",
        "            self._note_release_receipt(snapshot, now)\n"
        "            self._clear_execution_record()",
        "            self._note_release_receipt(snapshot, now)\n"
        "            self._quarter = None\n"
        "            self._reset_quarter_progress(None)\n"
        "            self._clear_execution_record()",
        f"{B36}::test_ending_a_quarter_at_campaign_scope_counts_that_quarter",
    ),
    (
        "A5: the completed row is judged against the slot, not against itself",
        "coordinator.py",
        "        objective = self._objective_kwh_for(quarter)\n"
        "        realised = realised_grid if export else objective",
        "        objective = self._quarter_objective_kwh\n"
        "        realised = realised_grid if export else objective",
        f"{ACC}::test_the_campaign_total_is_the_sum_of_its_rows",
    ),
    (
        "A6: the accrual is judged against the slot, not against the row it names",
        "coordinator.py",
        "        return self._objective_kwh_for(row)\n\n    @callback\n"
        "    def _campaign_row_is_final(",
        "        return self._quarter_objective_kwh\n\n    @callback\n"
        "    def _campaign_row_is_final(",
        f"{ACC}::test_a_serve_load_gap_does_not_break_the_accumulation",
    ),
    (
        "A7: rows_completed is the accrual count again, so it corroborates nothing",
        "coordinator.py",
        '            "rows_completed": len(campaign_rows) + (1 if pending_row else 0),',
        '            "rows_completed": quarters,',
        f"{ACC}::test_the_two_row_counters_are_genuinely_independent",
    ),
    # =====================================================================
    # T -- the target grows, and never shrinks
    # =====================================================================
    (
        "T1: growth is dropped, so a campaign is judged against its opening fragment",
        "coordinator.py",
        "        else:\n            self._grow_campaign_target()",
        "        else:\n            pass",
        f"{PUB}::test_growth_happens_on_the_refresh_that_republishes_the_campaign",
    ),
    (
        "T2: the target follows the live figure, so Stage A wanting less shrinks it",
        "coordinator.py",
        "        frozen = self._campaign_frozen_target_kwh\n"
        "        if frozen is None or live > frozen:\n"
        "            self._campaign_frozen_target_kwh = live",
        "        self._campaign_frozen_target_kwh = live",
        f"{PUB}::test_the_target_never_shrinks",
    ),
    (
        "T3: an unstarted campaign is grown, so the opening read is overruled",
        "coordinator.py",
        "        if self._campaign_started_at is None:\n            return\n"
        "        live = self._campaign_objective_kwh(self._campaign_id)",
        "        live = self._campaign_objective_kwh(self._campaign_id)",
        f"{PUB}::test_growth_is_asked_only_of_the_open_started_instance",
    ),
    # =====================================================================
    # O -- our own tail, and only our own
    # =====================================================================
    (
        "O1: the deadline is not compared, so a later dispatch inherits our receipt",
        "execution.py",
        "        if abs((live - claimed).total_seconds()) > "
        "OWNERSHIP_START_TOLERANCE_SECONDS:\n            return False",
        "        if False:\n            return False",
        f"{OWN}::test_a_dispatch_armed_after_our_release_is_still_foreign",
    ),
    (
        "O2: an expired receipt still excuses a running dispatch",
        "execution.py",
        "        return self.now < claimed",
        "        return True",
        f"{OWN}::test_a_passed_deadline_is_no_longer_our_tail",
    ),
    (
        "O3: a receipt with no deadline is treated as a claim",
        "execution.py",
        '        claimed = instant_of(receipt.get("timer_finishes_at"))\n'
        "        if claimed is None:\n            return False",
        '        claimed = instant_of(receipt.get("timer_finishes_at"))\n'
        "        if claimed is None:\n            return True",
        f"{OWN}::test_an_unreadable_timer_falls_back_to_foreign",
    ),
    (
        "O4: our own tail names an ownership conflict again, and can file canceled",
        "execution.py",
        "            stop_reason=None,\n"
        "            inhibit_reason=INHIBIT_OWN_DISPATCH_RELEASING,",
        "            stop_reason=EXECUTION_STOP_OWNERSHIP_CONFLICT,\n"
        "            inhibit_reason=INHIBIT_OWN_DISPATCH_RELEASING,",
        f"{OWN}::test_our_own_tail_names_no_ownership_conflict",
    ),
    (
        "O5: the receipt is written for an unreadable timer, inventing a grace",
        "coordinator.py",
        "        finishes = None if snapshot is None else "
        "snapshot.dispatch_timer_finishes_at\n"
        "        if finishes is None:\n"
        "            self._release_receipt = None\n"
        "            return",
        "        finishes = None if snapshot is None else "
        "snapshot.dispatch_timer_finishes_at\n"
        "        if finishes is None:\n"
        "            finishes = now",
        f"{OWN}::test_no_receipt_is_written_when_the_register_will_not_say",
    ),
    # =====================================================================
    # C -- representable is not controllable
    # =====================================================================
    (
        "C1: the controllability clause is dropped, so the live 0.04 kWh row arms",
        "economic.py",
        "        elif (\n            intent == EXECUTION_INTENT_NET_EXPORT\n"
        "            and objective_kwh < MIN_CONTROLLABLE_QUARTER_KWH\n        ):",
        "        elif False:",
        f"{MAT}::test_the_live_tiny_export_row_is_published_and_not_armable",
    ),
    (
        "C2: the floor is asked of a charge row too, forfeiting PV absorption",
        "economic.py",
        "        elif (\n            intent == EXECUTION_INTENT_NET_EXPORT\n"
        "            and objective_kwh < MIN_CONTROLLABLE_QUARTER_KWH\n        ):",
        "        elif objective_kwh < MIN_CONTROLLABLE_QUARTER_KWH:",
        f"{MAT}::test_a_small_charge_row_is_not_gated_by_the_export_floor",
    ),
    (
        "C3: the two floors collapse into one, so the refusals stop being separable",
        "const.py",
        "MIN_CONTROLLABLE_QUARTER_KWH: Final = (\n"
        "    DISPATCH_POWER_DEADBAND_KW + DISPATCH_POWER_STEP_KW\n) * 0.25",
        "MIN_CONTROLLABLE_QUARTER_KWH: Final = MIN_EXECUTABLE_QUARTER_KWH",
        f"{MAT}::test_the_two_floors_are_separate_and_the_controllable_one_is_larger",
    ),
    (
        "C4: Stage B's backstop asks the resolution question for an export row",
        "coordinator.py",
        "        if row.intent == EXECUTION_INTENT_NET_EXPORT:\n"
        "            return MIN_CONTROLLABLE_QUARTER_KWH\n"
        "        return MIN_EXECUTABLE_QUARTER_KWH",
        "        return MIN_EXECUTABLE_QUARTER_KWH",
        f"{MAT}::test_stage_b_asks_the_same_floor_stage_a_did",
    ),
]
