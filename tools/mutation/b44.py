"""Break each beta.44 claim on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out which
kind you have is to break the thing and watch. A surviving mutation means the test is
vacuous and **the test gets rewritten**; it never means the mutation gets weakened.

**Why an instrumentation release needs a table at all.** beta.44 changes no planner
decision — that is its hard release gate, and the neutrality digests prove it. What it
adds is measurement, and a measurement fails in the way a decision does not: silently,
plausibly, and in a number nobody can check against anything. Worse, these particular
numbers are the calibration set a later release will use to price a physical arm cycle,
so a quietly wrong figure here becomes a quietly wrong economic parameter there.

Two families:

The ``A`` mutations attack the **arm boundary and the refused run**. The whole release
rests on one claim: an arm is a maximal contiguous stretch of executable rows, because
a non-executable row makes the tick stop the dispatch and the next executable row claim
the marker again. Every way of getting that wrong produces a count that still looks
like a count.

The ``M`` mutations attack the **two clocks and the attribution**. The live capture is
the reason: one arm measured 37.3 s at the vendor register and 91.7 s at the first tick
that saw it, and a single figure would have reported the vendor as 2.5x slower than it
is. The attribution guards are the other half — ambient production charging the pack, or
already crossing the meter, must never be read as proof that a dispatch started.
"""

from __future__ import annotations

PLAN = "tests/test_beta44_arm_plan.py"
MEAS = "tests/test_beta44_arm_measurement.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # A -- the arm boundary and the refused run
    # =====================================================================
    (
        "A1: a non-executable row no longer breaks the stretch, so gaps vanish",
        "coordinator.py",
        '            armable = row.get("not_executable") is None',
        "            armable = True",
        f"{PLAN}::test_a_non_executable_gap_creates_a_second_arm",
    ),
    (
        "A2: every executable row is its own arm, so arm_count becomes a row count",
        "coordinator.py",
        "            elif not armable and first is not None:\n"
        "                spans.append((first, index - 1))\n"
        "                first = None",
        "            if armable:\n"
        "                spans.append((index, index))\n"
        "                first = None",
        f"{PLAN}::test_a_continuation_of_executable_rows_is_one_arm",
    ),
    (
        "A3: the trailing stretch is dropped, so the last arm of a plan is lost",
        "coordinator.py",
        "        if first is not None:\n            spans.append((first, len(rows) - 1))",
        "        if False:\n            spans.append((first, len(rows) - 1))",
        f"{PLAN}::test_a_continuation_of_executable_rows_is_one_arm",
    ),
    (
        "A4: serve_load counts as a published run, inflating the refusal denominator",
        "coordinator.py",
        '            if target.get("intent") not in CONTROL_LIVE_DISPATCH_INTENTS:',
        "            if False:",
        f"{PLAN}::test_a_serve_load_gap_inside_one_campaign_costs_two_arms",
    ),
    (
        "A5: a refused run is counted as an arm rather than as refused",
        "coordinator.py",
        "            if not spans:",
        "            if False:",
        f"{PLAN}::test_a_refused_run_reports_its_energy_and_its_advantage",
    ),
    (
        "A6: refused value is priced on raw cash, so ambient energy is credited",
        "coordinator.py",
        "                total -= entry.marginal_cost_eur\n        return total",
        "                total -= entry.cost_eur\n        return total",
        f"{PLAN}::test_the_refused_value_basis_excludes_ambient_energy",
    ),
    (
        "A7: the published arm list is unbounded, so an attribute becomes a log",
        "coordinator.py",
        "                if len(arms) < MAX_ARM_PLAN_ENTRIES_PUBLISHED:",
        "                if True:",
        f"{PLAN}::test_the_published_arm_list_is_bounded",
    ),
    # =====================================================================
    # M -- the two clocks, and attribution
    # =====================================================================
    (
        "M1: activation is timed from our own tick, merging the two clocks",
        "coordinator.py",
        '                open_arm["activation_latency_s"] = round(\n'
        "                    (changed - written).total_seconds(), 1\n"
        "                )",
        '                open_arm["activation_latency_s"] = round(\n'
        "                    (now - written).total_seconds(), 1\n"
        "                )",
        f"{MEAS}::test_the_two_clocks_are_measured_separately",
    ),
    (
        "M2: a register already active at the claim is timed as this arm's activation",
        "coordinator.py",
        "            and active\n            and not self._arm_saw_dispatch\n        ):",
        "            and active\n        ):",
        f"{MEAS}::test_a_dispatch_already_running_at_the_claim_proves_nothing",
    ),
    (
        "M3: a register predating the claim publishes a negative latency",
        "coordinator.py",
        "            elif changed < written:\n"
        '                open_arm["evidence"] = ARM_EVIDENCE_STALE_REGISTER',
        "            elif False:\n"
        '                open_arm["evidence"] = ARM_EVIDENCE_STALE_REGISTER',
        f"{MEAS}::test_a_register_predating_the_claim_is_refused",
    ),
    (
        "M4: observation no longer waits for proven ownership",
        "coordinator.py",
        "            and self._ownership_now(snapshot, now) == OWNERSHIP_OWNED\n        ):",
        "        ):",
        f"{MEAS}::test_observation_waits_for_proven_ownership",
    ),
    (
        "M5: export delivery credits pre-existing production as dispatch delivery",
        "coordinator.py",
        "            caused = max(0.0, max(0.0, flows.grid_export_w) / 1000.0 - surplus)",
        "            caused = max(0.0, flows.grid_export_w) / 1000.0",
        f"{MEAS}::test_export_delivery_ignores_pre_existing_pv_export",
    ),
    (
        "M6: charge delivery credits ambient PV charging as proof the dispatch started",
        "coordinator.py",
        "            caused = max(0.0, charge_kw - surplus)",
        "            caused = charge_kw",
        f"{MEAS}::test_charge_delivery_does_not_credit_ambient_pv_charging",
    ),
    (
        "M7: an incoherent sample is measured anyway",
        "coordinator.py",
        "        if self._coherence not in (None, COHERENCE_OK):",
        "        if False:",
        f"{MEAS}::test_an_incoherent_sample_yields_no_delivery_figure",
    ),
    (
        "M8: delivery fires inside the band the controller will not correct",
        "coordinator.py",
        "        if caused <= DISPATCH_POWER_DEADBAND_KW:\n            return",
        "        if caused < 0.0:\n            return",
        f"{MEAS}::test_delivery_must_clear_the_deadband",
    ),
    (
        "M9: an unattributable surplus is treated as zero rather than refused",
        "coordinator.py",
        "        if surplus is None:\n"
        '            arm["delivery_evidence"] = ARM_EVIDENCE_UNATTRIBUTABLE\n'
        "            return",
        "        if surplus is None:\n            surplus = 0.0",
        f"{MEAS}::test_an_unreadable_production_surplus_refuses_to_attribute",
    ),
    (
        "M10: a superseded claim is not filed, so the calibration set stays empty",
        "coordinator.py",
        '        if open_arm is not None and open_arm.get("claim_id") != claim:\n'
        "            self._close_arm(open_arm, now)",
        '        if open_arm is not None and open_arm.get("claim_id") != claim:\n'
        "            pass",
        f"{MEAS}::test_a_new_claim_files_the_previous_arm",
    ),
    (
        "M11: the measurement ring is unbounded",
        "coordinator.py",
        "        self._arm_measurements: deque[dict[str, Any]] = deque(\n"
        "            maxlen=MAX_ARM_MEASUREMENTS_REPORTED\n        )",
        "        self._arm_measurements: deque[dict[str, Any]] = deque()",
        f"{MEAS}::test_the_measurement_ring_is_bounded",
    ),
]
