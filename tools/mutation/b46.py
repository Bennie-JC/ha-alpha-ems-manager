"""Break each beta.46 claim on purpose, and prove a named test notices.

**This release exists because a test rig fabricated a type production never
produces.** The beta.44 arm suite set ``coordinator._coherence`` to the *string*
``"ok"``, so the delivery gate's ``self._coherence not in (None, COHERENCE_OK)``
compared a string to a string and behaved. Live, that field holds a
``ControlCoherence`` dataclass, the membership test was true on every tick that had a
verdict at all, and an eight-hour charge filed ``delivery_latency_s: null`` with
``sources_incoherent``. The rig now builds the real object, which is the correction
that makes the table below meaningful.

Three families:

The ``O`` mutations attack the **arm objective**: which rows it sums, which boundary
it reads them at, where the stretch begins and ends, and whether an underivable figure
is allowed to become zero. Every one of them leaves a coordinator that still publishes
a plausible-looking number.

The ``F`` mutations attack the **forgone derivation** -- the denominator above all,
because prorating a whole multi-row arm over a single quarter charges the arm's entire
promise to its first fifteen minutes and is off by exactly the row count.

The ``D`` mutations attack the **delivery evidence**: the coherence comparison itself,
the latch that let one bad tick label the whole arm, and both halves of the
attribution rule that keeps ambient production out of a dispatch's credit.

A survivor means the test is vacuous and **the test gets rewritten**; it never means
the mutation gets weakened.
"""

from __future__ import annotations

ARM = "tests/test_beta46_arm_attribution.py"
B44 = "tests/test_beta44_arm_measurement.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # O -- the arm objective
    # =====================================================================
    (
        "O1: the objective is the realised figure again, so every arm files zero",
        "coordinator.py",
        '        derived = self._arm_span_planned(moment, instant_of(arm.get("row_start")))\n'
        "        if derived is None:\n"
        "            return",
        '        arm["objective_kwh"] = round(self._objective_kwh_for(self._quarter), 3)\n'
        "        derived = None\n"
        "        if derived is None:\n"
        "            return",
        f"{ARM}::test_the_live_charge_arm_no_longer_files_a_zero_objective",
    ),
    (
        "O2: only the row covering now is counted",
        "coordinator.py",
        "        objective = sum(\n"
        "            rows[position].objective_kwh(plan.intent)\n"
        "            for position in range(first, last + 1)\n"
        "        )",
        "        objective = rows[index].objective_kwh(plan.intent)",
        f"{ARM}::test_a_multi_row_charge_arm_measures_the_whole_arm",
    ),
    (
        "O3: the forward walk stops, so only the arm's opening rows are counted",
        "coordinator.py",
        "        last = index\n"
        "        while last + 1 < len(rows) and rows[last + 1].executable:\n"
        "            last += 1",
        "        last = index",
        f"{ARM}::test_a_contiguous_export_arm_sums_its_executable_rows",
    ),
    (
        "O4: the forward walk ignores executability, so the arm eats the next one",
        "coordinator.py",
        "        while last + 1 < len(rows) and rows[last + 1].executable:",
        "        while last + 1 < len(rows):",
        f"{ARM}::test_a_non_executable_gap_bounds_the_arm_on_both_sides",
    ),
    (
        "O5: the backward walk ignores executability, so the arm inherits the last",
        "coordinator.py",
        "            and rows[first - 1].executable\n"
        "            and (since is None or rows[first - 1].start >= since)",
        "            and (since is None or rows[first - 1].start >= since)",
        f"{ARM}::test_a_stale_claim_never_carries_an_objective_across_a_gap",
    ),
    (
        "O6: the backward walk is unbounded, so a re-claim inherits earlier rows",
        "coordinator.py",
        "            and (since is None or rows[first - 1].start >= since)",
        "            and True",
        f"{ARM}::test_a_claim_retaken_mid_stretch_does_not_inherit_earlier_rows",
    ),
    (
        "O7: an export is measured at the battery, which is its ceiling not its promise",
        "coordinator.py",
        "        boundary = (\n"
        "            CAMPAIGN_BOUNDARY_METER\n"
        "            if plan.intent == EXECUTION_INTENT_NET_EXPORT\n"
        "            else CAMPAIGN_BOUNDARY_BATTERY\n"
        "        )",
        "        boundary = CAMPAIGN_BOUNDARY_BATTERY",
        f"{ARM}::test_a_single_row_export_arm_keeps_the_meter_objective",
    ),
    (
        "O8: an underivable objective is published as zero",
        "coordinator.py",
        '        derived = self._arm_span_planned(moment, instant_of(arm.get("row_start")))\n'
        "        if derived is None:\n"
        "            return",
        '        derived = self._arm_span_planned(moment, instant_of(arm.get("row_start")))\n'
        "        if derived is None:\n"
        '            arm["objective_kwh"] = 0.0\n'
        "            return",
        f"{ARM}::test_an_arm_with_no_frozen_schedule_withholds_the_objective",
    ),
    (
        "O9: a non-executable row still opens an arm objective",
        "coordinator.py",
        "        if index is None or not rows[index].executable:\n            return None",
        "        if index is None:\n            return None",
        f"{ARM}::test_a_stale_claim_never_carries_an_objective_across_a_gap",
    ),
    # =====================================================================
    # F -- the forgone derivation
    # =====================================================================
    (
        "F1: prorated over one quarter, so a multi-row arm loses its whole promise",
        "coordinator.py",
        '            elapsed = min(arm["delivery_latency_s"], span)\n'
        '            arm["objective_forgone_to_activation_kwh"] = round(\n'
        "                objective * elapsed / span, 3\n"
        "            )",
        '            elapsed = min(arm["delivery_latency_s"], QUARTER_SECONDS)\n'
        '            arm["objective_forgone_to_activation_kwh"] = round(\n'
        "                objective * elapsed / QUARTER_SECONDS, 3\n"
        "            )",
        f"{ARM}::test_the_forgone_objective_is_prorated_over_the_arm_not_one_quarter",
    ),
    (
        "F2: the delay is not clamped to the arm, so more is forgone than was planned",
        "coordinator.py",
        '            elapsed = min(arm["delivery_latency_s"], span)',
        '            elapsed = arm["delivery_latency_s"]',
        f"{ARM}::test_a_delay_longer_than_the_arm_forgoes_the_arm_and_no_more",
    ),
    (
        "F3: a forgone figure is invented for an arm that never delivered",
        "coordinator.py",
        "        if caused <= DISPATCH_POWER_DEADBAND_KW:",
        "        if False:",
        f"{ARM}::test_ambient_absorption_moves_the_battery_clock_and_nothing_else",
    ),
    # =====================================================================
    # D -- delivery evidence
    # =====================================================================
    (
        "D1: the object is compared to the state string again, as beta.44 shipped it",
        "coordinator.py",
        "        if self._coherence is not None and not self._coherence.usable:",
        "        if self._coherence not in (None, COHERENCE_OK):",
        f"{ARM}::test_a_coherent_attributable_sample_is_no_longer_discarded",
    ),
    (
        "D2: the coherence gate is gone, so a blind tick attributes a figure",
        "coordinator.py",
        "        if self._coherence is not None and not self._coherence.usable:\n"
        '            arm["delivery_evidence"] = ARM_EVIDENCE_INCOHERENT\n'
        "            return",
        "        if False:\n"
        '            arm["delivery_evidence"] = ARM_EVIDENCE_INCOHERENT\n'
        "            return",
        f"{ARM}::test_an_incoherent_tick_still_refuses_a_delivery_figure",
    ),
    (
        "D3: the reason latches again, so one hiccup labels the whole arm",
        "coordinator.py",
        '            arm["delivery_evidence"] = ARM_EVIDENCE_INCOHERENT\n            return',
        '            arm.setdefault("delivery_evidence", ARM_EVIDENCE_INCOHERENT)\n'
        "            return",
        f"{ARM}::test_the_published_reason_describes_the_latest_observation",
    ),
    (
        "D4: raw battery charge times the delivery, so absorption reads as dispatch",
        "coordinator.py",
        "            caused = max(0.0, charge_kw - surplus)",
        "            caused = charge_kw",
        f"{ARM}::test_ambient_absorption_moves_the_battery_clock_and_nothing_else",
    ),
    (
        "D5: raw meter export times the delivery, so production reads as dispatch",
        "coordinator.py",
        "            caused = max(0.0, max(0.0, flows.grid_export_w) / 1000.0 - surplus)",
        "            caused = max(0.0, flows.grid_export_w) / 1000.0",
        f"{ARM}::test_ambient_export_is_not_credited_to_an_export_arm",
    ),
    (
        "D6: an unreadable surplus is treated as zero, fabricating attribution",
        "coordinator.py",
        "        surplus = self._budget_surplus_kw()\n        if surplus is None:",
        "        surplus = self._budget_surplus_kw() or 0.0\n        if False:",
        f"{B44}::test_an_unreadable_production_surplus_refuses_to_attribute",
    ),
]
