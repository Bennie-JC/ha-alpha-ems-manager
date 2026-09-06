"""Break each beta.47 claim on purpose, and prove a named test notices.

beta.47 adds no dispatch behaviour at all. It looks sooner, and it says what the
forty seconds before activation were made of. So every mutation here attacks a
*measurement*, and the danger is uniform: a broken measurement still publishes a
plausible-looking number, and a later release would price an arm against it.

Three families:

The ``W`` mutations attack the **three-term decomposition** -- which instant each
term is anchored to, whether the write path is measured or assumed, and whether an
underivable figure may become zero. The sharpest is W1: reconcile with two terms and
the residual silently absorbs the one stage nobody has ever timed.

The ``S`` mutations attack the **bounded sweep**: its bound, its cancellation, and
the rule that it belongs to exactly one arm. An unbounded sweep is a second cadence
wearing a different name.

The ``E`` mutations attack the **evidence rules the new observer must not relax**.
Observing more often may not lower a bar: a register that predates the claim is still
stale, a dispatch already running is still no transition, ambient production is still
not delivery, and delivery is still timed from the claim rather than from the write.
E7 and E8 defend the two ordering invariants the whole diagnosis rests on.

A survivor means the test is vacuous and **the test gets rewritten**; it never means
the mutation gets weakened.
"""

from __future__ import annotations

OBS = "tests/test_beta47_observation.py"
HYP = "tests/test_beta47_hypothesis.py"
STAGE_B = "tests/test_stage_b_boundaries.py"
B44 = "tests/test_beta44_arm_measurement.py"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # W -- the three-term decomposition
    # =====================================================================
    (
        "W1: the write duration is dropped, so the residual hides our own write path",
        "coordinator.py",
        '        arm["dispatch_write_duration_s"] = round((enabled - started).total_seconds(), 3)',
        '        arm["dispatch_write_duration_s"] = 0.0',
        f"{OBS}::test_activation_latency_decomposes_into_exactly_three_measured_terms",
    ),
    (
        "W2: the write duration is taken as an absolute value, hiding a reversed order",
        "coordinator.py",
        '        arm["dispatch_write_duration_s"] = round((enabled - started).total_seconds(), 3)',
        '        arm["dispatch_write_duration_s"] = round(\n'
        "            abs((enabled - started).total_seconds()), 3\n"
        "        )",
        f"{OBS}::test_the_write_duration_is_signed_so_a_reversed_ordering_shows",
    ),
    (
        "W3: the solve term is anchored on the enable rather than the write start",
        "coordinator.py",
        '            arm["claim_to_write_latency_s"] = round(\n'
        "                (started - written).total_seconds(), 3\n"
        "            )",
        '            arm["claim_to_write_latency_s"] = round(\n'
        "                (enabled - written).total_seconds(), 3\n"
        "            )",
        f"{OBS}::test_activation_latency_decomposes_into_exactly_three_measured_terms",
    ),
    (
        "W4: the register term is measured from the claim, charging our solve to them",
        "coordinator.py",
        '        arm["enable_to_register_latency_s"] = round(\n'
        "            (changed - enabled).total_seconds(), 3\n"
        "        )",
        '        arm["enable_to_register_latency_s"] = round(\n'
        '            (changed - instant_of(arm["claim_written_at"])).total_seconds(), 3\n'
        "        )",
        f"{OBS}::test_the_register_latency_is_the_external_share_and_only_that",
    ),
    (
        "W5: an unmatched write is published as zero rather than withheld",
        "coordinator.py",
        '        if not isinstance(timing, dict) or timing.get("claim_id") != arm.get(\n'
        '            "claim_id"\n'
        "        ):\n"
        "            return",
        '        if not isinstance(timing, dict) or timing.get("claim_id") != arm.get(\n'
        '            "claim_id"\n'
        "        ):\n"
        '            arm["claim_to_write_latency_s"] = 0.0\n'
        '            arm["dispatch_write_duration_s"] = 0.0\n'
        "            return",
        f"{OBS}::test_an_unmatched_write_leaves_every_new_figure_null",
    ),
    (
        "W6: any timing is attached, so a later arm inherits an earlier one",
        "coordinator.py",
        '        if not isinstance(timing, dict) or timing.get("claim_id") != arm.get(\n'
        '            "claim_id"\n'
        "        ):\n"
        "            return",
        "        if not isinstance(timing, dict):\n            return",
        f"{OBS}::test_a_second_arm_never_inherits_the_first_arms_timing",
    ),
    (
        "W7: any sequence counts as an activation, so a sustain is timed as an arm",
        "coordinator.py",
        "        activated = bool(sent) and sent[-1].entity_id == DISPATCH_ENABLE",
        "        activated = True",
        f"{OBS}::test_only_a_sequence_ending_in_the_activation_is_timed",
    ),
    (
        "W8: the activation is looked for anywhere in the sequence, not strictly last",
        "coordinator.py",
        "        activated = bool(sent) and sent[-1].entity_id == DISPATCH_ENABLE",
        "        activated = any(step.entity_id == DISPATCH_ENABLE for step in sent)",
        f"{OBS}::test_only_a_sequence_ending_in_the_activation_is_timed",
    ),
    # =====================================================================
    # S -- the bounded sweep
    # =====================================================================
    (
        "S1: the sweep has no bound, so it becomes a second cadence",
        "coordinator.py",
        "        self._arm_observe_left -= 1\n"
        "        if self._arm_observe_left <= 0:\n"
        "            return",
        "        self._arm_observe_left -= 1\n        if False:\n            return",
        f"{OBS}::test_the_sweep_exhausts_its_bound_and_then_stops",
    ),
    (
        "S1b: a spent budget still runs, so a stray handle drives it past the ceiling",
        "coordinator.py",
        "        if self._arm_observe_left <= 0:\n"
        "            self._arm_observe_left = 0\n"
        "            return\n"
        "        arm = self._arm_open",
        "        arm = self._arm_open",
        f"{OBS}::test_the_sweep_exhausts_its_bound_and_then_stops",
    ),
    (
        "S2: the sweep keeps running after delivery has been attributed",
        "coordinator.py",
        '        if self._arm_open.get("delivery_latency_s") is not None:\n'
        "            self._arm_observe_left = 0\n"
        "            return",
        '        if self._arm_open.get("delivery_latency_s") is None and False:\n'
        "            self._arm_observe_left = 0\n"
        "            return",
        f"{OBS}::test_the_sweep_is_bounded_and_stops_on_delivery",
    ),
    (
        "S3: a pass belonging to a finished arm keeps sweeping",
        "coordinator.py",
        '        if arm is None or arm.get("claim_id") != claim:\n'
        "            self._arm_observe_left = 0\n"
        "            return",
        "        if arm is None:\n"
        "            self._arm_observe_left = 0\n"
        "            return",
        f"{OBS}::test_the_sweep_stops_when_the_claim_changes",
    ),
    (
        "S4: a closing arm leaves its sweep running against the next one",
        "coordinator.py",
        "        # The bounded sweep belongs to one arm and dies with it. beta.47.\n"
        "        self._cancel_arm_observation()\n"
        '        arm["closed_at"] = now.isoformat()',
        '        arm["closed_at"] = now.isoformat()',
        f"{OBS}::test_a_closing_arm_cancels_its_own_sweep",
    ),
    (
        "S5: a new arm does not cancel the previous handle, so two sweeps run",
        "coordinator.py",
        "        self._cancel_arm_observation()\n"
        "        self._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES",
        "        self._arm_observe_left = POST_ARM_OBSERVE_MAX_PASSES",
        f"{OBS}::test_scheduling_a_new_arm_cancels_the_previous_sweep",
    ),
    # =====================================================================
    # E -- evidence rules the new observer must not relax
    # =====================================================================
    (
        "E1: delivery is timed from the write, so looking sooner flatters the figure",
        "coordinator.py",
        '        arm["delivery_latency_s"] = round((now - written).total_seconds(), 1)',
        '        arm["delivery_latency_s"] = round(\n'
        "            (\n"
        "                now\n"
        '                - (instant_of(arm.get("dispatch_enable_written_at")) or written)\n'
        "            ).total_seconds(),\n"
        "            1,\n"
        "        )",
        f"{OBS}::test_delivery_is_still_timed_from_the_claim_not_from_the_write",
    ),
    (
        "E2: a register that predates our activation write is accepted",
        "coordinator.py",
        "        if changed is None or changed < enabled:\n            return",
        "        if changed is None:\n            return",
        f"{OBS}::test_a_register_that_predates_the_enable_is_refused",
    ),
    (
        "E3: the register term is published without the activation transition",
        "coordinator.py",
        '        if arm.get("activation_latency_s") is None:\n            return',
        "        if False:\n            return",
        f"{OBS}::test_a_dispatch_already_running_at_the_claim_is_still_refused",
    ),
    (
        "E4: the coherence gate is dropped now that we observe more often",
        "coordinator.py",
        "        if self._coherence is not None and not self._coherence.usable:",
        "        if False:",
        f"{OBS}::test_incoherent_sources_still_refuse_to_attribute_delivery",
    ),
    (
        "E5: ambient export counts as the dispatch starting",
        "coordinator.py",
        "            caused = max(0.0, max(0.0, flows.grid_export_w) / 1000.0 - surplus)",
        "            caused = max(0.0, max(0.0, flows.grid_export_w) / 1000.0)",
        f"{OBS}::test_ambient_export_is_still_never_credited_to_the_arm",
    ),
    (
        "E6: the register subscription observes with no arm open",
        "coordinator.py",
        "        if self._arm_open is None:\n"
        "            return\n"
        "        self._observe_arm(read_snapshot(self.hass), dt_util.now())",
        "        self._observe_arm(read_snapshot(self.hass), dt_util.now())",
        f"{OBS}::test_the_register_subscription_does_nothing_with_no_arm_open",
    ),
    (
        "E7: the tick may act on an inactive dispatch, becoming a second way to arm",
        "coordinator.py",
        "        if not snapshot.dispatch_active:\n"
        "            self._note_tick(now, TICK_SKIPPED_DISPATCH_INACTIVE)\n"
        "            return",
        "        if False:\n"
        "            self._note_tick(now, TICK_SKIPPED_DISPATCH_INACTIVE)\n"
        "            return",
        f"{HYP}::test_the_physical_tick_cannot_arm_an_inactive_dispatch",
    ),
    (
        "E8: Stage B is built before its targets, so it acts on a stale publication",
        "coordinator.py",
        "        self.execution_targets = self._execution_targets(",
        "        _unused_targets = self._execution_targets(",
        f"{STAGE_B}::test_the_targets_are_built_before_the_control_report",
    ),
    # =====================================================================
    # F -- coverage the plan named that the families above do not reach
    # =====================================================================
    (
        "F1: the beta.44 stale-register refusal is dropped, so a pre-claim register "
        "is timed",
        "coordinator.py",
        "            elif changed < written:",
        "            elif False:",
        f"{OBS}::test_a_register_that_predates_the_claim_is_still_stale",
    ),
    (
        "F2: observation_latency_s is re-anchored on the write, merging two clocks "
        "beta.44 separated",
        "coordinator.py",
        '            open_arm["observation_latency_s"] = round(\n'
        "                (now - written).total_seconds(), 1\n"
        "            )",
        '            open_arm["observation_latency_s"] = round(\n'
        "                (\n"
        "                    now\n"
        "                    - (\n"
        '                        instant_of(open_arm.get("dispatch_enable_written_at"))\n'
        "                        or written\n"
        "                    )\n"
        "                ).total_seconds(),\n"
        "                1,\n"
        "            )",
        f"{OBS}::test_observation_latency_is_still_timed_from_the_claim",
    ),
    (
        "F4: the register is never subscribed, so the release silently does nothing",
        "coordinator.py",
        "        self.entry.async_on_unload(\n"
        "            async_track_state_change_event(\n"
        "                self.hass, [SENSOR_DISPATCH_START], self._handle_dispatch_register\n"
        "            )\n"
        "        )",
        "        pass",
        f"{HYP}::test_the_dispatch_register_is_actually_subscribed_and_stays_read_only",
    ),
    (
        "F5: the subscription watches the wrong entity, so no arm is ever woken",
        "coordinator.py",
        "                self.hass, [SENSOR_DISPATCH_START], self._handle_dispatch_register",
        "                self.hass, [], self._handle_dispatch_register",
        f"{HYP}::test_the_dispatch_register_is_actually_subscribed_and_stays_read_only",
    ),
    (
        "F3: a beta.47 metric leaks into a module outside the coordinator",
        "dispatch.py",
        "def deadman_minutes(previous: float | None) -> int:",
        "claim_to_write_latency_s = None\n\n\n"
        "def deadman_minutes(previous: float | None) -> int:",
        f"{OBS}::test_no_decision_path_reads_a_beta47_metric",
    ),
]
