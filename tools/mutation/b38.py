"""Break each beta.38 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out which
kind you have is to break the thing and watch.

Most of these are literally the beta.37 line this release replaced -- the line that
filed a terminal against a 4.53 kWh Sell at the very refresh its first row opened, and
then armed 9.7 kW. A surviving mutation means the test is vacuous and **the test gets
rewritten**; it never means the mutation gets weakened.

Two classes deserve naming. The ``B`` mutations break the **Buy** direction
specifically: the lifecycle is shared but the objective domains are not, and a suite
that only proved the Sell would leave the charge's battery-versus-grid asymmetry
unguarded. The ``V`` mutations break the **fixture**, so its own vacuity gates are
tested rather than asserted -- a replan that still overlaps the current row would let
the broken code pass, and something has to notice that.

The beta.35, beta.36 and beta.37 tables are siblings and must all still pass: a
beta.38 correction that resurrects an earlier defect is a regression, not a fix.

Run with:  python tools/mutation/run.py b38 [-k substring]
"""

from __future__ import annotations

AUTH = "tests/test_beta38_opened_row_authority.py"
ZOMBIE = "tests/test_beta38_no_zombie.py"
LEDGER = "tests/test_beta38_ledger.py"
DIAG = "tests/test_beta27_diagnostics.py"
LIFECYCLE36 = "tests/test_beta36_lifecycle.py"
CONTINUITY = "tests/test_beta35_campaign_continuity.py"

SELL = "[net_export]"
BUY = "[grid_charge]"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------- the opened-row guard itself
    (
        "A1: restore the unguarded withdrawal-by-absence (Sell)",
        "execution.py",
        "    if row_open:\n        return CarryOutcome(carried=carried)",
        "    if False:\n        return CarryOutcome(carried=carried)",
        f"{AUTH}::test_an_opened_row_is_not_withdrawn_by_absence{SELL}",
    ),
    (
        "A1b: restore the unguarded withdrawal-by-absence (Buy)",
        "execution.py",
        "    if row_open:\n        return CarryOutcome(carried=carried)",
        "    if False:\n        return CarryOutcome(carried=carried)",
        f"{AUTH}::test_an_opened_row_is_not_withdrawn_by_absence{BUY}",
    ),
    (
        "A1c: and with Stage A publishing nothing at all",
        "execution.py",
        "    if row_open:\n        return CarryOutcome(carried=carried)",
        "    if False:\n        return CarryOutcome(carried=carried)",
        f"{AUTH}::test_publishing_nothing_at_all_does_not_end_an_opened_row{SELL}",
    ),
    (
        "A2: the opened-row guard swallows the window-end bound too",
        "execution.py",
        "    if now >= carried.window_end:\n        return CarryOutcome(\n"
        "            carried=None, ended=EXECUTION_STOP_WINDOW_ENDED, ended_run=carried\n"
        "        )",
        "    if False:\n        return CarryOutcome(\n"
        "            carried=None, ended=EXECUTION_STOP_WINDOW_ENDED, ended_run=carried\n"
        "        )",
        f"{CONTINUITY}::test_a_campaign_cannot_outlive_its_own_window",
    ),
    # -------------------------------------- the boundary, off by one either way
    (
        # A first version of this mutated ``_opened_row_owns`` to look a quarter
        # ahead and *survived*: the ``row_covering(now)`` clause beside it already
        # refuses, so the edit proved nothing about the boundary. The boundary is
        # ``has_opened``, so that is where it belongs.
        "A3: the row counts as open one interval early",
        "execution.py",
        "        return moment >= self.starts_at",
        "        return moment >= self.starts_at - timedelta(minutes=15)",
        f"{AUTH}::test_before_the_row_opens_a_withdrawal_is_still_allowed{SELL}",
    ),
    (
        "A3b: and the Buy sees it too",
        "execution.py",
        "        return moment >= self.starts_at",
        "        return moment >= self.starts_at - timedelta(minutes=15)",
        f"{AUTH}::test_before_the_row_opens_a_withdrawal_is_still_allowed{BUY}",
    ),
    (
        "A3c: the row counts as open one interval late, so the opening refresh is bare",
        "execution.py",
        "        return moment >= self.starts_at",
        "        return moment >= self.starts_at + timedelta(minutes=15)",
        f"{AUTH}::test_an_opened_row_is_not_withdrawn_by_absence{SELL}",
    ),
    (
        "A4: the admitted/opened distinction is removed entirely",
        "execution.py",
        "        return moment >= self.starts_at",
        "        return True",
        f"{AUTH}::test_before_the_row_opens_a_withdrawal_is_still_allowed{SELL}",
    ),
    (
        "A5: a run outliving its schedule keeps itself alive on its own window",
        "coordinator.py",
        "        plan = self._plan\n"
        "        if plan is None or plan.run_id != carried.run_id:\n"
        "            return False",
        "        plan = self._plan\n"
        "        if plan is None:\n"
        "            return True\n"
        "        if plan.run_id != carried.run_id:\n"
        "            return False",
        f"{CONTINUITY}::test_a_truly_stale_execution_still_fails_closed",
    ),
    # ------------------------------------------------- the authority predicate
    (
        "A6: restore the persisted-claim requirement",
        "coordinator.py",
        "        not_armed_under_another = recorded is None or recorded == authority",
        "        not_armed_under_another = recorded is not None and recorded == authority",
        f"{AUTH}::test_a_foreign_claim_still_refuses_authority{SELL}",
    ),
    (
        "A7: any claim at all passes authority, foreign or not",
        "coordinator.py",
        "        not_armed_under_another = recorded is None or recorded == authority",
        "        not_armed_under_another = True",
        f"{AUTH}::test_a_foreign_claim_still_refuses_authority{SELL}",
    ),
    (
        "A8: the abandonment latch stops refusing authority",
        "coordinator.py",
        "            and not_armed_under_another\n"
        "            and not self._admission_abandoned(plan)",
        "            and not_armed_under_another",
        f"{AUTH}::test_a_foreign_claim_still_refuses_authority{SELL}",
    ),
    (
        "A9: an abort reason becomes suppressible",
        "const.py",
        "EXECUTION_WITHDRAWAL_STOP_REASONS: Final = (\n    EXECUTION_STOP_STALE_PLAN,",
        "EXECUTION_WITHDRAWAL_STOP_REASONS: Final = (\n"
        "    EXECUTION_STOP_SAFETY,\n    EXECUTION_STOP_STALE_PLAN,",
        f"{AUTH}::test_the_authority_suppresses_absence_and_nothing_else{SELL}",
    ),
    # --------------------------------------- the frozen work, both directions
    (
        # **Both halves, because either alone survives -- which is the property.**
        # Dropping the keep-clause lets the plan be re-derived every refresh, and the
        # carried run's target is immutable, so it re-derives to the *same* schedule.
        # Defence in depth is welcome, but a mutation that only removes one layer
        # proves nothing about the other, so this removes the keep-clause and points
        # the rebuild at whatever Stage A published instead.
        "F1: an affirming publication replaces the accepted Sell target",
        "execution.py",
        "        target=carried.target,\n        revision=carried.revision",
        "        target=published,\n        revision=carried.revision",
        f"{AUTH}::test_an_affirming_publication_may_not_shrink_the_accepted_work{SELL}",
    ),
    (
        "F2: an affirming publication replaces the accepted Buy target",
        "execution.py",
        "        target=carried.target,\n        revision=carried.revision",
        "        target=published,\n        revision=carried.revision",
        f"{AUTH}::test_an_affirming_publication_may_not_shrink_the_accepted_work{BUY}",
    ),
    (
        "F2b: the opened schedule stops being kept at all",
        "execution.py",
        "    if current is not None:\n"
        "        if current.has_opened(now) and now < current.ends_at:\n"
        "            return current, None",
        "    if current is not None:\n        if False:\n            return current, None",
        # A first witness only compared the *figures*, which a rebuild preserves --
        # the carried run's target is immutable, so the schedule re-derives to the
        # same thing. ``admitted_at`` is what actually moves.
        f"{AUTH}::test_an_opened_schedule_is_returned_unchanged_not_re_derived{SELL}",
    ),
    (
        "F3: the second frozen row is skipped at the boundary (Sell)",
        "execution.py",
        "    def row_covering(self, moment: datetime) -> QuarterRow | None:",
        "    def row_covering(self, moment: datetime) -> QuarterRow | None:\n"
        "        return self.rows[0] if self.rows else None",
        f"{AUTH}::test_the_second_frozen_row_is_still_reachable{SELL}",
    ),
    (
        "F3b: and in the Buy",
        "execution.py",
        "    def row_covering(self, moment: datetime) -> QuarterRow | None:",
        "    def row_covering(self, moment: datetime) -> QuarterRow | None:\n"
        "        return self.rows[0] if self.rows else None",
        f"{AUTH}::test_the_second_frozen_row_is_still_reachable{BUY}",
    ),
    (
        # The latch, not the ordering -- Z6 covers the ordering. No replay arms
        # twice inside one campaign, so the witness asserts on the helper directly.
        "F4: the campaign target may shrink after it is frozen",
        "coordinator.py",
        "        if self._campaign_id is None or self._campaign_started_at is not None:\n"
        "            return",
        "        if self._campaign_id is None:\n            return",
        f"{AUTH}::test_the_campaign_start_freeze_is_idempotent{SELL}",
    ),
    (
        "F4b: and in the Buy",
        "coordinator.py",
        "        if self._campaign_id is None or self._campaign_started_at is not None:\n"
        "            return",
        "        if self._campaign_id is None:\n            return",
        f"{AUTH}::test_the_campaign_start_freeze_is_idempotent{BUY}",
    ),
    (
        # The first version added a line inside the branch that already resets both,
        # so it was a no-op. Reset the accumulator where it is *read* instead.
        "F5: realised campaign energy resets on every replan",
        "coordinator.py",
        "    def _campaign_realized_now(self) -> float:",
        "    def _campaign_realized_now(self) -> float:\n"
        "        self._campaign_realized_kwh = 0.0",
        f"{CONTINUITY}::test_the_campaign_survives_both_boundaries",
    ),
    # ------------------------------------------------- the Buy's own domains
    (
        "B1: the Buy objective becomes the grid ceiling",
        "execution.py",
        "        if self.intent == EXECUTION_INTENT_NET_EXPORT:\n"
        "            return max(0.0, self.grid_export_target_kwh)\n"
        "        return max(0.0, self.battery_allowance_kwh())",
        "        if self.intent == EXECUTION_INTENT_NET_EXPORT:\n"
        "            return max(0.0, self.grid_export_target_kwh)\n"
        "        return max(0.0, self.grid_authorised_kwh)",
        f"{AUTH}::test_a_buy_campaign_is_judged_at_the_battery",
    ),
    (
        "B2: the Sell objective becomes the battery ceiling",
        "execution.py",
        "        if self.intent == EXECUTION_INTENT_NET_EXPORT:\n"
        "            return max(0.0, self.grid_export_target_kwh)\n"
        "        return max(0.0, self.battery_allowance_kwh())",
        "        return max(0.0, self.battery_allowance_kwh())",
        f"{AUTH}::test_a_sell_campaign_is_judged_at_the_meter",
    ),
    (
        "B3: the grid authorisation is applied twice again",
        "dispatch.py",
        "    clamped_kw, clamp_reason = clamp_charge_kw(\n"
        "        applied_kw, replace(limits, remaining_grid_kw=None)\n"
        "    )",
        "    clamped_kw, clamp_reason = clamp_charge_kw(applied_kw, limits)",
        "tests/test_beta36_charge_domains.py"
        "::test_absorbing_production_causes_no_import_beyond_the_authorisation",
    ),
    (
        "B4: free production stops paying toward the battery objective",
        "dispatch.py",
        "    pv_surplus_kw = max(0.0, pv_kw - house_load_kw)",
        "    pv_surplus_kw = 0.0",
        "tests/test_beta36_charge_domains.py"
        "::test_an_unfinished_row_absorbs_production_when_the_budget_is_spent",
    ),
    # ------------------------------------------- the terminal that was filed
    (
        "T1: a terminal is filed even when the withdrawal is withheld",
        "coordinator.py",
        "        if outcome.ended is not None and outcome.ended_run is not None:\n"
        "            self._remember_ended_run(outcome.ended, outcome.ended_run, plan, now)",
        "        if outcome.ended_run is not None or outcome.carried is not None:\n"
        "            self._remember_ended_run(\n"
        "                outcome.ended or EXECUTION_STOP_STAGE_A_HOLD,\n"
        "                outcome.ended_run or outcome.carried,\n"
        "                plan,\n"
        "                now,\n"
        "            )",
        f"{AUTH}::test_an_opened_row_is_not_withdrawn_by_absence{SELL}",
    ),
    (
        "T2: cleanup is omitted after a terminal",
        "coordinator.py",
        "        elif resetting:\n"
        "            stage_one = plan_dispatch_stop()\n"
        "            stage_two = plan_dispatch_cleanup()",
        "        elif resetting:\n            stage_one = plan_dispatch_stop()",
        f"{ZOMBIE}::test_a_restart_mid_run_produces_a_verified_stop{SELL}",
    ),
    (
        "T3: a closed instance may reopen",
        "coordinator.py",
        "    def _instance_closed(self, instance_id: str | None) -> bool:",
        "    def _instance_closed(self, instance_id: str | None) -> bool:\n"
        "        return False",
        f"{AUTH}::test_an_instance_that_filed_its_terminal_files_no_second_one",
    ),
    # ------------------------------------------------------- the zombie guard
    (
        "Z1: the tick returns on no_quarter before asking what is running",
        "coordinator.py",
        "        if not snapshot.dispatch_active:\n"
        "            self._note_tick(now, TICK_SKIPPED_DISPATCH_INACTIVE)\n"
        "            return\n"
        "        owned_now = self._ownership_now(snapshot, now) == OWNERSHIP_OWNED",
        "        if quarter is None and run is None:\n"
        "            self._note_tick(now, TICK_SKIPPED_NO_QUARTER)\n"
        "            return\n"
        "        if not snapshot.dispatch_active:\n"
        "            self._note_tick(now, TICK_SKIPPED_DISPATCH_INACTIVE)\n"
        "            return\n"
        "        owned_now = self._ownership_now(snapshot, now) == OWNERSHIP_OWNED\n"
        "        if False:",
        f"{DIAG}::test_an_owned_dispatch_with_no_authority_is_stopped_not_reported",
    ),
    (
        "Z2: the orphan is reported rather than stopped",
        "coordinator.py",
        "            if owned_now:\n"
        "                # **The orphan, stopped on the cadence that found it.** Routed",
        "            if False:\n"
        "                # **The orphan, stopped on the cadence that found it.** Routed",
        f"{ZOMBIE}::test_the_tick_stops_an_owned_dispatch_with_no_authority{SELL}",
    ),
    (
        "Z3: the lifecycle wiring is removed again",
        "coordinator.py",
        "        self._note_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=ownership_state,",
        "        self._skip_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=ownership_state,",
        f"{AUTH}::test_the_lifecycle_field_leaves_idle{SELL}",
    ),
    (
        "Z4: an owned refresh may report idle",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING",
        "        if False:\n            return LIFECYCLE_EXECUTING",
        f"{ZOMBIE}::test_idle_is_unreachable_while_anything_is_owned",
    ),
    (
        "Z5: a stopping refresh reports idle instead",
        "coordinator.py",
        "        if resetting or releasing:\n            return LIFECYCLE_STOPPING",
        "        if resetting or releasing:\n            return LIFECYCLE_IDLE",
        f"{ZOMBIE}::test_idle_is_unreachable_while_anything_is_owned",
    ),
    (
        "Z6: the campaign start-freeze moves one refresh late again",
        "coordinator.py",
        "            if self._pending_activates:\n"
        "                # **The freeze happens in the same transition as the write. beta.38.**",
        "            if False:\n"
        "                # **The freeze happens in the same transition as the write. beta.38.**",
        f"{AUTH}::test_no_refresh_reports_an_armed_campaign_as_unstarted{SELL}",
    ),
    (
        "Z6b: and the Buy notices it too",
        "coordinator.py",
        "            if self._pending_activates:\n"
        "                # **The freeze happens in the same transition as the write. beta.38.**",
        "            if False:\n"
        "                # **The freeze happens in the same transition as the write. beta.38.**",
        f"{AUTH}::test_no_refresh_reports_an_armed_campaign_as_unstarted{BUY}",
    ),
    # ---------------------------------------------------------- the accounting
    (
        "L1: the opening position value is dropped again",
        "coordinator.py",
        "            opening_inventory_value_eur=self._position_value_eur(\n"
        '                outcome, opening_inventory_kwh(series["stored_energy_kwh"])\n'
        "            ),",
        "            opening_inventory_value_eur=None,",
        f"{LEDGER}::test_the_opening_position_value_is_no_longer_null",
    ),
    (
        "L2: the identity drops the opening term",
        "realized.py",
        "        return round(\n"
        "            base + self.closing_inventory_value_eur - self.opening_inventory_value_eur,\n"
        "            _EUR_DECIMALS,\n        )",
        "        return round(base + self.closing_inventory_value_eur, _EUR_DECIMALS)",
        f"{LEDGER}::test_the_position_identity_reconciles",
    ),
    (
        "L3: the two ends are valued at different energies again",
        "coordinator.py",
        "            closing_inventory_value_eur=self._position_value_eur(\n"
        '                outcome, closing_inventory_kwh(series["stored_energy_kwh"])\n'
        "            ),",
        "            closing_inventory_value_eur=self._position_value_eur(\n"
        "                outcome, outcome.desired.intervals[0].start_energy_dc_kwh\n"
        "            ),",
        f"{LEDGER}::test_the_energy_valued_is_the_energy_reported",
    ),
    (
        "L4: the opening energy is read by a second, different rule",
        "coordinator.py",
        '                outcome, opening_inventory_kwh(series["stored_energy_kwh"])',
        '                outcome, closing_inventory_kwh(series["stored_energy_kwh"])',
        f"{LEDGER}::test_the_energy_valued_is_the_energy_reported",
    ),
    (
        "L5: realised and the decision advantage are forced to sum",
        "realized.py",
        "        return round(\n"
        "            base + self.closing_inventory_value_eur - self.opening_inventory_value_eur,\n"
        "            _EUR_DECIMALS,\n        )",
        "        return round(base + 0.4397, _EUR_DECIMALS)",
        # **Re-anchored in beta.41.** Still caught, and by more than before -- eight
        # cases across the ledger and the beta.39 accounting family -- but the
        # assertion that fails first moved when the physical endpoint became one
        # quantity. The position identity is the sharper statement of the same
        # thing: realised cash and the decision advantage are different quantities
        # and forcing them to sum breaks the identity that reconciles them.
        f"{LEDGER}::test_the_position_identity_reconciles",
    ),
    (
        "L6: the position values go back to raw floats",
        "realized.py",
        "        opening_inventory_value_eur=(\n"
        "            None\n"
        "            if opening_inventory_value_eur is None\n"
        "            else round(opening_inventory_value_eur, _EUR_DECIMALS)\n"
        "        ),",
        "        opening_inventory_value_eur=(\n"
        "            None\n"
        "            if opening_inventory_value_eur is None\n"
        "            else opening_inventory_value_eur + 1e-9\n"
        "        ),",
        f"{LEDGER}::test_the_position_values_are_rounded_like_every_other_euro",
    ),
    # ------------------------------- the fixture's own vacuity, as b36/b37 do
    (
        "V1: the replan still overlaps the open row, so nothing is withdrawn",
        "tests/beta38_trace.py",
        "    start = opens_at(len(rows_for(intent)) + 4)",
        "    start = opens_at(0)",
        f"{AUTH}::test_before_the_row_opens_a_withdrawal_is_still_allowed{SELL}",
    ),
    (
        "V2: the Buy fixture authorises the whole objective from the grid",
        "tests/beta38_trace.py",
        "BUY_ROWS: tuple[tuple[float, float], ...] = ((0.56, 0.30), (0.56, 0.30))",
        "BUY_ROWS: tuple[tuple[float, float], ...] = ((0.56, 0.56), (0.56, 0.56))",
        f"{AUTH}::test_a_buy_campaign_is_judged_at_the_battery",
    ),
    (
        "V3: the run is admitted with its row already open",
        "tests/test_beta38_opened_row_authority.py",
        # Extended to a unique match. The shorter form matched the withdrawal step in a later test, and the
        # first match is the one that gets edited -- so the mutation could run
        # against code the named test does not exercise. Same site, same change.
        "    publish(coordinator, monkeypatch, (target_for(intent),))\n"
        "    report = await step_once(hass, coordinator, live_surface, **step_clock(-1))",
        "    publish(coordinator, monkeypatch, (target_for(intent),))\n"
        "    report = await step_once(hass, coordinator, live_surface, **step_clock(0))",
        f"{AUTH}::test_an_opened_row_is_not_withdrawn_by_absence{SELL}",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
