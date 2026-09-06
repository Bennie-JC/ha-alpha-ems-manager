"""Break each beta.49 claim on purpose, and prove a named test notices.

beta.49 adds two fields to one event and nothing else. The danger is not that they
break something -- they reach no decision -- but that they say something untrue about
a moment that has already passed. A Trading Log line is written once and then sits in
a user's history, so a start line that quietly re-reads a value which later changed is
worse than no line at all.

The ``P`` mutations attack **which figure a start line carries**: the creation
snapshot substituted for the execution target, an unknown target published as zero,
or the live classification read at fire time instead of the one frozen when execution
actually began.

The ``G`` mutations attack **the guards the new fields ride on**: the exactly-once
rule that keeps a multi-row campaign from logging a start at every row boundary, and
the null-instance rule that keeps an announcement from carrying execution figures.

A survivor means the test is vacuous and **the test gets rewritten**; it never means
the mutation gets weakened.
"""

from __future__ import annotations

START = "tests/test_beta49_start_payload.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # P -- which figure the start line carries
    # =====================================================================
    (
        "P1: the creation snapshot is published as the execution target",
        "coordinator.py",
        '                "frozen_target_kwh": (\n'
        "                    None\n"
        "                    if self._campaign_frozen_target_kwh is None\n"
        "                    else round(self._campaign_frozen_target_kwh, 3)\n"
        "                ),",
        '                "frozen_target_kwh": mark.get("planned_kwh"),',
        f"{START}::test_a_start_carries_the_target_it_actually_froze",
    ),
    (
        "P2: an unknown execution target is published as zero",
        "coordinator.py",
        '                "frozen_target_kwh": (\n'
        "                    None\n"
        "                    if self._campaign_frozen_target_kwh is None\n"
        "                    else round(self._campaign_frozen_target_kwh, 3)\n"
        "                ),",
        '                "frozen_target_kwh": round(\n'
        "                    self._campaign_frozen_target_kwh or 0.0, 3\n"
        "                ),",
        f"{START}::test_the_frozen_target_is_null_and_never_zero_when_unknown",
    ),
    (
        "P3: the classification is read live at fire time, so history rewrites itself",
        "coordinator.py",
        '                "classification_at_start": self._campaign_classification_at_start,',
        '                "classification_at_start": self._campaign_classification(\n'
        "                    self._campaign_id\n"
        '                ).get("classification"),',
        f"{START}::test_a_start_line_cannot_change_its_meaning_afterwards",
    ),
    (
        "P4: the classification is frozen at creation rather than at first execution",
        "coordinator.py",
        "        self._campaign_classification_at_start = self._campaign_classification(\n"
        "            self._campaign_id\n"
        '        ).get("classification")',
        "        self._campaign_classification_at_start = None",
        f"{START}::test_the_start_classification_is_frozen_from_the_live_value",
    ),
    # =====================================================================
    # G -- the guards the new fields ride on
    # =====================================================================
    (
        "G1: started fires per arm, so a multi-row campaign logs a start each row",
        "coordinator.py",
        '        if mark is None or LIFECYCLE_KIND_STARTED in mark["marks"]:\n'
        "            return",
        "        if mark is None:\n            return",
        f"{START}::test_started_still_fires_exactly_once_per_instance",
    ),
    (
        "G2: an announcement carries an execution figure it cannot have",
        "coordinator.py",
        '            "campaign_instance_id": None,\n'
        '            "purpose": mark.get("purpose"),',
        '            "campaign_instance_id": None,\n'
        '            "classification_at_start": self._campaign_classification_at_start,\n'
        '            "purpose": mark.get("purpose"),',
        f"{START}::test_an_announcement_never_carries_the_new_fields",
    ),
    (
        "G3: the start-time classification survives into the next campaign",
        "coordinator.py",
        "        self._campaign_frozen_target_kwh = None\n"
        "        self._campaign_classification_at_start = None\n"
        "        self._campaign_opening_target_kwh = None",
        "        self._campaign_frozen_target_kwh = None\n"
        "        self._campaign_opening_target_kwh = None",
        f"{START}::test_a_closed_campaign_leaves_no_start_classification_behind",
    ),
]
