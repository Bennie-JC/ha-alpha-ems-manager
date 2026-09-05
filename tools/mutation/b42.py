"""Break each beta.42 claim on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch. A surviving mutation means the
test is vacuous and **the test gets rewritten**; it never means the mutation gets
weakened.

**What this release actually changed, and why it needs a table of its own.** beta.42
touches no planner decision -- the DP objective, the reserve, the three purchase
categories, the terminal value, Stage A authority and Stage B admission are all
frozen, and the seven neutrality digests plus the beta.40/41 anchors are the proof.
What it changes is what the integration *says*, and a reporting layer fails in a way
a planner does not: silently, in one direction, and in a number nobody can check
against anything.

That shape is what these mutations attack. Five families, and each is a way a figure
could be wrong while every existing test stayed green:

The ``F`` mutations attack **day finalisation**. Every clause of ``day_finalizable``
is a way a day can look complete and not be, and a missing clause does not raise --
it seals a day short, once, permanently, and folds that into a lifetime total.

The ``S`` mutations attack the **lifetime cursor**. Monotonicity here is not tidiness:
the store is rewritten from memory on every closed quarter, so the same seal can
reach disk twice, and a cursor that merely tended forwards would double-count with
nothing able to notice afterwards.

The ``L`` mutations attack the **campaign lifecycle**. The guarantee is exactly-once
closure per attempt across a restart, and the two ways to break it are opposite: emit
a public ``stopped`` at a row boundary that re-arms (per-quarter spam), or route a
never-started campaign's terminal through the execution finality latch (which blocks
a legitimate later attempt).

The ``R`` mutations attack the **investment return**. Absence and zero are different
facts; a payback estimate below its sample threshold is arbitrary rather than
conservative; and a trailing mean at or below zero must refuse rather than divide.

The ``B`` mutations attack the **load boundary and the basis labels** -- the two
Phase-7 corrections whose failure mode is a plausible-looking number rather than an
error.

The ``H`` mutations attack the **truncated horizon**, captured live at 01:00 with
tomorrow's prices unpublished. Every repair they model looks helpful -- span the
hole, credit the unpriced intervals anyway, report the horizon as complete -- and
every one of them lets the optimiser plan across data nobody published. The
resulting plan would be confidently wrong with nothing downstream able to show it,
because invented prices are still real numbers.
"""

from __future__ import annotations

FINAL = "tests/test_beta42_day_finalisation.py"
LIFE = "tests/test_beta42_lifecycle_events.py"
ROI = "tests/test_beta42_battery_return.py"
BASIS = "tests/test_beta42_figure_basis.py"
PRICING = "tests/test_beta42_historic_pricing.py"
HORIZON = "tests/test_beta42_missing_tomorrow_prices.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # F -- day finalisation
    # =====================================================================
    (
        "F1: the write-once guard is dropped, so a day can be sealed twice",
        "storage.py",
        "        if self.final_benefit is not None:\n            return False",
        "        if False:\n            return False",
        f"{FINAL}::test_a_day_seals_once_and_a_second_attempt_is_refused",
    ),
    (
        "F2: today is finalisable, so a day seals on the intervals it has so far",
        "coordinator.py",
        '        if day >= today:\n            return False, "day_not_past"',
        '        if day > today:\n            return False, "day_not_past"',
        f"{FINAL}::test_today_is_never_finalizable",
    ),
    (
        "F3: a missing interval no longer withholds the seal",
        "coordinator.py",
        "        if any(record.measured[index] is None for index in range(count)):\n"
        '            return False, "intervals_missing"',
        "        if all(record.measured[index] is None for index in range(count)):\n"
        '            return False, "intervals_missing"',
        f"{FINAL}::test_one_missing_interval_withholds_the_seal",
    ),
    (
        "F4: an unrecorded flexible load no longer withholds the seal",
        "coordinator.py",
        "        if any(record.total_load_at(index) is None for index in range(count)):\n"
        '            return False, "load_boundary_incomplete"',
        "        if all(record.total_load_at(index) is None for index in range(count)):\n"
        '            return False, "load_boundary_incomplete"',
        f"{FINAL}::test_an_expected_but_unrecorded_flexible_load_withholds_the_seal",
    ),
    (
        "F5: a day with no stored prices seals at whatever it could price",
        "coordinator.py",
        "        if self._prices_for_day(day, count) is None:\n"
        '            return False, "no_stored_prices"',
        '        if False:\n            return False, "no_stored_prices"',
        f"{FINAL}::test_a_day_with_no_stored_prices_is_not_sealed_at_a_smaller_number",
    ),
    (
        "F6: the sealing pass re-seals an already-sealed day every refresh",
        "coordinator.py",
        "            if record.final_benefit is not None:\n                continue",
        "            if record.final_benefit is None and False:\n                continue",
        # **Retargeted after the first run: the original node was vacuous for
        # this claim.** The mutation is unchanged; what changed is which test is
        # named, because naming a test that cannot fail is how a table comes to
        # report confidence it has not earned.
        f"{FINAL}::test_a_sealed_day_is_never_priced_again",
    ),
    (
        # **Retargeted after the first run: the original node was vacuous for
        # this claim.** The mutation is unchanged; what changed is which test is
        # named, because naming a test that cannot fail is how a table comes to
        # report confidence it has not earned.
        "F7: the benefit reads a planner term, so a setting moves a sealed day",
        "coordinator.py",
        "        return window.realized_battery_benefit_eur",
        "        return window.realized_net_value_eur",
        f"{FINAL}::test_the_sealed_benefit_is_the_cash_comparator_and_not_the_household_position",
    ),
    # =====================================================================
    # S -- the lifetime cursor
    # =====================================================================
    (
        "S1: the cursor tends forwards rather than refusing, so a replay adds twice",
        "storage.py",
        "        if self.sealed_through is not None and day <= self.sealed_through:\n"
        "            return False",
        "        if False:\n            return False",
        f"{FINAL}::test_the_cursor_refuses_a_day_it_has_already_counted",
    ),
    (
        "S2: a backwards clock rewinds the cursor and re-adds counted days",
        "storage.py",
        "        if self.sealed_through is not None and day <= self.sealed_through:\n"
        "            return False",
        "        if self.sealed_through is not None and day == self.sealed_through:\n"
        "            return False",
        f"{FINAL}::test_a_backwards_clock_cannot_rewind_the_lifetime_total",
    ),
    (
        "S3: eviction drops a sealed day instead of folding it forward",
        "storage.py",
        "            benefit = record.benefit_eur_final\n"
        "            if benefit is not None:\n"
        "                self.seal_day(day, benefit)",
        "            benefit = record.benefit_eur_final\n"
        "            if benefit is None:\n"
        "                self.seal_day(day, 0.0)",
        f"{FINAL}::test_eviction_folds_a_sealed_day_and_skips_an_unsealed_one",
    ),
    (
        "S4: an unsealed day advances the cursor, so its gap becomes invisible",
        "storage.py",
        "            if benefit is not None:\n                self.seal_day(day, benefit)",
        "            self.seal_day(day, benefit or 0.0)",
        f"{FINAL}::test_eviction_folds_a_sealed_day_and_skips_an_unsealed_one",
    ),
    (
        "S5: the fold count is not kept, so the average climbs as days age out",
        "storage.py",
        "        self.sealed_day_count += 1",
        "        self.sealed_day_count += 0",
        f"{ROI}::test_the_cumulative_total_survives_the_days_being_evicted",
    ),
    (
        "S6: the cursor is written without its total, so coverage is unstatable",
        "storage.py",
        '                "days": self.sealed_day_count,',
        "",
        f"{FINAL}::test_the_lifetime_total_round_trips_with_its_cursor",
    ),
    (
        "S7: a malformed seal reads as zero rather than as unsealed",
        "storage.py",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n"
        "        return None\n"
        "    value = float(value)\n"
        '    if value != value or abs(value) == float("inf"):\n'
        "        return None\n"
        '    return {"at": at, "v": value, "bv": basis}',
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n"
        "        value = 0.0\n"
        "    value = float(value)\n"
        '    if value != value or abs(value) == float("inf"):\n'
        "        value = 0.0\n"
        '    return {"at": at, "v": value, "bv": basis}',
        f"{FINAL}::test_a_malformed_seal_reads_as_unsealed_and_never_as_zero",
    ),
    # =====================================================================
    # L -- the campaign lifecycle
    # =====================================================================
    (
        "L1: a never-started instance emits stopped, claiming an execution",
        "coordinator.py",
        '        if LIFECYCLE_KIND_STARTED not in mark["marks"]:\n            return',
        '        if LIFECYCLE_KIND_STARTED not in mark["marks"] and False:\n            return',
        f"{LIFE}::test_a_never_started_instance_emits_no_stopped_event_on_the_live_path",
    ),
    (
        "L2: started is re-emitted on every refresh, which is the beta.30 spam",
        "coordinator.py",
        '        if mark is None or LIFECYCLE_KIND_STARTED in mark["marks"]:\n            return',
        "        if mark is None:\n            return",
        f"{LIFE}::test_progress_alone_never_produces_an_event",
    ),
    (
        "L3: a restart replays a terminal that was already published",
        "coordinator.py",
        "        if self.store.lifecycle_closed(instance_id):\n"
        "            self.store.campaign_lifecycle = None\n"
        '            return "already_closed"',
        "        if False:\n"
        "            self.store.campaign_lifecycle = None\n"
        '            return "already_closed"',
        f"{LIFE}::test_row_d_a_published_terminal_is_never_republished",
    ),
    (
        "L4: the recovery publishes twice, so a deferred instance closes each refresh",
        "coordinator.py",
        # Extended by one line to reach *this* method. The shorter form matched
        # ``_lifecycle_removed`` as well, and the first match is the one that
        # gets edited, so the mutation ran against code the named test does not
        # exercise and was reported as a survivor. The runner now refuses an
        # ambiguous anchor outright rather than silently picking one.
        '        if instance_id is None or self.store.lifecycle_closed(instance_id):\n            self.store.campaign_lifecycle = None\n            return\n        recorded = mark.get("classification_at_creation", LIFECYCLE_CLASS_UNKNOWN)',
        '        if instance_id is None:\n            self.store.campaign_lifecycle = None\n            return\n        recorded = mark.get("classification_at_creation", LIFECYCLE_CLASS_UNKNOWN)',
        f"{LIFE}::test_the_publisher_refuses_a_second_terminal_for_one_instance",
    ),
    (
        "L5: a never-started campaign is guessed rather than left open",
        "coordinator.py",
        "        if window_end is not None and now >= window_end:\n"
        "            return EXECUTION_STOP_WINDOW_ENDED\n"
        "        return None",
        "        if window_end is not None and now >= window_end:\n"
        "            return EXECUTION_STOP_WINDOW_ENDED\n"
        "        return EXECUTION_STOP_WINDOW_ENDED",
        f"{LIFE}::test_row_a_stays_open_while_neither_answer_is_yet_true",
    ),
    (
        "L6: a started instance recovers as not_executed, hiding a lost quarter",
        "coordinator.py",
        "        if LIFECYCLE_KIND_STARTED not in marks:\n"
        "            reason = self._dangling_creation_reason(mark, now)",
        "        if True:\n"
        "            reason = self._dangling_creation_reason(mark, now)",
        f"{LIFE}::test_row_b_a_started_campaign_is_failed_with_progress_unknown",
    ),
    (
        "L7: a pre-restart verdict is downgraded, so a success is filed as failed",
        "coordinator.py",
        '        if LIFECYCLE_KIND_STOPPED in marks:\n            measurable = bool(mark.get("measurable", True))',
        '        if False:\n            measurable = bool(mark.get("measurable", True))',
        f"{LIFE}::test_row_c_preserves_a_verdict_that_was_already_reachable",
    ),
    (
        "L8: an unmeasurable total can still recover as success",
        "coordinator.py",
        "        if not measurable:\n            return OUTCOME_FAILED\n        if target_kwh is None:",
        "        if False:\n            return OUTCOME_FAILED\n        if target_kwh is None:",
        f"{LIFE}::test_row_c_still_refuses_success_when_the_total_was_not_a_measurement",
    ),
    (
        "L9: a displaced campaign is reported as a plant shortfall",
        "coordinator.py",
        "        if stop_reason == EXECUTION_STOP_PLAN_REPLACED:\n            return OUTCOME_SUPERSEDED",
        "        if False:\n            return OUTCOME_SUPERSEDED",
        f"{LIFE}::test_row_c_reports_a_displaced_campaign_as_superseded",
    ),
    (
        "L10: a recovered terminal wears this boot's classification",
        "coordinator.py",
        '        recorded = mark.get("classification_at_creation", LIFECYCLE_CLASS_UNKNOWN)',
        "        recorded = self._campaign_classification(\n"
        '            mark.get("campaign_id")\n'
        '        ).get("classification", LIFECYCLE_CLASS_UNKNOWN)',
        f"{LIFE}::test_a_recovered_terminal_publishes_the_classification_it_was_created_with",
    ),
    (
        "L11: the telemetry latch is unbounded, so the document grows without limit",
        "storage.py",
        "        self.closed_lifecycle.append(instance_id)\n"
        "        del self.closed_lifecycle[:-MAX_CAMPAIGN_LIFECYCLE_REMEMBERED]",
        "        self.closed_lifecycle.append(instance_id)",
        f"{LIFE}::test_the_latch_round_trips_and_stays_bounded",
    ),
    (
        "L12: a malformed mark is trusted, so a terminal is filed for no campaign",
        "storage.py",
        "            if isinstance(lifecycle, dict) and isinstance(\n"
        '                lifecycle.get("instance_id"), str\n'
        "            ):",
        "            if isinstance(lifecycle, dict):",
        f"{LIFE}::test_a_malformed_mark_reads_as_no_campaign_open",
    ),
    (
        "L13: the shortfall keeps the terminal's sign, so a miss reads as a surplus",
        "coordinator.py",
        "        return round(-float(value), 4)",
        "        return round(float(value), 4)",
        f"{LIFE}::test_a_partial_result_carries_a_positive_shortfall",
    ),
    # =====================================================================
    # R -- the investment return
    # =====================================================================
    (
        "R1: an absent investment defaults to zero, so recovery is measured "
        "against nothing",
        "coordinator.py",
        "        gross = config.battery_investment_eur\n        if gross is None:",
        "        gross = config.battery_investment_eur or 0.0\n        if gross is None:",
        f"{ROI}::test_no_investment_configured_is_unavailable_with_a_reason",
    ),
    (
        "R2: an unset option is coerced to a number at the config boundary",
        "coordinator.py",
        "    if raw is None or isinstance(raw, bool):\n        return None",
        "    if raw is None or isinstance(raw, bool):\n        return 0.0",
        f"{ROI}::test_an_unset_investment_stays_none_rather_than_becoming_zero",
    ),
    (
        "R3: the subsidy is not subtracted, so the recovery percentage is understated",
        "coordinator.py",
        "        net = round(gross - subsidy - credit, 2)",
        "        net = round(gross, 2)",
        f"{ROI}::test_the_net_investment_subtracts_the_subsidy_and_the_credit",
    ),
    (
        "R4: payback is published below the sample threshold",
        "coordinator.py",
        "        if days < ROI_MIN_SAMPLE_DAYS:",
        "        if days < 0:",
        f"{ROI}::test_payback_is_withheld_below_the_sample_threshold",
    ),
    (
        "R5: a non-positive trailing mean divides rather than refusing",
        "coordinator.py",
        "        if per_day <= 0.0:",
        "        if per_day == 0.0:",
        f"{ROI}::test_payback_is_withheld_rather_than_infinite_when_nothing_was_earned",
    ),
    (
        "R6: the trailing window re-derives, so an unsealed day is counted",
        "coordinator.py",
        "            if first <= day < today and record.benefit_eur_final is not None",
        "            if first <= day < today",
        f"{ROI}::test_the_trailing_windows_read_sealed_values_and_never_a_re_derivation",
    ),
    (
        "R7: history is claimed complete whenever a purchase date exists",
        "coordinator.py",
        "        complete = bool(configured and available and available <= configured)",
        "        complete = bool(configured)",
        f"{ROI}::test_a_purchase_date_before_the_evidence_is_reported_not_estimated",
    ),
    (
        "R8: the export leg is called cash whatever basis the evidence carries",
        "coordinator.py",
        "        is_cash = bool(observed) and observed <= {",
        "        is_cash = bool(observed) or observed <= {",
        f"{ROI}::test_the_two_price_legs_are_named_separately",
    ),
    # =====================================================================
    # B -- the load boundary and the basis labels
    # =====================================================================
    (
        "B1: the counterfactual is fed the EV-excluded baseline again",
        "coordinator.py",
        '            "load_kwh": [record.total_load_at(i) for i in range(count)],',
        '            "load_kwh": [record.baseline_at(i) for i in range(count)],',
        # **Retargeted after the first run: the original node was vacuous for
        # this claim.** The mutation is unchanged; what changed is which test is
        # named, because naming a test that cannot fail is how a table comes to
        # report confidence it has not earned.
        f"{FINAL}::test_the_counterfactual_is_differenced_against_the_meter_it_is_compared_to",
    ),
    (
        "B2: the whole-house total guesses zero for an unmeasured flexible load",
        "storage.py",
        "        if self.ev_expected[index] and self.ev[index] is None:\n            return None",
        "        if False:\n            return None",
        f"{FINAL}::test_an_expected_but_unrecorded_flexible_load_withholds_the_seal",
    ),
    (
        "B3: a past day falls back to today's prices instead of its own issuance",
        "coordinator.py",
        "        snapshot = self.history.latest_price_snapshot(day)\n"
        "        if snapshot is None:\n"
        "            return None",
        "        snapshot = None\n"
        "        if snapshot is None:\n"
        "            return None",
        f"{PRICING}::test_a_stored_snapshot_prices_a_day_the_live_forecast_has_forgotten",
    ),
    (
        "B4: the two price bases collapse to one word",
        "coordinator.py",
        "        return stored_buy, stored_sell, PRICE_BASIS_STORED_SNAPSHOT",
        "        return stored_buy, stored_sell, PRICE_BASIS_LIVE_FORECAST",
        f"{PRICING}::test_the_basis_of_every_priced_day_is_published",
    ),
    (
        "B5: the household position is labelled measured again",
        "realized.py",
        '        "realised_net_value_eur": LEDGER_BASIS_ATTRIBUTED,',
        '        "realised_net_value_eur": LEDGER_BASIS_MEASURED,',
        f"{BASIS}::test_a_total_is_no_stronger_than_its_weakest_addend",
    ),
    (
        "B6: a forecast is published as a planner valuation",
        "realized.py",
        '        "today_accounting.remaining_expected_today_eur": LEDGER_BASIS_FORECAST,',
        '        "today_accounting.remaining_expected_today_eur": '
        "LEDGER_BASIS_PLANNER_DERIVED,",
        f"{BASIS}::test_the_seventh_word_separates_a_forecast_from_a_valuation",
    ),
    (
        "B7: an unclassified euro figure is silently omitted at the entity",
        "sensor.py",
        "        basis[name] = word or LEDGER_BASIS_UNCLASSIFIED",
        "        if word:\n            basis[name] = word",
        # **Retargeted after the first run: the original node was vacuous for
        # this claim.** The mutation is unchanged; what changed is which test is
        # named, because naming a test that cannot fail is how a table comes to
        # report confidence it has not earned.
        f"{BASIS}::test_a_euro_figure_the_map_does_not_cover_is_reported_not_dropped",
    ),
    (
        "B8: the entity restates the basis instead of projecting the one map",
        "sensor.py",
        '        word = published.get(name) or published.get(f"today_accounting.{name}")',
        '        word = published.get(f"today_accounting.{name}")',
        f"{BASIS}::test_the_flattened_day_figures_keep_the_basis_of_their_nested_names",
    ),
    # =====================================================================
    # H -- the truncated horizon
    # =====================================================================
    (
        "H1: an unknown price is skipped rather than ending the horizon",
        "economic.py",
        "        if position >= len(prices) or not prices[position].known:\n"
        '            limited_by = "prices"\n'
        "            break\n",
        "        if position >= len(prices) or not prices[position].known:\n"
        '            limited_by = "prices"\n'
        "            continue\n",
        # **Retargeted: the original node could not fail.** Against a missing
        # *tail*, skipping and stopping are indistinguishable -- everything after
        # the hole is missing too. A partial publication is what separates them,
        # and the fixture for it was added because this mutation survived.
        f"{HORIZON}::test_a_hole_ends_the_horizon_rather_than_being_stepped_over",
    ),
    (
        "H2: the horizon reports itself complete when prices ran out",
        "economic.py",
        '            limited_by = "prices"\n',
        '            limited_by = "complete"\n',
        f"{HORIZON}::test_the_captured_refresh_reproduces_exactly",
    ),
    (
        "H3: unpriced intervals are credited to the reachability window anyway",
        "economic.py",
        "        if position >= len(prices) or not prices[position].known:\n"
        "            break\n",
        "        if position >= len(prices) and False:\n            break\n",
        f"{HORIZON}::test_an_unpublished_interval_is_never_valued_at_zero",
    ),
]
