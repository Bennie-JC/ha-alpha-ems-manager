"""Break each beta.39 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch. A surviving mutation means
the test is vacuous and **the test gets rewritten**; it never means the mutation
gets weakened.

Two families, matching the release.

The ``L`` mutations attack the lifecycle. Most of them restore the beta.38 line
that made ``executing`` unreachable: the projection order, the missing tick
cadence, the payload the write boundary never re-rendered. The dangerous
direction is the *other* one -- claiming ``executing`` from a command rather than
from the plant -- so several are deliberately inverted: they make the state
easier to reach, and a test that does not notice is a test that would let the
software lie about a battery that is not running.

The ``A`` mutations attack the accounting. The prize ones are the basis
substitutions: swap the no-battery avoidance for the planner's per-interval idle
figure, or ``remaining`` for ``decision_advantage_eur``, and every arithmetic
identity still balances -- the total is simply measuring something that does not
exist. Those are the mutations that would have shipped a plausible, wrong euro
figure, and they are why this file exists at all.

The ``M`` mutations attack Mixed Buy, and the interesting direction is again
the *other* one: the release adds a word, so the mutations that matter are the
ones that take it away again -- collapsing a mixed run back into ``safety_buy``,
which is exactly the defect, or letting it reach a decision, which would make an
observability change a control change. One of them removes the word from the
entity option set: the value is then computed, refused by the enum guard and
published as ``unknown``, which is worse than the defect it replaces.

The ``V`` mutations break the **fixtures**, so their own vacuity gates are tested
rather than asserted.

The beta.35, beta.36, beta.37 and beta.38 tables are siblings and must all still
pass: a beta.39 correction that resurrects an earlier defect is a regression, not
a fix.

Run with:  python tools/mutation/run.py b39 [-k substring]
"""

from __future__ import annotations

LIFE = "tests/test_beta39_lifecycle.py"
ACCT = "tests/test_beta39_accounting.py"
MIX = "tests/test_beta39_mixed_buy.py"
ZOMBIE = "tests/test_beta38_no_zombie.py"
LEDGER38 = "tests/test_beta38_ledger.py"
EV37 = "tests/test_beta37_economic_value.py"

SELL = "[net_export]"
BUY = "[grid_charge]"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # L -- the lifecycle
    # =====================================================================
    (
        "L1: restore arming ahead of confirmed ownership (Sell)",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING\n"
        "        if arming:\n"
        "            return LIFECYCLE_STARTING",
        "        if arming:\n"
        "            return LIFECYCLE_STARTING\n"
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING",
        f"{LIFE}::test_confirmed_execution_outranks_a_start_in_progress",
    ),
    (
        "L1b: delete the ownership branch of the projection (Sell)",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING",
        "        if False:\n            return LIFECYCLE_EXECUTING",
        f"{LIFE}::test_a_refresh_inside_a_confirmed_run_publishes_executing{SELL}",
    ),
    (
        "L1c: delete the ownership branch of the projection (Buy)",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING",
        "        if False:\n            return LIFECYCLE_EXECUTING",
        f"{LIFE}::test_a_refresh_inside_a_confirmed_run_publishes_executing{BUY}",
    ),
    (
        "L2: executing without a matching persisted claim",
        "execution.py",
        "    if not evidence.record_matches:\n        return OWNERSHIP_UNPROVEN",
        "    if False:\n        return OWNERSHIP_UNPROVEN",
        f"{LIFE}::test_a_dispatch_we_cannot_prove_is_ours_is_not_executing",
    ),
    (
        "L3: executing without an active dispatch",
        "execution.py",
        "    if not evidence.dispatch_active:\n        return OWNERSHIP_NONE",
        "    if False:\n        return OWNERSHIP_NONE",
        f"{ZOMBIE}::test_ownership_none_means_the_dispatch_is_not_running",
    ),
    (
        "L4: arming alone reaches executing",
        "coordinator.py",
        "        if arming:\n            return LIFECYCLE_STARTING",
        "        if arming:\n            return LIFECYCLE_EXECUTING",
        f"{LIFE}::test_executing_requires_confirmed_ownership_and_nothing_less",
    ),
    (
        "L4b: and the arming refresh then lies in the replay",
        "coordinator.py",
        "        if arming:\n            return LIFECYCLE_STARTING",
        "        if arming:\n            return LIFECYCLE_EXECUTING",
        f"{LIFE}::test_the_arming_refresh_is_starting_and_says_so_truthfully{SELL}",
    ),
    (
        "L5: delete the tick projection (Sell)",
        "coordinator.py",
        "        self._note_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        "        self._skip_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        f"{LIFE}::test_the_first_confirmed_observation_publishes_executing{SELL}",
    ),
    (
        "L5b: delete the tick projection (Buy)",
        "coordinator.py",
        "        self._note_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        "        self._skip_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        f"{LIFE}::test_the_first_confirmed_observation_publishes_executing{BUY}",
    ),
    (
        "L5c: without the tick, the observed bad sequence returns",
        "coordinator.py",
        "        self._note_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        "        self._skip_lifecycle(\n"
        "            self._lifecycle_state_from(\n"
        "                ownership_state=self._ownership_now(snapshot, now),",
        f"{LIFE}::test_the_observed_bad_sequence_is_now_impossible{SELL}",
    ),
    (
        "L6: the tick projects executing regardless of what it read",
        "coordinator.py",
        "                ownership_state=self._ownership_now(snapshot, now),\n"
        "                stop_reason=None,",
        "                ownership_state=OWNERSHIP_OWNED,\n"
        "                stop_reason=None,",
        f"{LIFE}::test_a_tick_that_cannot_prove_ownership_publishes_no_execution{SELL}",
    ),
    (
        "L7: drop the lifecycle half of the post-write patch",
        "coordinator.py",
        '        execution["lifecycle"] = self._lifecycle_block()\n'
        '        execution["open_campaign"] = self._open_campaign_block()',
        '        execution["open_campaign"] = self._open_campaign_block()',
        f"{LIFE}::test_the_refresh_that_resets_publishes_its_terminal{SELL}",
    ),
    (
        "L8: drop the campaign half of the post-write patch (Sell)",
        "coordinator.py",
        '        execution["lifecycle"] = self._lifecycle_block()\n'
        '        execution["open_campaign"] = self._open_campaign_block()',
        '        execution["lifecycle"] = self._lifecycle_block()',
        f"{LIFE}::test_the_payload_cannot_report_an_unstarted_campaign_that_started{SELL}",
    ),
    (
        "L8b: drop the campaign half of the post-write patch (Buy)",
        "coordinator.py",
        '        execution["lifecycle"] = self._lifecycle_block()\n'
        '        execution["open_campaign"] = self._open_campaign_block()',
        '        execution["lifecycle"] = self._lifecycle_block()',
        f"{LIFE}::test_the_payload_cannot_report_an_unstarted_campaign_that_started{BUY}",
    ),
    (
        "L9: never call the post-write patch at all",
        "coordinator.py",
        "        self._settle_execution_payload(control)",
        "        pass  # self._settle_execution_payload(control)",
        f"{LIFE}::test_the_payload_cannot_report_an_unstarted_campaign_that_started{SELL}",
    ),
    (
        "L10: collapse cleanup_complete back into the stop",
        "coordinator.py",
        "        await self._async_send_locked(plan_dispatch_cleanup(), now=now, verify=None)\n"
        "        self._note_lifecycle(LIFECYCLE_CLEANUP_COMPLETE, now)",
        "        await self._async_send_locked(plan_dispatch_cleanup(), now=now, verify=None)",
        f"{LIFE}::test_the_stop_and_its_cleanup_are_two_published_transitions{SELL}",
    ),
    (
        "L10b: and the refresh path's cleanup note too",
        "coordinator.py",
        "                self._note_lifecycle(LIFECYCLE_STOPPED, now)\n"
        "                self._note_lifecycle(LIFECYCLE_CLEANUP_COMPLETE, now)",
        "                self._note_lifecycle(LIFECYCLE_STOPPED, now)",
        f"{LIFE}::test_the_refresh_that_resets_publishes_its_terminal{BUY}",
    ),
    (
        "L11: let the projection return cleanup_complete, so it can stick",
        "coordinator.py",
        "        if self._carried is not None:\n            return LIFECYCLE_ADMITTED\n"
        "        return LIFECYCLE_IDLE",
        "        if self._carried is not None:\n            return LIFECYCLE_ADMITTED\n"
        "        return LIFECYCLE_CLEANUP_COMPLETE",
        f"{LIFE}::test_the_projection_can_never_return_a_terminal_note",
    ),
    (
        "L11b: a sticky cleanup_complete, seen through the replay",
        "coordinator.py",
        "        if self._carried is not None:\n            return LIFECYCLE_ADMITTED\n"
        "        return LIFECYCLE_IDLE",
        "        if self._carried is not None:\n            return LIFECYCLE_ADMITTED\n"
        "        return LIFECYCLE_CLEANUP_COMPLETE",
        f"{LIFE}::test_cleanup_complete_is_not_a_sticky_between_campaign_state{SELL}",
    ),
    (
        "L12: drop the transition trail",
        "coordinator.py",
        '        self._lifecycle_trail.append({"state": state, "at": now.isoformat()})',
        "        pass  # trail dropped",
        f"{LIFE}::test_the_payload_carries_every_transition_not_only_the_latest{SELL}",
    ),
    (
        "L12b: publish the trail but never read it out",
        "coordinator.py",
        '            "transitions": list(self._lifecycle_trail),',
        '            "transitions": [],',
        f"{LIFE}::test_the_payload_carries_every_transition_not_only_the_latest{BUY}",
    ),
    (
        "L13: let the trail grow without bound",
        "const.py",
        "LIFECYCLE_TRAIL_LIMIT: Final = 24",
        "LIFECYCLE_TRAIL_LIMIT: Final = 4096",
        f"{LIFE}::test_the_trail_is_bounded",
    ),
    (
        "L14: accept an unknown lifecycle word",
        "coordinator.py",
        "        if state not in LIFECYCLE_STATES:  # pragma: no cover - programming error\n"
        '            raise ValueError(f"unknown lifecycle state: {state}")',
        "        if False:\n"
        '            raise ValueError(f"unknown lifecycle state: {state}")',
        f"{LIFE}::test_the_vocabulary_is_checked_at_the_call_site",
    ),
    (
        "L15: a hazard no longer outranks confirmed execution",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_DEGRADED:\n"
        "            return LIFECYCLE_DEGRADED",
        "        if False:\n            return LIFECYCLE_DEGRADED",
        f"{LIFE}::test_a_hazard_still_outranks_confirmed_execution",
    ),
    (
        "L16: idle becomes reachable while something is owned",
        "coordinator.py",
        "        if ownership_state == OWNERSHIP_OWNED:\n"
        "            return LIFECYCLE_EXECUTING",
        "        if False:\n            return LIFECYCLE_EXECUTING",
        f"{ZOMBIE}::test_idle_is_unreachable_while_anything_is_owned",
    ),
    # =====================================================================
    # A -- the accounting
    # =====================================================================
    (
        "A1: drop the ambient term from the no-battery counterfactual",
        "economic.py",
        "        return self.idle_import_kwh + self.ambient_self_consumption_ac_kwh",
        "        return self.idle_import_kwh",
        f"{ACCT}::test_the_no_battery_counterfactual_is_exact_not_estimated[mixed]",
    ),
    (
        "A1b: and on the Sell horizon too",
        "economic.py",
        "        return self.idle_import_kwh + self.ambient_self_consumption_ac_kwh",
        "        return self.idle_import_kwh",
        f"{ACCT}::test_the_no_battery_counterfactual_is_exact_not_estimated[sell]",
    ),
    (
        "A2: use the planner's per-interval idle avoidance instead",
        "economic.py",
        "        return max(0.0, self.no_battery_import_kwh - self.grid_import_kwh)",
        "        return max(0.0, -self.marginal_grid_import_kwh)",
        f"{ACCT}::test_the_no_battery_counterfactual_is_exact_not_estimated[mixed]",
    ),
    (
        "A3: leave the avoidance unclamped, so a purchase nets against a saving",
        "economic.py",
        "        return max(0.0, self.no_battery_import_kwh - self.grid_import_kwh)",
        "        return self.no_battery_import_kwh - self.grid_import_kwh",
        f"{ACCT}::test_the_avoidance_clamps_at_zero_exactly_as_the_measured_one_does",
    ),
    (
        "A4: fold the switching fee into the remaining-today figure",
        "economic.py",
        '        "no_battery_value_eur": _round_eur(exports - imports + avoided_no_battery),',
        '        "no_battery_value_eur": _round_eur(\n'
        "            exports - imports + avoided_no_battery - fee_per_start_eur\n"
        "        ),",
        f"{ACCT}::test_the_remaining_figure_carries_no_model_term_and_no_inventory",
    ),
    (
        "A5: publish the remaining figure on the wrong avoidance basis",
        "economic.py",
        '        "no_battery_value_eur": _round_eur(exports - imports + avoided_no_battery),',
        '        "no_battery_value_eur": _round_eur(exports - imports + avoided),',
        f"{ACCT}::test_the_remaining_figure_is_the_realised_construction[mixed]",
    ),
    (
        "A6: use today_interval_value_eur as the remaining figure",
        "coordinator.py",
        '        value = block.get("no_battery_value_eur")',
        '        value = block.get("interval_value_eur")',
        f"{ACCT}::test_the_entity_publishes_the_identity_and_it_reconciles",
    ),
    (
        "A7: accept a day block on a basis that is not the no-battery one",
        "coordinator.py",
        '        if block.get("avoidance_basis") != AVOIDANCE_BASIS_NO_BATTERY:',
        "        if False:",
        f"{ACCT}::test_a_day_block_on_another_basis_publishes_no_total",
    ),
    (
        "A8: shift the day partition by one interval",
        "realized.py",
        "    return (\n        range(0, max(0, clamped - 1)),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        "    return (\n        range(0, clamped),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        f"{ACCT}::test_the_day_partition_is_disjoint_and_exhaustive[96]",
    ),
    (
        "A8b: the same shift, on a 92-interval spring-forward day",
        "realized.py",
        "    return (\n        range(0, max(0, clamped - 1)),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        "    return (\n        range(0, clamped),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        f"{ACCT}::test_the_day_partition_is_disjoint_and_exhaustive[92]",
    ),
    (
        "A8c: and on a 100-interval fall-back day",
        "realized.py",
        "    return (\n        range(0, max(0, clamped - 1)),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        "    return (\n        range(0, clamped),\n"
        "        clamped - 1 if clamped >= 1 else None,\n"
        "        range(clamped, count),\n    )",
        f"{ACCT}::test_the_day_partition_is_disjoint_and_exhaustive[100]",
    ),
    (
        "A9: make the quarter in flight the plan's head instead of head - 1",
        "realized.py",
        "        clamped - 1 if clamped >= 1 else None,",
        "        clamped if clamped < count else None,",
        f"{ACCT}::test_the_quarter_in_flight_is_the_one_the_plan_does_not_plan",
    ),
    (
        "A10: fold the quarter in flight into the closed history",
        "coordinator.py",
        "            self._sliced_series(series, len(closed)), limits",
        "            self._sliced_series(series, len(closed) + 1), limits",
        f"{ACCT}::test_the_partition_covers_the_whole_civil_day_on_a_real_plan",
    ),
    (
        "A11: drop the revaluation from the total",
        "realized.py",
        "        remaining_expected_eur,\n        forecast_revaluation_eur,\n    )",
        "        remaining_expected_eur,\n    )",
        f"{ACCT}::test_the_five_terms_sum_to_the_published_total",
    ),
    (
        "A11b: and the identity through the coordinator notices too",
        "realized.py",
        "        remaining_expected_eur,\n        forecast_revaluation_eur,\n    )",
        "        remaining_expected_eur,\n    )",
        f"{ACCT}::test_the_identity_holds_with_a_non_zero_revaluation",
    ),
    (
        "A12: sign-flip the revaluation",
        "coordinator.py",
        "        return current - float(persisted), None, persisted, valued_at",
        "        return float(persisted) - current, None, persisted, valued_at",
        f"{ACCT}::test_the_identity_holds_with_a_non_zero_revaluation",
    ),
    (
        "A13: value the opening position on the current curve, forcing zero",
        "coordinator.py",
        '        current = self._position_value_eur(outcome, float(stored["e"]))\n'
        "        if current is None:\n"
        "            return None, ACCOUNTING_UNAVAILABLE_NO_POSITION_VALUE, persisted, valued_at\n"
        "        return current - float(persisted), None, persisted, valued_at",
        "        return 0.0, None, persisted, valued_at",
        f"{ACCT}::test_the_identity_holds_with_a_non_zero_revaluation",
    ),
    (
        "A14: substitute the marginal shortcut for the position integral",
        "coordinator.py",
        '        current = self._position_value_eur(outcome, float(stored["e"]))',
        '        current = 0.2016 * float(stored["e"])',
        f"{ACCT}::test_the_identity_holds_with_a_non_zero_revaluation",
    ),
    (
        "A15: publish a total while an addend is unknown",
        "realized.py",
        "    if reason is None and all(\n"
        "        value is not None\n"
        "        and float(value) == float(value)\n"
        '        and abs(float(value)) != float("inf")\n'
        "        for value in addends\n"
        "    ):",
        "    if True:",
        f"{ACCT}::test_a_missing_addend_takes_the_total_with_it[realised]",
    ),
    (
        "A15b: and a non-finite one",
        "realized.py",
        "        value is not None\n"
        "        and float(value) == float(value)\n"
        '        and abs(float(value)) != float("inf")',
        "        value is not None",
        f"{ACCT}::test_a_non_finite_addend_withholds_the_total",
    ),
    (
        "A16: make realised_today_eur a plug that absorbs the residual",
        "realized.py",
        "        realised_today_eur=(\n"
        "            None if realised_today is None else round(realised_today, _EUR_DECIMALS)\n"
        "        ),",
        "        realised_today_eur=(\n"
        "            None\n"
        "            if total is None\n"
        "            else round(\n"
        "                total\n"
        "                - float(in_progress_eur or 0.0)\n"
        "                - float(remaining_expected_eur or 0.0)\n"
        "                - float(forecast_revaluation_eur or 0.0),\n"
        "                _EUR_DECIMALS,\n"
        "            )\n"
        "        ),",
        f"{ACCT}::test_no_addend_is_ever_a_plug",
    ),
    (
        "A17: write the opening valuation twice in one day",
        "storage.py",
        "        if self.open_value is not None:\n            return False",
        "        if False:\n            return False",
        f"{ACCT}::test_the_opening_valuation_is_written_once_and_never_revised",
    ),
    (
        "A17b: the same guard, seen through a reload rather than a direct call",
        "storage.py",
        "        if self.open_value is not None:\n            return False",
        "        if False:\n            return False",
        f"{ACCT}::test_a_reload_does_not_double_count_the_day",
    ),
    (
        "A18: never persist the opening valuation",
        "storage.py",
        "        if self.open_value is not None:\n            # Omitted while absent",
        "        if False:\n            # Omitted while absent",
        f"{ACCT}::test_the_opening_valuation_survives_a_round_trip",
    ),
    (
        "A19: accept a partial opening valuation from the document",
        "storage.py",
        "        number = raw.get(key)\n"
        "        if isinstance(number, bool) or not isinstance(number, (int, float)):\n"
        "            return None",
        "        number = raw.get(key) or 0.0\n"
        "        if False:\n            return None",
        f"{ACCT}::test_a_damaged_opening_valuation_degrades_to_absent[damaged0]",
    ),
    (
        "A20: accept a mismatched opening energy",
        "coordinator.py",
        "        if (\n"
        '            abs(float(stored["e"]) - opening_kwh)\n'
        "            > ACCOUNTING_OPENING_ENERGY_TOLERANCE_KWH\n"
        "        ):",
        "        if False:",
        f"{ACCT}::test_a_mismatched_opening_energy_refuses_rather_than_fudges",
    ),
    (
        "A21: accept a moved lattice pitch",
        "coordinator.py",
        '        if not bucket_kwh or round(bucket_kwh, 6) != round(float(stored["b"]), 6):',
        "        if not bucket_kwh:",
        f"{ACCT}::test_a_moved_lattice_refuses_rather_than_fudges",
    ),
    (
        "A22: compare the lattice pitch at float precision again",
        "coordinator.py",
        '        if not bucket_kwh or round(bucket_kwh, 6) != round(float(stored["b"]), 6):',
        '        if not bucket_kwh or float(stored["b"]) != bucket_kwh:',
        f"{ACCT}::test_a_stable_lattice_is_not_mistaken_for_a_moved_one",
    ),
    (
        "A23: publish a total over a day the horizon does not reach the end of",
        "coordinator.py",
        "        if remaining_count is not None and remaining_count != len(remaining_slice):",
        "        if False:",
        f"{ACCT}::test_a_horizon_short_of_midnight_publishes_no_total",
    ),
    (
        "A24: price the open quarter without the flexible-load subtraction",
        "coordinator.py",
        "            load_kwh=max(0.0, house - flexible),",
        "            load_kwh=house,",
        f"{ACCT}::test_the_open_quarter_prices_the_baseline_not_the_meter",
    ),
    (
        "A25: value an unpriceable open quarter at zero",
        "realized.py",
        "    if imported is None or exported is None or buy is None or sell is None:\n"
        "        return None",
        "    if imported is None or exported is None or buy is None or sell is None:\n"
        "        return 0.0",
        f"{ACCT}::test_an_unpriceable_open_quarter_is_unknown_and_not_zero",
    ),
    (
        "A26: let the publishing path mutate persisted state",
        "coordinator.py",
        "        count = record.interval_count\n        head = count",
        "        record.open_value = None\n"
        "        count = record.interval_count\n"
        "        head = count",
        f"{ACCT}::test_the_accounting_block_is_publish_only",
    ),
    (
        "A27: label the revaluation planner-derived, so it can be differenced",
        "realized.py",
        '        "today_accounting.forecast_revaluation_eur": LEDGER_BASIS_REVALUED,',
        '        "today_accounting.forecast_revaluation_eur": LEDGER_BASIS_PLANNER_DERIVED,',
        f"{LEDGER38}::test_the_position_values_are_still_planner_derived",
    ),
    (
        "A28: restore the self-contradicting basis wording",
        "sensor.py",
        '    "expected CASH advantage of the selected plan over the passive ambient-walk "',
        '    "expected advantage on the exact basis the optimiser minimised. "',
        f"{EV37}::test_the_entity_declares_no_state_class",
    ),
    (
        "A16b: tighten the tolerance below what rounding can reach",
        "const.py",
        "ACCOUNTING_RECONCILIATION_TOLERANCE_EUR: Final = 5e-4",
        "ACCOUNTING_RECONCILIATION_TOLERANCE_EUR: Final = 1e-5",
        f"{ACCT}::test_the_reconciliation_tolerance_is_above_what_rounding_can_reach",
    ),
    (
        # **The anchor tracks the constant; the mutation does not change.**
        # beta.42 moved the minor to 8, so the beta.39 anchor stopped matching
        # and this mutation silently did not run -- reported as an anchor loss,
        # which is exactly why the runner counts those separately from
        # survivors: a mutation that did not run is an untested claim wearing a
        # passing result. The claim itself is unchanged -- moving the minor
        # version *backwards* must be caught.
        "A29: move the storage minor version back",
        "const.py",
        # beta.42 moved this to 8, which is also what
        # ``FORECAST_STORAGE_MINOR_VERSION`` reads -- and the shorter name is
        # a substring of the longer one, so the anchor matched both. The
        # leading newline pins it to the declaration itself.
        "\nSTORAGE_MINOR_VERSION: Final = 8",
        "\nSTORAGE_MINOR_VERSION: Final = 7",
        f"{ACCT}::test_only_the_storage_minor_version_moved",
    ),
    # =====================================================================
    # M -- Mixed Buy
    # =====================================================================
    (
        "M1: collapse the mixed branch back into safety_buy",
        "economic.py",
        "    if safety_buy_kwh > 0.0 and economic_buy_kwh > 0.0:\n"
        "        return ECONOMIC_ACTION_MIXED_BUY",
        "    if False:\n        return ECONOMIC_ACTION_MIXED_BUY",
        f"{MIX}::test_both_components_present_is_a_mixed_buy",
    ),
    (
        "M1b: the same collapse, at the live campaign's magnitudes",
        "economic.py",
        "    if safety_buy_kwh > 0.0 and economic_buy_kwh > 0.0:\n"
        "        return ECONOMIC_ACTION_MIXED_BUY",
        "    if False:\n        return ECONOMIC_ACTION_MIXED_BUY",
        f"{MIX}::test_the_live_campaign_now_publishes_a_mixed_purpose",
    ),
    (
        "M1c: and the two modules then disagree about the same run",
        "economic.py",
        "    if safety_buy_kwh > 0.0 and economic_buy_kwh > 0.0:\n"
        "        return ECONOMIC_ACTION_MIXED_BUY",
        "    if False:\n        return ECONOMIC_ACTION_MIXED_BUY",
        f"{MIX}::test_the_purpose_and_the_activity_category_cannot_disagree[0.83-7.22]",
    ),
    (
        "M2: a zero discretionary share becomes mixed",
        "economic.py",
        "    if safety_buy_kwh > 0.0 and economic_buy_kwh > 0.0:",
        "    if safety_buy_kwh >= 0.0 and economic_buy_kwh >= 0.0:",
        f"{MIX}::test_a_zero_discretionary_component_is_not_mixed",
    ),
    (
        "M3: an unattributable run is called mixed rather than falling back",
        "economic.py",
        "    if safety_buy_kwh is None or economic_buy_kwh is None:\n"
        "        return ECONOMIC_ACTION_SAFETY_BUY",
        "    if safety_buy_kwh is None or economic_buy_kwh is None:\n"
        "        return ECONOMIC_ACTION_MIXED_BUY",
        f"{MIX}::test_an_unattributable_run_falls_back_and_never_invents_mixed",
    ),
    (
        "M4: the run purpose ignores the attribution again",
        "economic.py",
        '        "purpose": purchase_purpose(\n            run.action,',
        '        "purpose": ECONOMIC_ACTION_SAFETY_BUY if safety_buy else run.action,\n'
        '        "unused": purchase_purpose(\n'
        "            run.action,",
        f"{MIX}::test_the_live_campaign_now_publishes_a_mixed_purpose",
    ),
    (
        "M5: the Economic Action entity flattens mixed to safety_buy",
        "sensor.py",
        "    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):\n"
        "        return purpose\n"
        '    intent = view.get("intent")',
        "    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):\n"
        "        return ECONOMIC_ACTION_SAFETY_BUY\n"
        '    intent = view.get("intent")',
        f"{MIX}::test_the_economic_action_entity_relays_the_mixed_word",
    ),
    (
        "M6: the planned-action fallback runs ahead of the purpose again",
        "sensor.py",
        "    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):\n"
        "        return purpose\n"
        "    if run.start_index in outcome.safety_buy_runs:",
        "    if run.start_index in outcome.safety_buy_runs:\n"
        "        return ECONOMIC_ACTION_SAFETY_BUY\n"
        "    if purpose in (ECONOMIC_ACTION_SAFETY_BUY, ECONOMIC_ACTION_MIXED_BUY):",
        f"{MIX}::test_the_next_planned_action_entity_does_not_overwrite_the_mixed_word",
    ),
    (
        "M7: a mixed buy stops being a charge direction",
        "activity.py",
        "        ECONOMIC_ACTION_CHARGE,\n"
        "        ECONOMIC_ACTION_SAFETY_BUY,\n"
        "        ECONOMIC_ACTION_MIXED_BUY,",
        "        ECONOMIC_ACTION_CHARGE,\n        ECONOMIC_ACTION_SAFETY_BUY,",
        f"{MIX}::test_a_mixed_buy_still_moves_the_battery_one_way",
    ),
    (
        "M8: the Activity purpose collapses mixed to economic again",
        "activity.py",
        "    if category == ACTIVITY_CATEGORY_MIXED_BUY:\n"
        "        return ACTIVITY_PURPOSE_MIXED",
        "    if False:\n        return ACTIVITY_PURPOSE_MIXED",
        f"{MIX}::test_the_activity_purpose_distinguishes_all_three",
    ),
    (
        "M9: the word is computed but the entity may not state it",
        "const.py",
        "    ECONOMIC_ACTION_MIXED_BUY,\n)",
        ")",
        f"{MIX}::test_the_new_word_is_publishable_by_the_entities",
    ),
    (
        "M10: the classification reaches the executable intent",
        "economic.py",
        "    intent = EXECUTION_INTENT_GRID_CHARGE if safety_buy else execution_intent(run)",
        "    intent = EXECUTION_INTENT_HOLD if safety_buy else execution_intent(run)",
        f"{MIX}::test_the_classification_changes_nothing_a_decision_reads",
    ),
    (
        "M10b: and a mixed campaign then stops being admissible",
        "economic.py",
        "    intent = EXECUTION_INTENT_GRID_CHARGE if safety_buy else execution_intent(run)",
        "    intent = EXECUTION_INTENT_HOLD if safety_buy else execution_intent(run)",
        f"{MIX}::test_a_mixed_target_is_admitted_exactly_as_a_charge_target_is",
    ),
    (
        "M11: economic charging initiates a compulsory purchase",
        "economic.py",
        "    if not safety_buy:\n        return action",
        "    if False:\n        return action",
        f"{MIX}::test_an_economic_buy_can_never_initiate_a_compulsory_purchase",
    ),
    (
        # The anchor carried twelve spaces of indentation where the surrounding
        # file uses ten -- a pre-existing inconsistency that beta.42 normalised
        # when it rewrote the file to add the investment fields. Same line, same
        # deletion, same claim: an English label that disappears must be caught
        # rather than rendering the state as its slug.
        "M12: one language loses the label and the state renders as a slug",
        "translations/en.json",
        # **Pinned to Economic Action's own block.** The two state blocks are
        # byte-identical -- both entities publish the same eight words -- so
        # nothing inside either one distinguishes them. They were accidentally
        # distinguishable until beta.42, because one carried twelve spaces of
        # indentation and the other ten; normalising that removed the accident.
        # The entity name above the block is the only real disambiguator.
        '      "economic_action": {\n        "name": "Economic Action",\n        "state": {\n          "hold": "Hold",\n          "charge": "Charge",\n          "discharge": "Discharge",\n          "export": "Export",\n          "safety_buy": "Safety Buy",\n          "mixed_buy": "Mixed Buy",\n',
        '      "economic_action": {\n        "name": "Economic Action",\n        "state": {\n          "hold": "Hold",\n          "charge": "Charge",\n          "discharge": "Discharge",\n          "export": "Export",\n          "safety_buy": "Safety Buy",\n',
        f"{MIX}::test_both_languages_label_the_new_word",
    ),
    # =====================================================================
    # V -- the fixtures' own vacuity gates
    # =====================================================================
    (
        "V1: a fixture with no ambient interval leaves the second branch untested",
        "economic.py",
        "    if (\n        ambient_self_consumption\n"
        "        and 0.0 < unavoidable_import < smallest_discharge_ac_kwh\n    ):",
        "    if False:",
        f"{ACCT}::test_the_witness_that_the_ambient_branch_is_actually_exercised",
    ),
    (
        "V2: a Safety-Buy fixture that buys nothing proves no invariance",
        "economic.py",
        "        return max(0.0, self.no_battery_import_kwh - self.grid_import_kwh)",
        "        return 0.0",
        f"{ACCT}::test_the_avoidance_clamps_at_zero_exactly_as_the_measured_one_does",
    ),
    (
        "V3: a replay that never arms proves nothing about starting",
        "tests/test_beta38_opened_row_authority.py",
        "async def open_the_row(hass, coordinator, live_surface, monkeypatch, *, intent: str):",
        "async def open_the_row(hass, coordinator, live_surface, monkeypatch, *, intent: str):  # noqa: E501\n    return {}\n",
        f"{LIFE}::test_the_arming_refresh_is_starting_and_says_so_truthfully{SELL}",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
