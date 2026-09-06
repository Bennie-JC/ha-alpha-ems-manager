"""Break each beta.45 claim on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out which
kind you have is to break the thing and watch. A surviving mutation means the test is
vacuous and **the test gets rewritten**; it never means the mutation gets weakened.

**Why a lifecycle release needs a table.** beta.45 fixes a defect that every test in
the suite walked past for three releases: the recovery pass declared a live campaign a
restart corpse, published ``failed / quarter_progress_unknown / 0.0`` over a campaign
that went on charging, and swallowed the real terminal. Nothing was red. The tests
that existed asked whether recovery *worked*, never whether it fired at all -- which is
exactly the kind of gap a mutation table is for.

Three families:

The ``G`` mutations attack the **liveness guard**, which is the fix itself. Every way
of getting an identity test wrong -- dropping it, inverting it, comparing the wrong
field, making it absolute -- leaves a coordinator that still passes a naive "does
recovery produce a terminal" test.

The ``E`` mutations attack the **persisted evidence and the published window**. Both
failed quietly and plausibly: a realised figure frozen at its creation value, and a
window end that was a true statement about the wrong instant.

The ``A`` mutations attack the **announcement**: its overlap predicate, both halves and
its strictness, and the accounting fence around a plan that never became a campaign.
The fence mutations are the ones that matter most, because crossing it would put a
fabricated realised figure into a public result.
"""

from __future__ import annotations

LIFE = "tests/test_beta45_lifecycle.py"
ANN = "tests/test_beta45_announcement.py"

_GUARD = '''        if (
            instance_id is not None
            and self._campaign_instance_id is not None
            and instance_id == self._campaign_instance_id
        ):
            return "live"'''

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # G -- the liveness guard, which is the whole fix
    # =====================================================================
    (
        "G1: the guard is gone, so every live campaign is a restart corpse again",
        "coordinator.py",
        _GUARD,
        '        if False:\n            return "live"',
        f"{LIFE}::test_a_live_campaign_survives_twenty_refreshes_without_a_terminal",
    ),
    (
        "G2: the guard is inverted -- only the live instance is ever recovered",
        "coordinator.py",
        "            and instance_id == self._campaign_instance_id",
        "            and instance_id != self._campaign_instance_id",
        f"{LIFE}::test_a_live_campaign_survives_twenty_refreshes_without_a_terminal",
    ),
    (
        "G3: liveness asked of the campaign id, which two attempts can share",
        "coordinator.py",
        "            and instance_id == self._campaign_instance_id",
        '            and mark.get("campaign_id") == self._campaign_id',
        f"{LIFE}::test_a_second_attempt_does_not_shield_the_first_attempts_orphan",
    ),
    (
        "G4: the guard is absolute, so a genuine restart never closes its campaign",
        "coordinator.py",
        _GUARD,
        '        if True:\n            return "live"',
        f"{LIFE}::test_a_genuine_restart_mark_is_still_recovered",
    ),
    # =====================================================================
    # E -- the persisted evidence and the published window
    # =====================================================================
    (
        "E1: the observed end wins, so window_end is the row in flight again",
        "coordinator.py",
        "return self._campaign_planned_end_utc or self._campaign_end_utc",
        "return self._campaign_end_utc or self._campaign_planned_end_utc",
        f"{LIFE}::test_the_public_window_end_is_the_planned_end",
    ),
    (
        "E2: the planned end is no longer published beside the observed one",
        "coordinator.py",
        "            # planned gets its own key rather than borrowing that name.\n"
        '            "planned_end": (',
        "            # planned gets its own key rather than borrowing that name.\n"
        '            "planned_end_withheld": (',
        f"{LIFE}::test_the_open_campaign_block_publishes_both_ends",
    ),
    (
        "E3: the mark's realised figure is frozen at its creation value",
        "coordinator.py",
        '        mark["realized_kwh"] = round(self._campaign_realized_now(), 3)\n'
        '        mark["measurable"] = self._campaign_measurable\n'
        "        frozen = self._campaign_frozen_target_kwh",
        '        mark["realized_kwh"] = 0.0\n'
        '        mark["measurable"] = self._campaign_measurable\n'
        "        frozen = self._campaign_frozen_target_kwh",
        f"{LIFE}::test_refreshing_the_evidence_writes_the_live_figures",
    ),
    (
        "E4: a tolerance is invented for a campaign that froze no target",
        "coordinator.py",
        '        if frozen is not None:\n            mark["success_tolerance_kwh"] = round(',
        '        if True:\n            mark["success_tolerance_kwh"] = round(',
        f"{LIFE}::test_an_unfrozen_target_writes_no_tolerance",
    ),
    (
        "E5: this boot's figures are written into somebody else's mark",
        "coordinator.py",
        '        if mark.get("instance_id") != self._campaign_instance_id:',
        "        if False:",
        f"{LIFE}::test_another_instances_mark_is_never_written",
    ),
    (
        "E6: accrual no longer refreshes the evidence, so only a stop does",
        "coordinator.py",
        "            self._campaign_measurable = False\n"
        "        self._refresh_lifecycle_evidence()",
        "            self._campaign_measurable = False",
        f"{LIFE}::test_accrual_refreshes_the_evidence",
    ),
    # =====================================================================
    # A -- the announcement: overlap, and the accounting fence
    # =====================================================================
    (
        "A1: the first half of the overlap test is dropped",
        "coordinator.py",
        '            and published["window_start"] < announced["window_end"]',
        "            and True",
        f"{ANN}::test_a_campaign_entirely_after_the_announcement_is_not_the_same",
    ),
    (
        "A2: the second half is dropped -- the one-sided form the executor uses",
        "coordinator.py",
        '            and announced["window_start"] < published["window_end"]',
        "            and True",
        f"{ANN}::test_a_campaign_entirely_before_the_announcement_is_not_the_same",
    ),
    (
        "A3: overlap becomes inclusive, so abutting windows merge",
        "coordinator.py",
        '            and published["window_start"] < announced["window_end"]\n'
        '            and announced["window_start"] < published["window_end"]',
        '            and published["window_start"] <= announced["window_end"]\n'
        '            and announced["window_start"] <= published["window_end"]',
        f"{ANN}::test_abutting_windows_do_not_continue_one_another",
    ),
    (
        "A4: purpose is no longer part of continuity",
        "coordinator.py",
        '            published["purpose"] == announced["purpose"]',
        "            True",
        f"{ANN}::test_the_same_window_with_a_different_purpose_is_not_the_same",
    ),
    (
        "A5: continuity keyed on campaign_id, which churns as the tail moves",
        "coordinator.py",
        "                if self._announcement_continues(entry, window):",
        '                if entry["campaign_id"] == announced.get("campaign_id"):',
        f"{ANN}::test_a_moving_tail_does_not_announce_a_second_time",
    ),
    (
        "A6: the lead-time bound is gone, so tonight is announced at breakfast",
        "coordinator.py",
        '            if entry["window_start"] > now + lead:\n                continue',
        "            if False:\n                continue",
        f"{ANN}::test_a_campaign_beyond_the_lead_is_not_announced_yet",
    ),
    (
        "A7: a gap-only campaign is announced, promising energy nobody commands",
        "coordinator.py",
        "            if intent not in CONTROL_LIVE_DISPATCH_INTENTS:\n                continue",
        "            if False:\n                continue",
        f"{ANN}::test_a_campaign_of_gaps_alone_announces_nothing",
    ),
    (
        "A8: a planned-only closure is published as a campaign terminal",
        "coordinator.py",
        "self._fire_lifecycle(LIFECYCLE_KIND_PLAN_CLOSED, payload)",
        "self._fire_lifecycle(LIFECYCLE_KIND_REMOVED, payload)",
        f"{ANN}::test_a_planned_only_closure_never_touches_a_campaign_terminal",
    ),
    (
        "A9: the announcement fabricates a realised figure of zero",
        "coordinator.py",
        '            "campaign_instance_id": None,',
        '            "campaign_instance_id": None,\n            "realised_kwh": 0.0,',
        f"{ANN}::test_a_planned_only_closure_fabricates_no_instance_and_no_energy",
    ),
    (
        "A10: the announcement fabricates an instance id from the campaign id",
        "coordinator.py",
        '            "campaign_instance_id": None,',
        '            "campaign_instance_id": mark.get("campaign_id"),',
        f"{ANN}::test_a_planned_only_closure_fabricates_no_instance_and_no_energy",
    ),
    (
        "A11: a plan that never ran overwrites the last campaign result",
        "coordinator.py",
        "        self.store.campaign_announcement = None\n"
        "        self.store.schedule_save()\n"
        "        self._fire_lifecycle(LIFECYCLE_KIND_PLAN_CLOSED, payload)",
        "        self.store.campaign_announcement = None\n"
        "        self.store.schedule_save()\n"
        "        self._last_campaign_result = payload\n"
        "        self._fire_lifecycle(LIFECYCLE_KIND_PLAN_CLOSED, payload)",
        f"{ANN}::test_a_planned_only_closure_never_touches_a_campaign_terminal",
    ),
    (
        "A12: one silent refresh withdraws a plan whose window is still open",
        "coordinator.py",
        "            else:\n"
        "                # Nothing published this refresh and the window is still open.\n"
        "                # Silence is not withdrawal.\n"
        "                return",
        "            else:\n"
        "                self._fire_plan_closed(announced, now, OUTCOME_NOT_EXECUTED)\n"
        "                return",
        f"{ANN}::test_silence_inside_the_window_is_not_withdrawal",
    ),
    (
        "A13: a running campaign is announced again beside itself",
        "coordinator.py",
        "            and not (\n"
        "                open_window is not None\n"
        "                and self._announcement_continues(entry, open_window)\n"
        "            )",
        "            and not (\n"
        "                False\n"
        "                and self._announcement_continues(entry, open_window)\n"
        "            )",
        f"{ANN}::test_an_open_campaign_is_never_re_announced",
    ),
]
