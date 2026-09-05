"""Break each beta.40 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch. A surviving mutation means
the test is vacuous and **the test gets rewritten**; it never means the mutation
gets weakened.

Five families, matching the release.

The ``A`` mutations attack the arithmetic, and the dangerous direction is the one
that still *works*. Turning the branch into a ``min`` breaks it loudly; bounding it
by the grid rate instead of the surplus makes it quietly buy energy, and adding
``grid_cap_kw`` back into it makes the meter go positive on a row whose production
fell short. A1 is the one that would have shipped: it recovers a little of the
export and leaves most of it, which is exactly the outcome the acceptance case
refuses.

The ``B`` mutations attack the boundary. ``economic.py`` must hold no physical
limit, so these put one back -- an inverter rating, a pack ceiling -- and check that
the Phase-8 guard still fires. One of them is a rename rather than a limit: it
restores the identifier the live-fact guard forbids, which is how the first draft of
this release failed.

The ``E`` mutations attack the envelope's authority. They inherit the two
downward-only purchase caps that beta.40 deliberately refuses, and they let a later
publication re-decide an open row -- the beta.38 defect, reintroduced through a new
field.

The ``S`` mutations attack the split. S1 and S2 are the killed-campaign path: count
absorbed production into the objective and a sunny afternoon ends a campaign that
has not finished buying. S3 is subtler and was a real bug during implementation --
key the overshoot guard on the reported clamp token instead of on the branch, and a
*clamped* absorbing tick gets zeroed by its own guard.

The ``H`` mutations attack the satisfied row, which is where beta.36 measured the
hardware. Routing the third outcome back through the zero hold restores the leak
exactly; leaving ``_quarter_is_satisfied`` alone lets the economic cadence write a
zero over a live absorption; dropping the resolution floor leaves the tick writing a
trickle the device cannot express.

The ``D`` family is **retired in beta.41** and the reason is recorded at its old
position in the table: it mutated the two-endpoint distinction, and beta.41
collapsed that into one physical energy, which makes two of the three anchors
vanish and the third an equivalent mutant.

The ``V`` mutations break the **fixtures**, so their own vacuity gates are tested
rather than asserted.

The beta.35, beta.36, beta.37, beta.38 and beta.39 tables are siblings and must all
still pass: a beta.40 correction that resurrects an earlier defect is a regression,
not a fix.

Run with:  python tools/mutation/run.py b40 [-k substring]
"""

from __future__ import annotations

ARITH = "tests/test_beta40_absorption_arithmetic.py"
AUTH = "tests/test_beta40_opened_row_authority.py"
SPLIT = "tests/test_beta40_split_accumulator.py"
SAT = "tests/test_beta40_satisfied_with_envelope.py"
SAFETY = "tests/test_beta40_safety_buy_unchanged.py"
NEUTRAL = "tests/test_beta40_neutrality.py"
PHASE8 = "tests/test_phase_eight_boundaries.py"
B36 = "tests/test_beta36_charge_domains.py"
CEIL = "tests/test_beta40_retention_ceiling.py"
FLOOR = "tests/test_beta40_hard_floor.py"
TERM = "tests/test_beta40_campaign_terminal.py"
PACE = "tests/test_beta40_grid_pace.py"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # A -- the arithmetic
    # =====================================================================
    (
        "A1: the branch reduces instead of raising, so most export stays",
        "dispatch.py",
        "    absorbing = absorb_kw > applied_kw + 1e-9\n",
        "    absorbing = 0.0 < absorb_kw < applied_kw - 1e-9\n",
        f"{ARITH}::test_the_authorised_row_stores_the_surplus_instead",
    ),
    (
        "A2: recover a tenth of the surplus rather than the whole of it",
        "dispatch.py",
        "    absorb_kw = pv_surplus_kw if progress.retention_authorised else 0.0\n",
        "    absorb_kw = pv_surplus_kw * 0.1 if progress.retention_authorised else 0.0\n",
        f"{ARITH}::test_the_gain_is_the_whole_surplus_and_not_a_fraction_of_it",
    ),
    (
        "A3: bound the branch by the grid rate, so it buys",
        "dispatch.py",
        "    absorb_kw = pv_surplus_kw if progress.retention_authorised else 0.0\n",
        "    absorb_kw = (\n"
        "        pv_surplus_kw + grid_rate_cap_kw\n"
        "        if progress.retention_authorised\n"
        "        else 0.0\n"
        "    )\n",
        f"{ARITH}::test_the_absorption_branch_can_never_cause_grid_import",
    ),
    (
        "A4: apply the verdict as a min over total battery power (beta.36 mirror)",
        "dispatch.py",
        "    if absorbing:\n        applied_kw = absorb_kw\n",
        "    if absorbing:\n        applied_kw = absorb_kw\n"
        "    elif progress.retention_authorised:\n"
        "        applied_kw = min(applied_kw, absorb_kw)\n",
        f"{ARITH}::test_the_verdict_never_caps_a_grid_fed_objective",
    ),
    (
        "A5: an unauthorised row no longer matches beta.39",
        "dispatch.py",
        "    absorb_kw = pv_surplus_kw if progress.retention_authorised else 0.0\n",
        "    absorb_kw = pv_surplus_kw\n",
        f"{ARITH}::test_an_unauthorised_row_never_absorbs_at_all",
    ),
    (
        "A6: the branch escapes the physical clamps",
        "dispatch.py",
        "    clamped_kw, clamp_reason = clamp_charge_kw(\n"
        "        applied_kw, replace(limits, remaining_grid_kw=None)\n"
        "    )\n",
        "    clamped_kw, clamp_reason = (\n"
        "        (applied_kw, DISPATCH_LIMIT_NONE)\n"
        "        if absorbing\n"
        "        else clamp_charge_kw(applied_kw, replace(limits, remaining_grid_kw=None))\n"
        "    )\n",
        f"{ARITH}::test_the_physical_clamps_still_bound_an_absorbing_tick",
    ),
    # =====================================================================
    # R -- the retention ceiling, which the audit added
    #
    # The first implementation froze a boolean and let Stage B absorb to the
    # physical limits. Swept over the seven production shapes that is reachable
    # authority the economics never granted: in five of them the verdict grants
    # at the opening level while a level the same row reaches fails the same
    # comparison, worst case -0.171 EUR/kWh over 2.108 kWh DC. These put the
    # boolean back, one way at a time.
    # =====================================================================
    (
        "R1: drop the ceiling and go back to unbounded boolean authority",
        "economic.py",
        "                if retention_ok:\n",
        "                if False:\n",
        f"{CEIL}::test_an_authorised_row_publishes_its_ceiling",
    ),
    (
        "R1b: the ceiling method itself stops bounding",
        "economic.py",
        "        if not self.marginal_curve_eur_kwh or self.bucket_dc_kwh <= 0.0:\n",
        "        if True:\n",
        f"{CEIL}::test_the_ceiling_retains_no_negative_value_step_anywhere",
    ),
    (
        "R2: the walk keeps going past the first crossing",
        "economic.py",
        "            if value is None or self.round_trip_efficiency * value <= export_price:\n                break\n",
        "            if value is None:\n                break\n",
        f"{CEIL}::test_the_ceiling_stops_at_the_first_crossing_and_not_beyond_it",
    ),
    (
        "R3: an undefined level is treated as passing",
        "economic.py",
        "            if value is None or self.round_trip_efficiency * value <= export_price:\n",
        "            if value is not None and self.round_trip_efficiency * value <= export_price:\n",
        f"{CEIL}::test_an_undefined_level_stops_the_walk",
    ),
    (
        "R4: no curve reads as a ceiling of zero rather than unbounded",
        "economic.py",
        "        if not self.marginal_curve_eur_kwh or self.bucket_dc_kwh <= 0.0:\n            return None\n",
        "        if not self.marginal_curve_eur_kwh or self.bucket_dc_kwh <= 0.0:\n            return 0.0\n",
        f"{CEIL}::test_a_gate_with_no_curve_is_unbounded_rather_than_zero",
    ),
    (
        "R5: the controller ignores the ceiling it was handed",
        "dispatch.py",
        "    if retention_rate_kw is not None:\n        absorb_kw = min(absorb_kw, retention_rate_kw)\n",
        "    if retention_rate_kw is None:\n        absorb_kw = min(absorb_kw, 99.0)\n",
        f"{CEIL}::test_the_command_is_bounded_by_what_is_still_worth_keeping",
    ),
    (
        "R6: the ceiling is applied to the objective as well as to keeping",
        "dispatch.py",
        "        absorb_kw = min(absorb_kw, retention_rate_kw)\n",
        "        absorb_kw = min(absorb_kw, retention_rate_kw)\n        applied_kw = min(applied_kw, retention_rate_kw)\n",
        f"{CEIL}::test_the_ceiling_never_reduces_the_objective",
    ),
    (
        "R7: the pack bound is taken from the stale plan instead of live",
        "coordinator.py",
        "        stored_dc = limits.energy_for_soc(soc_percent)\n",
        "        stored_dc = ceiling_dc0 = (\n            limits.energy_for_soc(100.0) - plan.state.headroom_energy_kwh\n        )\n",
        f"{SAT}::test_a_stale_plan_state_cannot_widen_the_pack_bound",
    ),
    (
        "R8: the pack ceiling drops out of the bound",
        "coordinator.py",
        "        cap_dc = ceiling_dc if until_dc is None else min(until_dc, ceiling_dc)\n",
        "        cap_dc = ceiling_dc if until_dc is None else until_dc\n",
        f"{SAT}::test_absorption_is_bounded_by_the_packs_own_room_at_every_soc",
    ),
    (
        "R9: an exhausted ceiling still counts as live absorption",
        "coordinator.py",
        "        retainable = progress.retention_remaining_kwh\n        return retainable is None or retainable > QUARTER_TARGET_TOLERANCE_KWH\n",
        "        return True\n",
        f"{SAT}::test_an_exhausted_economic_ceiling_stops_absorption_with_room_to_spare",
    ),
    # =====================================================================
    # B -- the Stage-A boundary
    # =====================================================================
    (
        "B1: give the optimiser back an inverter rating to compare against",
        "economic.py",
        "    marginal_value_eur_kwh: float | None\n",
        "    marginal_value_eur_kwh: float | None\n    max_charge_kw: float = 10.0\n",
        f"{NEUTRAL}::test_the_gate_holds_no_physical_limit",
    ),
    (
        "B2: constrain by a hardware limit inside the gate",
        "economic.py",
        "        if self.round_trip_efficiency * value <= export_price:\n",
        "        if max(self.round_trip_efficiency * value, 0.0) <= export_price:\n"
        "            return False, RETENTION_GATE_EXPORT_SUPERIOR\n"
        "        if self.round_trip_efficiency * value <= export_price:\n",
        f"{NEUTRAL}::test_the_verdict_is_a_strict_comparison_in_one_place",
    ),
    (
        "B3: restore the live-fact identifier the Phase-8 guard forbids",
        "economic.py",
        "        retention_ok = False\n",
        "        absorption = retention\n        retention_ok = False\n",
        f"{PHASE8}::test_no_live_installation_fact_reaches_the_optimizer",
    ),
    (
        "B4: the gate grants where the dual is undefined",
        "economic.py",
        "        if value is None:\n            return False, RETENTION_GATE_VALUE_UNDEFINED\n",
        "        if value is None:\n            return True, RETENTION_GATE_VALUE_UNDEFINED\n",
        f"{SAFETY}::test_an_undefined_dual_refuses_rather_than_granting",
    ),
    (
        "B5: the gate grants with no export price to compare against",
        "economic.py",
        "        if export_price is None:\n            return False, RETENTION_GATE_NO_PRICE\n",
        "        if export_price is None:\n            return True, RETENTION_GATE_NO_PRICE\n",
        f"{SAFETY}::test_a_missing_export_price_refuses_too",
    ),
    (
        "B6: the verdict alters the battery objective it sits beside",
        "economic.py",
        '            row["retention_authorised"] = retention_ok\n',
        '            row["retention_authorised"] = retention_ok\n'
        '            row["battery_kwh"] = _round_kwh(max(0.0, battery_kwh) * 1.5)\n',
        f"{SAFETY}::test_the_gate_adds_two_keys_and_moves_no_other_figure",
    ),
    (
        "B7: the gate reaches a row that is not a charge",
        "economic.py",
        "            if intent != EXECUTION_INTENT_GRID_CHARGE or not_executable is not None:\n"
        "                retention_gate = RETENTION_GATE_NOT_A_CHARGE\n",
        "            if False:\n"
        "                retention_gate = RETENTION_GATE_NOT_A_CHARGE\n",
        f"{SAFETY}::test_the_gate_never_reaches_a_row_that_is_not_a_charge",
    ),
    (
        "B8: publish the keys even with no gate, changing the beta.39 shape",
        "economic.py",
        "        if retention_gate is not None:\n",
        "        if True:\n",
        f"{SAFETY}::test_a_publication_without_a_gate_carries_no_gate_keys_at_all",
    ),
    # =====================================================================
    # E -- the opened row's authority
    # =====================================================================
    (
        "E1: inherit the run-level purchase cap onto the verdict",
        "execution.py",
        "        return self.retention_authorised\n",
        "        return self.retention_authorised and not (\n"
        "            self.frozen_remaining_at_admission_kwh is not None\n"
        "            and self.frozen_remaining_at_admission_kwh <= 0.0\n"
        "        )\n",
        f"{AUTH}::test_the_run_level_frozen_remainder_does_not_bound_the_verdict",
    ),
    (
        "E2: absence reads back as a grant rather than a refusal",
        "execution.py",
        '                retention_authorised=entry.get("retention_authorised") is True,\n',
        '                retention_authorised=entry.get("retention_authorised") is not False,\n',
        f"{AUTH}::test_a_publication_without_a_verdict_reads_back_as_a_refusal",
    ),
    (
        "E3: a truthy string authorises a malformed record",
        "execution.py",
        '                retention_authorised=entry.get("retention_authorised") is True,\n',
        '                retention_authorised=bool(entry.get("retention_authorised")),\n',
        f"{AUTH}::test_a_non_boolean_verdict_is_a_refusal_rather_than_a_truthy_grant",
    ),
    (
        "E4: the serialiser drops the verdict, losing it across a restart",
        "execution.py",
        '            "retention_authorised": self.retention_authorised,\n'
        '            "retention_gate": self.retention_gate,\n'
        '            "retention_until_dc_kwh": self.retention_until_dc_kwh,\n'
        "        }\n",
        "        }\n",
        f"{AUTH}::test_the_verdict_round_trips_through_the_persisted_claim",
    ),
    (
        "E5: the executing quarter loses the row's verdict",
        "execution.py",
        "            retention_authorised=row.retention_authorised,\n",
        "            retention_authorised=False,\n",
        f"{AUTH}::test_a_later_publication_cannot_revoke_the_open_row_verdict",
    ),
    (
        "E6: every row of a plan shares one verdict",
        "execution.py",
        "        row = self.row_covering(moment)\n",
        "        row = self.rows[0] if self.rows else self.row_covering(moment)\n",
        f"{AUTH}::test_the_verdict_survives_a_row_boundary_inside_one_plan",
    ),
    # =====================================================================
    # S -- the split, and the campaign it protects
    # =====================================================================
    (
        "S1: the objective absorbs everything the pack took",
        "coordinator.py",
        "        return min(self._quarter_battery_kwh, quarter.battery_allowance_kwh())\n",
        "        return self._quarter_battery_kwh\n",
        f"{SPLIT}::test_objective_and_absorbed_always_sum_to_the_measured_charge",
    ),
    (
        "S2: the campaign counts absorbed production and terminates early",
        "coordinator.py",
        "        # Objective-attributed, beta.40: see ``_row_objective_kwh``. A campaign\n"
        "        # counting absorbed production towards its frozen target would read\n"
        "        # itself complete while it still had energy to buy, and terminate.\n"
        "        return self._quarter_objective_kwh\n",
        "        return self._quarter_battery_kwh\n",
        f"{SPLIT}::test_a_sunny_row_does_not_trip_the_campaign_terminal_early",
    ),
    (
        "S3: key the overshoot guard on the clamp token, zeroing a clamped tick",
        "dispatch.py",
        "    if absorbing:\n"
        "        spendable_kwh = max(spendable_kwh, applied_kw * progress.hours)\n",
        "    if reason == DISPATCH_LIMIT_FREE_PV_ABSORPTION:\n"
        "        spendable_kwh = max(spendable_kwh, applied_kw * progress.hours)\n",
        f"{ARITH}::test_the_absorption_branch_can_never_cause_grid_import",
    ),
    # **Not the shortfall expression, and the reason is worth recording.**
    # ``shortfall = max(0, planned - realised)`` with ``objective = min(total,
    # planned)`` is provably identical to using the total: below the allowance the
    # two are equal, and above it both give zero. A mutation there cannot be killed
    # because it is not a defect. The published split *is* observable, so that is
    # what is attacked instead.
    (
        "S4: the completed row publishes the whole charge as the objective",
        "coordinator.py",
        '                "objective_battery_kwh": round(self._quarter_objective_kwh, 3),\n',
        '                "objective_battery_kwh": round(self._quarter_battery_kwh, 3),\n',
        f"{SPLIT}::test_a_completed_row_records_the_split_and_the_shortfall_on_the_objective",
    ),
    (
        "S5: the objective is not capped, so the identity breaks",
        "coordinator.py",
        "        return max(0.0, self._quarter_battery_kwh - self._quarter_objective_kwh)\n",
        "        return self._quarter_battery_kwh\n",
        f"{SPLIT}::test_deriving_the_split_matches_crediting_the_objective_first",
    ),
    (
        "S6: an export row reports an absorbed share it cannot have",
        "coordinator.py",
        "        if quarter.intent == EXECUTION_INTENT_NET_EXPORT:\n"
        "            # An export has no absorption envelope -- there is no such thing as\n"
        "            # free production to discharge -- so its whole movement is objective.\n"
        "            return self._quarter_battery_kwh\n",
        "        if False:\n            return self._quarter_battery_kwh\n",
        f"{SPLIT}::test_an_export_row_has_no_absorbed_share_at_all",
    ),
    # =====================================================================
    # H -- the satisfied row, and the total hold
    # =====================================================================
    (
        "H1: route the third outcome back through the zero hold",
        "coordinator.py",
        "            if self._absorption_live(progress):\n",
        "            if False:\n",
        f"{SAT}::test_a_satisfied_row_with_surplus_keeps_charging",
    ),
    (
        "H2: leave _quarter_is_satisfied as satisfied-only",
        "coordinator.py",
        "        progress = self._quarter_progress(now)\n"
        "        return progress is None or not self._absorption_live(progress)\n",
        "        return True\n",
        f"{SAT}::test_the_refresh_does_not_command_zero_over_a_live_absorption",
    ),
    (
        "H3: drop the actuator resolution floor from the live gate",
        "coordinator.py",
        "        if surplus_kw is None or surplus_kw < CONTROL_MIN_POWER_KW:\n",
        "        if surplus_kw is None:\n",
        f"{SAT}::test_a_trickle_is_not_recorded_as_absorption",
    ),
    (
        "H4: an unreadable production sensor authorises absorption",
        "coordinator.py",
        # beta.42 added ``_budget_surplus_kw`` with the same guard shape, so
        # this anchor stopped being unique. The two reads above it are what
        # tell the absorption authority apart from the grid budget: this one
        # is deliberately unsanitised, and that is a separate finding.
        "        pv_w = self._read_pv_power_w()\n"
        "        load_w = self._read_house_load_w()\n"
        "        if pv_w is None or load_w is None:\n            return None\n",
        "        pv_w = self._read_pv_power_w()\n"
        "        load_w = self._read_house_load_w()\n"
        "        if pv_w is None or load_w is None:\n            return 5.0\n",
        f"{SAT}::test_an_unreadable_production_sensor_earns_nothing",
    ),
    # **H5 removed, and the reason recorded.** "a full pack absorbs nothing" is
    # now protected twice over: the live state-of-charge clause, and the
    # retainable-energy bound the audit added, which reads zero at a full pack.
    # Either alone catches it, so no single-edit mutation can break the claim --
    # which is defence in depth working rather than a test to strengthen. The
    # claim itself is still asserted by
    # ``test_a_full_pack_holds_rather_than_absorbing``, and R8/R9 attack the
    # second guard on its own.
    (
        "H6: an unauthorised satisfied row absorbs anyway",
        "coordinator.py",
        "        if not progress.retention_authorised:\n            return False\n",
        "        if False:\n            return False\n",
        f"{SAT}::test_an_unauthorised_row_is_never_recorded_as_absorbing",
    ),
    # **Not the early return, and this one was measured rather than assumed.**
    # Replacing ``_async_finish_satisfied_row(); return`` with ``pass`` is a no-op:
    # the tick falls through to the setpoint path, the command comes out below the
    # actuator's minimum, and the sub-resolution net calls the same function with
    # the same effect -- one rest, one terminal, the same tick reason. The two paths
    # are equivalent in every reachable state, so a mutation between them describes
    # no defect. That is what defence in depth looks like when it works, and the
    # honest record of it is this comment rather than a weakened assertion.
    #
    # What *is* observable is the net's own case, which the early return cannot
    # reach: a row the live gate authorised whose command a physical clamp then
    # reduced to nothing must still be finished rather than left open.
    (
        "H7: an authorised row clamped to nothing is left open instead of finished",
        "coordinator.py",
        "            and self._quarter_target_reached_at is not None\n"
        "        ):\n"
        "            await self._async_finish_satisfied_row(now, snapshot)\n",
        "            and self._quarter_target_reached_at is None\n"
        "        ):\n"
        "            await self._async_finish_satisfied_row(now, snapshot)\n",
        f"{SAT}::test_an_absorbing_row_clamped_to_nothing_is_finished_not_left_open",
    ),
    (
        "H8: the absorbing row is not recorded as absorbing",
        "coordinator.py",
        "                self._note_quarter_clamp(SHORTFALL_ABSORBING_FREE_PV)\n",
        "                pass\n",
        f"{SAT}::test_the_absorbing_row_is_recorded_as_absorbing",
    ),
    # =====================================================================
    # P / W / D -- the three defects the pre-release audit of the live
    #             beta.39 campaign turned up
    #
    # P: the run's grid budget was a flat pace and capped the battery in
    #    every row, throttling the three rows Stage A had sized at full
    #    inverter power to 34-44 % of the power they needed and leaving
    #    5.076 kWh of authorised purchase unspent.
    # W: a campaign scope stood in for a campaign reason, so a window that
    #    ended 45 % delivered published campaign_objective_reached.
    # D: one name carried two quantities -- the ambient-corrected reported
    #    walk and the decided lattice state -- and the projection was the
    #    one that looked like the trajectory.
    # =====================================================================
    (
        "P1: restore the flat run pace that throttled the concentrated rows",
        "coordinator.py",
        "            row = self._quarter\n            if row is not None:\n",
        "            row = None\n            if row is not None:\n",
        f"{PACE}::test_a_concentrated_row_is_not_capped_by_the_runs_average_pace",
    ),
    (
        "P2: the run budget stops bounding the row at all",
        "coordinator.py",
        "            grid_kw = revised / (minutes / 60.0)\n",
        "            grid_kw = None\n",
        f"{PACE}::test_a_nearly_spent_run_budget_still_binds_hard",
    ),
    (
        "P3: an exhausted budget still permits a purchase",
        "coordinator.py",
        "            remaining_kwh = max(0.0, demand.grid_cap_kwh - demand.grid_charged_kwh)\n",
        "            remaining_kwh = max(0.05, demand.grid_cap_kwh - demand.grid_charged_kwh)\n",
        f"{PACE}::test_an_exhausted_run_budget_permits_no_purchase_at_all",
    ),
    (
        "P4: the row clock loses its floor, so a boundary tick unbounds the budget",
        "coordinator.py",
        "                        CONTROL_TICK_ENERGY_HORIZON_SECONDS / 60.0,\n                        row.seconds_remaining(now) / 60.0,\n",
        "                        0.0,\n                        row.seconds_remaining(now) / 60.0,\n",
        f"{PACE}::test_the_row_permits_at_most_the_remaining_budget_across_itself",
    ),
    (
        "W1: every campaign-scoped stop claims the objective again",
        "coordinator.py",
        "        if self._campaign_objective_met():\n            return EXECUTION_STOP_CAMPAIGN_COMPLETE\n        return EXECUTION_STOP_WINDOW_ENDED\n",
        "        return EXECUTION_STOP_CAMPAIGN_COMPLETE\n",
        f"{TERM}::test_a_window_that_ended_short_says_window_ended",
    ),
    (
        "W2: no campaign-scoped stop can ever claim the objective",
        "coordinator.py",
        "        if self._campaign_objective_met():\n            return EXECUTION_STOP_CAMPAIGN_COMPLETE\n",
        "        if False:\n            return EXECUTION_STOP_CAMPAIGN_COMPLETE\n",
        f"{TERM}::test_a_delivered_objective_still_says_objective_reached",
    ),
    (
        "W3: the objective predicate ignores the tolerance",
        "coordinator.py",
        "        return self._campaign_realized_now() >= frozen - tolerance\n",
        "        return self._campaign_realized_now() >= frozen\n",
        f"{TERM}::test_an_objective_reached_inside_tolerance_counts_as_reached",
    ),
    (
        "W4: an unpublished objective reads as reached",
        "coordinator.py",
        "        if frozen is None or frozen <= 0.0:\n            # No objective was ever published, so none can have been reached.\n            return False\n",
        "        if frozen is None or frozen <= 0.0:\n            # No objective was ever published, so none can have been reached.\n            return True\n",
        f"{TERM}::test_a_campaign_with_no_published_objective_never_claims_one",
    ),
    (
        "W5: a run with no campaign claims a campaign terminal",
        "coordinator.py",
        "        if self._campaign_id is None:\n            return row_reason\n",
        "        if self._campaign_id is None:\n            return EXECUTION_STOP_CAMPAIGN_COMPLETE\n",
        f"{TERM}::test_no_campaign_open_keeps_the_rows_own_reason",
    ),
    # =====================================================================
    # D -- retired in beta.41, with the reason recorded
    # =====================================================================
    #
    # beta.40 published two endpoints with a basis apiece because they were two
    # numbers: the level the recursion decided, and the energy the pack would hold.
    # The three D mutations attacked that pair. beta.41 made them one quantity, so
    # D1 and D3 mutate constants that are no longer written anywhere, and D2 --
    # swapping ``edge_energy_kwh`` for ``end_energy_dc_kwh`` -- became *equivalent*,
    # because those are now the same number by construction. That equivalence is the
    # release's central claim rather than a gap in a test.
    #
    # What this family protected is now the stronger statement that there is only
    # one endpoint, and the beta.41 table guards it: P12 splits the two apart again
    # and dies against the meter-side walk, and P1, P2 and P13 attack the state
    # model that made them one.
    # =====================================================================
    # V -- the fixtures' own vacuity gates
    # =====================================================================
    (
        "V1: a capture with no surplus proves nothing about storing one",
        "tests/beta40_trace.py",
        "PV_KW: Final = 3.309\n",
        "PV_KW: Final = 0.800\n",
        f"{ARITH}::test_the_gain_is_the_whole_surplus_and_not_a_fraction_of_it",
    ),
    (
        "V2: a gate that refuses everything makes every neutrality claim vacuous",
        "tests/beta40_trace.py",
        "MARGINAL_VALUE_EUR_KWH: Final = 0.2237\n",
        "MARGINAL_VALUE_EUR_KWH: Final = 0.0001\n",
        f"{SAFETY}::test_the_gate_authorises_on_the_captures_own_numbers",
    ),
    (
        "V3: a row already met leaves nothing for the objective rate to want",
        "tests/beta40_trace.py",
        "ROW_REMAINING_AT_CAPTURE_KWH: Final = 0.0373\n",
        "ROW_REMAINING_AT_CAPTURE_KWH: Final = 0.0\n",
        f"{ARITH}::test_beta39_predicted_the_export_it_then_measured",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
