"""Break each beta.41 invariant on purpose, and prove a named test notices.

A green suite is not evidence. A test that would also pass against the broken
implementation it exists to protect is decoration, and the only way to find out
which kind you have is to break the thing and watch. A surviving mutation means
the test is vacuous and **the test gets rewritten**; it never means the mutation
gets weakened.

**The first draft of this table found a real hole, and it was in the tests.** Of
37 mutations, 24 survived -- and not because the code was over-guarded. Almost every
property of a solved plan is a *self-consistency* property, and self-consistency
survives breaking the state model: replace ``_physical_energy_kwh`` with the
identity and the recursion, the forward walk, the terminal credit and both published
endpoints all move together, back to the beta.40 model exactly, with every "the walk
closes onto the endpoint" assertion still passing because both sides moved.

So two files were written rather than the mutations weakened:

* ``test_beta41_invariants.py`` anchors on the exact meter-side household figure,
  which the carry axis cannot touch. A trajectory reconstructed from it is what the
  pack really does, and the published one has to match it -- measured across 42
  shape-and-state combinations at 1.72 carry steps, against the beta.40 model's
  divergence of the whole of consumption.
* ``test_beta41_units.py`` pins the rounding directions, the materiality thresholds
  and the precedence arithmetic on the functions themselves, where a changed sign
  or a moved boundary has nowhere to hide.

That took the table from 12 kills to 29. The **second** pass did not add tests: each
remaining survivor was applied and every observable quantity measured against the
unmutated baseline, and most of them moved nothing at all. Those are equivalent
mutants, not unguarded defects, and they are retired below with the measurement
beside them -- because a table that reports equivalent mutants as survivors teaches
a reader to ignore it. Two were genuinely mis-anchored and now name the test that
does hold the line.

Five families.

The ``P`` mutations attack the **physical state**, which is the whole of Phase 1.
P7 is a mistake actually made during implementation, kept because a defect that was
real once is the best mutation there is: excluding below-floor states rather than
penalising them sent every survival horizon to
``economic_terminal_unreachable`` -- the beta.31 immobilisation under a new name.

The ``S`` mutations attack the **service contract**. Two quantities are published
because they are two quantities: what the meter really sees, and how far the
modelled state moved. S1 repurposes the meter -- which was tried, and fabricated
export across 230 rows of Stage B -- and S2 and S3 break the residual that
reconciles them, which is the only thing making the pair auditable rather than
merely adjacent.

The ``T`` mutations attack the **terminal window**. T1 hard-codes the 96-interval
day, which is wrong on the two days a year that have 92 or 100. T2 lets the
estimator read past the priced prefix, which is how an unknown price becomes a
cheap one.

The ``C`` mutations attack **coverage**, and every one is an attempt to turn it into
the second economic trader the design forbids. Let the counterfactual export and a
purchase can pay by being sold. Leave the terminal credit standing and a purchase
can pay by being *held*. Compare the two plans on cash alone and the one that simply
bought less wins. Drop a precedence subtraction and the same kilowatt-hour is
published twice, once as compulsory and once as cheap. C9 is the bug this release
actually had.

The ``R`` mutation restores the autonomy figure to the frozen execution claim --
the 6a provenance correction, which moved a value nothing yet reads and was made
before something starts to.

The ``V`` mutations break the **fixtures**, so their own vacuity gates are tested
rather than asserted.

**Mutations removed as equivalent or redundant carry their reason in the table**, at
the position they used to occupy. Three were retired before the table first ran, for
reasons visible by inspection rather than measurement:

* ``landed = 0.0 + _physical_energy_kwh(...)`` -- arithmetically identical.
* ``clock_slot`` as ``index % today_interval_count`` -- identical for every horizon
  shorter than two civil days, which is every horizon this planner builds. Replaced
  by T1, which hard-codes 96 and so genuinely breaks a 92- or 100-interval day.
* ``no_less_safe = True`` -- the guard can never fire. The coverage solve runs on the
  same reserve curve, the same table and the same floor, and forbidding export can
  only remove a way of *losing* energy, so its achievable minimum violation is the
  gated solve's. Retained deliberately as a defensive invariant; it has no
  observable behaviour to mutate.
* ``horizon=relaxed_horizon`` in the coverage solve -- this swaps the reserve, not
  the prices, so it does not test the unknown-price boundary it was written for.
  Coverage inherits ``gated_horizon``, so that boundary is enforced once by
  ``build_horizon`` and ``actionable_intervals``, and T2 is the mutation for it.

The beta.35 through beta.40 tables are siblings and must all still pass: a beta.41
correction that resurrects an earlier defect is a regression, not a fix.

Run with:  python tools/mutation/run.py b41 [-k substring]
"""

from __future__ import annotations

INV = "tests/test_beta41_invariants.py"
UNIT = "tests/test_beta41_units.py"
PHYS = "tests/test_beta41_physical_energy.py"
COVER = "tests/test_beta41_coverage.py"
WINDOW = "tests/test_beta41_terminal_window.py"
LIVE = "tests/test_beta41_live_trace.py"
PROV = "tests/test_beta41_reserve_floor_provenance.py"
NEUTRAL = "tests/test_beta39_neutrality.py"
MIXED = "tests/test_beta39_mixed_buy.py"

WALK = f"{INV}::test_the_published_trajectory_matches_the_meter_side_walk"
FLOORWALK = (
    f"{INV}::test_a_zero_violation_plan_keeps_the_meter_side_walk_above_the_floor"
)
ENDPOINT = f"{INV}::test_the_endpoint_respects_the_floor_it_was_required_to_reach"
NONNEG = f"{INV}::test_the_meter_side_walk_never_goes_below_nothing"
OVERLAP = f"{INV}::test_the_three_shares_never_overlap_on_any_run"
BEYOND = (
    f"{INV}::test_coverage_is_exactly_what_the_executed_plan_buys_beyond_discretion"
)
CONSUME = f"{INV}::test_coverage_never_buys_more_than_the_household_will_consume"
SAVED = f"{INV}::test_a_promoted_plan_always_saved_the_household_money"

# (name, file, old, new, test node id)
#
# ``file`` is resolved inside the package unless it starts with ``tests/``, so a
# mutation may break a *fixture* as well as production.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # =====================================================================
    # P -- the physical state
    # =====================================================================
    (
        "P1: physical energy is the lattice level again (the beta.40 defect)",
        "economic.py",
        "    return energy_kwh - carry * carry_step_kwh\n",
        "    return energy_kwh\n",
        WALK,
    ),
    (
        "P3: household service rounds to nearest, so the inverter over-serves",
        "economic.py",
        "    return int(dc_kwh / carry_step_kwh)\n",
        "    return int(dc_kwh / carry_step_kwh + 0.5)\n",
        f"{UNIT}::test_household_service_rounds_down_and_never_up",
    ),
    (
        "P4: floor the service per interval instead of cumulatively",
        "economic.py",
        "            owed += hold.ambient.dc_kwh\n"
        "        whole = int(owed / carry_step_kwh + 1e-12)\n"
        "        steps.append(whole - taken)\n",
        "            owed = hold.ambient.dc_kwh\n"
        "        whole = int(owed / carry_step_kwh + 1e-12)\n"
        "        steps.append(whole)\n",
        WALK,
    ),
    (
        "P5: derive the served amount from the step count, not the two states",
        "economic.py",
        "                served_dc_kwh = (\n"
        "                    energy_now_kwh\n"
        "                    + move.delta_dc_kwh\n"
        "                    - _physical_energy_kwh(\n"
        "                        table.energy(target_bucket), landed_carry, carry_step_kwh\n"
        "                    )\n"
        "                )\n",
        "                served_dc_kwh = (landed_carry - carry) * carry_step_kwh\n",
        f"{PHYS}::test_the_energy_balance_closes_quarter_by_quarter",
    ),
    (
        "P6: violations are scored on the lattice level again",
        "economic.py",
        "            _physical_energy_kwh(energies[bucket], carry, carry_step_kwh)\n"
        "            for carry in range(carry_states)\n",
        "            energies[bucket]\n            for carry in range(carry_states)\n",
        f"{MIXED}::test_the_safety_buy_quantity_is_untouched_by_the_classification",
    ),
    (
        "P7: exclude below-floor states instead of penalising them (beta.31)",
        "economic.py",
        "            feasible = physical[bucket][carry] >= enforced_floor_kwh - 1e-9\n",
        "            feasible = (\n"
        "                physical[bucket][carry] >= enforced_floor_kwh - 1e-9\n"
        "                and physical[bucket][carry] >= hard_floor_kwh - 1e-9\n"
        "            )\n",
        f"{NEUTRAL}::test_the_seven_shapes_are_unchanged",
    ),
    (
        "P9: the carry axis collapses to one state, so nothing is representable",
        "const.py",
        "AMBIENT_CARRY_STEPS: Final = 8\n",
        "AMBIENT_CARRY_STEPS: Final = 1\n",
        f"{PHYS}::test_the_quantisation_residual_is_bounded_by_one_carry_step",
    ),
    (
        "P12: the two published endpoints diverge again",
        "economic.py",
        "    held_end_kwh = _physical_energy_kwh(table.energy(bucket), carry, carry_step_kwh)\n",
        "    held_end_kwh = table.energy(bucket)\n",
        WALK,
    ),
    (
        "P13: the pack drains at twice the rate the household consumes",
        "economic.py",
        "    return energy_kwh - carry * carry_step_kwh\n",
        "    return energy_kwh - 2.0 * carry * carry_step_kwh\n",
        WALK,
    ),
    (
        "P14: a full carry stops closing onto the next lattice level",
        "economic.py",
        "    return energy_kwh - carry * carry_step_kwh\n",
        "    return energy_kwh - carry * carry_step_kwh * 0.75\n",
        f"{UNIT}::test_a_full_carry_is_exactly_one_bucket_and_no_more",
    ),
    # =====================================================================
    # S -- the published service contract
    # =====================================================================
    (
        "S1: repurpose the meter figure as the quantised state movement",
        "economic.py",
        "                ambient_self_consumption_ac_kwh=(\n"
        "                    0.0 if ambient is None else ambient.discharge_ac_kwh\n"
        "                ),\n",
        "                ambient_self_consumption_ac_kwh=(\n"
        "                    served_dc_kwh * table.limits.discharge_efficiency\n"
        "                ),\n",
        f"{PHYS}::test_the_two_service_quantities_reconcile_through_the_residual",
    ),
    (
        "S2: the residual is unsigned, so it cannot reconcile a suspension",
        "economic.py",
        "                    0.0 if ambient is None else ambient.dc_kwh - served_dc_kwh\n",
        "                    0.0\n"
        "                    if ambient is None\n"
        "                    else abs(ambient.dc_kwh - served_dc_kwh)\n",
        f"{PHYS}::test_the_two_service_quantities_reconcile_through_the_residual",
    ),
    (
        "S3: publish the unrounded service as the state movement",
        "economic.py",
        "                battery_state_service_dc_kwh=served_dc_kwh,\n",
        "                battery_state_service_dc_kwh=(\n"
        "                    0.0 if ambient is None else ambient.dc_kwh\n"
        "                ),\n",
        f"{PHYS}::test_the_walk_ends_where_the_recursion_decided_to_end",
    ),
    # =====================================================================
    # T -- the terminal window
    # =====================================================================
    (
        "T1: the civil day is hard-coded to 96 intervals again",
        "economic.py",
        "        if index < today_interval_count:\n            return index\n"
        "        return index - today_interval_count\n",
        "        return index % 96\n",
        f"{UNIT}::test_the_clock_slot_is_the_civil_slot_on_a_day_of_any_length",
    ),
    (
        "T2: source the proxies from the whole series, priced or not",
        "economic.py",
        "    head = max(0, min(horizon_intervals, len(demands), len(prices)))\n",
        "    head = max(0, min(len(demands), len(prices)))\n",
        f"{UNIT}::test_the_window_reads_no_price_the_horizon_did_not_price",
    ),
    (
        "T2b: and the same hole seen through the replay's own test",
        "economic.py",
        "    head = max(0, min(horizon_intervals, len(demands), len(prices)))\n",
        "    head = max(0, min(len(demands), len(prices)))\n",
        f"{WINDOW}::test_the_replay_reads_only_prices_the_horizon_priced",
    ),
    (
        "T3: an empty horizon guesses a window instead of refusing one",
        "economic.py",
        "    if head <= 0:\n        return PostHorizonWindow()\n",
        "    if head < 0:\n        return PostHorizonWindow()\n",
        f"{WINDOW}::test_an_empty_horizon_is_refused_rather_than_guessed",
    ),
    (
        "T4: the replay ignores the lookahead cap",
        "economic.py",
        "        for offset in range(lookahead):\n",
        "        for offset in range(lookahead * 4):\n",
        f"{WINDOW}::test_the_replay_is_never_wider_than_the_lookahead_allows",
    ),
    (
        "T5: the day-ahead publishing collapses the curve again",
        "economic.py",
        "        basis = TERMINAL_WINDOW_CLOCK_MATCHED\n",
        "        window = ()\n        basis = TERMINAL_WINDOW_CLOCK_MATCHED\n",
        f"{WINDOW}::test_the_window_survives_the_day_ahead_publishing",
    ),
    (
        "T6: a missing interval count raises instead of degrading",
        "economic.py",
        "        if today_interval_count <= 0:\n            return -1\n",
        "        if today_interval_count < 0:\n            return -1\n",
        f"{UNIT}::test_a_zero_interval_count_degrades_to_an_empty_window",
    ),
    # =====================================================================
    # C -- coverage
    # =====================================================================
    (
        "C1: the counterfactual may export, so it becomes arbitrage",
        "economic.py",
        "    coverage_permitted = desired_permitted - {ECONOMIC_ACTION_EXPORT}\n",
        "    coverage_permitted = desired_permitted\n",
        f"{COVER}::test_nothing_coverage_buys_can_be_sold_even_where_selling_pays_more",
    ),
    (
        "C5: a rounding may promote the whole trajectory",
        "economic.py",
        "        buys_more = (\n"
        "            coverage.planned_charge_ac_kwh\n"
        "            > desired.planned_charge_ac_kwh + table.bucket_kwh\n"
        "        )\n",
        "        buys_more = True\n",
        f"{COVER}::test_a_spread_the_gates_would_have_taken_needs_no_coverage",
    ),
    (
        "C7: coverage keeps the user's gates, so the band case never fires",
        "economic.py",
        '    coverage_economics["minimum_trade_gain_eur"] = 0.0\n'
        '    coverage_economics["grid_charge_margin_eur_per_kwh"] = 0.0\n',
        "    pass\n",
        f"{COVER}::test_the_gates_reject_the_trade_and_coverage_still_happens",
    ),
    (
        "C9: safety is differenced against a pair priced a different way",
        "economic.py",
        "    if desired is gated_plan:\n"
        "        safety_attribution = _safety_buy_attribution(desired, relaxed, compelled_ac_kwh)\n",
        "    if True:\n"
        "        safety_attribution = _safety_buy_attribution(desired, relaxed, compelled_ac_kwh)\n",
        f"{COVER}::test_the_gates_reject_the_trade_and_coverage_still_happens",
    ),
    (
        "C10: coverage claims what discretion would have bought anyway",
        "economic.py",
        "        extra = max(0.0, run.battery_charge_ac_kwh - discretionary)\n",
        "        extra = run.battery_charge_ac_kwh\n",
        BEYOND,
    ),
    (
        "C11: a rounding labels a run as coverage",
        "economic.py",
        "        index for index, energy in sorted(coverage.items()) if energy > bucket_kwh\n",
        "        index for index, energy in sorted(coverage.items()) if energy >= 0.0\n",
        f"{UNIT}::test_a_coverage_run_must_clear_a_whole_bucket_to_be_labelled",
    ),
    (
        "C14: the economic share is published gross of coverage",
        "economic.py",
        "    return {\n"
        "        index: (compelled, max(0.0, economic - coverage.get(index, 0.0)))\n"
        "        for index, (compelled, economic) in safety.items()\n"
        "    }\n",
        "    return dict(safety)\n",
        f"{UNIT}::test_the_economic_share_is_published_net_of_coverage",
    ),
    (
        "C15: the compelled quantity is allocated latest-run-first",
        "economic.py",
        "    for run in executed.runs:\n"
        "        if run.action != ECONOMIC_ACTION_CHARGE:\n"
        "            continue\n"
        "        compelled = min(run.battery_charge_ac_kwh, remaining)\n",
        "    for run in reversed(executed.runs):\n"
        "        if run.action != ECONOMIC_ACTION_CHARGE:\n"
        "            continue\n"
        "        compelled = min(run.battery_charge_ac_kwh, remaining)\n",
        f"{UNIT}::test_the_compelled_quantity_is_carried_forward_earliest_first",
    ),
    (
        "C16: a discharge run is given a compelled purchase share",
        "economic.py",
        "        if run.action != ECONOMIC_ACTION_CHARGE:\n"
        "            continue\n"
        "        compelled = min(run.battery_charge_ac_kwh, remaining)\n",
        "        compelled = min(run.battery_charge_ac_kwh, remaining)\n",
        f"{UNIT}::test_a_non_charge_run_is_never_given_a_compelled_share",
    ),
    # =====================================================================
    # R -- the frozen reserve claim (6a)
    # =====================================================================
    (
        "R1: the autonomy figure returns to the frozen execution claim",
        "coordinator.py",
        "            demand.index: value\n"
        "            for demand, value in zip(\n"
        "                outcome.horizon.demands,\n"
        "                outcome.horizon.planning_reserve_kwh,\n"
        "                strict=False,\n"
        "            )\n",
        "            entry.index: entry.required_dc_kwh\n"
        "            for entry in (projection.intervals if projection else ())\n",
        f"{PROV}::test_the_published_claim_carries_the_enforced_curve",
    ),
    # =====================================================================
    # Retired after measurement, with the reason recorded
    # =====================================================================
    #
    # Each of these was applied and the plan's observable quantities measured
    # against the unmutated baseline: the seven neutrality shapes at six starting
    # states, the coverage band, a low-load band, a flat-price band, and the
    # below-floor survival horizon at 0.02 and 0.90 EUR/kWh. Reporting an
    # equivalent mutant as a survivor devalues the ones that matter.
    #
    # **Redundant duplicates** -- the same source edit as a sibling entry that is
    # already killed, aimed at a weaker assertion. The sibling is the guard:
    #
    #   * P2: duplicate of P1 -- the same edit, aimed at a weaker assertion
    #   * P3b: duplicate of P3 -- the rounding direction is a property of the function
    #   * S1b: duplicate of S1 -- the same edit, caught by the reconciliation identity
    #   * C9b: duplicate of C9 -- the same edit, caught by the band fixture
    #
    # **Equivalent on every reachable shape** -- nothing observable moved at all,
    # not one euro and not one kilowatt-hour. In each case the line the mutation
    # attacks is retained deliberately; the note says why it cannot be observed:
    #
    #   * P8: the terminal condition is redundant wherever the enforced reserve curve
    #     already binds at the horizon's last interval, which it does on every shape in
    #     the suite. It is *not* redundant in general -- the eight-interval fixture this
    #     release measured discharged 11.0 to 9.0 against an 11.0 floor without it -- so
    #     the condition stays
    #   * P10: the guard only bites where the forward walk would serve the house below
    #     the hard floor, and no plan in the suite reaches that state. Defensive,
    #     retained
    #   * P11: ``violations`` already dominates the lexicographic pair, so making every
    #     terminal state feasible changes no chosen trajectory. The seed condition is
    #     belt-and-braces on these shapes
    #   * C2: unreachable on the coverage fixtures: they supply no ``TerminalValue``,
    #     so the ``else`` branch builds one with a zero edge credit and this branch
    #     never runs. Reached only on the live replay, where discretion already buys the
    #     useful energy and coverage contributes nothing
    #   * C3: ``solve`` prefers a supplied ``TerminalValue`` over the scalar
    #     ``edge_value_eur_per_kwh``, and the coverage economics always supply one, so
    #     the scalar is dead by then. Set anyway, so the counterfactual cannot be
    #     credited by whichever route a future change opens
    #   * C8: ``extra`` -- what the executed plan buys beyond the discretionary one --
    #     binds before ``remaining`` on every reachable shape, so ``min`` returns the
    #     same value with or without the subtraction. The precedence subtraction stays:
    #     it is what makes the disjointness true by construction rather than by luck
    #   * C12: promotion also requires the coverage plan to buy more than one bucket
    #     beyond discretion, and on every reachable shape buying more implies a positive
    #     saving. The saving condition stays as the statement of intent
    #
    # **C2 and C4 -- measured equivalent on a fixture written specifically to catch
    # them, and the reason is instructive rather than a shrug.**
    #
    # A terminal-value coverage fixture was added for these two: real post-horizon
    # household demand, coverage buying 11.389 kWh on top of a 5.000 kWh
    # discretionary baseline, the pack ending at 10.541 against the band case's
    # 5.534. Neither mutation moved one euro or one kilowatt-hour of it.
    #
    #   * **C2** -- leaving the terminal *spare* segment priced at the export rate.
    #     Unreachable by construction, which is the satisfying answer: the spare
    #     segment begins where terminal inventory exceeds what the post-horizon
    #     household will take, and coverage may never buy past that point. Measured
    #     on the fixture, the plan ends at exactly the segment boundary -- 6.32 kWh
    #     above the floor against a 6.0 kWh demand at 0.9487 efficiency. So the
    #     export price cannot enter the decision unless a *different* rule fails
    #     first. The line stays: it is the guard that keeps that true if one does.
    #
    #   * **C4** -- comparing the two candidate plans on metered cash alone,
    #     ignoring the inventory each ends holding. The inventory term decides only
    #     where the endpoints differ *and* the extra purchase is not already paid
    #     for by import it avoids inside the horizon. On this installation the
    #     inverter serves the house from the pack unbidden, so bought energy is
    #     consumed in-horizon and cash alone already prefers the coverage plan.
    #     Its direction is also fail-safe: cash-only under-promotes and can never
    #     authorise a purchase the household will not consume. Retained because
    #     "a plan that spends less because it bought less is not cheaper" is the
    #     rule the comparison is meant to state, whether or not a fixture can
    #     currently make the two answers disagree.
    #
    # **V3** -- the export-pays tariff was mutated to a non-paying one to prove the
    # C1 fixture matters, and the test passed anyway: with nothing worth exporting
    # the bound holds trivially. The premise is now asserted inside the test rather
    # than guarded from outside it, which is the stronger arrangement.
    # =====================================================================
    # V -- the fixtures themselves
    # =====================================================================
    (
        "V1: the band fixture stops being a band, so the core test is vacuous",
        COVER,
        "CHEAP = 0.25\n",
        "CHEAP = 0.42\n",
        f"{COVER}::test_the_gates_reject_the_trade_and_coverage_still_happens",
    ),
    (
        "V2: the live trace stops matching the installation it replays",
        "tests/beta41_trace.py",
        "STORED_DC_KWH: Final = 9.936\n",
        "STORED_DC_KWH: Final = 2.0\n",
        f"{LIVE}::test_the_pack_would_otherwise_sit_on_the_floor_all_day",
    ),
]

# The runner lives in ``tools/mutation/run.py``: it snapshots every source file by
# content before the first mutation, verifies after each one, holds a lock so two
# tables cannot edit the tree at once, and restores on a signal. This file is the
# table; it does not run itself.
