"""Break each beta.48 claim on purpose, and prove a named test notices.

beta.48 repairs nothing. It measures two things that were never measured -- how much
of a row was actually integrated, and how the accounted export compares against a
counter the meter keeps itself -- and the whole value of it is that the numbers are
honest even when they are unflattering.

So the mutations here attack **honesty**, not arithmetic. Every one of them produces
an audit that looks finished and is not:

The ``M`` mutations attack the **measured window**: whether the gap the accrual reset
creates is measured or assumed away. M1 is the sharpest -- report the nominal window
as the measured one and the defect this release exists to quantify vanishes.

The ``C`` mutations attack the **counter discipline**: null becoming zero, a
backwards counter being wrapped instead of rejected, and -- worst -- a reading taken
at the wrong end of the interval, which compares a counter against itself and reports
a zero delta for real exported energy. That one was a live bug during implementation.

The ``V`` mutations attack the **verdict**: an ``exact`` that was never reconciled
against anything physical is the single most damaging thing this release could
publish, because Path A and Path B agreeing proves only that our own arithmetic is
self-consistent -- both integrate the same sensor.

A survivor means the test is vacuous and **the test gets rewritten**; it never means
the mutation gets weakened.
"""

from __future__ import annotations

RESET = "tests/test_beta48_reset_arithmetic.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # M -- the measured window
    # =====================================================================
    (
        "M1: the nominal window is reported as the measured one, hiding the gap",
        "coordinator.py",
        '            "measured_seconds": round(measured, 1),\n'
        '            "unmeasured_seconds": round(max(0.0, nominal - measured), 1),',
        '            "measured_seconds": round(nominal, 1),\n'
        '            "unmeasured_seconds": 0.0,',
        f"{RESET}::test_the_audit_publishes_the_gap_the_reset_created",
    ),
    (
        "M2: the gap is hardcoded to zero, so the reset looks free",
        "coordinator.py",
        '            "unmeasured_seconds": round(max(0.0, nominal - measured), 1),',
        '            "unmeasured_seconds": 0.0,',
        f"{RESET}::test_the_audit_publishes_the_gap_the_reset_created",
    ),
    (
        "M3: the window starts at the accrual rather than at the interval it covers",
        "coordinator.py",
        "            self._quarter_sampled_from = previous",
        "            self._quarter_sampled_from = now",
        f"{RESET}::test_the_audit_publishes_the_gap_the_reset_created",
    ),
    (
        "M4: an unmeasured row reports a zero window instead of refusing",
        "coordinator.py",
        '                "measured_seconds": None,\n'
        '                "unmeasured_seconds": None,',
        '                "measured_seconds": 0.0,\n'
        '                "unmeasured_seconds": 0.0,',
        f"{RESET}::"
        "test_a_row_that_was_never_measured_refuses_a_window_rather_than_zeroing_it",
    ),
    # =====================================================================
    # C -- counter discipline
    # =====================================================================
    (
        "C1: the counter is read at the end of the interval, comparing it to itself",
        "coordinator.py",
        "            self._quarter_counter_from = prior_counter",
        "            self._quarter_counter_from = self._quarter_counter_at_cursor",
        f"{RESET}::test_a_row_reconciles_exactly_when_the_counter_agrees",
    ),
    (
        "C2: a backwards counter is wrapped rather than rejected",
        "coordinator.py",
        "        elif ended < started:",
        "        elif False:",
        f"{RESET}::test_a_decreasing_counter_is_rejected_and_never_wrapped",
    ),
    (
        "C3: an unreadable counter is published as a zero delta",
        "coordinator.py",
        "        elif started is None or ended is None:\n"
        "            status = METER_COUNTER_UNAVAILABLE",
        "        elif started is None or ended is None:\n"
        "            status = METER_COUNTER_UNAVAILABLE\n"
        "            physical = 0.0\n"
        "            unexplained = 0.0",
        f"{RESET}::test_an_unreadable_counter_is_uncertain_not_zero",
    ),
    (
        "C4: no counter configured still reports a physical figure",
        "coordinator.py",
        "        if not self.config.grid_export_energy_entity:\n"
        "            status = METER_COUNTER_NOT_CONFIGURED",
        "        if not self.config.grid_export_energy_entity:\n"
        "            status = METER_COUNTER_NOT_CONFIGURED\n"
        "            physical = attributed\n"
        "            unexplained = 0.0",
        f"{RESET}::test_without_a_counter_the_verdict_can_never_be_exact",
    ),
    (
        "C5: the counter is not read on discarded ticks, so the cursor lags a minute",
        "coordinator.py",
        "        prior_counter = self._quarter_counter_at_cursor\n"
        "        self._quarter_counter_at_cursor = self.read_grid_export_counter_kwh()\n"
        "        if previous is None:\n"
        "            return",
        "        prior_counter = self._quarter_counter_at_cursor\n"
        "        if previous is None:\n"
        "            return\n"
        "        self._quarter_counter_at_cursor = self.read_grid_export_counter_kwh()",
        f"{RESET}::test_a_row_reconciles_exactly_when_the_counter_agrees",
    ),
    # =====================================================================
    # V -- the verdict
    # =====================================================================
    (
        "V1: uncertain is reported as exact, so an unreconciled audit reads clean",
        "coordinator.py",
        "        if unexplained is None:\n            verdict = METER_AUDIT_UNCERTAIN",
        "        if unexplained is None:\n            verdict = METER_AUDIT_EXACT",
        f"{RESET}::test_without_a_counter_the_verdict_can_never_be_exact",
    ),
    (
        "V2: any residual reads exact, so a real discrepancy is swallowed",
        "coordinator.py",
        "        elif abs(unexplained) <= METER_AUDIT_TOLERANCE_KWH:",
        "        elif True:",
        f"{RESET}::test_a_material_discrepancy_is_never_reported_as_exact",
    ),
    (
        "V3: a charge row is audited as if import were a metered channel",
        "coordinator.py",
        "        if quarter is None or quarter.intent != EXECUTION_INTENT_NET_EXPORT:\n"
        "            return",
        "        if quarter is None:\n            return",
        f"{RESET}::test_a_charge_row_files_no_export_reconciliation",
    ),
    (
        "V4: an audit figure leaks into a module outside the coordinator",
        "dispatch.py",
        "def deadman_minutes(previous: float | None) -> int:",
        "unexplained_kwh = None\n\n\n"
        "def deadman_minutes(previous: float | None) -> int:",
        f"{RESET}::test_no_decision_path_reads_a_beta48_audit_figure",
    ),
    (
        "V5: the attributed figure is rounded before the comparison, not after",
        "coordinator.py",
        "        attributed = round(self._quarter_grid_export_kwh, 4)",
        "        attributed = round(self._quarter_grid_export_kwh, 1)",
        f"{RESET}::test_a_row_reconciles_exactly_when_the_counter_agrees",
    ),
]
