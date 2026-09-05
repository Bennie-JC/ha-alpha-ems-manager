"""Break each beta.37 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out which
kind you have is to break the thing and watch.

beta.37 is an observability release, so most of its mutations are not "the plant does
the wrong thing" -- they are "the number lies". A sensor that renders missing data as
0.00 EUR, or suppresses a genuine zero into ``unknown``, or sums two figures on
different bases, is worse than no sensor: it is a confident wrong answer on a
dashboard somebody will act on.

Two of the mutations are about the release's load-bearing invariant instead: that
none of this can move a decision.

A surviving mutation means the test is vacuous and **the test gets rewritten**; it
never means the mutation gets weakened.

The beta.35 and beta.36 tables are siblings and must still pass: a beta.37 change that
resurrects an earlier defect is a regression, not a fix. Run all three.

Run with:  python tools/mutation/run.py b37 [-k substring]
"""

from __future__ import annotations

VALUE = "tests/test_beta37_economic_value.py"
SPLIT = "tests/test_beta37_day_split.py"
NEUTRAL = "tests/test_beta37_neutrality.py"
PERSIST = "tests/test_beta37_persistence.py"
CONTRACT = "tests/test_entity_contract.py"
STORED = "tests/test_beta35_stored_value.py"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production -- which is how the vacuity
# traps get tested rather than merely asserted.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------ what the state means
    (
        "E1: the advantage sign is flipped",
        "economic.py",
        "    advantage = plan.hold_cost_eur - plan.cost_eur",
        "    advantage = plan.cost_eur - plan.hold_cost_eur",
        f"{VALUE}::test_the_state_is_the_plan_against_the_passive_counterfactual",
    ),
    (
        "E2: the state publishes the plan cost directly",
        "economic.py",
        "    advantage = plan.hold_cost_eur - plan.cost_eur",
        "    advantage = plan.cost_eur",
        f"{VALUE}::test_the_state_is_none_of_the_other_euro_figures",
    ),
    (
        "E2b: the state publishes the objective instead",
        "economic.py",
        "    advantage = plan.hold_cost_eur - plan.cost_eur",
        "    advantage = plan.objective_eur",
        f"{VALUE}::test_the_state_is_the_plan_against_the_passive_counterfactual",
    ),
    (
        "E3: the cash figure stops being the advantage",
        "economic.py",
        '        "advantage_cash_eur": _round_eur(advantage),',
        '        "advantage_cash_eur": _round_eur(advantage - plan.edge_value_eur),',
        f"{VALUE}::test_the_advantage_carries_no_model_term",
    ),
    # --------------------------------------------- none versus zero
    (
        "N1: a genuine zero advantage is suppressed into unknown",
        "economic.py",
        "    advantage = plan.hold_cost_eur - plan.cost_eur\n",
        "    advantage = plan.hold_cost_eur - plan.cost_eur\n"
        "    if abs(advantage) < 1e-9:\n"
        "        return {\n"
        '            "available": False,\n'
        '            "state": None,\n'
        '            "unavailable_reason": ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN,\n'
        "        }\n",
        f"{VALUE}::test_a_valid_comparison_that_comes_out_equal_publishes_zero",
    ),
    (
        "N2: an unavailable comparison is published as 0.0 EUR",
        "economic.py",
        '        return {"available": False, "state": None, "unavailable_reason": reason}',
        '        return {"available": True, "state": 0.0, "unavailable_reason": reason}',
        f"{VALUE}::test_a_reserve_violation_is_unavailable_and_not_zero",
    ),
    (
        "N2b: and the no-outcome case with it",
        "economic.py",
        '            "state": None,\n            "unavailable_reason": '
        "ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN,",
        '            "state": 0.0,\n            "unavailable_reason": '
        "ECONOMIC_VALUE_UNAVAILABLE_NO_PLAN,",
        f"{VALUE}::test_no_outcome_at_all_is_unavailable_and_not_zero",
    ),
    (
        "N3: a reserve violation is priced anyway",
        "economic.py",
        "    elif plan.violation_kwh > 0.0:\n        reason = "
        "ECONOMIC_VALUE_UNAVAILABLE_VIOLATION",
        "    elif False:\n        reason = ECONOMIC_VALUE_UNAVAILABLE_VIOLATION",
        f"{VALUE}::test_a_reserve_violation_is_unavailable_and_not_zero",
    ),
    (
        "N4: missing tomorrow prices null the headline",
        "economic.py",
        '    return {\n        "available": True,\n        "state": _round_eur(advantage),',
        "    if not tomorrow_prices_known:\n"
        "        return {\n"
        '            "available": False,\n'
        '            "state": None,\n'
        '            "unavailable_reason": ECONOMIC_VALUE_UNAVAILABLE_EMPTY_HORIZON,\n'
        "        }\n"
        '    return {\n        "available": True,\n        "state": _round_eur(advantage),',
        f"{VALUE}::test_a_today_only_horizon_still_has_a_state",
    ),
    # ------------------------------------------ the marginal value of stored energy
    (
        "M1: the headline uses the upward side instead of retention",
        "economic.py",
        "        retention, retention_reason = plan.marginal_value_eur_per_kwh(\n"
        "            current_bucket - 1, bucket_kwh=bucket_kwh\n"
        "        )",
        "        retention, retention_reason = plan.marginal_value_eur_per_kwh(\n"
        "            current_bucket, bucket_kwh=bucket_kwh\n"
        "        )",
        f"{VALUE}::test_the_retention_side_is_discriminable_from_the_upward_side",
    ),
    (
        "M2: an undefined marginal value is published as zero",
        "economic.py",
        '        "stored_energy_marginal_value_eur_kwh": _round_eur(retention),',
        '        "stored_energy_marginal_value_eur_kwh": _round_eur(retention or 0.0),',
        f"{VALUE}::test_the_bottom_bucket_has_no_lower_side",
    ),
    (
        "M3: the bottom bucket loses its own reason",
        "economic.py",
        "    if current_bucket <= 0:",
        "    if False:",
        f"{VALUE}::test_the_bottom_bucket_has_no_lower_side",
    ),
    (
        "M4: the marginal value is read from the wrong run-state row",
        "economic.py",
        "    def _head_row(self, bucket: int) -> tuple[float, float] | None:\n"
        '        """Return the head value at ``bucket`` in the head run state, or '
        '``None``."""',
        "    def _head_row(self, bucket: int) -> tuple[float, float] | None:\n"
        '        """Return the head value at ``bucket`` in the head run state, or '
        '``None``."""\n'
        "        return self.head_value[bucket][0] if self.head_value else None",
        f"{VALUE}::test_the_marginal_value_is_read_from_the_head_run_state_row",
    ),
    (
        "M5: the resolution is rounded to two decimals",
        "economic.py",
        '            "marginal_value_resolution_kwh": _round_eur(bucket_kwh),',
        '            "marginal_value_resolution_kwh": _round_kwh(bucket_kwh),',
        f"{VALUE}::test_the_headline_marginal_value_is_the_retention_side",
    ),
    (
        "M6: the top-bucket guard is removed",
        "economic.py",
        "        if bucket >= len(self.head_value) - 1:\n"
        "            return None, STORED_VALUE_UNDEFINED_TOP_BUCKET",
        "        if False:\n            return None, STORED_VALUE_UNDEFINED_TOP_BUCKET",
        f"{VALUE}"
        "::test_the_top_bucket_guard_holds_and_is_not_reachable_from_a_start_state",
    ),
    (
        "M7: the violation tie is priced as if it were comparable",
        "economic.py",
        "        if here[0] != above[0]:\n"
        "            return None, STORED_VALUE_UNDEFINED_VIOLATION",
        "        if False:\n            return None, STORED_VALUE_UNDEFINED_VIOLATION",
        f"{STORED}::test_the_marginal_value_is_undefined_across_a_violation_boundary",
    ),
    # --------------------------------------------------- the bucket lookup
    (
        "B1: the bare int() bucket lookup is restored in economic.py",
        "economic.py",
        "def bucket_at_or_below_kwh(energy_kwh: float, *, bucket_kwh: float) -> int:",
        "def bucket_at_or_below_kwh(energy_kwh: float, *, bucket_kwh: float) -> int:\n"
        "    return int(energy_kwh / bucket_kwh) if bucket_kwh > 0.0 else 0",
        f"{VALUE}::test_the_bucket_lookup_carries_the_epsilon",
    ),
    # ------------------------------------------------ the civil-day split
    (
        "D1: the day boundary is inclusive",
        "economic.py",
        "        entry for entry in plan.intervals if entry.index < today_interval_count",
        "        entry for entry in plan.intervals if entry.index <= today_interval_count",
        f"{SPLIT}::test_the_boundary_is_the_days_own_length[96]",
    ),
    (
        "D1b: and the tomorrow side with it",
        "economic.py",
        "            entry for entry in plan.intervals if entry.index >= "
        "today_interval_count",
        "            entry for entry in plan.intervals if entry.index > "
        "today_interval_count",
        f"{SPLIT}::test_the_two_days_partition_the_plan_exactly",
    ),
    (
        "D2: the day length is hardcoded to 96",
        "economic.py",
        "    if today_interval_count <= 0:\n        return ()",
        "    today_interval_count = 96\n    if today_interval_count <= 0:\n"
        "        return ()",
        f"{SPLIT}::test_the_boundary_is_the_days_own_length[92]",
    ),
    (
        "D3: an unknown day length is guessed at instead of refused",
        "economic.py",
        "    if today_interval_count <= 0:\n        return ()",
        "    if today_interval_count <= 0:\n        today_interval_count = 96",
        f"{SPLIT}::test_an_unknown_day_length_refuses_to_guess",
    ),
    (
        "D4: the per-day interval value is forced to sum to the state",
        "economic.py",
        '        "today_interval_value_eur": today_block["interval_value_eur"],',
        '        "today_interval_value_eur": _round_eur(advantage),',
        f"{SPLIT}::test_the_per_day_interval_values_sum_to_the_plan_marginal",
    ),
    (
        "D5: the terminal credit is apportioned to a day",
        "economic.py",
        '        "grid_import_cost_eur": _round_eur(imports),',
        '        "grid_import_cost_eur": _round_eur(imports),\n'
        '        "edge_value_eur": 0.0,',
        f"{SPLIT}::test_the_terminal_credit_is_not_apportioned_to_a_day",
    ),
    (
        "D6: the switching fee is apportioned to every interval, not the run start",
        "economic.py",
        "            fee_per_start_eur * sum(1 for entry in entries if entry.run_start)",
        "            fee_per_start_eur * len(entries)",
        f"{SPLIT}::test_the_per_day_switching_fee_sums_to_the_plan_fee",
    ),
    (
        "D7: the day figures are renamed to imply they sum to the state",
        "economic.py",
        '        "today_interval_value_eur": today_block["interval_value_eur"],\n'
        '        "tomorrow_interval_value_eur": tomorrow_block["interval_value_eur"],',
        '        "today_value_eur": today_block["interval_value_eur"],\n'
        '        "tomorrow_value_eur": tomorrow_block["interval_value_eur"],',
        f"{VALUE}::test_the_day_split_names_carry_their_basis",
    ),
    # ------------------------------------------- the terminal edge value
    (
        "T1: the terminal edge value is republished as a replacement cost",
        "economic.py",
        '        "terminal_edge_value_eur_kwh": _round_eur('
        "outcome.edge_value_eur_per_kwh),",
        '        "replacement_cost_eur_kwh": _round_eur('
        "outcome.edge_value_eur_per_kwh),",
        f"{VALUE}::test_the_terminal_edge_value_is_published_under_its_own_name",
    ),
    # ----------------------------------------------------- the reason code
    (
        "R1: the reason code becomes a bare price comparison",
        "economic.py",
        "    run = outcome.desired.current_run\n"
        "    if run is not None and run.start_index in outcome.safety_buy_runs:\n"
        "        return REASON_CODE_PHYSICAL_SAFETY_BUY",
        "    if (\n"
        "        export_price_eur_kwh is not None\n"
        "        and marginal_eur_kwh is not None\n"
        "        and export_price_eur_kwh > marginal_eur_kwh\n"
        "    ):\n"
        "        return REASON_CODE_EXPORT_NOW_DOMINATES\n"
        "    run = outcome.desired.current_run\n"
        "    if run is not None and run.start_index in outcome.safety_buy_runs:\n"
        "        return REASON_CODE_PHYSICAL_SAFETY_BUY",
        f"{VALUE}::test_holding_now_with_a_sale_later_is_not_reported_as_immaterial",
    ),
    (
        "R2: a material hold is reported as immaterial",
        "economic.py",
        "    if outcome.desired.next_run is not None:\n"
        "        return REASON_CODE_AWAITING_PLANNED_ACTION",
        "    if False:\n        return REASON_CODE_AWAITING_PLANNED_ACTION",
        f"{VALUE}::test_holding_now_with_a_sale_later_is_not_reported_as_immaterial",
    ),
    (
        "R3: an exporting head is not reported as such",
        "economic.py",
        "    if action in (ECONOMIC_ACTION_EXPORT, ECONOMIC_ACTION_DISCHARGE):\n"
        "        return REASON_CODE_EXPORT_NOW_DOMINATES",
        "    if False:\n        return REASON_CODE_EXPORT_NOW_DOMINATES",
        f"{VALUE}::test_exporting_at_the_head_says_so",
    ),
    # ------------------------------------------------- the comparator fix
    (
        "C1: hold_cost stops receiving the plan's own model",
        "economic.py",
        "            ambient_self_consumption=ambient_self_consumption,\n"
        "            hard_floor_kwh=hard_floor_kwh,\n        ),",
        "        ),",
        f"{NEUTRAL}::test_the_comparator_correction_moves_only_the_comparator",
    ),
    (
        "C1b: and the pure function ignores the argument",
        "economic.py",
        # **Re-anchored twice in beta.41.** The interval outcomes were built by an
        # inline comprehension when this was written; the carry axis made them
        # ``_outcomes_with_carried_service``. The mutation is the same one -- the
        # solver builds its outcomes with the ambient model switched off while
        # claiming to honour it -- but the anchor now includes the two preceding
        # arguments, because without them it also matched the *other* call that
        # passes this flag. A non-unique anchor is not a detail: this mutation sat
        # applied in the solver through two runs, making every carry mutation inert,
        # while a leftover check based on the same anchor reported the tree clean.
        #
        # And the node changed with it: measured, this is caught by nine cases in
        # the beta.32 ambient family, which is where a solver that stops serving
        # the house from the battery shows up first.
        "        ac_by_delta=ac_by_delta,\n"
        "        permitted=permitted,\n"
        "        ambient_self_consumption=ambient_self_consumption,\n"
        "        max_discharge_ac_kwh=max_discharge_ac_kwh,\n",
        "        ac_by_delta=ac_by_delta,\n"
        "        permitted=permitted,\n"
        "        ambient_self_consumption=False,\n"
        "        max_discharge_ac_kwh=max_discharge_ac_kwh,\n",
        "tests/test_beta32_ambient_self_consumption.py"
        "::test_a_load_below_one_bucket_is_served_from_the_battery",
    ),
    (
        "C2: the ambient floor clamp is dropped from the baseline",
        "economic.py",
        "                        and _physical_energy_kwh(\n"
        "                            table.energy(landed), spent, carry_step_kwh\n"
        "                        )\n"
        "                        >= hard_floor_kwh - 1e-9",
        "                        and _physical_energy_kwh(\n"
        "                            table.energy(landed), spent, carry_step_kwh\n"
        "                        )\n"
        "                        >= -1e9",
        f"{NEUTRAL}::test_the_comparator_is_priced_under_the_plans_own_model",
    ),
    # --------------------------------------------------- decision neutrality
    (
        "X1: a decision path reads the economic value",
        "economic.py",
        "def _walk_forward(\n    *,\n    table: PhysicsTable,",
        "def _walk_forward(\n    *,\n    table: PhysicsTable,  # noqa: E501\n"
        "    _unused_summary=economic_value_summary,",
        f"{NEUTRAL}::test_only_publishing_functions_read_the_economic_value",
    ),
    (
        "X2: the solver grows an economic-value argument",
        "economic.py",
        "def build_outcome(\n    *,",
        "def build_outcome(\n    *,\n    economic_value: dict | None = None,",
        f"{NEUTRAL}::test_build_outcome_does_not_accept_an_economic_value_argument",
    ),
    # ------------------------------------------------------- persistence
    (
        "P1: the hot ring is grown instead of using the partitioned store",
        "const.py",
        "MAX_DECISION_RECORDS_RETAINED: Final = 192",
        "MAX_DECISION_RECORDS_RETAINED: Final = 2880",
        f"{PERSIST}::test_the_hot_ring_is_not_grown",
    ),
    (
        "P2: the change-triggered digest absorbs a published figure",
        "economic.py",
        "        fingerprint=fingerprint_economic(\n"
        "            price_fingerprint=price_fingerprint,",
        "        fingerprint=fingerprint_economic(\n"
        '            price_fingerprint=f"{price_fingerprint}'
        "{value.get('decision_advantage_eur')}\",",
        f"{PERSIST}::test_the_snapshot_fingerprint_is_not_moved_by_the_new_figures",
    ),
    (
        "P3: an absent figure is read back as zero",
        "economic.py",
        '            decision_advantage_eur=_finite(raw.get("eva")),',
        '            decision_advantage_eur=_finite(raw.get("eva")) or 0.0,',
        f"{PERSIST}::test_a_document_without_the_figures_loads_with_them_absent",
    ),
    (
        "P4: the evidence store's minor version is not bumped",
        "const.py",
        "FORECAST_STORAGE_MINOR_VERSION: Final = 8",
        "FORECAST_STORAGE_MINOR_VERSION: Final = 7",
        f"{PERSIST}::test_only_the_evidence_store_minor_version_moved",
    ),
    # ------------------------------------------------------- the entity
    (
        "S1: the sensor renders missing data as zero",
        "sensor.py",
        "    payload = _economic_value_payload(coordinator)\n"
        '    if not payload.get("available"):\n'
        "        return None",
        "    payload = _economic_value_payload(coordinator)\n"
        '    if not payload.get("available"):\n'
        "        return 0.0",
        f"{VALUE}::test_the_entity_is_unknown_before_a_plan_exists",
    ),
    (
        "S2: the entity gains a state class it must not have",
        "sensor.py",
        "        device_class=SensorDeviceClass.MONETARY,\n"
        "        native_unit_of_measurement=CURRENCY_EURO,",
        "        device_class=SensorDeviceClass.MONETARY,\n"
        "        state_class=SensorStateClass.MEASUREMENT,\n"
        "        native_unit_of_measurement=CURRENCY_EURO,",
        f"{VALUE}::test_the_entity_declares_no_state_class",
    ),
    (
        "S3: the entity id is hardcoded rather than derived",
        "tests/test_entity_contract.py",
        '    "sensor.alpha_ems_economic_value": {',
        '    "sensor.alpha_ems_manager_economic_value": {',
        f"{CONTRACT}::test_no_entity_is_missing_or_extra",
    ),
    # -------------------------------- the fixture's own vacuity, as beta.36 did
    # **V1 retired in beta.41, with the reason recorded.** It flattened the price
    # series to prove that
    # ``test_the_state_is_the_plan_against_the_passive_counterfactual`` depends on
    # there being a spread, and the test passed anyway. It should: the assertion is
    # that the published state *is* the plan measured against doing nothing, and
    # that identity holds whether or not the plan finds anything worth doing. The
    # figure it would have been vacuous about is pinned by V2 and V3 below, which
    # break the fixture's shape rather than its prices.
    (
        "V2: the day-length parameter is ignored by the fixture",
        "tests/beta34_shape.py",
        "    return (index % max(1, day_intervals)) / 4.0",
        "    return (index % 96) / 4.0",
        f"{SPLIT}::test_the_clock_mapping_follows_the_days_own_length[92]",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
