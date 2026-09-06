# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.0-beta.48] - 2026-09-07

**An audit release. It measures the accounting; it does not correct it.** No planner
change, no economics, no Stage B, no reserve, no ownership, no dispatch timing. The
accumulator this release indicts is deliberately left exactly as it was.

### The dropped-interval defect, now confirmed

Code inspection said `_accrue_quarter_progress` discards the whole sample interval in
which the `(claim_id, quarter_start)` key changes: the reset nulls the cursor and the
next line returns on `previous is None`. The cursor is not among the eight fields
`_capture_quarter_progress` preserves, and `_async_end_row` resets on every handover.

That was a **mechanism**, not a magnitude. Five deterministic tests -- fixed
timestamps, constant 8.0 kW export -- now fix the magnitude, and the numbers correct
the naive reading:

- **A new claim mid-row loses exactly 45 s / 0.100 kWh**, not a full tick. The
  discarded interval carried export only for its last 45 seconds, because the vendor
  register does not go active until after the solve.
- **A row boundary under one claim loses exactly 60 s / 0.1333 kWh**, split as 30 s
  missing from the closing row and 30 s from the opening one. Asserted on both rows,
  since a single total would hide which was short.
- **Two changes on one tick cost one interval, not two.** The reset condition is a
  single `or`.
- **Two changes on different ticks are additive** -- 0.2667 kWh -- and that is the
  shape of every real arm, because the row advances at the boundary while the claim
  cannot land until the refresh has finished its solve.

So it is now a **confirmed accounting defect**, one-sided: it can only under-count.
Whether to repair it is a separate decision, because changing realised energy changes
campaign outcomes and every downstream euro figure.

### Added

- **`sampled_from`, `sampled_to`, `measured_seconds`, `unmeasured_seconds`** on every
  completed row. The nominal window and the measured one are no longer conflated, and
  the gap between them is the defect above, published as a number. It is a duration,
  never converted to an energy: that would need a rate for an interval nobody sampled.

- **An optional cumulative grid export counter** (`grid_export_energy_entity`) and a
  `meter_reconciliation` diagnostics block. This is the point of the release. Every
  other kWh the integration reports is a numerical integral of the one instantaneous
  grid power sensor, so a scaling error, a wrong sign convention or a publication
  stall corrupts all of them identically and **none of them can detect it**. A counter
  the meter maintains itself can.

  Each audited export row states four things and refuses to blur them:
  `physical_export_kwh` (the counter delta -- the only physically independent figure),
  `attributed_export_kwh` (what the accounting accrued over **exactly** the same
  measured window, so the two compare like with like), `unmeasured_seconds`, and
  `unexplained_kwh`.

  **Ambient production is subtracted from neither side.** The realised export figure
  deliberately includes it -- using the marginal figure as the objective *"would
  under-export by exactly the production the site was exporting anyway"* -- so
  subtracting it here would manufacture a discrepancy out of correct behaviour.

  Following the `daily_house_load_entity` precedent exactly: optional, validated by
  unit rather than by a `device_class` selector filter (which is frontend-only and
  would hide entities the integration accepts), no auto-discovery, and **no
  `CONFIG_ENTRY_VERSION` bump** -- an entry saved by an older release loads unchanged.

### Refusals, which are the substance

`not_configured`, `unavailable` and `reset_detected` are all statuses, and none of
them is ever a zero delta. A counter that goes backwards is **rejected, never
wrapped**: one reading cannot distinguish a reset from a rollover. And without a
configured counter the verdict can never be `exact` -- Path A and Path B agreeing
proves only that our own arithmetic is self-consistent, since both integrate the same
sensor. `uncertain` is a pass; a fabricated `exact` would be the one result worth
nothing.

### Validation

- **14 tests**, five of them the deterministic A/B/C proof above
- **193 + 108 passed** across quarter execution, diagnostics, config flow,
  translations, migration, campaign accounting and the beta.44/46/47 arm suites
- **79 passed** on neutrality; planner digests and the beta.40/41 anchors **unchanged
  and not re-baselined**
- **Mutations: b48 14/14 -- 0 survived, 0 anchors lost.** Two survived the first pass
  and both were vacuous tests: nothing inspected a row that was never measured, and
  nothing ever reached the tolerance branch with a real discrepancy. Both gaps were
  closed with tests rather than by weakening a mutation. One of the mutations, C1,
  reproduces a bug found during implementation: reading the counter at the end of the
  interval compares it against itself and reports a zero delta for real energy.
- Lint and format clean; one sharded full suite green

### Live validation pending

The audit is **not yet hardware-validated**, and the reference installation has not
configured a counter entity -- which is expected, and works honestly as
`counter_status: not_configured`. The next live export day should provide, over at
least two export arms:

1. `unmeasured_seconds` non-zero on every row, and consistent with the A/B/C figures.
2. With a counter configured: `physical_export_kwh` present, and `unexplained_kwh`
   either within tolerance or explained by a named flow.
3. A control interval with no Alpha EMS export, proving the audit attributes nothing
   it should not.

The observed ~0.63 kWh gap of 2026-09-06 is **still not called a bug**: its window
endpoints were never aligned, and misalignment alone is arithmetically sufficient to
explain it. The instrument now exists to settle it properly.

## [1.0.0-beta.47] - 2026-09-06

**An observability release. No dispatch is made faster.** The battery starts exactly
when it did before: no planner change, no economics, no Stage B authorisation, no
reserve, no ownership or dead-man semantics, no quarter trigger second, and no
boundary-aligned arming. What changes is *when the controller looks*, and what it can
say about what it saw.

The 2026-09-06 evening export session took ~40 s from ownership claim to the vendor
register reading active, and ~80 s to controller-observed delivery. Two separate
causes, and neither was the vendor.

### Fixed

- **The controller was not watching.** `_observe_arm` ran only inside the
  sixty-second physical tick, whose phase is whatever instant setup happened to
  finish. So the delay between the register going active and this component noticing
  was uniform on `[0, 60)`: the three export arms measured **37.1 s, 42.5 s and
  45.2 s** of pure waiting. That delay was charged to `delivery_latency_s`, which is
  prorated into `objective_forgone_to_activation_kwh` -- so an arm that began
  delivering promptly could report having forgone roughly twice what it really did.
  The dispatch register is now subscribed directly, and a **bounded** post-arm sweep
  (10 s, at most 12 passes) covers attributable delivery, which depends on battery
  and meter readings a register event says nothing about. This is a correction to a
  published economic figure, not only to a diagnostic.

### Added

- **Activation latency is decomposed, and every term is measured.** The capture
  showed `solve_ms` of 32.4-35.2 s against `activation_latency_s` of 38.6-41.5 s,
  which says **84-86 % of it is our own Stage A solve** rather than the vendor -- the
  opposite of what the code had assumed since beta.44. Saying so is not measuring it,
  so the interval is now published in parts:

  ```
  activation_latency_s ~= claim_to_write_latency_s
                        + dispatch_write_duration_s
                        + enable_to_register_latency_s
  ```

  The middle term is measured rather than assumed. An identity with an estimated term
  is not a reconciliation, and a residual that silently absorbed the write path would
  hide the one stage nobody has ever timed.

- **`enable_to_register_latency_s` is the first figure this component has published
  about anything outside itself**, and it is named for exactly what it measures: our
  activation write against *the AlphaESS integration's register entity* changing.
  That is not the vendor device and it is certainly not the battery. It was
  deliberately **not** called a vendor latency.

- New arm-measurement keys, all additive and all `null` before evidence exists:
  `dispatch_write_started_at`, `dispatch_enable_written_at`,
  `claim_to_write_latency_s`, `dispatch_write_duration_s`,
  `enable_to_register_latency_s`.

### Not changed

`claim_written_at` keeps its meaning and its value. It precedes the actual write by
the whole solve, and ownership provenance is measured from it -- moving it would have
shrunk the published activation latency with no real improvement, which is a prettier
measurement rather than a faster one. `activation_latency_s`, `observation_latency_s`,
`delivery_latency_s`, `battery_delivery_latency_s` and
`objective_forgone_to_activation_kwh` keep their definitions and their origins; every
attribution and coherence rule is untouched, and ambient production is still never
credited to a dispatch on either boundary.

### Validation

- **5 hypothesis tests**, written and green against the pre-change tree, pinning the
  diagnosis itself: no activation can be written while the solve runs; the tick
  cannot arm an inactive dispatch; a second refresh over a running dispatch never
  re-arms; and both halves of a later boundary-aligned arm -- an admitted row *can*
  build a seven-step sequence without a solve, and *cannot* authorise one without
  recomputing `start_index`, which `INHIBIT_STALE_PLAN_INTERVAL` refuses.
- **21 focused tests**, including a three-term reconciliation that fails if the write
  duration is dropped
- **266 passed** across the arm, Stage-B, ownership, restart and lifecycle suites
- **63 passed** on neutrality; planner digests and the beta.40/41 anchors **unchanged
  and not re-baselined**
- **Mutations: b47 22/22, b44 18/18, b45 23/23, b46 18/18 -- 0 survived, 0 anchors
  lost.** Seven b47 mutations survived the first pass and every one was a vacuous
  test: the reconciliation never exceeded its own terms, the sweep was never run to
  its bound, and the activation predicate was never shown a stop sequence -- which
  writes the enable *first* and then cleans up. The tests were rewritten and one
  predicate was moved where it could be tested; no mutation was weakened.
- Lint and format clean

### Live validation pending

This prerelease is **not yet hardware-validated**. The next live day must show, over
at least two charge arms and two export arms, reported as median and worst case:

1. `activation_latency_s` **statistically unchanged** from the 39.8 s median baseline
   -- beta.47 is not supposed to make dispatch faster, and a change means something
   unintended moved.
2. register-to-observed **under 2 s median**, against the 42.5 s baseline.
3. `enable_to_register_latency_s` present on **every** arm, and never exceeding
   `activation_latency_s`.
4. The three-term identity reconciling to within ~0.2 s on every arm.

## [1.0.0-beta.46] - 2026-09-06

**An arm-observability fix, and nothing else.** No planner decision, no economics, no
buy or sell scheduling, no reserve, no export protection, no admission, no dispatch
behaviour, no actuation timing and no campaign-lifecycle change. This is not Phase 9
and adds no activation cost to the planner.

The 2026-09-06 charge ran for eight hours, moved 13.72 kWh against a 15.11 kWh promise,
and filed an arm measurement that said:

```
objective_kwh: 0.0
objective_forgone_to_activation_kwh: null
delivery_latency_s: null
delivery_evidence: sources_incoherent
battery_delivery_latency_s: 43.4
```

Three separate defects, all of them in the read-only measurement layer.

### Fixed

- **The arm objective was the wrong quantity, and was structurally always zero.**
  `_observe_arm` captured `_objective_kwh_for(self._quarter)`, which returns the
  *realised* objective of the row in flight -- `min(realised, allowance)`. An arm opens
  the instant its claim is written, when nothing has been realised, so the figure was
  `0.0` on every arm that has ever run. It now sums the **planned** objective of the
  arm's own maximal contiguous stretch of executable rows, read off the frozen schedule
  Stage B holds, at `QuarterRow.objective_kwh`'s own boundary: battery for a charge,
  meter export for an export. Published beside it: `objective_boundary`, `row_count`
  and `planned_span_s`, so the derivation is legible rather than asserted.

- **`objective_forgone_to_activation_kwh` could not exist, and would have been wrong
  the moment it could.** It is derived from the objective, and zero times anything is
  zero, which the guard then correctly withheld -- the right answer for the wrong
  reason. It was also prorated over a single quarter, which against a real multi-row
  objective charges the arm's entire promise to its first fifteen minutes. It is now
  prorated at the arm's mean planned rate over `planned_span_s`, and the delay is
  clamped to that span, so an arm can never forgo more than it ever planned to deliver.

- **Delivery evidence compared a dataclass to a string.** `self._coherence` holds a
  `ControlCoherence`; `COHERENCE_OK` is `"ok"`. So
  `self._coherence not in (None, COHERENCE_OK)` was true on **every** tick that carried
  a verdict at all -- which is every tick after the first of a run, the field being
  `None` only while idle. Delivery was therefore evaluated exactly once per arm, before
  the setpoint had reached the pack, and every later sample was discarded as
  `sources_incoherent`. The gate now reads the state. The live 43.4 s
  `battery_delivery_latency_s` was the honest half of that same tick.

- **One bad tick no longer labels the whole arm.** The incoherent reason was written
  with `setdefault`, so the first hiccup latched for the arm's life. Evidence is a
  statement about the observation that produced it, so it is assigned; and a tick whose
  sources were all readable with nothing above the production surplus now says
  `incomplete` rather than wearing an older failure.

- **The beta.44 arm test rig fabricated a type production never produces.** It set
  `coordinator._coherence` to the *string* `"ok"`, which is why an object-versus-string
  comparison passed a suite written to protect it. The rig now builds a real
  `ControlCoherence`.

### Not changed

Stage A's objective and every planner decision; buy and sell economics; reserve
feasibility, headroom and the export gate; tomorrow-price handling; run count,
direction changes and campaign grouping; scheduling; Stage B setpoints, ownership, the
dead-man and admission semantics; the beta.45 campaign lifecycle and restart recovery.
The attribution rules themselves are untouched: ambient production is still never
credited to a dispatch on either boundary, an unreadable surplus still refuses to
attribute, and `null` is still never zero.

`arm_measurements` remains session-local and is read by the diagnostics block alone --
retention is not redesigned here.

### Validation

- **17 new tests**, and the beta.44 arm suite corrected to the real coherence type
- **277 passed** across the arm, Stage-B, ownership, restart and lifecycle suites
- **98 passed** on neutrality; planner digests and the beta.40/41 anchors **unchanged
  and not re-baselined**
- **18 mutations killed, 0 survived, 0 anchors lost** (`tools/mutation/b46.py`). Four
  survived the first pass, every one of them a vacuous test: the gap test never
  exercised the backward walk, the forgone test never exceeded its own span, and the
  evidence test never asked a healthy arm to go blind. The tests were rewritten; no
  mutation was weakened. Two beta.44 anchors pointed at lines this release rewrote and
  were re-anchored on the current source, same claim and same test: b44 is 18/18 again.
- Lint and format clean
- **Final full suite: 5005 passed, 0 failed, 3 pre-existing skips** -- one sharded
  run at `-n 16` with `PYTHONHASHSEED=0`, no serial rerun

## [1.0.0-beta.45] - 2026-09-06

**Every campaign that started published a failure moments later, and then finished in
silence.** The live trading log read:

```
Laden gestart — doel 15,11 kWh — economic buy
Campagne mislukt — 0.0 kWh van 15,11 kWh — quarter progress unknown
```

while that same charge went on running for five more hours. The 2026-09-06 capture
shows both halves at once: `campaign_realized_kwh` at **6.323 kWh** and still
accumulating across thirteen closed quarters, beside a public result of `0.0`.

**Nothing was wrong with the execution, the accounting or the planner.** Stage B was
running, ownership was `owned`, and the thirteen quarter figures sum to 6.324 kWh
against the 6.323 the campaign held. The defect was entirely in the publication.

### Fixed

- **A live campaign was recovered as a restart corpse, on the refresh after it
  started.** `_recover_campaign_lifecycle` runs on every report — deliberately, since a
  never-started instance cannot be classified at restore time — and asked only whether
  a persisted mark existed and which marks it carried. Neither question can tell a mark
  left by a previous process from the mark of the campaign this process is running. So
  the first report after `started` found `started` present and `stopped` absent, took
  the branch labelled *"the restart is the stop"*, and published `failed` /
  `quarter_progress_unknown` — with `recovered_after_restart: true`, when no restart had
  happened. It then latched the instance closed, so the genuine terminal was refused by
  `_lifecycle_removed`'s own exactly-once guard and the campaign ended with no line at
  all. A campaign is now recoverable only when its instance is **not** the one open in
  memory; on a real restart that attribute is `None`, so recovery is untouched.
- **The realised figure a restart recovers was structurally zero.** The mark's
  `realized_kwh` was written once at creation and refreshed only by a campaign-scoped
  stop, so even a *legitimate* restart mid-campaign reported `0.0` and filed `failed`.
  The realised total, the frozen target, its tolerance and measurability now move with
  the campaign, on the save the caller already schedules. A genuine restart files
  `partial` against the real figure.
- **The published `window_end` was the row in flight, not the campaign.**
  `_campaign_end_utc` is a high-water mark of rows already executed — its own
  declaration warns that conflating the two is how a thirty-three-row campaign reported
  a window three rows in — and it was published as `window_end` on every event and on
  the persisted mark. The capture had 10:15Z against a planned end of 15:00Z. The
  public window is now the planned end, with the observed one as a fallback only.
  `campaign_end` keeps its name and meaning in diagnostics, and `planned_end` is
  published beside it.

### Added

- **A plan line, before the action.** Two new lifecycle kinds, `planned` and
  `plan_closed`, and a `planned` state on *Current Campaign*. `planned` fires about one
  planning cadence before the first executable row, carrying the purpose, the objective
  boundary, the promise and the window — and no plan id in any rendered field.
- **Continuity that survives replanning.** An announcement cannot be tracked by
  `campaign_id`: that identifier is a digest of the campaign's *end*, and the end moves.
  The same live charge was published as `af82a579ac6a803a` (end 15:00Z) and ninety
  minutes later as `c9d9217306560d3a` (end 14:45Z), because the optimiser ends a charge
  earlier as the pack fills. Continuity is therefore *same purpose, and the windows
  genuinely overlap* — **both** halves of the overlap, strictly, on half-open intervals.
  The one-sided form the executor uses for runs is sound there only because a carried
  run is already executing; an announcement has no such footing and would match a
  campaign lying entirely in the past.
- **Policy C on a material change:** the first `planned` event stands and the attributes
  move under it. The tail moves on nearly every refresh, so a "replanned" line per
  wiggle would rebuild exactly the per-quarter noise this surface exists to replace. A
  campaign that does *not* continue the announcement closes it `superseded`; a window
  that passes unstarted closes it `not_executed`.

### The accounting fence

A planned-only announcement exists before any instance is minted, so it can end without
a campaign ever having existed. That ending is **publication-only**: it carries
`campaign_instance_id: null` and **no `realised_kwh` key at all** — not `0.0`, not
`null`, because a null invites a template to render a zero and "0.0 kWh" against a
promise is the precise lie this release removes. It never enters `_close_campaign`,
`_lifecycle_removed` or `_publish_recovered_terminal`, never latches an instance, never
writes the campaign mark, and **never updates `last_campaign_result`** — which answers
"how did the last campaign that actually ran turn out", a question an announcement has
no answer to. One rule spans both vocabularies: *no announcement event uses a campaign
kind, and no campaign event carries a null instance id.*

### Not changed

Stage A's objective, every planner decision, reserve, Safety Buy, Coverage Buy, the
switching cost, the grid-charge margin, PV retention and absorption, the controllability
floor, Stage B's equations, rolling required power, setpoint correction, ownership,
`releasing`, the beta.44 arm instrumentation, economic-value and battery-return
accounting, and the `campaign_id` algorithm itself. **No physical stop timing moves:**
`_campaign_still_published`, `_campaign_row_is_final` and `_completion_scope` are not
touched, and the diff contains no line naming any of them.

The `campaign_id` churn documented above is real and remains **deferred**. It is masked
by the frozen-schedule clause of `_campaign_row_is_final` for a campaign inside one
admitted plan, which covers both shapes on the reference installation today.

### Verification

- 31 new tests; **23 mutations, 23 killed, 0 survived, 0 anchors lost**. One survivor in
  the first pass was a vacuous test — it gave the orphan a different `campaign_id` from
  the live campaign, so a campaign-keyed guard reached the right answer by accident. The
  test was rewritten to the shape that actually matters, one campaign attempted twice.
- Neutrality digests and the beta.40/41 anchors **unchanged** — a pass, not a re-baseline.
- One beta.42 test changed by design: it pinned the vocabulary at exactly four kinds. It
  now asserts the stronger rule — the campaign kinds are exactly those four and the
  announcement kinds are disjoint from them.

### Not validated

No beta.45 code has run on hardware. This is a prerelease for live validation.

## [1.0.0-beta.44] - 2026-09-06

**One economic campaign can ask for several physical arm cycles, and no published
figure said so.** The optimiser prices a campaign as one uninterrupted run and charges
one fee for it. Stage B may arm, stop and re-arm several times inside that same
campaign: a `serve_load` gap between two exports, or a PV-only `hold` inside a charge,
each force a stop and a fresh marker claim. On the 2026-09-05 horizon that was **eleven
arms against two direction changes** — nine of them free.

The reason is structural and is now measured rather than guessed at. The DP's state is
`(interval, bucket, carry, run_state)` and `run_state` is a *direction*: both
`ECONOMIC_ACTION_EXPORT` and `ECONOMIC_ACTION_DISCHARGE` set `_RUN_DISCHARGE`, so
`export → serve_load → export` costs the objective exactly nothing. The codebase already
called those splits reporting artefacts. They are — for the fee. Underneath each one is
a real arm cycle.

**Nothing in the planner moved, and that is this release's hard gate.** The DP
objective, `minimum_trade_gain_eur`, the grid-charge margin, the throughput cost,
terminal value, reserve feasibility, reachability, headroom, Safety Buy, the
battery-side charge objective, the meter-side export objective, PV retention and
absorption, and every beta.43 behaviour are untouched — proven by the seven neutrality
digests and the beta.40/41 anchors passing **unchanged**, not re-baselined.

### Added

- **An arm plan.** `arm_count`, `campaign_count`, `segment_count`,
  `executable_segment_count`, `direction_changes` and `runs_total` published together,
  so the eleven-against-two gap is readable without reconstructing it by hand. An arm is
  one maximal contiguous stretch of executable rows, because a non-executable row makes
  the tick stop the dispatch and the next executable row claim the marker again — it is
  **not** inferred from the campaign or direction-change counts.
- **Per-arm detail**, bounded: `arm_index`, `intent`, `starts_at`, `ends_at`,
  `objective_kwh` at the boundary it is paid at, and `marginal_value_eur`.
- **The plan-versus-execution mismatch.** `runs_refused_nothing_armable`,
  `energy_planned_on_refused_runs_kwh` and `value_planned_on_refused_runs_eur` — runs
  Stage B can never arm because no row of them is executable. Value is measured as the
  advantage over leaving the battery alone, so ambient production and unavoidable
  household import sit inside the idle counterfactual on **both** sides of the
  difference and neither can be reported as dispatch-caused.
- **Per-arm physical measurement, with the two clocks kept apart.** The live capture is
  why: one arm's claim was written at 22:15:05.2, the vendor register's own
  `last_changed` was 22:15:42 — **37.3 s** — and the first tick that saw it was
  22:16:36.9, giving **91.7 s**. A single figure would have reported the vendor as two
  and a half times slower than it is, and a later release would have priced an arm
  against our own sixty-second cadence.
  - `activation_latency_s` — the vendor's clock, accepted only across an observed
    inactive-to-active transition at or after the claim. Precision is bounded by the
    source integration's poll interval, which is disclosed and never corrected for.
  - `observation_latency_s` — our clock, and it includes our cadence on purpose.
  - `delivery_latency_s` — the first coherent delivery **attributable to the dispatch**:
    caused export above the measured production surplus at the meter, or grid-caused
    charge above it at the battery.
  - `battery_delivery_latency_s` — any battery charge, published beside it precisely so
    ambient absorption can never be read as proof that a dispatch started.
  - `objective_forgone_to_activation_kwh` — derived, not measured, with its formula
    published, and an upper bound because ambient behaviour may have delivered part of
    it anyway.
  - `null` is never zero. Missing evidence names itself: `no_observed_transition`,
    `register_predates_claim`, `sources_incoherent`, `delivery_not_attributable`,
    `restarted_before_evidence`, `incomplete`.

### Fixed

- **A duplicated attribution computation.** `_coverage_attribution` was called twice
  with byte-identical arguments; the second call is removed. No figure changes.
- **A docstring describing a solve that has never existed.** `_safety_buy_attribution`
  claimed `build_outcome` re-solves under the coverage economics where coverage was
  promoted. It does not, and never has — it carries the compelled total forward instead.
- **A stale `solve()` docstring** that described the recursion as three-dimensional. The
  carry axis has been a dimension since beta.41.

### Not changed, deliberately

An **activation cost inside the objective** is not added here. Making the DP see an arm
boundary requires widening `run_state`, which is roughly a third more work on five or
six full backward inductions — 31–35 s becomes ~41 s on the reference hardware, on a
host where beta.43 already documented that budget as the cause of two-cadence staleness.
And the figure it would be calibrated against does not exist yet: two live tracking
errors, −0.017 kWh on a 0.25 kWh objective and −0.047 kWh on a 1.02 kWh one, cannot
separate a fixed per-arm loss from a proportional one. This release produces that
measurement.

An **export-threshold alignment** was designed, challenged and withdrawn. `caused_export`
is derived *from* `flows.export_kwh` and never feeds back into it, and the cash term
prices `flows.export_kwh` unconditionally — so moving that threshold changes the label
and the permission and not one euro. It would also have stopped sub-threshold export
remainders from needing the export permission, loosening `allow_battery_export`.

A **charge-side materiality gate** is not added, and the question is not closed:
`charge → HOLD → charge` mints a second arm while `run_start` stays false, so
`minimum_trade_gain_eur` does not cover every physical charge arm. No additional gate is
currently justified; revisit only if these measurements expose an uncovered
economically relevant shape.

### Verification

- 25 new tests; **18 mutations killed, 0 survived, 0 anchors lost**. Two survivors in the
  first table were vacuous tests and both were rewritten — one asserted a bound on a
  ring the test itself had constructed, so the declaration it was meant to protect was
  never executed.
- Neutrality digests and the beta.40/41 anchors **unchanged**.
- No new entity, no new persistent store, no configuration key. Everything lands in
  existing bounded diagnostic rings.

### Not validated

No beta.44 code has run on hardware. This is a prerelease for live validation.

## [1.0.0-beta.43] - 2026-09-06

**A row's measured energy did not survive its own boundary, and a campaign with more
than one row could not report what it had done.** On the sixty-second tick the
executing slot advanced to the successor row and the accumulators were rebased onto it
*before* the ended row was read — so the row that had just finished recorded `0.0`.
The capture/restore pair inside `_async_end_row` was written to protect those totals
across the physical stop, which happens two statements later than the loss.

The outcome depended on something with nothing to do with the plant: a row **with** a
successor recorded zero, and a row that ended with nothing after it recorded the truth,
because that path returns early and never resets.

Measured on the reference installation, 2026-09-05. The 20:15–20:30 export row filed
`realized_grid_kwh: 0.0` with a 100 % shortfall while the physical decision ring held
`grid_realized_kwh: 0.494` for that same row twenty-three seconds before it ended. All
nine completed rows of that capture split exactly along "has a successor", across two
campaigns and both directions — which is why a 3.62 kWh charge campaign reported an
empty result beside five recorded rows.

**Nothing in the planner moved.** The DP objective, the reserve, the three purchase
categories, terminal value, PV retention and absorption, Stage A authority, Stage B
admission, the meter-side export objective, the battery-side charge objective,
household-load compensation, charge ramping and every safety gate are unchanged —
proven by the seven neutrality digests and the beta.40/41 decision anchors being
byte-identical.

### Fixed

- **The boundary.** The ended row's totals are captured before the slot advances and
  handed to the row-closing path, so a row is recorded and accrued from its own
  measurement. beta.35's stop-before-record rule is untouched: only the accounting
  moved, never the physical write.
- **The accrual reaches the campaign.** `_async_end_row` now accrues before the stop,
  which is what beta.36 did for its sibling `_async_end_quarter` and only that one —
  the stop reaches `_close_campaign`, which nulls the campaign identity, so the
  accrual afterwards early-returned on its own guard.
- **A row is judged at its own allowance.** `_record_completed_quarter` has stated
  since beta.35 that *"the subject is a parameter, not `self._quarter`"* and then read
  three figures off properties resolving against the field. Both recording callers
  arrive after the slot has moved, so a finished charge row was capped by the
  **successor's** envelope — zeroed outright where that successor was a `serve_load`
  gap. Export was never affected; its objective is a plain metered field.
- **The terminal is not read through a rebased row.** `_async_stop_dispatch` cleared
  the executing slot and the exactly-once accrual latch *before* `_close_campaign`,
  making the open-quarter term it promises to include structurally zero. Both moved
  after the terminal.
- **`rows_completed` is a real count.** It was the same local as `quarters_admitted`
  published twice, which read as corroboration: on 2026-09-05 both said 2 while three
  completed rows carried the campaign id. It now counts rows, and the closing row that
  beta.35 deliberately records *after* the terminal is counted through the accrual
  latch rather than dropped.
- **`objective_row_count` is set on both branches**, so a campaign read through the
  `execution_targets` fallback no longer publishes the previous campaign's count.

### Added

- **The campaign target may grow, and still may never shrink.** A campaign that opened
  with one published row froze a 0.25 kWh target, ran three rows whose meter
  objectives summed to 1.50 kWh, and filed `success` at `0.233 of 0.25` beside
  1.206 kWh of recorded export. The freeze exists so Stage A changing its mind cannot
  make a shortfall retroactively successful; it was never meant to cap a campaign at
  the fraction of itself that happened to be published when it started. Monotonic,
  identity-guarded, and read only from the live publication — nothing recorded is
  rewritten.
- **`releasing`, for a dispatch we armed and have already stopped.** The vendor's
  dead-man keeps `dispatch_active` true for minutes after our own quarter-boundary
  stop releases the marker and clears the causal record, so `ownership_of` answered
  `foreign` about our own cleanup — and `_decide` turned that into
  `ownership_conflict`, a reason in neither the failed nor the completion set, which
  falls through to `canceled`. **Authorisation-identical to `foreign`**: no write, not
  owned, every gate refusing on "not owned" refuses on it unchanged. Reachable only
  against a release receipt whose recorded dead-man deadline still matches the
  register's and has not passed; without that proof the answer stays `foreign`, and no
  fixed grace period is invented.
- **A controllability floor for `net_export`, beside the resolution floor and not
  instead of it.** `MIN_EXECUTABLE_QUARTER_KWH` keeps answering *can the actuator
  express this?* — one step held for a quarter. The new
  `MIN_CONTROLLABLE_QUARTER_KWH` answers *can the loop hold it?* and is derived from
  the deadband the controller will not correct inside plus one step it cannot express.
  The live row that made it necessary published a 0.04 kWh meter objective, 0.16 kW
  average: its entire incremental export revenue was 0.0083 EUR against 0.010 EUR of
  deadband exposure, while the 0.21 kWh of avoided import beside it — the great
  majority of the row's published value — needed no dispatch at all. Export only; a
  charge objective is battery-side, its rows are continuations inside one arm, and
  `retention_authorised` is how 62 % of the live charge campaign's energy reached the
  pack. **A skipped row is not a failure**: self-consumption continues and the energy
  stays for a window that can be steered.
- **`upcoming` on Next Planned Action** — the campaigns ahead, at most eight, ordered
  by start, each with absolute instants, the objective at the boundary it is paid at,
  `will_execute` and `skip_reason`. A dashboard no longer has to parse the diagnostics
  download to show a trading log.
- **Join keys.** `open_campaign` gains `campaign_instance_id` and `campaign_end`;
  `admitted_plan` publishes the `campaign_id` and `campaign_end` it has carried since
  beta.32.
- **`objective_rows_realised` on the terminal** — the rows the total was summed from,
  so a terminal that disagrees with its own history is visible from one payload.
- **`seq` and `cadence` on every lifecycle transition.** Three cadences write the
  trail and the quarter refresh threads one instant through a body that takes 31–35 s
  on the reference hardware, so it stamps transitions half a minute stale while the
  tick stamps fresh ones — the 2026-09-05 trail holds `cleanup_complete` at 17:30:20
  followed by `foreign` at 17:30:05. `at` is unchanged; the order is now readable
  without inventing a clock.
- **`published_at` and `completed_campaign_is_current`**, and `completed_campaign` is
  re-rendered at the write boundary alongside the two blocks that already were.

### Verification

- 38 new tests across four files; 19 mutations killed, 0 survived, 0 anchors lost.
  Five survivors in the first table were tests aimed at the wrong surface and every
  one was rewritten — including a gap test whose fixture allowances made two
  equal-and-opposite errors cancel exactly in the sum, so it now asserts per-row
  accrual increments.
- The mutation runner is hardened. `read_text`/`write_text` is not a round trip on
  Windows: it rewrote every line ending in an LF file, its own content hash then
  reported drift on a file it had just restored correctly, and `restore()` reached for
  `git checkout` — which discards uncommitted work on the dirty tree this harness is
  designed to run against. Reads and writes now preserve line endings, and recovery
  restores captured content instead of `HEAD`.

### Not validated

No beta.43 code has run on hardware. This is a prerelease for live validation.

## [1.0.0-beta.42] - 2026-09-05

**The figure labelled "realised net value" was never a battery comparator, and an
investment return built on it would have said the battery destroys value.** Written
out, `realized_net_value_eur` equals `TRUE − Σp·min(I,N) + Σs·X`: it subtracts the
household's whole unavoidable import bill and credits back PV export that needed no
battery. `Σp·min(I,N)` dominates, so the number is structurally negative for any
household that imports anything. Its own docstring was accurate — *"what the
household is better off by"* — it was the name that misled, and this release adds the
comparator rather than renaming that one.

Two further Phase-7 defects were found alongside it and are fixed here. The
no-battery export leg `max(0, PV − L)` **did not exist anywhere**, so PV surplus
revenue was credited to the battery. And the counterfactual was differenced against
the wrong meter: `baseline_at` excludes the electric vehicle, `grid_import_at`
includes it, so on any interval the vehicle drew, `max(0, load − pv) − import`
collapsed to zero and the battery's contribution vanished from the realised figures
with nothing saying so. Only one of the two terms had ever been re-based.

Nothing in the planner moved. The DP objective, the reserve, the three purchase
categories, Safety Buy and Coverage precedence, terminal value, PV retention, Stage A
authority, Stage B admission, opened-row authority and the no-catch-up rule are all
unchanged — proven by the seven neutrality digests and the beta.40/41 decision
anchors being byte-identical.

### Added

- **Realised battery benefit.** `realized_battery_benefit_eur` = no-battery net cash
  less actual net cash, over four measured cash legs. It reads no state of charge, no
  capacity and no efficiency, which is what makes it the only figure here an
  investment return may be built on — and a test pins that rather than assuming it.
- **The no-battery export counterfactual**, with `realized_no_battery_export_kwh` and
  `realized_no_battery_export_revenue_eur` published beside the import leg.
- **`DayRecord.total_load_at()`**, the whole-household reading, carrying
  `baseline_at`'s validity rule: `None` when an expected flexible-load sample is
  missing, because the total is then unknown by exactly the amount nobody measured.
  `baseline_at` is unchanged — the forecast and learning paths legitimately want it.
- **Historical persisted-day pricing.** A past day is priced from the issuance stored
  for it, on the basis published at the time, never on a setting the operator has
  since changed. `realized_window` previously reported `days_priced: 1` on every
  installation because `price_forecasts` only ever holds today and tomorrow.
- **Day finalisation and a sealed lifetime total.** `day_finalizable` refuses a day
  for five named reasons; a qualifying day's benefit is sealed once and folded into a
  running total when the record is evicted at 365 days. Storage minor 2.7 to 2.8,
  additive.
- **Battery Return sensor**, state in percent so Economic Value stays the only
  EUR-valued state. Optional gross investment, subsidy, other credit and a purchase
  date. Trailing 30- and 90-day figures, a payback estimate from the 90-day mean, and
  two named refusals: below 30 priceable days it is arbitrary rather than
  conservative, and at a non-positive mean it withholds rather than dividing.
- **Campaign lifecycle events and two sensors.** `alpha_ems_campaign` fires
  `created`, `started`, `stopped` and `removed` at most once each per
  `campaign_instance_id`, persisted so a restart replays none of them.
  `current_campaign` and `last_campaign_result` publish the same state a dashboard
  needs. Two new outcomes, `not_executed` and `superseded`.
- **A missing-tomorrow-prices regression**, captured live at 01:00: a 91-interval
  horizon `limited_by: prices`, no bridge requirement, no Safety Buy, no violation.
  Across eight starting states the compelled purchase is byte-identical to the
  complete-horizon solve, so an unpublished day can never manufacture a Safety Buy.
- **A `forecast` ledger basis**, the seventh word. `planner_derived` was carrying two
  claims: the optimiser valuing energy that *exists*, and its estimate of energy that
  has not moved. Only one of those can still be falsified by the weather.
- **`figure_basis` on the Economic Value entity.** The basis map existed for four
  releases and the entity could not see it — an operator saw fifteen adjacent euro
  attributes spanning four kinds of number, distinguished only by their names, on an
  entity Home Assistant labels `MONETARY`.
- **`source_incompatible` daily-validation status.** A 220 kWh whole-site counter
  against a 20 kWh house published roughly -91 % beside the word `comparable`. The
  observed ratio is now published and no unit is reinterpreted.
- **Persisted solve timing.** `solve_ms` and `solves` join the decision ring, so
  "what does a refresh cost, and has it moved?" is answerable from inside the
  codebase.

### Fixed

- **`realised_net_value_eur` was labelled `measured`** while one of its addends is
  attributed. A total is no stronger than its weakest addend.
- **The published `solve_rule` misdescribed the pass structure it exists to
  explain**: three unconditional passes claimed, four actual, `coverage` named
  nowhere.
- **`reconciliation_error_eur` advertised a check it does not perform.** The total is
  *defined* as the sum of the addends, so the residual can only ever be rounding. It
  now says what it actually verifies.
- **Plausibility sanitisers on the grid-budget path.** A PV spike could drive the
  measured surplus high enough that the grid-import ceiling accrued nothing for a
  whole quarter, removing the only bound on buying.
- **Control-grade freshness on the safety gate.** The actuation path was handed the
  300-second diagnostics bound where the 90-second control bound exists for exactly
  this, and the state-of-charge entity is not in the coherence source set — so a
  reading between 90 and 300 seconds old was caught by nothing.
- **Two stale execution claims that ship to readers**, including the `contract_rule`
  carried inside *every published execution target*, which still said only a charge
  can execute — untrue since beta.27 admitted `net_export`.

### Verification

- Suite 4790 to 4894 tests. Serial 90 min to 51:33; `-n 16` 34 min to 14:06; the
  development tier is 28 s; the slowest single test 14.2 min to 4.2 min. No test was
  removed and no assertion weakened.
- Solve grids for the four hot files are memoised behind a shared cache that refuses
  anonymous callables, so a key collision is not expressible.
- Deterministic sharding from measured JUnit timings, with the manifest committed
  and CI reading it. **Measured on GitHub: 29.6 min total against beta.41's 127 min**,
  on 4-core runners at `-n auto`. The four shards ran 27:52 / 15:46 / 16:03 / 13:51 --
  a 1.94x spread rather than the 1.00x the plan projected from local timings, because
  a 4-core runner parallelises differently from a 16-worker development machine.
  Re-planning from the CI artifacts closes the spread on paper and gains almost
  nothing in practice: `test_beta40_hard_floor.py` alone is 26.92 min of CI processor
  time, which is 97 % of an ideal shard, so the slowest shard was already at the floor
  no split can beat. Splitting that file further is the next real gain and is not
  attempted here.
- Mutation testing moved into `tools/mutation/` and hardened: content-hash snapshots,
  deterministic restore, a lock, and — new here — refusal of any anchor that does not
  match **exactly once**. Six anchors were ambiguous and had been silently mutating
  their first match; four of those predated this release.
- 352 mutations killed, 0 survived, 0 anchors lost. The beta.42 table found five
  vacuous tests, including both guarding this release's own headline corrections;
  every one was rewritten rather than the mutation weakened.

### Not validated

No beta.42 code has run on hardware. This is a prerelease for live validation.

## [1.0.0-beta.41] - 2026-09-04

**The optimiser could not value stored energy above the export rate, so it stopped
buying entirely.** On 2026-09-03 at 20:45, live on `beta.40`, the reference
installation held 9.936 kWh in a 21.6 kWh pack against a 4.32 kWh floor, tomorrow's
day-ahead prices were fully published, tomorrow forecast 7.79 kWh of production
against 21.65 kWh of load, and the pack reached the floor at 02:45 and could not
refill itself. Stage A returned `runs: []`, `run_count: 0`,
`reason_code: no_material_economic_action`.

The figure that explains it is `stored_energy_marginal_value_eur_kwh: 0.15013`,
which is exactly `discharge_efficiency x terminal_export_price`
(0.948683 x 0.15825). Break-even for any grid charge was therefore **0.0924
EUR/kWh** -- below the cheapest quarter this installation has ever recorded
(0.15285). **No price cleared it, ever.** The user's 0.20 EUR minimum trade gain
and 0.05 EUR/kWh grid-charge margin were never reached: the per-kWh gate refused
first, and would have refused at a zero margin too. They are unchanged by this
release, and the settings were never the cause.

Two defects locked together to produce that number, and neither substitutes for
the other.

### Fixed: the solver held two different beliefs about how much energy was in the pack

The recursion measured the hard floor, the reserve, reachability, the terminal
credit, the worth of storage and the export permission against the **lattice
level** -- the addressable state -- while the forward reconstruction subtracted
household self-consumption **afterwards**. Two representations of one physical
quantity, and on the live horizon they were 5.4 kWh apart: the optimiser believed
it held 9.75 kWh where the pack held 4.32, published `battery_discharge_ac_kwh:
5.15` alongside `battery_throughput_kwh: 0.0`, and paid a terminal credit on
energy the household had already eaten. Holding cost nothing *and* depleted
nothing, which is an unbounded free-energy source, and the same kilowatt-hour
could be credited twice.

**The state now carries the drain.** A second axis was added to the dynamic
program: `(lattice_bucket, carry)`, where `carry` is sub-bucket household service
accumulated so far in eight sub-divisions of one bucket, and physical energy is

    physical = lattice_energy - carry * bucket_kwh / 8

read from one place by every physical constraint. The axis advances
deterministically with the interval rather than with a decision, so it adds states
without adding transitions -- a linear cost rather than the quadratic one a finer
lattice would carry.

Both alternatives were measured before this one was chosen, and both were
rejected on evidence rather than taste:

* **A one-dimensional coordinate transform is not exact.** Over the live
  108-interval horizon 95.4 % of intervals have a residual load below one lattice
  discharge step, so sub-lattice service is the dominant mode, and the
  unconditional drain totals 17.34 kWh against 5.62 kWh of room above the floor.
  The pack is exhausted after 32 of the 103 sub-lattice intervals and must then
  *stop* serving and import instead. A state at the floor therefore needs two
  successors -- serve, and suspend -- differing by a sub-bucket amount. No
  relabelling of a single grid carries both.
* **Refining the lattice is quadratic and still not enough.** Halving the bucket
  costs 3.6x, quartering 13.6x, eighths 52.9x (82 to 656 buckets, 1 390 to 84 093
  moves, 101 ms to 5 356 ms). Quantising the drain down to the grid leaks
  `103 x step` kWh, so a 0.5 kWh tolerance needs roughly a 730x solve. Ruled out.

Two things were corrected during implementation, both found by measurement rather
than by reading. The served amount is **differenced from the two states** rather
than derived from the step count, because the top of the lattice is clamped to the
pack ceiling and is not uniformly spaced -- a closed form was 8.9e-03 kWh out on
exactly that interval. And the floor stayed a **reserve** rather than becoming a
state exclusion: excluding below-floor states made every survival horizon report
`economic_terminal_unreachable` and plan nothing, which is the beta.31
immobilisation under another name. Only negative energy is excluded; the floor is
enforced through `violations`, lexicographically above cost, exactly as before.

The `_ambient_*` drain helpers and the whole-horizon drain bound are deleted rather
than corrected. There is no second representation left to reconcile.

### Fixed: the post-horizon demand window collapsed the moment the day-ahead published

`TerminalValue.credit_eur` is piecewise-linear: a first segment serving
post-horizon household demand at the displaced import price, a second exporting
the spare at the export price. It is concave **only because** the first segment's
rate is higher. With `demand_ac_kwh = 0.0` that segment has zero width and the
whole curve flattens to `discharge_efficiency x export_price` -- the beta.34 cliff
the class was written to replace.

It was zero every afternoon. The window was built from `demands[horizon_intervals:]`,
the horizon ran to the last interval carrying both a price and a demand, and the
demand series is only ever "the rest of today, then all of tomorrow". So once
tomorrow's prices published, the horizon consumed the entire series and the tail
was empty. `build_horizon` had been publishing the tell all along -- `limited_by`
flipped from `"prices"` to `"complete"` -- and nothing read it.

**The window is now a clock-matched replay of the horizon's own tail.** The
post-horizon window begins where tomorrow ends, so the day-after's overnight
profile, matched by civil-time slot, *is* tomorrow's own overnight profile --
already in the series, with its own production forecast, at a price that is known
by construction. Extending the forecast a third day was rejected: production
cannot be forecast that far on the free Solcast tier, and a missing production
figure makes the free-refill break silently unreachable, which would credit a whole
day of load at the import price and license hoarding.

Sourcing the estimator from the priced prefix only is a structural guarantee that
no unknown-price interval is ever read. Five break rules each shorten the window
and record why in `post_horizon_window_stopped_by`; every one of them errs toward
understating the worth of stored energy, which is the safe direction -- overstating
it authorises real spending on a day nobody has published a price for.

### Fixed: two clock defects in the estimator, both live

The clock-matched price estimator computed an **absolute** civil-day slot and then
read it out of an array whose zero is the **head** of the horizon. With a 14:00
head, tomorrow's 02:00 was priced at today's 16:15. It also used a modulus against
a fixed 96 intervals, which is wrong on the two days a year that have 92 or 100.
Both now use the same head-relative arithmetic the price alignment already used,
with an unmatchable sentinel so a missing interval count degrades to an empty
window rather than raising.

`_terminal_value` had no test coverage at all. It has a family of its own now.

### Added: minimum-cost coverage of energy the household will buy anyway

Separate from safety, and separate from discretionary trading.

Where the forecast says the pack will reach the floor and the household will then
import at the meter, that import is going to be bought. The only open question is
*when*. Buying it earlier at a cheaper reachable quarter is not a trade and does
not answer to the thresholds that govern one -- the 0.20 EUR minimum trade gain and
0.05 EUR/kWh margin exist to demand a *profit*, and there is no profit here, only
the same purchase at a better hour.

The coverage counterfactual is built so that a purchased kilowatt-hour has exactly
one way to pay for itself: **export is forbidden**, so nothing bought can be sold;
**the terminal spare segment earns nothing**, so nothing bought can be parked at
the horizon edge and credited; and the discretionary gates are set aside. What is
left is displacement of household import the forecast says will otherwise happen.
Every grid charge in that solve is coverage **by construction** rather than by
inspection. The reserve, the hard floor, the power limits and the physical state
are identical to the ordinary solve: this changes what a purchase must *earn*,
never what the pack may *do*.

It is promoted **whole** or not at all -- the two plans are never spliced, so
whatever executes is a single complete solution with one physical trajectory -- and
only when it leaves the household better off in cash **including the inventory each
plan ends with**, buys more than discretion would by more than one bucket, and is
no less safe. A plan that merely spends less because it bought less is not cheaper,
and valuing the terminal inventory under the rule the executed plan will be judged
by is what stops that being mistaken for a saving.

Purchases now carry three disjoint categories, in precedence order: `safety_buy_*`
is what physical reachability compels and is price-blind; `coverage_buy_*` is
household import moved to a cheaper reachable hour; `economic_buy_*` is a
discretionary trade and answers to the user's own thresholds. Each kilowatt-hour
belongs to exactly one.

On the live case coverage contributes **nothing**, and that is the intended result:
once the state model and the terminal window are corrected, ordinary economics
already buys the useful energy. Measured on a horizon where the gates genuinely
bind -- a 0.25 against 0.42 band -- discretion buys 0.000 kWh and coverage buys
8.611 kWh, saving 0.247 EUR, with export structurally zero.

### Fixed: Safety Buy could absorb coverage energy and publish it as compulsory

The safety attribution differences two solves and calls the difference compulsory.
That is sound while the two differ *only* in the reserve, and the coverage
counterfactual also sets the gates aside -- so the difference is partly the reserve
and partly the gates, and attributing all of it to the reserve published
discretionary energy as physically mandatory. Measured: the band fixture above
reported all 8.611 kWh of coverage energy as Safety Buy.

Where the pair is not comparable the compelled quantity is now the bridge itself,
which is what physical reachability actually demands and is price-blind by
construction. The published safety figure remains identical at 0.02 and at 0.90
EUR/kWh.

### Added: the two household-service quantities are published separately

Exact meter-side household energy and quantised state movement are two different
numbers, and both are now published, reconciled by a signed residual:

    ambient_self_consumption_ac_kwh (exact, meter side)
    battery_state_service_dc_kwh    (how far the modelled state moved)
    battery_state_quantisation_residual_kwh  (the signed difference)

The meter figure is untouched and stays exact -- every grid figure on the interval
is split against it, which is what makes the beta.39 no-battery counterfactual
arithmetic rather than an estimate. Substituting the solver's quantised movement
for it was tried and fabricated export across 230 rows of Stage B. The residual is
bounded by one carry step per interval and reaches no decision; publishing it is
what makes the pair one model rather than two accounts.

### Changed: the frozen execution claim carries the curve the plan obeyed

`reserve_floor_kwh` is documented as "Stage-A physical limits Stage B must honour,
frozen with the schedule". It was filled from the **autonomy** projection -- the
counterfactual that asks what the pack would need if the grid vanished, which read
21.93 kWh against a 21.6 kWh pack on the live horizon. It now carries
`horizon.planning_reserve_kwh`, the enforced reachability curve the recursion
actually obeyed, still floored at the hard floor.

Inert before and after: all ten references were checked one at a time and every one
is a declaration, a pass-through or a serialisation. `dispatch.py` never mentions
it. What enforces the floor is the configured state of charge, which never reads
this field. This is a provenance correction made before something starts trusting
it, and no claim schema changes -- the field's shape is unchanged, so pre-upgrade
claims parse identically.

### Deliberately not changed

* **The user's settings.** The 0.20 EUR minimum trade gain and 0.05 EUR/kWh
  grid-charge margin are passed through untouched and remain authoritative for
  discretionary trading. The reported fault was a defect, and weakening a setting
  to hide it would have been the wrong fix.
* **Reachability's grid credit still ignores `allow_grid_charging`.** With the
  switch off the plan may still spend down to the hard floor plus margin and let
  the household import at the meter, because the *physical* reachability answer
  does not change with a preference. Gating it collapses reachability to autonomy,
  which nothing can satisfy, and the optimiser would then hoard regardless of
  price -- the beta.31 immobilisation verbatim, for the default configuration. Now
  stated in `reserve.py` as a recorded decision rather than a latent surprise.
* **`terminal_bucket` is not raised by the drain.** On a no-production horizon that
  collapses to "end no lower than you are now", which is exactly what beta.18
  removed after measuring it selling nothing into a 1.20 EUR/kWh peak and buying
  4.74 kWh at peak prices.
* **`CLAIM_SCHEMA_VERSION` is still written and never read.** Validation is by
  parsing, which is the right design; the constant's docstring overstates what it
  does and is now the only thing that says so.

### Schema

Additive only. `EconomicInterval` gains `battery_state_service_dc_kwh` and
`battery_state_quantisation_residual_kwh`; `EconomicOutcome` gains
`coverage_buy_attribution`, `coverage_buy_runs`, `coverage_saving_eur` and
`coverage_baseline_charge_ac_kwh`; `TerminalValue` gains `window_basis`,
`window_intervals` and `window_stopped_by`. Every new field is defaulted.
Diagnostics gain `coverage_buy_ac_kwh`, `coverage_saving_eur` and
`coverage_baseline_charge_ac_kwh` alongside the existing `safety_buy_ac_kwh`. No
stored claim, entity or option changes shape.

`coverage_baseline_charge_ac_kwh` is what the discretionary plan would have bought,
published so the coverage attribution is checkable from outside: coverage is a
difference against that plan, and without the figure "coverage never takes credit
for energy discretion would have bought anyway" is a property only the solver could
verify.

### Verified

The live 20:45 horizon is replayed verbatim as a fixture. Before: 0 runs, stored
energy worth 0.15013 EUR/kWh, the pack projected to the floor and staying there.
After: **2 runs** -- 3.889 kWh the reserve compels at 0.270, and 12.778 kWh bought
economically in tomorrow's cheap window at 0.160 -- stored energy worth 0.3809
EUR/kWh, the two decided endpoints identical, zero violation and zero export.

**The test suite was strengthened because mutation testing said it had to be.** Of
the first 37 mutations written against this release, 24 survived -- and not because
the code was over-guarded. Almost every property of a solved plan is a
*self-consistency* property, and self-consistency survives breaking the state model:
replace the physical-energy function with the identity and the recursion, the
forward walk, the terminal credit and both published endpoints all move together,
back to the beta.40 model exactly, with every "the walk closes onto the endpoint"
assertion still passing because both sides moved.

Two files were added rather than the mutations weakened. One anchors every physical
claim on the **exact meter-side household figure**, which the state model cannot
touch: a trajectory reconstructed from it is what the pack really does, and the
published one has to match -- measured across 42 shape-and-state combinations at
1.72 carry steps, against the old model's divergence of the whole of consumption.
The other pins the rounding directions, the materiality thresholds and the
precedence arithmetic on the functions themselves. Four mutations were removed as
provably equivalent, each with the reason recorded beside it.

## [1.0.0-beta.40] - 2026-09-03

**Free production stopped going to the meter while the pack had room for it.** On
2026-09-03 at 12:14, mid-campaign and working exactly as designed, the reference
installation had PV at 3.309 kW against a 0.792 kW house -- 2.517 kW of surplus --
with 12.61 kWh of pack headroom, `lifecycle.state: executing`, `ownership: owned`,
25 of 25 safety checks passing, and **8.527 kWh of grid authorisation still unspent
and still intended to be bought later at 0.1745 EUR/kWh**. The battery was taking
1.490 kW of the surplus and **0.942 kW was crossing the meter outward**.

Nothing had malfunctioned. `decide_charge` seeds its command from the frozen row's
objective and every line after the seed only reduces, so the row's objective was
also the ceiling on storing production nobody had to buy. Its own `desired_grid_kw`
came out **-1.027** -- the controller predicted the export in watts before anybody
measured it, which is the same signature `beta.36` was diagnosed from with the
binding term the other way round.

**The plan was not the problem, and that finding narrowed the fix.** The schedule's
three 2.50 kWh rows are exactly the three cheapest quarters in the window: the
optimiser already concentrates its purchase at full inverter power. The fifteen
0.28 kWh rows are one lattice bucket each (`10.0 * 0.25 / 9`, the 1.111 kW the
payload publishes three times), sized to the *forecast* surplus -- 0.22 kWh of
production against 0.06 kWh of grid at interval 49, i.e. 0.88 kW. Reality delivered
2.517 kW. The row was right about the forecast; a frozen row turns a forecast into
a hard cap.

### Added: Stage A says whether keeping free production beats selling it

One economic question, asked per interval and answered from the optimiser's own
dual:

    keep beats sell   <=>   eta_charge * eta_discharge * V  >  export_price

`V` is the gradient of the cost-to-go table across one bucket -- the figure the
Economic Value sensor already publishes as
`stored_energy_marginal_value_eur_kwh`. On the capture that is
`0.90 * 0.2237 = 0.2013` against `0.10134`, so keeping wins by **0.0999 EUR/kWh**.
Priced against the purchase it would displace instead
(`0.90 * 0.1745 - 0.10134`) the same decision is worth **0.0557 EUR/kWh**. Both are
published; neither is averaged into the other, and neither is extrapolated to a
day.

The verdict travels as `retention_authorised` on each published quarter row, with
`retention_gate` saying why. **The refusals are the load-bearing half**:
`refused_export_superior` is the tariff answering that selling wins, which is why
this is an economic preference and not a zero-export rule. `refused_value_undefined`
and `refused_no_export_price` are the honest answers where the lattice or the source
cannot price the question, and both refuse rather than granting.

### Added: Stage B may raise a charge, up to the measured surplus and no further

`decide_charge` gains the first term in its history that may *increase* a command:

    absorb_kw  = pv_surplus_kw if retention_authorised else 0.0
    applied_kw = max(applied_kw, absorb_kw)

A `max`, never a `min` -- the objective may legitimately exceed the surplus because
the objective may be grid-fed, and applying this as a `min` would cap total battery
power by a free-production figure and re-break `beta.36` in mirror image.

**It cannot buy a watt, for every input rather than for one capture:**

    delta = max(objective, surplus) - objective = max(0, surplus - objective)
          <= surplus = pv - house
    =>  desired_grid = house - pv + applied <= 0

Same row, same objective, same authorisation, on the capture's own numbers:
`applied_kw` **1.490 -> 2.500 kW** and `achievable_grid_kw` **-1.027 -> -0.017**.
The residual is the actuator's own floor-toward-zero quantisation.

**How much is not Stage A's question and it does not ask it.** Inverter power and
pack headroom are bounds this integration keeps in exactly one clamp, and Stage B
already applies both to every charge it commands -- so the published row carries a
verdict and not a kilowatt-hour. A second copy of a physical limit is a second
thing to keep in step, and the first time the two disagreed it would be the copy
that got believed.

### Added: a ceiling on how much is worth keeping, not just whether

**Found by pre-release audit, and it blocked the release.** The first
implementation of this release froze a boolean -- *keeping a kilowatt-hour beats
selling it here* -- and let the controller absorb to the physical limits. That is
broader than the economics behind it.

The comparison is made at the level the pack stands at. One quarter at full charge
power moves the pack several lattice steps, and the dual falls as the pack fills, so
the first kilowatt-hour of a row can clear the tariff comfortably while a later one
in the *same row* does not. Swept over the seven horizon shapes at their own export
prices, that state is reachable in **five of them**, worst case bucket 58 to 63 on
the Sell shape: `V` 0.21196 to 0.01823 against an export price of 0.18788, i.e.
**-0.171 EUR/kWh over as much as 2.108 kWh DC -- 0.36 EUR on one row**, which is
larger than the gain the release was built to capture.

"It is free production" is not a defence. Free production still carries the
opportunity cost of the export it forgoes, and that is exactly what the comparison
prices.

So the row now also carries `retention_until_dc_kwh`: the stored energy above which
keeping stops paying on that interval's own price. Stage B differences it against a
**live** state of charge, together with the pack's own ceiling, and bounds the
absorption branch by whichever is lower. Re-swept: **zero negative-value steps
retained, across 2,939 authorised cases in all seven shapes.**

**The first crossing, and nothing cleverer.** The curve is not concave -- swept at
full resolution it rises again in places -- so the set of passing levels is not an
interval and there is no highest-passing level to aim at. Walking up to the first
failure is the only bound that cannot over-retain; stopping early where the curve
later recovers forgoes a gain rather than taking a loss.

**The acceptance case is unchanged.** On the capture's own curve and price the
crossing sits at 20.291 kWh DC against 8.986 stored -- 11.9 kWh AC of room where one
row can take 2.5 -- so the ceiling does not bind, and `applied_kw` is still
1.490 to **2.500 kW** with `achievable_grid_kw` still **-0.017**. A bound that also
bounded the case the release exists for would have traded one defect for another.

### Fixed: the charge path had no per-tick pack-energy clamp

`headroom_kw` is `None` unless Stage A published `max_end_energy_kwh`, which it does
only when a later interval absorbs surplus -- `null` on the live capture. So a
charge was bounded in software by power and by the row's objective, never by the
pack's remaining room, and at 99.9 % state of charge the absorption branch commanded
the full inverter limit. The device's own charge cutoff (written on every arm, rest
and sustain) and the pack's management stopped it. Those are real protections and
neither is a software bound.

`retention_until_dc_kwh` carries the pack's ceiling as well as the economic one, so
the command is now bounded on every tick by what the pack can actually take -- and
differenced against a live reading, because `battery_plan.state` is rebuilt on the
economic cadence and would be a quarter of an hour stale on the cadence that
commands.

### Added: a satisfied row can go on storing free production

The third outcome, and the release exists for it. A row whose objective is met took
one of two paths before: hold at zero, or end the quarter. Both stop the battery --
and `beta.36` *measured* Mode 2 at 0 kW to be a **total** hold that suppresses
charging as well as discharging. The satisfied row is therefore precisely where
free production is guaranteed to leak.

A satisfied objective with the verdict on the row and measured surplus present now
falls through to the ordinary setpoint path. The target-reached latch stays set, so
the moment production goes the next tick takes the `beta.39` path and the row is
satisfied and held exactly as before: no new lifecycle state, and no recovery path
to invent. Absorption stops when the surplus goes, the pack fills, a clamp binds,
or the quarter ends.

Two conditions are load-bearing rather than defensive. The surplus must clear
`CONTROL_MIN_POWER_KW`, because the below-resolution hold is guarded on the latch
and would otherwise leave the tick writing a trickle the device cannot express. And
the pack's room is read **live** from the state of charge rather than from
`battery_plan`, whose state is rebuilt on the economic cadence -- a quarter of an
hour at the surplus this branch will command is long enough to fill the room a
stale figure is still reporting.

### Fixed: objective and absorbed are separate, and a campaign is judged on one

`_completion_scope` ends a campaign when its realised total reaches the target
frozen at first activation, summed from each row's *objective*. Counting free
production stored above a row's objective into that total would end a campaign that
had not finished buying. The capture's frozen target was 13.1 kWh against
12.6144 kWh of pack headroom, so the pack ceiling happens to sit half a
kilowatt-hour below the trip point -- which is why this is closed by construction
rather than left to a coincidence.

    objective = min(total, allowance)      absorbed = total - objective

Derived rather than integrated, and that is a proof rather than a shortcut:
crediting the objective first and capping it hard gives `min(T, A)` for any sequence
of increments, and an opened quarter's allowance cannot move. So there is no second
accumulator to reset, capture, restore or lose across a stop -- and the capture
tuples in the coordinator are positional. A row's shortfall is judged on the
objective too: absorbed production must not paper over a promise the row missed.

### Fixed: a clamped absorbing tick is no longer zeroed by its own guard

Found by the Gate 1 sweep rather than by reading. The overshoot guard was widened to
the absorbing branch by reading the reported clamp token -- but a physical clamp
overwrites that token, so 2.5 kW of surplus under a 1.0 kW inverter limit came out
at 1.0 kW and then at 0.0 with `tick_energy_horizon` printed beside it. The guard is
now keyed on whether the branch bound, which a clamp cannot rewrite.

### Fixed: the run's grid budget was a pace, and it throttled the plan

**The largest single finding of this release, and it is not about production at
all.** The same 2026-09-03 campaign that motivated the retention work also
under-delivered badly: **13.100 kWh planned, 5.939 kWh realised**, a 7.161 kWh
miss over 23 completed rows, with `outcome: partial`.

`_charge_limits` turned the run's remaining grid authorisation into a rate by
dividing it by the *run's* remaining time — a flat average pace — and that rate
then capped the battery through `battery_cap_kw` in every individual row. It
throttled precisely the three rows Stage A had deliberately sized at full
inverter power:

| row | needed | row authorised | flat pace | observed mean | delivered |
|---|---|---|---|---|---|
| 11:45 | 10.00 kW | 9.08 kW | 2.73 kW | 3.40 kW | 0.795 of 2.50 |
| 12:15 | 10.00 kW | 9.28 kW | 2.99 kW | 3.46 kW | 0.808 of 2.50 |
| 13:00 | 10.00 kW | 9.84 kW | 3.78 kW | 4.38 kW | 1.021 of 2.50 |

Each observed mean is predicted by `pace + measured surplus` to within
**0.5–2.8 %**, on three independent rows. Those three rows carry **4.876 kWh —
68 % — of the whole shortfall**, and the campaign finished with **5.076 kWh of
authorised grid purchase unspent**. The budget was never the binding constraint;
its pace was.

**It is the `beta.36` defect one level up.** `beta.36` stopped the *row's* grid
authorisation capping battery power. The *run's* remaining budget was still doing
it, as an average. A budget is an energy, and the honest rate it permits is the
rate that would spend it inside the row now executing — which is the shape the
other two remainders already have. Stage A's own per-row `grid_authorised_kwh`
still paces each row, tick by tick, through `progress.grid_rate_kw`.

The total stays bounded exactly: across the open row the run figure permits at
most its remaining budget, recomputed every tick from measured
`grid_charged_kwh`. **It is emphatically not catch-up** — energy missed in an
expired quarter never becomes available in a later one, because each row is
bounded by its own frozen authorisation first and that figure never moves.

### Fixed: a campaign terminal could claim an objective it never reached

The same campaign filed:

```
objective_target_kwh     13.100
objective_realized_kwh    5.939      45 % delivered
outcome                 partial      correct
reason   campaign_objective_reached  a claim about the objective, and false
```

`outcome` was never wrong — it is computed from the measurement — but the two
fields sat side by side and a reader trusting `reason` would have recorded a
45 %-delivered campaign as a success.

**A scope was standing in for a reason.** `_completion_scope` returns one
`campaign` scope for two entirely different endings: the objective was delivered,
or the last planned row closed without it. Both are legitimate and both stop the
same dispatch, so one scope is right — but both call sites then published
`campaign_objective_reached`, and only one of the two endings supports that
claim. With 23 of 23 rows closed the schedule was final, so the scope was correct
and the reason was not.

The reason is now decided by the energy, at the same tolerance the terminal
verdict and the scope test already use. **No new vocabulary**: `window_ended`
already exists, already means exactly "the last planned quarter closed", and is
already a completion reason — so the outcome mapping is untouched and a short
campaign still files `partial`, never `canceled`.

### Fixed: two plan endpoints, one name

The `beta.39` diagnostic carried three figures that could not all describe one
trajectory: a configured hard floor of **4.32 kWh**, an economic-plan
`end_energy_dc_kwh` of **3.51 kWh** *below* it, a terminal energy of **4.74 kWh**
*above* it, and `violation_kwh` of **0.00**.

They describe two different quantities, and only one is a decision.
`end_energy_dc_kwh` is the **ambient-corrected reported walk**:
`start_energy_dc_kwh` is reduced every interval by
`ambient_self_consumption_ac_kwh` — the pack feeding the house, no decision
involved — while `battery_delta_dc_kwh`, the lattice move the recursion chose,
stays put. The two live in different resolutions on purpose; 0.105 kWh of ambient
discharge against a 0.264 kWh bucket is not representable, which the reserve
block has recorded as deferred since `beta.31`.

`edge_energy_kwh` is the **decided lattice state**, and it is now proven to equal
the lattice walk's endpoint exactly in all seven horizon shapes. So 4.74 kWh was
the real planned endpoint all along, comfortably above the floor, and the
violation of 0.00 was correct — violations are evaluated on the decided state,
which never went below the reserve.

**The planner was never at fault, and that was established by sweep rather than
by argument.** Across 250 solves spanning five horizon shapes, ten starting
energies and five price/production variants, the decided trajectory **never
discharges below the configured hard floor and never below where it started**.
The deepest level that appears anywhere is the *seed* — `bucket_at_or_below`
models a measured 4.32 kWh as 4.2164 kWh, one bucket down, which is the
conservative direction for an amount you have — and the plan then holds there
rather than digging deeper. The only shapes whose decided state sits under the
floor are the ones that *start* under it, at 0.3 and 1.2 kWh, and each reports a
non-zero violation honestly.

What was wrong was the diagnostic. Both endpoints are now published with their
basis named from a closed vocabulary —
`end_energy_basis: ambient_corrected_reported_walk` beside
`planned_end_energy_dc_kwh` and `planned_end_energy_basis: decided_lattice_state`
— with a rule stating that the projection is not an executable target and that
Stage B executes the quarter schedule. The counterfactual `capability` plan, where
the live 3.51 was read, carries the same distinction.

### Deliberately not changed

- **No tie-break, no objective term, no `minimum_trade_gain_eur` change.** The
  0.28 kWh rows are one lattice bucket, and in a genuinely flat window the choice
  between spreading and concentrating is a tie the enumeration order resolves
  arbitrarily. That is real, and its correct direction is *undetermined*:
  front-loading concentrates cheaply and destroys the capacity headroom free
  production needs, deferring preserves it and pays the run fee again. Shipping a
  guess would trade a proven gain for an unproven one. The envelope makes the
  smeared shape harmless, which is the right order of operations.
- **No zero-export rule.** The gate refuses whenever `eta_rt * V <= export_price`,
  and a full pack, a spent inverter or a vanished surplus each end absorption on
  their own terms. Some export on the capture's own day is unavoidable: the reserve
  layer already reports `surplus_beyond_headroom_kwh: 4.01`.
- **No new public sensor.** Diagnostics only, on the existing quarter block.
- **No `realized.py` change.** Absorbing instead of exporting *lowers*
  `in_progress_interval_eur` for the duration of a row, because
  `open_quarter_value_eur` is pure cash with no inventory term; the value returns
  through inventory revaluation at quarter close. The five terms still reconcile
  exactly and no residual is absorbed into an addend -- so the only valid measure of
  this release is `total_economic_value_today_eur`, never realised cash alone.
- **No Safety Buy change.** Only physical reachability may initiate a purchase. The
  verdict may sit on a compulsory row -- keeping free production strictly reduces
  what must be bought -- and it cannot change what is compelled, because it can
  only ever command energy nobody bought.
- **No grid-ceiling change.** `grid_authorised_kwh` bounds the grid and not the
  battery. `beta.40` is that invariant read the other way round: a ceiling on
  buying is not a ceiling on storing something nobody bought.
- **The forward-authorisation machinery is not inherited.** It bounds grid
  purchase. Applying it to a verdict over free production would let an ordinary
  replan silently revoke an open row's authority, which is the class of defect
  `beta.38` closed.

### Schema

**No schema version moves, and that is argued rather than skipped.**
`STORAGE_MINOR_VERSION` stays at **2.7** and `CLAIM_SCHEMA_VERSION` at **2**. The
verdict is two additive keys on a published quarter row: the publication is rebuilt
from the solve every refresh, and where it *is* persisted -- inside the claim
record's round-tripped `target` -- absence reads back as a refusal, which is
`beta.39` behaviour. A `beta.39` record read by `beta.40` authorises nothing new; a
`beta.40` record read by `beta.39` ignores two keys. No compatibility boundary is
crossed in either direction, and `beta.20` set the precedent for exactly this
situation: *"bumping the version would claim a compatibility boundary that was
never crossed."* `ECONOMIC_MODEL_VERSION` and `RESERVE_MODEL_VERSION` are unchanged;
no term entered the objective.

### Verified

4698 automated tests, serial and under sixteen workers with identical results
(10:22 against 4:09). Six mutation harnesses report **zero survivors and zero lost
anchors**: `beta.40` (58 mutations), `beta.39` (79), `beta.38` (44), `beta.37` (43),
`beta.36` (35), `beta.35` (19) -- 278 in total. Three `beta.36` anchors were
repaired because this release moved the lines they pointed at; neither mutation was
weakened, and both were re-anchored on the narrowest text that still identifies the
layer they attack. Two proposed `beta.40` mutations were *removed* rather than
weakened, each with the reason recorded in the table: the shortfall expression and
the satisfied row's early return are both provably equivalent to what they were
changed to, so neither described a defect. A third was removed after the corrective
for the same reason -- "a full pack absorbs nothing" is now protected by two
independent clauses, so no single-edit mutation can break it.

**One decision moved and it is named.** The seven horizon shapes and their canonical
per-interval digests -- recorded against `ff3e912` in a detached worktree under
`PYTHONHASHSEED=0` and reproduced at `508c18a` -- hold unchanged for a third
release: the optimiser chose nothing differently, and solve counts stay at
4/5/5/4/5/5/5. The published contract gains exactly two additive row keys and
changes no other byte. On a row Stage A did not authorise, `decide_charge` is
field-for-field the `beta.39` decision across 180 swept live conditions, including
the reported clamp token.

`test_beta35_stored_value.py` was amended rather than widened: through `beta.39` it
claimed the marginal value was an observation and that a release making it an input
would have changed. `beta.40` is that release, so the claim is restated and the new
reader carries its argument. The `forbidden` set that actually holds the invariant
is unmoved -- nothing in the recursion, the outcome build, the policy or the safety
evaluation reads the curve.

**Hardware validation is outstanding.** The next supervised Live day must show a
quarter with measured surplus above its row objective reporting
`battery_absorbed_extra_this_quarter_kwh > 0`; `desired_grid_kw <= 0` on **every**
tick where `dispatch_limited_by` is `free_pv_absorption`, a single positive watt
being cause to revert; a satisfied row with surplus present still charging and never
writing 0 kW, with `hold_reason` never `quarter_satisfied` while absorbing;
`objective + absorbed == realised` to three decimals on every completed row;
`retainable_now_kwh` falling as the pack fills and absorption stopping when it
reaches zero **with room still in the pack**, which is the ceiling doing its job;
`remaining_grid_authorisation_kwh` unconsumed by any absorbing tick, and a
concentrated row reaching the power its own authorisation permits rather than a
flat pace; a campaign that ends short reporting `window_ended` and never
`campaign_objective_reached`; exactly one
campaign terminal, at the frozen objective and not early; and the five euro figures
summing to the published total with `accounting_reconciliation_error_eur` at `0.0`.

## [1.0.0-beta.39] - 2026-09-02

**The lifecycle says what the battery is doing, and the day says what it earned.**
`beta.38` passed its first real Sell on the production installation -- run
`0492b715ccf76ce0`, 1.701 of 1.75 kWh at the pack and 1.437 of 1.49 kWh at the
meter, one terminal, marker released, no reopen. The opened row held its authority
through Stage-A replanning, which is the release `beta.38` was.

Two things the day exposed, and both are observability. `execution.lifecycle.state`
walked `admitted → starting → stopping` while 7.4 kW was demonstrably crossing the
meter. And the Economic Value sensor could not answer *"what has today earned, what
is still coming, and what is the honest total?"* -- while its own basis string
contradicted itself in a single sentence.

**No economic decision moves.** Stage A's chosen plan, its per-interval actions,
both counterfactual baselines, the ambient term, the published execution targets,
the Stage-B wire traffic and the solve count are byte-identical to `beta.38` across
seven horizon shapes.

### Fixed: `executing` was structurally unreachable

Not a control fault -- a publish-ordering one, in four parts. `_lifecycle_state_from`
has exactly one call site, inside the *pure* `_build_control_report`, which runs one
call frame **before** the write boundary. So the ownership it projects from is the
pre-arm reading: the 20:45 payload shows `ownership.state: "none"` beside
`power.executed: true, applied_kw: 6.9`. `arming` was tested *ahead* of
`OWNERSHIP_OWNED`. Together those meant `executing` needed a second quarter refresh
inside the same run -- which a single-row campaign does not have. And the
sixty-second tick never projected at all, so through fifteen ticks the field was
frozen.

The criterion is the one that was already there and only its timing moves:

> A run is `executing` **iff `ownership_of(evidence) == OWNED`** -- the vendor
> register reports `dispatch_active`, our persisted claim matches this run, and the
> owner marker is on.

It is emphatically **not** "the arm write landed". The live probe dates the register
going active at 20:45:49, 44.7 seconds after the claim, so on the refresh that arms
`starting` is the truthful answer and the first *observation* that satisfies
`ownership_of` records `executing`. Nothing here manufactures vendor state from a
command just sent.

Three mechanisms, and no second state machine: the `OWNED` test moves above the
`arming` test in the same projection; the sixty-second tick projects through that
same function once it has read an active dispatch; and the write boundary
re-publishes the block it already built.

**`executing` now has exactly one route.** `beta.38` had three -- `holding`,
`sustaining` or ownership -- and the first two are computed with `owned` conjoined,
so they reached no state ownership did not while leaving the predicate able to
answer `executing` for `holding=True, ownership=none` when asked directly. One
criterion is what the field is for.

### Fixed: two of the three cadences published nothing

`lifecycle.state` can only ever show the latest answer, and the lifecycle advances
at the write boundary and on the physical tick as well as on the quarter refresh --
neither of which publishes a report. A run that started and finished between two
publications therefore left **no trace of having executed at all**, which is
exactly what the incident produced.

Every transition is now appended to a bounded trail and published beside the state.
`stopped` and `cleanup_complete` are two verified events rather than one word for
both -- a distinction `_async_stop_dispatch` already made and threw away -- and
`cleanup_complete` had no writer anywhere in the package. Neither is sticky: the
projection cannot return either, so the next ordinary refresh reads `idle` and the
trail keeps the fact.

`LIFECYCLE_UPDATING` stays deliberately unused, and now says why: a setpoint
correction is not a state of the run, and `tick.reason` already names which
correction happened.

### Fixed: the payload said a started campaign had not started

`open_campaign.started` read `false` and `frozen_target_kwh` `null` on the very
refresh a campaign began, beside a `completed_campaign` whose `started_at` was that
same instant. `beta.38` moved the freeze to the correct instant -- and asserted it
on coordinator state *after* the refresh, which is why that test passed and the
payload went on lying. Same cause, same fix, one extra line.

### Added: Gerealiseerd vandaag, Nog verwacht, Totaal

On the **existing** `Economic Value` entity, whose state is unchanged, so no history
breaks. Four figures and a published total:

```
realised_today_eur
+ in_progress_interval_eur
+ remaining_expected_today_eur
+ forecast_revaluation_eur
= total_economic_value_today_eur
```

and it telescopes to something a person can state in one sentence: **the civil day's
cash, plus what the pack is worth now, less what it was worth when the day opened.**
Every intermediate term cancels, so no residual is hidden inside any addend -- and
if the four ever fail to sum, the total is withheld and the error is published.

Four things had to be proven before any of it could be published.

**One counterfactual, and it was the release's declared blocker.** Realised load
avoidance is measured against a household with *no battery*; the plan's
`avoided_import_eur` is measured against leaving the battery alone this interval,
which since `beta.31` includes the inverter serving residual load unbidden. Adding
one to the other was the single dishonest move available here. The no-battery
residual turns out to be exactly recoverable from the solved plan --
`idle_import_kwh + ambient_self_consumption_ac_kwh` -- with **zero error over 380
intervals including 55 on the ambient branch**, and no second solve.

**The day's three slices are disjoint and exhaustive by construction**, at 92, 96
and 100 intervals alike, because they are defined by the plan's own head index and
not by which intervals happen to have data. That is what closes the gap the live
captures showed: at 21:00 the realised window covered 0–83 and the plan's remaining
slice covered 85–95, and index 84 -- the Sell row itself, one quarter earlier -- was
in neither.

**The quarter in flight is its own named term**, measured from the live
integrators with its coverage beside it, and it becomes part of realised history
only when its measurement closes. A partial quarter folded into history is a figure
that can go down.

**Forecast revaluation is required, not useful.** Subtract `beta.38`'s operational
identity from the position total and what is left is exactly
`V[now](e_open) − V[open](e_open)`. Without it, forecast movement is attributed to
today's operation under a name that says operation -- and it is material: the same
12.269 kWh was worth 2.3001 € at 20:45 and 2.3659 € at 21:00, six and a half cents
of pure curve movement in one quarter on unchanged energy. It is also **not**
reconstructible from anything already persisted: the marginal value is a slope where
the position is an integral over a curve the model itself reports as kinked, and on
the same capture the product gives 2.30 € against an actual 3.0942 €.

Every figure fails honest rather than fails silent. No opening valuation, a moved
lattice, an opening energy that does not match, a day block on another basis, or a
priced horizon that does not reach local midnight each yield `None` with a named
reason. **Never a zero.**

### Added: a charge campaign can now say it has two reasons

A planned charge with `battery_target_kwh: 8.06`, `safety_buy_kwh: 0.83` and
`economic_buy_kwh: 7.22` published `purchase.classification: mixed` — correctly,
since `beta.32` — beside a run-level `purpose: safety_buy`. So the figures a reader
audits said one thing and the word a *user* reads said another: seven of the
campaign's eight kilowatt-hours were presented as compelled survival energy when
they were a deliberate trade the optimiser chose on price.

There is now a third word, `mixed_buy`, on the run's purpose and on both economic
entities:

- **Safety Buy** — reachability compelled the purchase, and nothing more was
  bought.
- **Charge** — nothing was compelled; the whole purchase is a trade.
- **Mixed Buy** — reachability compelled a component *and* the optimiser
  independently found further charging worth doing in the same window.

**It is not a third kind of purchase and it weakens neither word beside it.** The
economic component never becomes safety energy, and **only physical reachability
may initiate a compulsory purchase** — that rule is untouched and is asserted on a
horizon that buys hard on price alone and is still classified as an ordinary
charge. `safety_buy_kwh`, `economic_buy_kwh` and `purchase.classification` remain
the numerical source of truth and are unchanged.

The classification is derived strictly *after* the plan and its purchase
attribution, from the two figures they produced, and it reaches nothing that
decides: two targets built from one run and one window, differing only in the
split, are asserted identical in intent, plan id, campaign, both instants, the
battery target, the grid target, the quarter schedule, the headroom constraint and
every power figure. A mixed campaign is admitted exactly as the equivalent charge
campaign is, because admission keys on the intent and never on the purpose.

`Activity` has classified this shape as `Mixed Buy` since `beta.32` from the same
attribution pair; what it lacked was a purpose to map it to, so a mixed run
reported `economic` and the compulsory component vanished from the line a user
reads. It now reports `mixed`. `0.83 + 7.22` is `8.05` against a target of `8.06`,
and that centilitre of kilowatt-hour is the display rounding that was already
there: the energy arithmetic was **not** changed to make rounded figures add up.

### Fixed: the basis string contradicted itself

It said *"on the exact basis the optimiser minimised"* and, four words later,
*"both sides are metered cash"*. Those cannot both be true: the scalar the dynamic
programme minimises is `objective_eur`, which carries the minimum trade gain, the
grid-charge margin, the throughput cost and the terminal credit; the state carries
none of them. A `beta.37` test had asserted `state != objective_eur` since the day
the prose was written. The cash half was the true half and is what is kept.

### Deliberately not changed

- **No second economic sensor.** A family of them is how two come to disagree.
- **No new schema for Mixed Buy.** A word on a published target is not persisted
  state, and the storage minor bump below is the release's only schema movement.
- **No Dutch attribute keys.** Home Assistant does not translate attribute *names*,
  so "Gerealiseerd vandaag / Lopend kwartier / Nog verwacht / Herwaardering /
  Totaal" are Lovelace card labels over correctly-named English keys. A card
  snippet is in the release notes.
- **No FIFO, no acquisition cost, no invented purchase price** for stored energy.
  The position is priced from now forward, at the margin, exactly as it has been
  since `beta.18`.
- **`net_cash_flow_eur` is still not called profit.** Its sign convention is import
  less export, so a negative value means money arrived.
- **The reserve floor moving is not a refusal.** It moves with the load and
  production forecasts, and its effect on what the position is worth *is* forecast
  revaluation. Both floors are published beside the figure. The lattice pitch is
  different in kind and is refused.
- **`_terminal_value`'s horizon offset and `ForecastRisk.today_interval_count`**
  remain as they were. Both change economics; the live evidence implicates neither.
  They shift the value curve and therefore the *magnitude* of the revaluation, not
  its definition.

### Schema

`STORAGE_MINOR_VERSION` 2.6 → **2.7**, and it is the only movement: one optional
nested dict per civil day, holding what the energy the day opened with was worth on
the curve that existed then. Additive -- a `beta.38` document reads back with the
key absent, which is a defined state with its own published reason -- so there is no
migration and no reset. `LEDGER_BASES` gains a sixth word, `revalued`, for the same
energy valued on two different curves: neither a measurement nor a single-instant
planner figure, and calling it `planner_derived` would let a reader difference it
against one.

### Verified

4542 automated tests. Five mutation harnesses report **zero survivors and zero lost
anchors**: `beta.39` (79 mutations), `beta.38` (44), `beta.37` (43), `beta.36` (35),
`beta.35` (19).

Decision neutrality is proven **cross-commit** against `ff3e912` in a detached
worktree under `PYTHONHASHSEED=0`: seven horizon shapes, a canonical per-interval
digest over eighteen named fields, and the published execution targets plus the
Stage-B wire traffic from the same replay in both economic directions --
byte-identical, 13601 and 8687 bytes respectively. Solve counts unchanged at
4/5/5/4/5/5/5.

Two `beta.38` mutation anchors were repaired because this release moved the lines
they pointed at, and one of them had become a false pass: with a second
`_note_lifecycle` call site in the file, the harness's first-occurrence replacement
was silently disabling the *tick's* projection while the test read the report.
Neither mutation was weakened; both were re-anchored on the narrowest text that
still identifies the layer they attack.

**Hardware validation is outstanding.** The next supervised Live day must show
`lifecycle.state` reaching `executing` in the *payload* and holding it through every
tick of the row; `stopped` then `cleanup_complete` at the terminal; the four euro
figures summing to the published total with `accounting_reconciliation_error_eur` at
`0.0`; `forecast_revaluation_eur` non-zero and drifting; and, across local midnight,
yesterday's realised unchanged with no double count after a reload.

## [1.0.0-beta.38] - 2026-09-01

**An opened row is not withdrawn by absence.** On 2026-09-01 at 20:30:05, on the
supervised Live installation, a frozen two-row `net_export` run was recorded as
*ended* on the very refresh its first row opened — and the same refresh armed 9.7 kW.
A terminal was filed against a run the software then started. This release fixes the
two independent defects behind that contradiction, plus three ordering and
observability faults the trace exposed, and closes a one-line gap in the position
accounting.

Nothing about the economics moves. Stage A's chosen plan, its per-interval actions and
energies, its published execution targets and its solve count are byte-identical to
`beta.37` across four horizon shapes.

### The incident

Stage A's horizon head is `elapsed + 1` — the *next* interval, never the one in
progress. Affirmation requires a publication whose window starts at or before the
carried run's end, so **no publication issued after a row opens can describe it**, and
every run's final row is unaffirmable by construction. Withdrawal-by-absence was
therefore the normal state of every run's last quarter, and the suppression downstream
was load-bearing on every single run rather than on an edge case.

Two defects, either sufficient alone:

1. `carry_forward`'s withdrawal-by-absence was an unguarded terminal `return` with no
   way to see that the row was open. `AdmittedPlan.authority_rule` had promised the
   opposite since `beta.29` — *"withdrawal is never inferred from a horizon that cannot
   describe an open quarter"* — and the pure function never implemented it.
2. `_plan_authority_holds` demanded a persisted arm claim as proof of authority. The
   claim is written **by** an arm, at the write boundary, *after* the stop is decided
   in the same refresh. On the refresh a row opens — the first refresh that can arm
   anything — the proof it asked for cannot exist. Measured: `record_present: false`,
   `plan_authority_holds: false`, a `stage_a_hold` terminal against a 4.53 kWh Sell
   with `remaining_battery_kwh: 4.827`.

No reset followed only because `ownership_of` answers `none` while the dispatch is
still inactive. With the marker already on — the second row, or a back-to-back
campaign — the same path would have torn the campaign down.

### Fixed: an opened frozen row has execution authority

`carry_forward` takes `row_open` and keeps a run whose row has opened rather than
withdrawing it for want of an affirming publication. The flag is read from the *frozen
schedule* by the caller, never from the run's own window, so a run whose schedule has
gone is not kept alive by it. Bounded by the run's own `window_end`, the plan's
`ends_at`, a row covering this instant, the abandonment latch and the vendor dead-man
— and placed *after* the window test, so it can never outlive the work it protects.

It suppresses absence and nothing else. Safety, a lost marker, a foreign claim, a
stalled dead-man, a failed write, the user's own switch and an unknown quarter after a
restart all reach the run by other paths and are untouched.

### Fixed: authority no longer asks for proof it cannot have yet

The claim test inverts from *"this schedule has armed something"* to *"nothing has
been armed under a different authority"*. A foreign claim still refuses. A plan with
no claim can only ever *withhold* a withdrawal — `resetting` requires `owned`, which
requires `record_matches` — so an authority proven this way can never itself stop a
dispatch.

Two drafts got this wrong in the same way one refresh apart, and both are recorded at
the predicate: a first asked `_campaign_started_at is not None`, which is wrong by one
refresh because the campaign lifecycle advances at the *end* of the report; `beta.29`
replaced it with the persisted claim, which is wrong by one refresh in a worse place.

### Fixed: `execution.lifecycle.state` is no longer dead

It had zero callers anywhere in the package, so it read `idle` permanently and the
other eleven states were unreachable — including on refreshes where the battery was
executing. It is now a hazard-first projection of booleans the report already
computes, and the projection is proven **exhaustively over its whole input space**: no
combination in which anything is owned can produce `idle`.

### Fixed: the campaign start-freeze is one transition with the physical start

`open_campaign.started` read `false` and `frozen_target_kwh` `null` on the refresh that
armed the hardware, because the freeze ran one refresh later. Both are now set where
the activation write lands, idempotently, and no refresh reporting an armed dispatch
may report an unstarted campaign.

### Fixed: the 60-second tick stops an orphan instead of returning silently

It returned `skipped_no_quarter` before it looked at whether a dispatch was active, so
an armed dispatch whose plan had been nulled got no stop from the physical cadence for
up to a refresh interval. Ownership and activity are read first, and an owned active
dispatch with no authority is routed to the existing abort teardown as
`stopped_orphan_dispatch`.

### Fixed: the opening position value is no longer null

The parameter existed and the caller simply never supplied it, which was the sole
cause of both nulls. Both ends of the position are now valued on **one curve — this
refresh's** — and each at its own reported energy, so

```
realised_plus_remaining = realised_net + closing_inventory − opening_inventory
```

reconciles and carries no revaluation. Both figures are rounded like every other euro
the ledger publishes; the closing value previously escaped as a raw 15-decimal float.

### Deliberately not changed

- **A restart still stops a started run.** The frozen schedule and the quarter's
  measured progress are not persisted, so a restart cannot know how much of the row is
  already delivered and continuing would execute against an unknown remainder. No
  *"started, therefore immune"* flag was added: it would survive while the schedule it
  refers to would not.
- **No schema version moves.** Storage stays at 2/6, config entry at 2, the ownership
  claim at 2, forecast storage at 8. No new persisted field is required by any of the
  above, and that is a finding rather than a convenience.
- **Safety Buy is untouched and still price-blind.** Proven on a horizon that actually
  issues one, at 2 c/kWh and at 90 c/kWh.
- **No cross-quarter catch-up.** Keeping a run alive is not permission to make up a
  shortfall; a missed quarter stays missed.
- **No new ledger basis word, no new sensor, no forecast revaluation term.** An honest
  revaluation needs the opening value *as it was believed then* — a new persisted key
  and a new basis name. Both are additive and neither is a one-line gap; shipping them
  under time pressure from a live defect is how the wrong basis gets frozen into a
  name. Deferred.
- **`_terminal_value`'s horizon offset and `ForecastRisk.today_interval_count`** remain
  as they were. The first reaches a priced quantity and fixing it would change the DP
  objective; the live evidence implicates neither.

### Verified

4395 automated tests. Four mutation harnesses report **zero survivors and zero lost
anchors**: `beta.38` (44 mutations), `beta.37` (43), `beta.36` (35), `beta.35` (19).

Every `beta.38` scenario is replayed in **both** economic directions, because a Sell's
objective is metered export with battery discharge as a ceiling while a Buy's is
battery charge with grid import as a ceiling — the lifecycle is shared, the objectives
are not, and free production must keep paying toward a Buy.

Three `beta.35`/`beta.36` mutations stopped failing because this release added a guard
*in front of* the layer they attack. None was retired or weakened: two were re-pointed
at the layer that now decides, one at a witness that constructs the state directly.

**Hardware validation is outstanding.** The next supervised Live day must show, at the
refresh a frozen row opens: `ended_reason: null`, `plan_authority_holds: true`,
`started: true` with `frozen_target_kwh` populated on that same refresh, and
`lifecycle.state` walking `admitted → starting → executing` — never `idle` with the
dispatch armed.

## [1.0.0-beta.37] - 2026-09-01

**Economic Value observability.** `beta.36` made the execution lifecycle correct;
this release makes the optimiser's reasoning *inspectable*. One new sensor answers
the two questions a supervised Live day actually turns on — *is retaining one more
kilowatt-hour worth more than exporting it now?* and *is the selected plan better
than doing nothing, and by how much?* — and nothing about it can move a decision.

Most of the machinery already existed and was simply not on a dashboard. `beta.35`
computed the marginal value of stored energy and retained the head layer of the value
table on every plan; the plan-versus-passive comparator has existed since `beta.16`;
a bounded decision ledger since `beta.31`; a month-partitioned evidence store since
Phase 2. So this is largely a surfacing release, plus two corrections the work turned
up.

### Added: one sensor, `Economic Value`

Its **state** is the expected advantage of the selected plan over the passive
counterfactual, over the horizon currently known, in EUR:

```
state = desired.hold_cost_eur - desired.cost_eur
```

`device_class: monetary`, and **no state class** — it is a forecast over a horizon
that shortens through the day, so a long-term statistic would average a moving
definition. It is not cash profit, not the plan's cost, not `objective_eur`, not
`expected_net_value_eur`, and not realised money.

**`unknown` and `0.00` are different answers, and the distinction is load-bearing.**
`unknown` means no valid comparison could be formed — no plan, an empty horizon, no
actionable interval, or a reserve violation, which under the lexicographic objective
means no monetary alternative was ever ranked. `0.00` means a *valid* comparison that
came out equal, which is a real result. Neither is allowed to render as the other.

**Missing tomorrow prices do not make it unavailable.** Before the day-ahead auction
publishes there is still a horizon and still a comparison; `tomorrow_prices_known`
goes `false` and the tomorrow-specific figures go null, and the headline stays a
number. A sensor that went `unknown` for half of every day would be useless.

Its attributes carry the audit trail: the retention-side marginal value of stored
energy with both one-sided slopes and a kink flag beside it, the current import and
export price for context, the terminal edge value, the plan's cost decomposition with
`model_terms_are_cash: false`, the plan-level expected energies, and a per-civil-day
breakdown. One entity, because two economic sensors could disagree.

### Added: the retention-side marginal value of stored energy

`stored_energy_marginal_value_eur_kwh` is `(V(b-1) - V(b)) / bucket_kwh` — what
*keeping* the energy already in the pack is worth. The retention side, because the
alternative to holding is giving energy up; the upward difference is published beside
it as a diagnostic and is a different number at every kink. Read from the row of the
value table matching the run state Stage B reports as physically running, which
`beta.36` is what made truthful.

Absent with a reason rather than zero, in five named cases: no curve, the top bucket
whose width is clamped, an unreachable state, two buckets that disagree about reserve
feasibility, and — new in `beta.37` — the bottom bucket, which has no lower side.
Publishing `0.00 EUR/kWh` for any of them would read as "this energy is worthless",
which is a claim, and the wrong one.

### Added: a per-civil-day decomposition, on a basis that says so

`today_interval_value_eur` and `tomorrow_interval_value_eur`, plus per-day import
cost, export revenue, avoided import, switching fee and energies. Every one of those
is exactly additive, because `cost_eur` is built as a sum over the same intervals.

**They are named with `interval` in them because they are on a different basis from
the state and do not sum to it.** The state is the plan measured against one
whole-horizon ambient walk, which has no per-interval series and whose trajectory
depends on the whole horizon; these are each interval measured against its own
leave-the-battery-alone baseline. A test asserts the non-equality, so a future tidy-up
cannot silently force an identity the mathematics forbids. The terminal credit is a
boundary term and is published once, unsplit, belonging to neither day.

The civil-day boundary is the day's own length — 92, 96 or 100 — and this is the first
economic figure whose correctness depends on it, so the shape fixture stopped
hardcoding 96 and all three lengths are tested.

### Fixed: the passive comparator was priced under the wrong model

`hold_cost()` never received `ambient_self_consumption`, whose default is `False`,
while every plan it is compared against *is* solved with it on an installation whose
inverter reports that it serves house load from the battery unbidden. So the baseline
never discharged to the house while the plan modelled that it did, and the reported
advantage credited the battery for a saving the inverter would have delivered while
idle. That is the "battery power exactly zero" baseline, and it is not this product's
concept of doing nothing economically active.

Measured on the reference horizon: `hold_cost_eur` **3.7125 → 3.1941**, and the
advantage with it.

**`expected_net_value_eur` and the new sensor therefore read lower on affected
installations.** That is the correction, not a regression. `cost_eur`, the objective,
every action, both energy trajectories, the campaign boundaries and the execution
targets are unchanged — proven by solving the same horizon through a reconstructed
`beta.36` path and comparing structurally.

One residual is recorded and deliberately not fixed: `hold_cost` passes
`permitted=_ALL_ACTIONS` including `curtail`, which matches `desired` — the plan the
sensor reads — but not `capability`.

### Fixed: a publish-only bucket lookup was off by one

Both readers of the value curve converted kWh to a bucket index with a bare
`int(energy / bucket_kwh)`. `start_energy_dc_kwh` is the float product
`n * bucket_kwh`, which need not divide cleanly, so an exact multiple floors to
`n - 1` for roughly four per cent of bucket sizes in the live 0.15–0.40 band. When it
happened, the published marginal value was the slope of the neighbouring interval and
`stored_value_eur` was short by one bucket's worth. Both sites now use an
epsilon-carrying helper, and a regression pins a pair that exposes the bug.

Publish-only: neither site is read by any decision path.

### Added: thirty days of decision-time economic evidence

Two tiers, both existing architecture, no new persistence framework:

- the **hot ring** — `LearningStore.decisions`, per refresh, still bounded at 192
  records. Each record simply grows by the economic scalars, prefixed `ev_`. **No
  schema version moves**, because that record has always been a free-form dict;
- the **thirty-day evidence** — `EconomicSnapshot` in the month-partitioned
  forecast-history store, written change-triggered on the input fingerprint. That is
  what makes a month affordable: a quiet day costs one row rather than ninety-six
  near-identical ones, and Home Assistant never rewrites a monolithic document on the
  sixty-second debounce.

This is the decision-time half of a comparison a future release can complete against
measured outcomes, and it is the half that cannot be recovered afterwards: prices are
revised, forecasts are replaced, the pack moves. **Nothing learns from it, and nothing
reads it back into a decision.** Linkage to the realised ledger is by
`(day, price_fingerprint)`, both of which the record already carried.

### Unchanged, deliberately

**Zero additional dynamic-programming solves.** Every figure is read from the outcome
the refresh already produced, and `solve_count` is asserted rather than argued. No
economics moved: the minimum trade gain, the grid-charge margin, the throughput cost,
the configured minimum state of charge, the Safety Buy's meaning, the export gate, the
reserve and the terminal-value policy are exactly as they were. No user-configurable
setting was added. `realized.py` is untouched.

The entity id remains **derived from the config-entry title**, as every entity here
always has been — `sensor.alpha_ems_economic_value` on an installation titled "Alpha
EMS", `sensor.alpha_ems_manager_economic_value` on one titled "Alpha EMS Manager".
A parametrised regression pins the derivation for both, and no existing entity id
changed.

### Not implemented, on purpose

`replacement_cost_eur_kwh` is **not published.** The only candidate figure is
`edge_value_eur_per_kwh`, which exists to price the *horizon edge* and already seeds
the terminal credit into the value table — so it is inside the marginal stored-energy
value already, and publishing it as an acquisition price would both double-count it
and present a boundary parameter as something a person could go and buy at. It is
published under its own name, `terminal_edge_value_eur_kwh`. No replacement-cost
number was manufactured.

`next_refill_price_eur_kwh` is not published either: "next refill" names no quantity
in the optimiser. In its place, `next_planned_charge_price_eur_kwh` — the average
import price of the next planned charge run, or null when the plan has none.

Per-day grid-charge cost is not published: it needs a per-interval field that does not
exist, and approximating it would be a different number wearing the same name.

### Found, not fixed

Two frame confusions between an absolute interval index and a horizon position, both
recorded with file and line so the next release starts from evidence. Neither is fixed
here, because fixing either would change the dynamic programme's objective and this
release must be decision-neutral.

1. **`_terminal_value` reads a clock-matched price at the wrong offset.** It computes
   `clock = demand.index % today_interval_count` and reads `prices[clock]`, but the
   price series is positionally aligned with a horizon whose head is `elapsed + 1`, so
   it reads the price of `clock + elapsed + 1`. **This reaches a priced quantity** —
   the terminal credit — and so is a candidate economic defect for its own release.
2. **`ForecastRisk.today_interval_count` is a whole-day count compared against a
   horizon position.** Lower severity: it affects only `upper_net_demand_curve`, which
   is protection-only and documented as never entering a priced quantity.

### Verified

4320 automated tests. Three mutation harnesses break each invariant on purpose and
require a named test to notice — 43 mutations for this release, 35 for `beta.36` and
19 for `beta.35`, all killed, including two that break the test *fixture* to prove its
own vacuity gates and five that check the sensor cannot lie about missing data.

Nine of the 43 survived their first run. Every one was a vacuous test rather than a
weak mutation: comparisons made at the wrong tolerance, edge states chosen where the
two candidate answers happen to coincide, and node ids pointing at the wrong test.
All nine were fixed by strengthening the test, which is what the harness is for.

## [1.0.0-beta.36] - 2026-09-01

**The release that stopped a quarter's success from destroying its campaign.** On
two consecutive days a Live charge campaign was terminated hours before its window
ended, and on neither day was anything wrong with the economics, the plant, the
ownership claim or the dead-man timer. Both times the campaign was destroyed by its
own execution layer, and both times the trigger was an ending that was not a
failure.

**2026-08-30 — a quarter reaching its target.** At 06:59:26Z a row met its own
battery objective, which is a success, and `beta.35` routed that success through the
atomic abort helper. The campaign identity was latched into `_abandoned_campaigns`;
`campaign_identity` is a digest of the campaign's *end*, so it is byte-identical
across every republication of one live campaign, and `_refresh_executing_quarter`
therefore nulled the frozen schedule on **every refresh for the next five and a half
hours**. The charge ran on the degraded run-level fallback: no admitted quarter, no
campaign, no per-row grid ceiling, no completed-quarter record. At 12:45:06Z a
`stage_a_hold` arrived — and `_plan_authority_holds` returns `False` whenever the
plan is gone, so `beta.35`'s own withdrawal suppression was structurally unreachable
and the run was reset with **9.889 of 16.11 kWh unrealised**. The terminal reported
`0.27 / 16.74 kWh`, `quarters_admitted: 2` against three rows, and Activity rendered
`Finished — Partial`.

**2026-08-31 — a quarter resting.** Campaign `1be3a9699b41dab1` was terminated at
09:00:06Z with `reason: safety`, at the refresh where its *fourth* row opened.
Nothing was unsafe. Production was covering the house and the row's grid budget was
70–96 % spent, so `decide_charge` clamped the authorised rate to zero — a correct
clamp — `quarter_intent_for` returned nothing for a row that was open, owned and
armed, `safety.evaluate(None, …)` reported unsafe by construction, and
`unsafe_while_owned` promoted that to `EXECUTION_STOP_SAFETY`: the one member of the
abort family that may never be suppressed. Then the same latch, and a zombie loop on
top of it — the run layer minted a fresh run from a target still naming the dead
campaign every fifteen minutes while the plan layer destroyed that run's plan on the
same refresh.

**`beta.36`'s premise is that a row ending, a run ending and a campaign ending are
three different events.** Through `beta.35` they shared one teardown.

### Fixed: the grid ceiling bounds the grid, not the battery

**Found by measuring the hardware, and it is the most consequential fix in the
release.** `decide_charge` applied the grid authorisation **twice**: once correctly,
added to the production surplus to give the battery's cap, and again through the
physical clamp list as a bare bound on **battery power**. So once the grid budget was
spent the second application pulled the battery to zero however much free production
was standing there — and the function's own `desired_grid_kw` said so, in watts.

Its docstring had promised the opposite since `beta.27`:

> **Free production is still absorbed once the grid budget is spent**: the cap falls
> to the surplus alone, and the charge continues under the battery, headroom and
> reserve limits rather than pushing production to the meter.

Reproduced exactly, at the arithmetic level: an unfinished 0.56 kWh charge row with
its grid budget 98 % spent, PV 2.8 kW against a 1.5 kW house — 1.3 kW of free
surplus. `applied_kw` came out `0.000` and `desired_grid_kw` came out **−1.240**. The
controller was predicting that it would export production it had been asked to store.
The same row now commands the surplus and imports `0.060 kW` — exactly the
authorisation that remained, and not a watt more.

**The ceiling is not weakened; it is honoured in its own domain.** A charge at the
production surplus needs no grid at all, so the fix cannot buy energy the plan did not
authorise, and the accrual attributes production before grid so a PV-sourced charge
never spends authorisation it did not use. The run-level authorisation and its
downward revision are both kept, folded into the grid term where they belong. The
published clamp token is unchanged.

`decide_export` and the run-level `decide_setpoint` fallback are untouched.

### Changed: the 0 kW hold is for a satisfied row, and says why

Hardware measurement, 2026-09-01, with the helpers driven by hand: dispatch on, mode
2, command `0.0 kW`, SoC ~75 %, PV 2.8 kW, house 1.5 kW → **battery power exactly
0 W**, and the 1.3 kW surplus exported.

**Mode 2 at 0 kW is a total hold.** It suppresses battery *charging* as well as
discharging; it does not merely withhold commanded grid charging. That makes it
exactly right for a row whose frozen objective is already met — it is the only
command on this surface that cannot overshoot one, and there is no "charge from PV
only" primitive among the modes this release may command — and it would be
indefensible on an unfinished row. Asking why the controller ever wanted 0 kW on an
unfinished row is what found the domain error above.

After that correction, a sub-resolution rate implies there is genuinely under one
commandable step of production and grid authorisation combined, so the hold gives up
at most `CONTROL_MIN_POWER_KW × 60 s` — about 3 Wh, an eighth of an actuator step —
before the next tick re-evaluates. That is the case `const.py` has documented as
covered by the inverter's own behaviour since `beta.24`.

**Mode 2 is never released mid-row, and that is deliberate.** Ownership in this design
is *defined* by a running dispatch: `ownership_of` answers `none` the instant
`dispatch_active` is false, and a marker on with nothing behind it is by definition
stale and gets released. Pausing the dispatch to let the inverter fall back to its own
behaviour would mean surrendering the claim, the per-row grid ceiling and the frozen
objective, then re-acquiring them by claiming the marker again from the sixty-second
tick. Correcting the domain error keeps the battery under command for the whole row
instead — so same-row recovery needs no re-arm, and natural fallback discharge, whose
interaction with a frozen charge objective could not have been guaranteed, never
arises.

### Fixed: a row that meets its objective rests instead of ending the campaign

`_async_end_quarter` stopped the dispatch with no "does a further executable row
follow?" test, and the stop it performed was the total abort. A satisfied row now
**holds at 0 kW**: ownership, the claim, the frozen schedule and the campaign
instance all stay, and the next boundary transitions straight into the next frozen
row.

The hold had three traps and all three are closed. It bypasses `_dispatch_setpoint`,
because `_finish` substitutes the *held* value for any move smaller than
`DISPATCH_POWER_DEADBAND_KW` — two whole actuator steps — so a row satisfied while
sitting at 0.1 kW would have kept drawing 0.1 kW with `within_deadband` printed
beside it. It is **not** spelled `ACTION_HOLD`, because `_cutoff_for` gives a
non-charge action the discharge *floor*, which would write "stop at 21 %" to a pack
at 61 % — the `beta.19` inversion. And it keeps re-arming the vendor dead-man, whose
expiry raises `EXECUTION_STOP_TIMER_NOT_REFRESHED`, an abort reason: a rest that
stopped re-arming would be a stop with extra steps.

### Fixed: a rate below the actuator's resolution is a rest, not a safety abort

The 2026-08-31 path. Anything inside `CONTROL_MIN_POWER_KW` — the same two-step
figure `safety` uses for `power_below_device_minimum` and `limit_command` uses for
its floor — now rests at zero and **recovers inside its own row** the moment the
clamp lifts. Nothing about the row, the plan, the campaign or the claim is torn down
to achieve it.

### Fixed: the stop vocabulary is a partition, and the inhibit vocabulary has classes

Seven stop reasons belonged to none of the three published vocabularies, and
`_decide` sets `reset_required` for four of them — `target_reached`,
`battery_ceiling`, `grid_ceiling`, `headroom_reached` — all ordinary *successful*
endings. Every one reached the total-teardown helper and latched its campaign.
`EXECUTION_COMPLETION_STOP_REASONS` already existed and was read in exactly one
place: the outcome verdict, never the teardown path. Every `EXECUTION_STOP_*` reason
is now in exactly one of withdrawal / completion / abort, asserted structurally.

The inhibit vocabulary gains the same treatment, by closed enumeration with a
**hazard default**: withdrawal and no-command are listed, hazard is everything else,
so an inhibit added in a later release is a hazard until somebody argues otherwise
in a diff. Nothing in the hazard list is weakened — a stale sensor, a lost marker, a
contested dispatch, an out-of-range cutoff and a would-export all still abort, still
unsuppressably. A campaign terminal reporting `reason: safety` must now have a
matching hazard inhibit from the same refresh.

### Fixed: Stage A publishing no plan is a withdrawal, not a hazard

`INHIBIT_NO_PLAN` had two producers at the same ladder position and they were
indistinguishable — one meaning "Stage A published nothing", a statement about the
future, and one meaning "there is nothing to send at this instant", a statement about
now. They are now `no_plan` (the published string is unchanged, so existing
automations are unaffected) and `nothing_to_command`. The first is withheld while a
frozen plan still covers this instant, bounded exactly as every other withdrawal is;
the second is a rest.

### Fixed: the lifecycle is keyed on the attempt, not on the identity

"No campaign reopens after its terminal" was written at the level of the campaign
*identity*, and that is what barred both campaigns for a whole session. It is now
per **instance**, with an immutable `campaign_instance_id` minted exactly once when
an attempt opens and never recomputed, and the asymmetry is deliberate:

- after a genuine **abort**, a new admission of the same campaign may open a **new
  instance** — with its own frozen objective and its own zeroed accounting, because
  it genuinely is a second physical attempt, and the first attempt's measured energy
  is never touched;
- after a **completion** the economic campaign is final, and Stage A continuing to
  publish it — its horizon still contains it — may never open another instance.

What an abort latches is the **admission**, so the attempt that went wrong can never
re-arm while the intention behind it stays available. `carry_forward` reads the same
latch, so the run layer and the plan layer can no longer disagree about whether an
attempt is dead.

### Fixed: the terminal counts the quarter it closed on

`_async_end_quarter` stopped the dispatch first, and the physical stop reaches
`_close_campaign`, which nulled `_campaign_id`; only *then* was the row recorded, so
the accrual returned early on its campaign-identity guard and the quarter that
**caused** the terminal was missing from the total. `_close_campaign` also read the
realised figure after nulling the identity, so the open-quarter term it promised to
include was structurally always `0.0` — while the live `open_campaign` figure beside
it used the same helper with the campaign open and *did* include it. The two
published figures disagreed by exactly the closing quarter, provable from the public
payload alone.

The accrual now happens before the stop, guarded to fire exactly once per row
because three separate sites can record a completed row and nothing said they were
mutually exclusive. The ledger identities are asserted as **equalities**.

### Fixed: a campaign Stage A is still publishing is not over

A run reaching its `window_end`, and a withdrawal standing once the plan's authority
is genuinely spent, both left the dispatch finished and the campaign running — which
is the ordinary shape of a campaign split by a `serve_load` interval into two
published runs, and the shape the 2026-08-30 campaign had. Closing it anyway filed a
terminal mid-campaign. One predicate now owns that question and both layers read it.

### Fixed: the head run state no longer lies about the physics

`_head_run_state` read the admitted row and nothing else, so with the schedule gone
it reported `IDLE` while the inverter was demonstrably charging under a live claim —
and every Stage-A solve paid a fresh run-start fee to continue a charge it was
already running, silently reverting `beta.35`'s own stored-value correction. The
carried run is now the fallback, and it is a fact of the same kind. A genuinely
torn-down execution still seeds `IDLE`.

### Added: what a row actually attempted

The 0.56 kWh row of 2026-08-30 was admitted, derived, ticked against fifteen times
and moved nothing, and its whole published trace was
`binding_clamps: ["quarter_expired"]` — which is also exactly what a mid-row teardown
writes. No tick reason, authorisation refusal or write-boundary refusal could reach
that record. Completed rows now publish `armed`, `arm_attempts`, `write_count`,
`hold_writes` and `refusals`, kept apart from `binding_clamps` on purpose: a clamp
reduced a command that was *sent*, a refusal means none was.

**No claim is made about what happened on that row.** It is not determinable from the
capture and it is not guessed; what changed is that the next occurrence names itself.

### Added: why there is no admitted plan

`carry_plan` had eight refusal clauses and reported none of them, so for an incident
whose whole shape is "no admitted plan for five and a half hours" the payload said
only `admitted_plan: null`. A new `admission` block names the clause that refused,
the admission key, the campaign instance, and the same question one layer down for
the carried run — and it is never silent while there is no plan.

### Unchanged, deliberately

No economics moved. `minimum_trade_gain_eur`, `grid_charge_margin`, the throughput
cost, the configured minimum state of charge, the Safety Buy's meaning, the export
gate, the terminal-value policy, the forecast horizon and the completion tolerance
are all exactly as they were. Missed energy is still never carried into another
quarter. The three shortfalls of 2026-08-31 are physically explained by an exhausted
grid ceiling with production helping, which is the documented regime, and nothing was
changed to flatter them. No schema version moved.

### Verified

4251 automated tests, and two mutation harnesses that break each invariant on purpose
and require a named test to notice: 35 mutations for this release and 19 for
`beta.35`, all killed, including two that break the *fixture* to prove its own
vacuity gates and six that protect the hardware contract above. The offline economic re-solve the plan called for is **not** included:
it needs the installation's persisted price history, which is in neither diagnostics
capture, and inventing prices to report euros would be the exact defect this suite
exists to distrust. What could be proven without it — the capture's internal
contradiction, the loss arithmetic, the run-start fee distortion and the dominance
properties — is proven and labelled, and what could not is declared as not
recoverable.

**The 0 kW hold is measured, not assumed.** See the release notes for what the
measurement showed and what it changed.

## [1.0.0-beta.35] - 2026-08-29

**The release that kept a campaign alive across its own boundary.** `beta.34` put
the first real economic Sell on the reference inverter, and quarter one worked
exactly as designed: 10.05 kW battery discharge, 8.7–8.9 kW meter export, P1
genuinely negative, ownership `owned`, dead-man armed, **2.211 kWh** measured out
of the pack and **1.92 kWh** across the meter.

Quarter two did not happen, and then quarter three came back from the dead.

At 20:00:05.889489 the refresh adopted the persisted ownership claim, read it as
`stale_plan`, and reset the dispatch. Through the whole of quarter two the
controller went on *describing* the campaign — same `plan_id`, same `run_id`, the
same 2.50 kWh / 2.28 kWh targets — while its own ticks reported
`dispatch_not_active` and **≈0.001 kWh** crossed the meter against 2.28 planned.
At 20:15 the still-live frozen schedule advanced to its third row and **re-armed
the inverter**. The logbook recorded the whole thing as
`Canceled — Plan Replaced — 0.00 / 5.05 kWh`.

So the defect was never "the campaign ended early". **Lifecycle and authority
disagreed**: the run had been declared stale and its claim released, while the
frozen schedule retained enough authority to skip a quarter and then restart the
same campaign. `beta.35` makes that state unrepresentable rather than unlikely.

Beside it, the second half of the release: the optimiser now knows what the energy
it is holding is worth, and says so.

### Fixed: the ownership claim expired at the boundary it was made for

A `beta.34` regression, introduced by the `beta.34` ownership fix. A
quarter-authority arm persisted `stale_after = quarter.quarter_end` — reasoning
correctly about the *arm*, which is reissued every quarter, and wrongly about the
*claim*, which has to survive the boundary for the next refresh to adopt it and
hand over.

The arithmetic closes exactly. The refresh fires a few seconds after the row ends
against a deadline that **is** the instant it was triggered by; `carry_forward`
hits its staleness guard and the dispatch resets. Structural, not jitter: every
such claim was already expired by the time anything read it.

The claim is now bounded by `plan.ends_at` — frozen at admission, immutable
afterwards, and exactly how long that authority legitimately lasts.

### Fixed: an opened frozen row was aborted because Stage A revised the future

`carry_plan` has stated the rule since `beta.29` and states it correctly: **an
opened plan is returned unchanged** until its own end, because Stage A's horizon
head is `elapsed + 1` and no publication issued after a row opened can describe
it. `AdmittedPlan` has no staleness field at all. The stop path never honoured
that rule, so a revision of the *future* aborted a row that was already frozen and
already running.

Two named vocabularies now separate the two kinds of stop:

- **Withdrawal** — `stale_plan`, `stage_a_hold`, `plan_replaced`. Stage A has
  changed its mind about what comes next. Suppressed while an opened frozen
  schedule still covers this instant, and published as `withheld_stop_reason` so a
  reset that does not happen is still readable.
- **Abort** — safety, an ownership conflict, a lost marker, a stalled dead-man, a
  failed command, the user's own switch, and a genuinely lost measurement.
  **Never suppressed.**

The suppression is bounded three ways and cannot become indefinite execution: the
plan's own `ends_at`, the row covering this instant, and the vendor dead-man, which
is re-armed only while the sustain actually runs.

Three further disagreements between the arm, the claim and the sustain are closed
with it. The sustain compared against the carried run alone — `None` on every
ordinary quarter-authority refresh — so continuation was unreachable by the path
meant to reach it; all three now read one identity helper. Adopting a persisted
claim set `quarter_progress_unknown` unconditionally, which forces a reset with no
stop reason at all; it is now set only where continuity is genuinely absent, which
is a restart and not a boundary. And an affirming publication present in the same
refresh is read **before** the deadline is judged: the deadline detects Stage A
having gone quiet, and a publication in hand is proof it has not.

### Fixed: the abort was partial, so a terminated schedule re-armed the inverter

The post-reset cleanup cleared the record, the dead-man observation and the row,
and argued the rule correctly for the row: *its authority came from a dispatch that
is no longer running*. The same sentence is true of the schedule the row came from,
and it was never applied. `self._plan` survived every reset — which is why quarter
three came back.

There is now **one teardown**, `_abandon_execution`, and every genuine abort path
converges on it: the refresh reset, the sixty-second tick's stop, the emergency
self-stop, and — new in this release — the refresh's *own* emergency stop, which
turned the dispatch off and tore nothing down, so the next row armed it again. It
clears the record, the dead-man, the row, the carried run, the admitted plan and
the campaign; files exactly one terminal; and remembers the abandoned campaign so
no later row of that schedule may re-arm and no closed identity may reopen.

Realised history is immutable from the moment a campaign starts. Its identity, its
frozen target and its measured energy survive every replan, and a campaign closes
exactly once.

### Fixed: a campaign that moved 1.92 kWh reported 0.00 against a null target

Three independent faults compounded into one line in the logbook.

The frozen target was read from `execution_targets` — *this* refresh's solve, whose
head is `elapsed + 1`. A campaign whose remaining rows are all behind that head
appears in no published target at all, so every read returned `None` and the freeze
had nothing to freeze. The objective now comes from the **admitted schedule**,
which was frozen before any of it happened.

The freeze itself is also spelled `X if X is not None else Y` rather than
`X or Y`. That one is a latch and not a fix — today the two agree in every
reachable state — but "this campaign sells nothing" and "nobody published it" are
different answers, and the difference is one refactor away from mattering.

Run-level realised energy was accumulated charge-only, in both bases: a power
accumulator clamped at `max(0.0, power)` and a state-of-charge delta clamped at
`max(0.0, stored - opening)`. A `net_export` campaign integrates to **exactly
zero** on both — so every export campaign this project has ever run reported zero
realised energy at run level. Both bases are now signed by direction.

And `sensor.py` read `progress.get("grid_export_realized_kwh")`, a key written
nowhere in the package — one hit in the whole tree, and it was the reader. The
execution payload now publishes `objective_realized_kwh` and `objective_boundary`,
and a **structural test** walks every reader module's payload reads against a
payload the production path actually produced. That defect class has shipped three
times; it will not ship a fourth.

Finally, Activity no longer retracts a campaign that has started because a *future*
plan moved. Only the campaign's own terminal can close it, which is what makes
"exactly once" true rather than hoped for.

### Fixed: both public entities described the wrong run

`Economic Action` asked Stage A what was happening now. Stage A structurally cannot
answer: its head is the *next* interval and never the one in progress, so a real
owned 10 kW export read `idle`. It now reads the execution surface — the intent of
the admitted open row, whether it is owned, under which mode, for which campaign —
and its attributes describe the same execution its state does. In **Shadow** it
shows the intent that *would* execute, marked `owned: false`: a mode that exists to
be watched cannot read `idle` all day.

`Next Planned Action` took the first run *after* the head and therefore skipped the
run **at** the head — which has not started either, and is precisely the next
planned action. That is why it read `charge` while a Sell stood fifteen minutes
away. It now takes the nearest run at or after the head, excluding the campaign
already executing, and prints full ISO instants: a bare `19:45–20:30` is how a sale
planned for the following evening came to be read as one starting within the hour.

### Changed: the horizon's edge is priced by what the energy will actually do

Through `beta.34` the terminal credit was `v_edge × min(E, cap)` — one flat rate,
where `v_edge` was the 25th percentile of known import prices. That is a
replacement cost sampled from the wrong distribution: energy left in the pack at
the horizon's end is consumed by the household overnight and next morning, not at
the cheapest quartile of the whole day. The consequence was visible in one number
— the chosen plan ended at `end_energy_dc_kwh: 0.00`, having exported 14.22 kWh on
its last evening. The optimiser was liquidating the pack because it had been told
the pack was nearly worthless.

The replacement asks the question properly. Energy the household will consume
before the pack next refills for free is worth the import price it displaces;
whatever is left over is worth what it could be sold for. Two segments, so the
function is **concave** — the first kilowatt-hour above the floor is worth the most
and the marginal worth falls as the pack fills. That shape is what stops a fix for
liquidation becoming a licence to hoard: a flat rate high enough to prevent the one
would have justified buying a full pack at any price.

Every input is a quantity the planner already had. Post-horizon demand comes from
the reserve forecast, which already outlives the price horizon; it stops at the
first forecast production surplus — the next *free* refill, a physical fact
carrying no price — and is capped at one civil day. The price is the known series
read by clock position: today's 02:00 is the best available estimate of tomorrow's
02:00. **Nothing new is forecast**, and no price beyond the published horizon is
invented.

The new terminal value **binds**, and every applicable refresh also solves the same
horizon with the `beta.34` flat credit and publishes the difference in end energy,
export, import, cost and campaign count. The legacy solve reaches no execution
target; it is diagnostics, and it is skipped when the two rules are the same
arithmetic.

### Added: what the energy already in the pack is worth

The backward induction ends holding the optimal cost-to-go for every storage state
at the head of the horizon — the dual of the storage constraint, the exact marginal
worth of one more stored kilowatt-hour with all future load, prices, fees and the
reserve already accounted for. `beta.34` computed it on every refresh and threw it
away.

It is now kept and published: the marginal value at the current level, the curve
across the lattice, and `V(floor) − V(current)` — how much better off the plan is
for standing where it stands rather than at the floor. **This is the answer to "was
buying twenty kilowatt-hours and selling five a good trade", and it needs no
inventory model at all.** It prices the position, from now, at the margin.

Deliberately **not** implemented: FIFO layers, average purchase cost, and any rule
that refuses to sell below what the energy cost. Those price *sunk cost* —
`realized.py` has argued since `beta.18` why that is wrong — and a "never sell
below average buy price" veto would decline a profitable spike because of a price
paid yesterday.

Where the value is undefined it is published as `null` **with a reason**, never as
zero: buckets whose violation terms differ were never ranked on money, so their
difference prices nothing; an unreachable state carries a sentinel rather than a
cost; and the top bucket's energy interval is clamped short by the ceiling.
`0.00` would read as "this energy is worthless", which is a claim, and the wrong
one.

Zero solver cost, and **no decision path reads any of it** — pinned structurally.

### Added: a realised economic ledger, with every figure's provenance beside it

`realized.py` gains grid-charge and PV-charge attribution, battery-to-grid and
battery-to-house energy, avoided import value, a conversion-loss estimate, opening
and closing stored energy and value, realised net value, and realised-plus-remaining
value. It still imports no Home Assistant, is read by no decision path, and
`trade_profit_eur` is still `None`.

Every figure carries one of five bases, published beside it: `measured` from a
meter, `attributed` where measured energy is split by a stated per-interval rule,
`estimated` from a model constant, `planner_derived` from the optimiser's value
function, and `model_term` for the objective's hurdle rates and wear proxy —
**which are not money anybody paid and appear in no cash total.**

The attributed split is `min(import, charge)` and `min(export, discharge)` per
interval — a bound, never a claim that particular energy took a particular path,
because a battery has no physical ordering that would make one true. Both sides of
each minimum are AC: the series are state-of-charge deltas, and comparing DC
against a meter reading overstates the grid's share by the whole charging loss.

The ledger is also unbound from the civil day. A pack charged overnight and sold
the next evening is one economic position, and `realized_window(days=N)` describes
it. **No new storage**: every input is already persisted — `DayRecord` keeps the
measured series for 365 days and `PriceSnapshot` keeps the prices — so it rebuilds
itself after a restart with nothing else to remember.

### Changed: Stage B tells Stage A what is physically running

The recursion charges the minimum trade gain on every transition out of idle, and
every solve began at idle — so an export physically in flight was priced as a
**fresh run start** on every refresh, once a quarter, for a fee already paid when
the campaign began. That is the economic half of the same boundary failure: at
20:00 the plan moved the export to 21:00.

The head is now seeded with what is actually running, read from the admitted open
row and the persisted claim and from nothing else. **Stage B invents no economics
here**: it reports a physical fact — which direction the inverter is being driven
in — and Stage A remains the only thing that decides anything. Only the head is
seeded, so starting anything genuinely new still pays.

### Not changed

- **No new setting, and none removed.** `control_horizon_minutes` stays withdrawn
  and inert; the 20/25 dead-man alternation and the five-minute cleanup duration
  stay internal.
- **No storage migration.** `CONFIG_ENTRY_VERSION`, `STORAGE_VERSION` and
  `CLAIM_SCHEMA_VERSION` all stay at 2. The value written into `stale_after`
  changed; its shape did not, so a `beta.34` record still parses — it simply
  expires earlier, which is the safe direction. On the first refresh after an
  upgrade an old row-bounded claim is stopped once, and the teardown is total, so
  nothing survives for a later row to arm from.
- **The reserve stays price-blind.** No protection quantity enters the violation
  term.
- **Energy balance stays an observation.** The three tolerance constants are
  unchanged and it is still never a control gate.
- **All eight `beta.34` economic findings are preserved**, including the absence
  of a non-negativity invariant on the export gate cost — which is not forced
  positive, because it is genuinely negative in cases the sweep contains.
- **The head value curve is not asserted to be pointwise concave**, because it is
  not: a fixed switching fee inside a minimisation breaks concavity of the
  cost-to-go, and that is a property of the model rather than a defect in it. What
  is asserted is what is true — finite, non-negative, and worth materially less at
  the top of the pack than just above the floor.

### Hardware validation

**Not yet performed.** Everything above is proved against the measured 2026-08-29
trace replayed through the production refresh path, and against the full suite and
the mutation harness. The multi-quarter Sell has not been re-run on the reference
inverter under `beta.35`, and until it has, the campaign-continuity fix is
software-proved and hardware-unproved.

## [1.0.0-beta.34] - 2026-08-29

**The release that read its own diagnostics.** `beta.33` ran a full supervised
*Live* day on the reference installation, and two diagnostics captures from that
day — 13:55:46 and 14:01:55 — are the evidence for everything below. The campaign
machinery worked on hardware. Eleven distinct defects surfaced across four layers,
one of them a control-safety defect and one an economic frame error that produced
an **impossible export floor**. None was visible from the test suite, because in
every case a test existed that pinned the behaviour under a condition production
cannot produce.

The pack was never in danger and no energy was lost. What was wrong is that the
integration could not describe what it had done, and in one case could not stop
what it had started.

### Fixed: the export protection used an index where an offset was expected

`survival_window_end()` returned `campaign.start_index` — a **civil-day index**,
0–95 today and 96–191 tomorrow. `survival_curves()` consumes that value as a
**relative position** into a curve indexed from the head of the horizon. The two
branches of one function were returning quantities in different frames, and the
other branch — `actionable_intervals` — was correctly a count.

Invisible for three releases because every test horizon starts at index zero,
where an index and an offset are the same number. On the live two-day horizon at
14:00 the head was interval 57 and the next refill was interval 132: the correct
offset is 75, the published figure was **132**. A 33-hour protection window
instead of a 19-hour one.

The floor that window produces was then never clamped to the battery's capacity.
The live installation published **`export_floor_dc_kwh: 23.09` on a 21.6 kWh
pack**, and the solver applies that as a hard test against `energies[move.target]`
whose maximum *is* the ceiling. Every caused export was forbidden at every
interval for every reachable bucket. `export_free` read `[false] × 12`: not a
protection, a prohibition. The same fixture used by the minimum-SoC tests produces
a raw requirement of **32.755 kWh**, 52 % above capacity, and had been green
throughout.

Three changes, and they are deliberately separate:

- The window is **converted at the boundary**: the horizon head is passed in and
  the campaign's absolute index becomes an offset, clamped at both ends.
- The published floor is **clamped to usable capacity**, and the raw requirement
  is published beside it as `export_floor_raw_dc_kwh` so an absurd number stays
  visible as evidence without acting as a veto.
- Where the requirement genuinely cannot be met, the energy test has stopped
  discriminating — it is true for every reachable state — and the decision falls
  back to the **price** comparison alone. This cannot loosen any case where the
  floor is reachable, which the existing 48-shape sweep pins.

With the frame fixed, a two-day horizon selects **two complete cycles** — buy,
sell, buy, sell — and extending the horizon no longer changes what the plan does
today. Both are asserted.

### Fixed: a dispatch was armed that ownership could never prove was ours

At 13:30:07 on 2026-08-29 the integration armed a full Mode 2 dispatch — marker
on, mode 2, −10 kW, duration 20 — and wrote **no causal claim**. Every
sixty-second tick from 13:31 to 13:44 then read `ownership_not_owned` and declined
to write, correctly: an unproven dispatch must never be touched. The pack charged
3.14 kWh nobody had authorised and the vendor dead-man ended it at 13:50:28.

The arm path and the claim path disagreed about what an authority is. The command
was built from the **admitted plan's open row**, which is exactly what `beta.29`
designed — Stage A's head is `elapsed + 1`, so the publication made at 19:45
structurally cannot affirm the 19:45 run, the run ends and the row stays open. The
claim path required the carried run, which by then was gone.

The claim now follows whichever authority produced the command, and the admitted
plan keeps the publication it was admitted from so the claim is a full round trip.
Where there is no authority at all the sequence is **refused before anything is
written** — not even stage one — and publishes `arm_refused_reason`. Nothing about
ownership is weakened: a claim is still only a claim, and ownership still requires
the later `dispatch_start` readback to match.

Found on the way: `target_as_published()` dropped `campaign_id`, so the round trip
its own docstring asserts was false for the two fields the campaign lifecycle is
keyed on. A restart mid-campaign adopted a run whose campaign was gone.

### Fixed: a successful charge was recorded as a failure

The reference installation performed a Safety Buy that delivered **1.063 kWh
against 1.11 planned** — 4.3 % short, quarter expired normally — and Home
Assistant recorded:

> Failed Plan ID: 9d3c04 — Measurement Unavailable

Three faults stacked to produce that sentence.

**The freeze was structurally one refresh late.** `_note_campaign_progress` runs
before `_async_dispatch` sets `_activation_confirmed`, so the freeze can only fire
on the *next* refresh — by which time the published targets come from a solve
whose head cannot contain the quarter just executed. A one-quarter campaign froze
`None`. A multi-quarter one escaped, which is why 18.33 kWh froze correctly on the
same day and 1.11 did not. The objective is now captured when the campaign opens
and refreshed until it starts.

**"No target was published" was filed under "the measurement is not
trustworthy."** Those are different failures with different readers. A campaign
whose target never got published is now **Partial** with an explicit
`target_unavailable` reason, and *Measurement Unavailable* is reachable only from
a genuinely untrustworthy measurement.

**The completion tolerance was the actuator resolution.** 0.025 kWh — the smallest
command that can be issued — used to decide whether an objective was met. The
tolerance is now built from what the hardware cannot do better than (one actuator
step per quarter, plus the pack's own 1 % state-of-charge reporting resolution)
and **capped** by a proportional term, so a large campaign cannot miss materially
and still claim success. On the reference pack a one-quarter 1.11 kWh campaign
gets 0.056 kWh and a 22-quarter 18.33 kWh campaign gets 0.766 — 4.2 %, not 5 %.

And `window_ended`, the ordinary terminal of a campaign that runs to the end of
its window, was truthy and not in the failed set, so it fell through to
**Canceled**. It is now a completion.

A campaign can no longer stay open past its own end: an hour's grace after
`campaign_end` closes it even if a stale quarter is still carried.

### Fixed: the public entities described the wrong tense

**Economic Action** published `current_run or next_run` with no bound on how far
`next_run` may be. At 14:00, when tomorrow's prices arrived and the horizon grew
from 40 intervals to 135, it announced `export` for a sale planned at 20:30 the
**following** evening — and its `window` attribute renders `HH:MM–HH:MM` with no
date, so nothing in the reading revealed it. It now reports the present interval
only, and `idle` when no run occupies it. `idle` is not a synonym for `hold`:
`hold` is a verdict on the prices, `idle` is an observation about now.

**A new entity, `sensor.alpha_ems_next_planned_action`**, carries the plan that
used to be hidden inside the other one — with full ISO instants, because the run
it describes is routinely on another day, plus the energy, the price, the purpose
and the campaign it belongs to.

**Control State** was set to `executed` by the generic "the staged write did not
raise" branch, which every successful write reaches — including a stale ownership
marker release whose entire command list is one `input_boolean.turn_off`. The
English label for that value is *Executing*. So at 14:00, with the dispatch off,
the timer inactive and one boolean written, the dashboard read **Executing**. It
now publishes `executing` only while a command that moves the battery is on the
wire, and a stop or a marker release reads `idle`. `executed` keeps its place in
the enum so no dashboard loses a value, and moves to
`control.execution.result.command_result` where "did the last write succeed" is
exactly the question being asked.

`execution.power` published `applied_kw: 0.9, executed: true` on that same
refresh. It is now written only when a power step was actually on the wire.

### Fixed: the export gate reported a benefit where it cost the most

`export_gate_cost_eur` was `desired.objective_eur − ungated.objective_eur`, and
`objective_eur` subtracts the terminal edge credit. A gated plan cannot sell, so
it ends holding more energy and earns a larger credit — and the figure came out
**negative**, in contradiction of the invariant its own docstring asserted. The
live installation published −0.1338 and −1.3259. Decomposed on a reconstruction:
the gated plan spent **€1.46 more cash** and kept **7.93 kWh more**, credited
€1.74, for a published −0.28.

It is now measured on the cash the household actually moves, with the retained and
withheld energy published beside it rather than netted into it. **And it is no
longer claimed to be non-negative**: two shapes where it is negative are pinned as
tests, because a universal claim proved over a sample that happens to contain no
counterexample is not a proof.

### Added: the evidence needed to replay a decision

The 13:00 collapse of 2026-08-29 — five runs to none, €0.55 of value to −€0.54 —
could be *seen* in the decision records and not explained by them, because the
export gate's intermediate quantities lived only for the length of one refresh.
Decision records now carry a whole-horizon price fingerprint, the first two hours
of the priced series, and the survival window, floor, protection price and
per-interval permission that the gate reached its verdict from.

Alongside it, each campaign now publishes its buy cost, export revenue, avoided
import, PV-versus-grid charge split and start and end state of charge; and the
campaigns the export permission **withheld** are named, with `rejected_because`,
rather than simply being absent.

#### What the 13:00 collapse turned out to be

Reproduced on the production solver and isolated to a single variable: the
survival window has two branches, and it falls through from "protect until the
next refill" to "protect until the end of the horizon" the moment the plan stops
intending a refill. That is what happened when the morning's buy campaign
completed with tomorrow's prices not yet published, and the floor jumps from
4.32 kWh to 15.46.

**The flip is not what cost the money.** At the live stored energy the protection
withheld 0.56 kWh and cost **€0.136**. The remaining ~€0.78 is the cheap-buy
opportunity itself passing out of the horizon — an economic fact about the day,
not an artefact. The collapse was, in the main, correct, and `beta.34` does not
make the sale reappear.

### Added: energy-balance failures are attributed, not excused

The residuals are two distinct populations and neither is a fault: samples caught
mid-ramp while the setpoint moves by kilowatts in seconds, and a proportional
DC/AC boundary term that grows with power (≈41 W of excess per failed sample in
the 500–2000 W band against ≈412 W in the 2000–5000 W band). Reported as one
number they look like one problem with a slightly tight tolerance.

Each sample now carries `seconds_since_dispatch_write`,
`setpoint_delta_kw_since_previous` and a derived `regime`, and the monitor
publishes `failed_samples_by_regime` and `pass_rate_steady_state` beside the
unchanged overall rate. **The three tolerance constants are unchanged and asserted
so**, no verdict is altered, and balance remains an observation rather than a
control gate.

### Changed: the Activity vocabulary

A Stage-A withdrawal rendered as *No Longer Economically Valid*, which reads as a
verdict on the plan's worth; an ordinary re-solve is one plan replacing another,
and it now reads **Plan Superseded**. A safety or ownership stop is filed as
`stopped` rather than `cancelled`, so an ownership incident stops looking like a
change of mind in a history view — the kind has been declared since `beta.19` and
emitted by nothing.

Every Activity event now carries a **structured payload** beside the sentence:
`kind`, `outcome`, `purpose`, `campaign_id`, `run_id`, `plan_id`, `planned_kwh`,
`realised_kwh`, `started_at`, `ended_at` and `reason`. The message text is
unchanged, so automations that match on it keep working.

Five event kinds that no production path ever constructed are retired: `changed`,
`ended`, `refused`, `would_start` and `would_stop`. The last two had already been
withdrawn in behaviour — Shadow shows planning and stops there — and the constants
outlived the decision. A vocabulary a consumer subscribes to must not contain
words nothing says.

### Not changed

The reserve stays price-blind, and no protection quantity enters the violation
term. Run and campaign lifecycles stay decoupled — which is why 7.019 kWh of
realised progress survived the 13:00 withdrawal. The admitted plan still outlives
the carried run, because that is what makes a quarter boundary a lookup rather
than a hand-off. Stage B still recomputes power every quarter. Safety Buy
behaviour, the 20/25 dead-man and its re-arm cadence, `CONFIG_ENTRY_VERSION`, the
storage versions and every shipped default are exactly as `beta.33` left them. No
new setting was added; one new entity was, which needs no migration.

---

## Not yet validated on hardware

Everything in this section is proven in software and has never been watched on a
real inverter.

- **A same-day economic Sell has never executed on hardware.** Charging was
  validated in `beta.26`; every export to date has been a plan, not a meter
  reading. This is the release's primary hardware question.
- **The clamped export floor and the priced fallback have never been exercised
  live.** The over-capacity requirement that motivated them was observed on
  hardware; the fix for it was not.
- **The two-cycle two-day plan is software-only.** Four campaigns across thirty
  hours has been selected by the solver and never executed.
- **The quarter-authority ownership claim has never been written on hardware.**
  The condition that needs it — an open row whose parent run has ended — occurred
  live on 2026-08-29 and produced no claim at all, which is the defect. The fix
  runs for the first time on this release.
- **An arm refused for want of any authority has never been observed**, and as the
  pipeline currently stands cannot be: it is a guard against the two paths
  disagreeing again, and its test says so plainly rather than implying otherwise.
- **A campaign terminal with a frozen target on a one-quarter campaign** is
  software-proven only. The live example is exactly the case that failed.
- Carried forward and still outstanding: a multi-quarter export across a
  `serve_load` gap; a configured minimum state of charge at or above 50 % on a
  real pack; and the 20/25 dead-man alternation watched across four or more
  consecutive sustains with the vendor timer observed advancing each time.

## [1.0.0-beta.33] - 2026-08-29

**The release that connects what beta.32 built.** `beta.32` shipped a complete
campaign layer — the accumulator, the frozen objective, the immutability rule and
the single terminal — and shipped it **unwired**. Every published execution target
carried `campaign_id: null`, so no campaign ever opened and none of that machinery
ever ran. It was found in a live 00:02 diagnostics download, not by a test: the
campaign tests all constructed their own identities by hand and stayed green.

Alongside it, a full audit of all thirty user-configurable settings. Two were
broken in ways only certain configurations could reach, one was reachable only by
hand-editing storage, and one governed nothing at all while claiming to control the
battery. All four are resolved here.

Nothing in the optimiser's economics changed. An installation on the shipped
defaults plans exactly as `beta.32` did.

### Fixed: the campaign lifecycle was published but never connected

`execution_target()` accepted `campaign_id` and `campaign_end` from `beta.32`
onward, and `_execution_targets()` never passed them. The consequence was total
rather than partial: `_note_campaign_progress` returned early on every refresh,
no campaign was ever opened, the realised accumulator never advanced, the objective
was never frozen and no campaign terminal was ever filed.

`beta.33` resolves each run to the campaign that contains it and passes both fields
through. The identity is **derived, never minted** — `sha256(direction|campaign
end)`, truncated — and it is anchored on the campaign's **end** rather than its
start, because the head of the horizon advances every refresh while the end does
not. A restart recomputes the same identity from the same plan, so recovery needs
no stored state.

Everything the machinery was written to do now happens:

- **A multi-segment Sell is one campaign.** The reference shape from the live
  install is `net_export → serve_load → net_export`: a small export, a quarter
  where the house eats everything the pack gives it, then the large export. Three
  targets and three intents, one identity, one lifecycle held open across the gap.
- **The realised accumulator advances across the gap** instead of resetting at each
  segment boundary.
- **The objective is frozen at the first confirmed activation**, so a campaign that
  promised 2.65 kWh and delivered 1.80 because Stage A changed its mind is filed as
  Partial rather than as a retroactively successful 1.80 of 1.80.
- **A replacement plan may not shrink the frozen objective or reset progress.**
- **Exactly one terminal is filed per campaign.**

The test suite that proves this may not construct a campaign identity by hand.
Every identity is read back from the production builder running over a real solved
plan, and seven of the eight campaign tests fail against released `beta.32`.

### Fixed: a minimum state of charge at or above 50 % produced no plan at all

`build_physics_table` calibrated the charge and discharge ratios by probing the
pack at a hardcoded 50 % state of charge. On the reference installation that is a
fine probe. With a configured floor at or above 50 % the probe state *is* the
floor, the clamp reduces the discharge reading to zero, and the function returns
`None` — taking the entire optimiser with it, silently, with no diagnostics reason.
Measured: a 49 % floor built a table and a 50 % floor did not.

The probe now sits at the midpoint of floor and ceiling, which is the only point
unclamped for every legal configuration, and its power is scaled to the width of
the window so a narrow one still reads cleanly. The ratios are pure efficiency
constants and therefore magnitude-independent — verified identical at every floor
from 0 % to 99.5 % — so the scaling costs no accuracy. A floor at or above the
ceiling is now a named early return rather than a case reached by accident.

### Removed: "Command duration", because it did not control anything

`control_horizon_minutes` was offered in the Control settings as *Command duration*
/ *Duur van een opdracht*, 20 to 60 minutes. It never reached the Live Dispatch
duration register. Every writer of that register is the internal dead-man, which
alternates 20 and 25 minutes — the vendor automation triggers on the helper
*changing state*, so writing the same duration twice re-arms nothing and the run
would expire silently mid-charge. The setting only ever sized the advisory
helper-family command and a range gate.

A setting that cannot change what the battery does is worse than no setting, so it
was withdrawn rather than relabelled. The dead-man is **not** offered in its place
under any name: its value and its re-arm cadence are safety mechanics, not
preferences, and they remain automatic and unchanged.

For anyone upgrading:

- **A stored value stays where it is.** Nothing rewrites or deletes it, and saving
  the Control page carries it forward untouched.
- **A stored value is inert.** The runtime field was deleted rather than hidden, so
  an old 60 cannot be quietly honoured; an entry storing 60 and an entry that never
  had the key produce identical control intents.
- **`CONFIG_ENTRY_VERSION` does not move.** It stays at 2. The migration is a
  deliberate refusal rather than a converter, so a bump would make every existing
  entry fail to load — and withdrawing a field changes no key any entry depends on.

### Fixed: the duration diagnostics reported a setting instead of a value

Both duration fields in the download published the configured "Command duration",
which never reached the device — so a reader comparing them against the inverter
saw two numbers that could not agree. That misreading is what sent an earlier audit
down a false trail and produced a high-severity finding that did not exist.

They now report what this refresh will actually write and what the device currently
holds, side by side, with the rule stated in the payload: `deadman_duration_minutes`,
`deadman_duration_basis`, `deadman_alternation_minutes`, `deadman_rule`,
`commanded_duration_minutes` and `readback_duration_minutes`. Between runs the
readback rests at the helper's own five-minute minimum; that reading is correct and
is not a stale dead-man.

### Fixed: three published fields that stated something other than the truth

None of these changed what the battery did. All three are what a user reads when
they are trying to find out.

- **`not_executable: null` on a `serve_load` quarter row.** In this contract that
  is a positive claim — *Stage B may arm this row* — and Stage B never could:
  `serve_load` unlocks no action and admission refuses it on intent long before it
  reads a row. Those rows now name `intent_not_executable`. Rows of an intent that
  *does* have an actuator are still judged on the energy they ask for, so
  `below_actuator_resolution` is unaffected.
- **`execution_blocked_reason: "execution_unavailable"`** in the diagnostics payload
  and in every stored evidence snapshot. That is a release-level claim that no
  command reaches the battery, which stopped being true in `beta.24`. Three
  surfaces published this field and two of them hardcoded it, so they could
  disagree about the same instant. There is now one implementation, on the
  coordinator, and it reports the deepest real barrier first: the release, then the
  user's own enable, then the mode, then the action.
- **`execution_available: false`** in the capability block, one line away from it
  and equally untrue.

The package docstring made the same claim and has been corrected. It said "no
command reaches the battery", which was written when that was so and stayed
unchanged through `beta.24` and `beta.27`. What actually holds is stated instead:
three independent gates and a vendor dead-man that expires on its own.

### Added: battery wear cost per kWh, at last reachable

`battery_throughput_cost_eur_per_kwh` has been wired into every solve pass, the
diagnostics and the settings fingerprint since `beta.18`, and had no field — the
only way to set it was to hand-edit `.storage`. Its declared bounds went unenforced
for the same reason. Both are now used.

It is the only one of the three economic terms whose basis is throughput: the
minimum gain per trade is charged once per run, and the grid-charge margin only on
energy a charge actually causes to be bought. Set above zero, a plan has to earn
more than the cycling it asks for, which suppresses long shallow round trips that
clear every other test on volume alone. It is charged linearly, which under-prices
deep cycling and over-prices shallow cycling — real degradation is convex in depth
— so it is a lever, not a wear model. The default stays 0.00, so upgrading changes
nothing until it is set.

### Added: how old each source reading is

The control path refuses a source older than its window, and the diagnostics
download published each source's value without its timestamp — so `INHIBIT_SOC_STALE`
named a family without saying which member had gone quiet. Every source now carries
`last_updated`, `last_changed`, `age_seconds` and `unchanged_for_seconds`.

### Changed: internal

- The "allow sending commands" field now defaults from `DEFAULT_CONTROL_EXECUTION_ENABLED`
  rather than a literal beside it. Both read `False`; the desync would only have
  appeared the day the constant changed.
- A structural test now walks every published execution target and fails if any
  contract field is null outside a short, argued allow-list — including a test that
  fails if an allow-listed field turns out to be populated after all. This is the
  guard that would have caught the campaign wiring before it shipped.
- `_campaign_objective_kwh` counted `serve_load` battery targets into a sale's
  objective. Found only because the wiring now works: a 2.64 kWh objective was
  reported as 5.39.

### Not changed

No economics were redesigned. The objective, the reserve architecture, the
ownership model, the dead-man values and their re-arm cadence, the storage versions
and `CONFIG_ENTRY_VERSION` are all exactly as `beta.32` shipped them.

### Not yet validated on hardware

Everything below is proven in software and has never been watched on a real
inverter. This list is the honest state of the release, not a disclaimer.

- **The campaign lifecycle has never executed on hardware, and could not have.**
  It was inert in `beta.32` — no campaign ever opened — so every accumulator
  advance, every frozen objective, every immutability refusal and every campaign
  terminal runs for the first time on this release.
- **A multi-quarter export across a `serve_load` gap is not hardware-validated.**
  Live `net_export` itself has been performed on the reference installation; a sale
  held open as one campaign across a quarter where the house takes everything the
  pack gives it has been tested and not yet observed.
- **A minimum state of charge at or above 50 % is software-tested only.** The
  calibration fix is proven across every floor from 0 % to 99 %, on the model. No
  pack has been run at such a floor.
- **The 20 / 25 dead-man alternation still needs observation across real
  sustains.** The write sequence is pinned in tests and the vendor automation's
  response to it — the reason the alternation exists at all — has not been watched
  across several consecutive re-arms on the inverter.

If you run this release in *Live*, watch the grid meter through the first long
planned export, and check that the campaign files exactly one terminal when it
ends.

## [1.0.0-beta.32] - 2026-08-28

**The calm optimiser.** `beta.31` shipped the right economic architecture. `beta.32`
fixes what sits *around* it — the surfaces that describe the plan, the layer that
groups it, and the one thing standing between a discretionary export and the 20 %
floor. Every root cause was found by reading the code against a live diagnostic, and
four of the design's own formulas were wrong and were corrected by working the
numbers.

The objective is unchanged in every case where measured evidence is absent. An
installation with no rolling forecast window plans exactly as `beta.31` did.

### Fixed: a discretionary export could sell the household into a compelled purchase

The only protection between a sale and the hard floor was the **uncertainty margin**,
and on this installation that is a constant 0.42 kWh on a reachability curve that is
a flat line at `floor + margin` on every refresh. Its statistical half was
`mae × √n` with `n ≡ 0`, because `grid_credit_allowed` is `position < actionable` and
actionable is at least 1 whenever prices exist — so the term was structurally inert.

Measured: a quiet forecast exported 3.950 kWh at 0.29 €/kWh and took the pack to
**22.0 %**; reality arrived at 3.4× the forecast load and the next refresh was
compelled to buy **0.833 kWh** at whatever the market asked.

`beta.32` prices the export instead of raising the floor. An export delta that
crosses the meter beyond what the site would have spilled anyway is refused when its
price does not beat the **demand-weighted mean import price** across the window to
the refill the plan itself expects to use — and only when the pack would land below
what it needs to reach that refill. On the invariant scenario the compelled purchase
goes **0.833 → 0.000 kWh** while the minimum state of charge stays at **25.6 %**: the
pack is still spent on the house, which is the half a raised floor would have broken
(it stranded the pack at 35.4 % and cost €0.547, against €0.031 for the price).

The window is **the plan's own charge campaign**, not the first tolerable price. Every
price-only rule fails the same counter-example: with `[0.30 now, 0.24 tonight,
0.35 × n, 0.12 tomorrow]` a relative test picks tonight's mediocre 0.24, and the
household would be far better off surviving to 0.12. The circularity is cut by two
fixed passes, and the difference between them is published as
`export_gate_cost_eur` — so a protection that ever costs real money says so.

### Fixed: an export run could report success before delivering anything

**The highest-severity defect in the set.** `demand_for` compared the **battery**
remainder against `TARGET_TOLERANCE_KWH` for every intent. An export run whose
battery ceiling was at or below 0.25 kWh therefore satisfied
`remaining <= tolerance` on its *first* evaluation — before a single kilowatt-hour
crossed the meter — and reported `target_reached`, stopped and reset. Reachable on any
mid-quarter refresh while owned: a reload, a restart, a user action. The observed
run's ceiling was exactly 0.25.

The objective is at the meter; the controller's progress is battery-side, so it
cannot judge completion for an export at all. The battery figure is a **ceiling**, and
a ceiling is never a completion test.

### Fixed: three lines per label slice, for one decision

`runs_from` splits on the action *label*, and the label flips between `discharge` and
`export` whenever house load crosses the smallest representable discharge. Measured on
a realistic today+tomorrow horizon: the objective flagged **three** run-state
transitions and charged three switching fees, while the published plan carried
**fifteen** runs — with `charged_switching_fee` false on every artefact split, because
the objective never saw them.

`beta.32` adds the **campaign**: a maximal contiguous stretch of one objective run
state, which reproduces exactly the transitions the fee was charged against. Fifteen
label slices become three campaigns, and `len(campaigns) == direction_changes` by
construction — the proof that this layer changed no decision. The label slices are
still published; what changed is what gets *announced*.

Inside a campaign, three further layers are now named rather than conflated:
`segments` (contiguous same-intent, which is what the controller can be handed),
the per-quarter frozen objective, and the campaign's own meter objective. On the live
discharge campaign that means 8.750 kWh of battery, 2.648 kWh sold at the meter and
**6.102 kWh AC fed to the house** — the largest quantity in the campaign, and
previously invisible.

### Fixed: a finished export terminated in silence

Since `beta.29` the hardware is armed from the admitted quarter and stopped from the
60-second tick. **`Decision` stopped being the executor two releases ago, and the
Activity surface was still wired to it** — so the ending happened on a tick that wipes
the carriers and publishes no coordinator data, the next refresh had no intent, and
the view returned nothing. The measured 17:30–17:45 export therefore ended with no
line at all, its `Planned` announcement left standing as though still true.

The campaign outcome is now computed **where the energy was measured**, latched before
anything can be wiped, and rendered by a surface that decides nothing. Consequences:

- `Finished — Partial` exists. `beta.31` had only Success and Cancelled, so a campaign
  that delivered most of what it promised was filed as a cancellation.
- `Failed Plan ID:` replaces `Finished … — Error`, which was self-contradictory in
  four words. The event **kind** is unchanged, so nothing a consumer subscribes to
  moved.
- The observed 0.096 / 0.11 kWh export is a **Success**. The shortfall was 0.56 of one
  actuator step; no command could have closed it, and the completion tolerance has
  left the presentation layer entirely.
- Success outranks Cancelled deliberately: *the money made outranks the reason the
  plan then changed*. Failed outranks Success, as the honesty guard.
- The target is **frozen at the first confirmed activation** and may never shrink. A
  vanishing later segment yields `Partial` against what was promised, not a
  retroactively satisfied smaller number.
- Four reasons the controller has always produced — safety, marker lost, progress
  unknown after a restart, and a failed command — reached the surface as
  "Canceled — Plan Replaced", which was false in each case. All four are now named,
  and `execution_error` is read for the first time since it was written.
- Two reasons the surface named and nothing could produce (`reserve_limit`,
  `no_charge_ceiling`) are deleted. A new test forbids the next one.

### Fixed: `export` claimed to have no actuator

`CONTROL_EXECUTABLE_ACTIONS_BY_INTENT` has authorised an admitted `net_export` since
`beta.27`, `CONTROL_LIVE_DISPATCH_INTENTS` contains it, and the hardware has performed
one — while `IMPLEMENTED_ACTIONS` said no actuator existed. Not a label: it bounded the
**capability solve**, so every export day reported euros the plant supposedly could not
capture, and it put an `Advisory` marker on Live export lines a command was about to be
sent for.

`export` is now in the set. Export remains gated by your `allow_battery_export`
setting at every decision — an actuator existing and a user permitting its use are
different questions.

### Fixed: an idle interval was charged full import price

`docs/ARCHITECTURE.md` has asserted since Phase 2 that baseline self-consumption is
real in the default configuration, and nothing checked: the only measurement in the
codebase detects the **charge** direction. `beta.32` adds the missing detection,
mirroring the surplus-absorption detector's shape exactly — wrapped, never raises, and
**unknown means not modelled**, because the optimistic error would be a plan that
believes the house is fed for free.

It matters beyond reporting. `unavoidable_import` feeds `grid_charge_kwh`, which is the
basis for the grid-charge margin, so an overstated unavoidable import *understates* the
margin and biases the plan toward charging too readily. And an idle interval whose real
import is near zero was being priced as a full purchase, which biased the objective
toward paying a switching fee to start a discharge that idling already achieves.

With ambient self-consumption unmodelled, every published euro figure is byte-identical
to `beta.31`. The state *transition* is deliberately not corrected, and the reason is
recorded: 0.105 kWh DC of ambient discharge against a 0.264 kWh lattice bucket is not
representable.

### Fixed: four formulas in the design, corrected by arithmetic

Each of these was wrong in a reviewed draft and was caught by working the numbers.

- **The exportable surplus double-counted the hard floor.** `reserve.py` computes
  `required = floor + deficit`, and production calls it twice — a probe at the bare
  floor to size the margin, then the real projection at `floor + margin` — so
  `reachability_now` already contains both. On the live figures the surplus is
  `14.77 − 5.13 = 9.64` kWh DC, not 5.32.
- **The hold-versus-export comparison had one conversion too many.** One DC kWh held
  to serve the house avoids `p_import × η_d`; the same kWh exported earns
  `p_export × η_d`. Same energy, same single conversion — **it cancels**, and the
  comparison is prices directly. Dividing by the round trip made the gate ~11 % too
  strict, enough to refuse genuinely good trades.
- **The anti-churn buffer's decay was inverted**, so it was largest when the refill
  was *closest*. There is no decay constant now: the distance lives in the quantity.
- **The export-gate audit was measured on the wrong objective.** `cost_eur` is the
  metered cash flow alone; the recursion also charges the switching fee, the
  grid-charge margin and the throughput cost. On the live horizon `cost_eur` fell
  €0.022 while the fee rose €0.20, so the audit published **−0.022** for a protection
  that had cost €0.178. `EconomicPlan.objective_eur` now names the scalar the
  recursion minimises, and comparisons use it.

### Added: the forecast quality that was measured and read by nobody

Every input is an existing computed quantity. No price forecasting, no new user
setting, no learning redesign.

- **`error_persistence`** — new in `metrics.py`, and it answers a question no
  assumption could settle. An allowance for cumulative error over `n` intervals grows
  as `mae × √n` if the errors are independent and as `mae × n` if they are perfectly
  persistent; neither is defensible, and `beta.31`'s implicit `√n` is why its
  statistical term was inert. The window's own rows hold the answer:
  `|predicted − actual| / Σ|error|`, per day, averaged. **Measured, with no free
  statistical constant.**
- **The signed bias, one-sided.** `bias_kwh` is positive when the model
  over-predicts, so only a measured *under*-prediction widens the allowance: only that
  direction can strand the pack.
- **Today's adaptation, protection only and capped at 1.5.** Today's measured
  consumption is the best evidence about today's remaining consumption, so it is used
  — for protection, never for the cost objective, and never for tomorrow. A structural
  test asserts the adapted estimate reaches no priced quantity.
- **Sparse history yields *more* protection, never zero.** A missing persistence ratio
  means `rho = 1`, the conservative end. An earlier draft would have removed all
  protection at 11 learned days, which is exactly when it is most needed.

### Added: the anti-churn extension, and why it is not a reserve

A compelled Safety Buy that buys the bare minimum is compelled again on the next
refresh. So while a bridge exists, the enforced requirement at the **head interval
only** is raised by one lattice bucket plus the smaller of the expected load and the
measured error over the window to the refill.

Four properties make it structurally incapable of becoming a second autonomy reserve:
it is gated on a condition its own action destroys, so it cannot survive two
consecutive refreshes after being satisfied; it touches interval 0 and nothing else;
it never raises the physical curve, so the compulsory/discretionary split stays
measured against pure physics; and **it cannot initiate a purchase** — no bridge, no
bump.

Purchase attribution is therefore three-way, not two: the physical requirement, the
triggered extension, and ordinary discretionary energy. Each publishes **two**
booleans, because initiating a purchase and enlarging an already-triggered one are
different powers, and only physical reachability carries the first.

### Unchanged, deliberately

- **The 20 % physical floor** remains the only hard inventory bound.
- **`reserve.py` is still price-blind.** A draft proposed narrowing its grid credit to
  economically attractive intervals via a boolean mask; it was **withdrawn**, because
  even as a boolean a price-derived mask makes the lexicographic physical reserve
  depend on economics. The physical curve therefore stays flat at `floor + margin` on
  this installation, and that is correct physics — with a 10 kW connection the grid
  genuinely can refill the pack next quarter. The defect was never the flatness; it
  was using that flat line as the only protection for a discretionary export.
- **Self-consumption is never gated**, so the pack can always reach its floor feeding
  the house. The export permission refuses a caused-export delta and nothing else.
- **With `allow_battery_export` off, nothing broadens permitted export** — and the
  battery is nonetheless usable again at low load. A draft proposed permitting a
  bounded spill so that a house load below one discharge bucket could still be served
  from the pack; implementation measured the production lattice and found the assumed
  bound wrong by an order of magnitude. The smallest non-zero discharge is
  **0.250 kWh AC**, not the 0.025 kWh actuator step, so serving a 0.19 kWh load would
  spill 0.060 kWh every quarter — approaching several kWh a day of metered export
  against an explicit instruction. That proposal was withdrawn.

  The fault it was trying to fix was real, and severe: with export disabled, every
  representable discharge overshot a sub-bucket load, tripped `caused_export` and was
  refused, so **the pack sat idle while the house imported**. Measured at
  0.19 kWh/quarter in a dear 0.35 window: 0.000 kWh discharged, 4.560 kWh imported,
  **€2.143** for a day the battery could have covered outright.

  It is fixed by the idle-counterfactual correction instead, and no export is
  involved. When the inverter is known to serve residual load from the battery
  unbidden, an idle interval is *modelled as* doing so — continuously, outside the
  bucket grid, clamped at the floor and at discharge power — so the plan stops
  believing it must choose between a 0.250 kWh dispatch and a full-price import.
  Measured after the fix, at 0.05 / 0.10 / 0.19 / 0.22 / 0.24 kWh per quarter: the
  house is served in full with **zero export and zero import**, and the cost is zero.
  Nothing about the no-export instruction was weakened — the spill is not permitted;
  it is no longer necessary.
- **Stage-B safety is untouched.** The fail-closed gates, the ownership protection, the
  dead-man failsafe, the foreign-dispatch protection and the non-executable
  `serve_load` intent are all unchanged.
- **No published `quarter_expired`-family value strings change.** Five strings are
  shared across four vocabularies; renaming them would break a user automation for
  tidiness. Disambiguation is *added* — every published ending carries
  `reason_vocabulary` — and a new test allows exactly those five collisions and fails
  the build on a sixth.

### Changed: figures and text a reader may notice

- `shortfall_percent` can now be **null**. It published `140 %` against a 0.01 kWh
  objective — arithmetically correct and useless, since a percentage of a figure
  smaller than one actuator step is noise. A signed
  `objective_tracking_error_kwh` is published instead, which is the figure that can
  distinguish meter-side tracking lag from noise.
- `economic_value_forgone_eur` drops to ~0 on export days, because the plant can now
  do what the plan wants.
- `capability_gap_reason` no longer returns `no_primitive` for an export; curtailment
  is the only action left without an actuator.
- `execution_blocked_reason` can now say `none`. It returned a blocking reason
  unconditionally, including on a Live charge this release does send.
- Activity message text changes: `Partial`, `Failed Plan ID:`, and no `Advisory`
  marker on a Live sale. A filter on the literal `"Error"` stops matching.
- Execution plan ids change, because identity is now anchored on the campaign. Run
  ownership is unaffected — it keys on the minted `run_id` — and `execution_revision`
  restarts at 1 once. A pre-`beta.32` in-flight claim that cannot be matched **fails
  closed**.
- `campaign_id` is optional with a `None` default everywhere it appears; absent means
  fall back to run-level behaviour, never an error.
- No entity id, unique id or state value changes. `STORAGE_MINOR_VERSION` unchanged.

### Verification

4020 tests pass, 1 skipped. `ruff check`, `ruff format --check` and
`git diff --check` clean. New suites: the export permission and its measured evidence,
the campaign lifecycle, multi-quarter export execution (the release's real new risk
surface, which nothing covered before), the published diagnostics and their
arithmetic, the four-quantity architectural gate, and the vocabulary guard.

Live `net_export` has been executed on this hardware, and the multi-quarter export
campaign this release creates has **not** — it is new behaviour, tested but not yet
observed on the inverter across a boundary. `objective_tracking_error_kwh` is
published so a week of real export quarters can settle whether the observed 13 %
shortfall was meter-side lag or noise.

## [1.0.0-beta.31] - 2026-08-28

**The economic correctness release.** `beta.30` fixed the controller's hands;
`beta.31` fixes what it is told to do. The planner had been spending real money to
maintain a reserve that was conceptually wrong for an economic battery, and its own
code measured the bill.

Stage B is unchanged. `execution.py`, `safety.py`, `dispatch.py`, `control.py`,
`quarter.py`, `alphaess_device.py` and `alphaess_adapter.py` are **not touched at
all** since `v1.0.0-beta.30`.

### Fixed: the reserve assumed the grid would never be used again

The dynamic reserve was a **price-blind, no-grid-ever-again autonomy requirement over
a 36-hour horizon**, imposed as a hard lexicographic floor inside a 12-hour priced
window. `build_reserve(limits, floor_energy_kwh, demands)` took three arguments: no
price, no charge term. Future *sun* was credited 7.3 kWh of replenishment; the future
*grid* was not representable at all — and the grid is the more reliable and more
controllable of the two.

Measured on the live installation at 12:00 on 2026-08-28: `required_reserve_kwh` 15.8
(**73.1 % SoC**) against a 4.32 kWh floor, `margin_to_reserve_kwh` 0.36 out of 11.84
kWh usable — **96.9 % of the discretionary pack immobilised**. The curve peaked at
17.18 kWh at 16:45, above the 16.157 kWh actually stored, so a violation was
unavoidable; violations outrank cost lexicographically, so the solver had to buy at
any price. It bought 1.94 kWh at 0.244–0.258 €/kWh and exported the surplus at
0.158–0.212 €/kWh in the same twelve hours. The relaxed re-solve the code already
runs every refresh measured the cost of that constraint at **€1.3185** for one
afternoon.

The constraint also **self-disabled exactly when the risk was real**: the
whole-horizon bridge runs 33–61 kWh in winter against a 22 kWh pack, so every winter
horizon violates, the first lexicographic term ties, and economics resumes. Its grip
was tightest in the summer shoulder where the danger was lowest.

### Added: reachability hard, edge priced

One extra term in the existing recursion, and one in the objective. No second physics
— every conversion still goes through `apply_request`.

```
R_reach[n] = F
R_reach[i] = max(F, R_reach[i+1] + discharge(i) - pv_credit(i) - grid_credit(i))
```

`grid_credit(i)` is what a full-power grid charge could add in interval `i`, through
the same clamp `_credit()` already uses for production — so headroom-limited,
power-limited and efficiency-correct. It is **zero for every interval beyond the
priced horizon**, and that single clause is the whole design: inside the window the
optimiser can see, price and choose a refill; beyond it, it cannot, and assuming one
would be assuming away the one thing it does not know.

This stays a hard lexicographic constraint, and now deserves to be, because it
encodes only physics. The 20 % floor is its unconditional terminal condition.

The unpriced tail becomes a **value** rather than a bound: `- v_edge x E(N)` in the
cost term, where `v_edge = clamp(eta_discharge x Q25(known import), 0,
eta_discharge x max known import)`. A kWh still in the pack when prices run out is
worth what the cheapest refill actually visible would cost. Bounded by construction,
so it can never produce "pay anything"; self-consistent, so the solver is indifferent
between holding a kWh to the edge and buying one at that price — which removes the
hoarding incentive and the dumping incentive with one parameter. The credited edge
energy is capped by the room forecast production still needs.

A bounded uncertainty margin is the **only** place forecast error enters:
`u = min(U_MAX, max(u_blind, u_statistical))`, with `u_blind` four quarters of demand
at zero credit — a structural control-failure buffer — and `u_statistical` the
forecast MAE scaled by the square root of the intervals to the next refill. `U_MAX`
is 5 % of usable capacity, so 27 % WAPE can never become "keep the pack full".

### Added: every discretionary purchase passes the economic gates

Reserve-protection charging used to bypass `minimum_trade_gain_eur` entirely — the
lexicographic term laundered a purchase past the economic gate. Reachability is now
the only exemption, and a reachability purchase is unavoidable by construction.

Three disjoint bases, none overlapping: `minimum_trade_gain_eur` per run,
`grid_charge_margin_eur_per_kwh` per grid-caused charge kWh, and a new
`battery_throughput_cost_eur_per_kwh` on `charge_ac + discharge_ac`. The last one
exists because `beta.31` makes micro-cycling cheap for the first time, and adding
freedom without the cost of using it is how a newly-freed optimiser finds a way to
lose money the constrained one never could.

`grid_charge_margin_eur_per_kwh` was accepted by `solve` and read into the runtime
config, and the executor call between them was the gap. It is now plumbed through and
**published** — in the planning diagnostics, the decision record and the settings
fingerprint — so the number a user reads is the number their money was spent under.
The resolution order is unchanged: `entry.options`, then `entry.data`, then the
default. **No migration touches an explicitly configured value.**

### Added: economic attribution, so a purchase can answer for itself

Every grid charge now answers *why now*, *why this much* and *why not wait*, with a
named classification: `reachability_bridge`, `uncertainty_margin`,
`economic_arbitrage`, `strategic_future_self_use`, `mixed` or `unknown`.

Two corrections came out of writing the tests, and both were real defects.

The compulsory share is what the **reserve-relaxed counterfactual declines to buy**,
not the deficit at the horizon head. The requirement is a *curve* and it usually peaks
ahead of the head: on a winter shape the head asked 9.59 kWh while the curve peaked at
10.855 four quarters later, so a pack at 10.80 had a head bridge of **zero** and
0.56 kWh it could not decline. Reporting that as discretionary would have been the
mirror of the fault this release exists to fix.

And `economic_arbitrage` no longer requires the charging window to pay for itself. It
asked whether the run's own marginal cost was negative, which for a purchase measured
against its own idle counterfactual is essentially never true — so the label was close
to unreachable and the strategic bucket absorbed everything. It is now a *concrete*
future interval inside the priced horizon whose import price beats the purchase after
the outbound conversion, published together with the price it was measured against.
Buying at 0.10 to displace a 0.38 import tonight is arbitrage.

### Added: winter chained replenishment, proven

A pack at 45 % facing two 16-quarter stretches at 0.75 €/kWh that draw 0.70 kWh a
quarter against a 2 kW charge path delivering 0.5. Solved on the production lattice:
**zero violation**, minimum SoC 24.2 %, end 32.9 %, and a chain rather than a state
machine — 2.00 kWh at 0.28, 4.00 kWh at 0.04, 2.00 kWh at 0.09, **nothing** bought
during either expensive stretch, and 9.45 kWh of import avoided.

The first window buys exactly what its four quarters can physically deliver. The
cheap main window lies on the far side of 11.20 kWh of demand that 5.400 kWh of usable
inventory cannot cover, so each of those 2.00 kWh displaces a kWh that would cost
0.75 — €0.94 saved. All three runs are entirely discretionary; nothing bypassed the
gate.

### Added: economic auditability

No economic claim about this system was auditable before. `MAX_COMPLETED_QUARTERS_REPORTED`
held three hours, there was no historical price series, and the pack energy at decision
time was recorded nowhere — so the night campaigns could not be costed even in
principle.

A bounded, append-only **decision record** now persists what each refresh solved:
start energy, the demand and price series, the floor, the limits and the settings
fingerprint. A replay harness runs the **production** solver over it and compares four
architectures — the old autonomy floor, the new reachability plan, floor-relaxed, and a
cheapest-feasible no-export baseline. The quarter ring holds a full day.

In Shadow only, a fourth solve publishes `beta.30`'s plan beside `beta.31`'s on
identical inputs, so the change of architecture can be *watched* rather than trusted.
It is off in Live, off by default, flagged temporary, and no decision reads it.

### Changed: Activity is a plan lifecycle, not a refresh log

An export of the real Activity history held 79 messages, roughly **47 of them churn**
about plans that had not changed. One charge campaign ending at 16:15 announced itself
planned and then *"has finished the planned window"* **six times** while its start slid
08:45 → 12:15 and its energy shrank 13.33 → 11.11 kWh. Both halves of each pair were
false: nothing new had been planned, and nothing had finished — the announcement had
been superseded.

Identity was `(direction, start_utc)`, and the horizon head is
`elapsed_intervals + 1`, so a running campaign's start advances every refresh. Its end
does not. The lifecycle is now anchored on `(category, end_utc)` with a one-interval
tolerance, and a **plan id** is a six-character digest of that identity — stable across
a reload, and reproducible from a diagnostic.

One plan, at most three lines. `Planned` once, `Started` at most once, then exactly one
terminal — `Success`, `Canceled` or `Error`. Structural rather than filtered: the
record carries what has been said and a terminated id is closed, so there is no path
from a refresh to a second line.

```
Plan ID: 9175fa — Safety Buy Planned — 15:15-16:15 — 2.22 kWh
Plan ID: 9175fa — Buy Started — Tracking 2.22 kWh
Finished Plan ID: 9175fa — Success — Target Reached — 2.22 / 2.22 kWh
Canceled Plan ID: 9175fa — Plan Replaced — 1.40 / 2.22 kWh
Finished Plan ID: 9175fa — Error — Command Failed
```

Activity **cannot** print a figure it should not, because `RunContent` carries only a
category, an energy, a window and an instant. No power, no price, no expected value, no
charge-source prose, no reserve arithmetic — all of it stays in diagnostics, where it
was verified rather than assumed to remain. The repeated *"Advisory only: no command is
sent for this action."* becomes one word, `— Shadow` or `— Advisory`; a disclaimer a
reader learns to skip is worse than a short one they read. Shadow now emits no start,
success or error **at all**, which is a stronger guarantee than any wording.

Two defects were found while building it. A start with no lifecycle to attach to was
adopting one before the plan had had its turn to announce, so one physical dispatch
produced two `Started` lines under two plan ids. And a cancelled campaign took its
denominator from Stage B's *current* target, which the replacing refresh has already
revised — putting two plans in one fraction.

The event name is now **`Alpha EMS`**. It was `Economic plan`, which was accurate when
the surface carried nothing but Stage-A advice and stopped being accurate the moment it
reported real dispatches.

### Changed: professional Control State labels, with no breaking rename

Every existing state value is unchanged and still means what it meant, so an automation
matching `executed` still matches `executed`. Home Assistant renders an `ENUM` sensor's
state through the translation layer, exactly as `select.control_mode` has since
Phase 4:

| internal | English | Dutch |
|---|---|---|
| `off` | Off | Uit |
| `inhibited` | Inhibited | Geblokkeerd |
| `eligible` | Planned | Gepland |
| `idle` | Idle | Rust |
| `executed` | Executing | Actief |
| `error` | Error | Fout |

`error` is **added**, and additive is not breaking. A failed write previously published
whatever eligibility had computed *before* the write was attempted, so a reader could
not tell a refresh that sent nothing from one whose command failed. It is set inside
`_mark_execution_error` so a future error path cannot forget it; the execution
*barrier* is explicitly not a failure.

`Starting`, `Updating`, `Completed` and `Canceled` were **declined**. There is no
refresh between deciding to write and the write landing; a setpoint correction happens
on the sixty-second tick, so `Updating` would flip the entity every minute of a
campaign; and plan terminals belong to the Activity lifecycle, where a plan id ties
them to what they terminated. `battery_recommendation` and `economic_action` were
labelled too, because they appear in the same history view.

### Not changed

Verified by AST against `v1.0.0-beta.30`: `decide`, `carry_forward`, `carry_plan`,
`ownership_of`, `withdrawal_basis`, `rolling_power_kw`, `demand_for`,
`quarter_intent_for`, `evaluate`, `absorbing_capacity_kw`, `safe_discharge_power_kw`,
`direction_permitted`, `authorize_start`, `authorize_export`, `authorize_reset`,
`permitted_sign`, `tick_energy_cap_kw`, `write_refusal` and
`steps_outside_capability` — nineteen primitives, all byte-identical. Seven Stage-B
modules are untouched entirely.

`CONTROL_EXECUTABLE_ACTIONS` is still `{charge}` for the unconditional set;
`serve_load` remains unexecutable; the Force Charging and Force Discharging helper
families are written for neither Live intent; `TARGET_TOLERANCE_KWH` is still `0.25`;
the dead-man remains a failsafe, foreign Dispatch is still never touched, and the 20 %
physical floor is unconditional.

The whole-horizon autonomy figure is **kept and renamed** `autonomy_requirement_kwh`,
published with `credits: ["pv"]` and consumed by nothing. A pinned test asserts it
reaches no solver input.

**Physical PV curtailment is still not executed.** Its fail-safe direction is *restore
generation* — the inverse of every other actuator here — and it needs its own reviewed
actuator design.

### Beta status

**`net_export` has still never executed on real hardware.** Live grid charging was
validated on the live installation in `beta.26` and repaired structurally in
`beta.30`; exporting has been proven only in tests. If an economically valid export
plan occurs on this release it will execute, under all of `beta.30`'s fail-closed
safety — watch your grid meter during the first planned export quarter.

`beta.31` materially changes Stage-A economic planning and will now be evaluated in
**supervised Live operation** on the real installation. Phase 8 is not
hardware-complete.

### Upgrading

Nothing to migrate. `STORAGE_MINOR_VERSION` is unchanged and the decision record loads
additively — an installation upgrading from `beta.30` simply starts recording. Your
configured `minimum_trade_gain_eur`, `grid_charge_margin_eur_per_kwh`, minimum SoC,
battery power limits and control mode are all preserved exactly as saved.

## [1.0.0-beta.30] - 2026-08-28

**The structural execution fix.** A full-night hardware run of `beta.29` produced
enough evidence to stop patching one defect per release and repair the architecture
instead. Four defects, three of them structural, and one systemic lesson about how
they stayed hidden.

Anyone on any earlier release should upgrade. Before `beta.30` the controller could
*start* a Live charge and then neither correct it, stop it, nor clean up after it.

### Fixed: ownership could never be proven on the real inverter

Every provenance path since `beta.24` required the vendor's dispatch-start register.
`_dispatch_start_instant` invents a calendar for it — the register is a bare number,
and the code assumed "seconds since local midnight" — and the settle path then
subtracts a real wall-clock `written_at` from that synthetic instant. If the register
is in UTC-midnight seconds, that is out by two hours against a 180-second window; if
it is a Unix epoch, by decades. And because the `exact` path needs a stamp only the
settle path can write, one unvalidated assumption disabled **both** factors
permanently.

Measured on the installation: sixteen consecutive ticks reporting
`ownership_not_owned`, `last_successful_write` still sitting at the arm thirty
minutes later, the EMS classifying **its own dispatch** as untouchable at the next
boundary, and the hardware dead-man stopping the run.

Ownership now rests on three factors Alpha EMS controls: **the marker**, **a claim
written before the writes**, and **a readback of the helpers it wrote itself** — mode
and the sign the intent permits. The register is corroborating-only: it may upgrade
provenance to `exact` or `settling`, and it can no longer withhold ownership. That is
what makes this release safe to run before the register is measured, and it is
asserted directly: no reading of it — zero, an epoch, a two-hour offset, a moving
counter — can take ownership away.

**Three candidate factors were tried and rejected during implementation**, each for
the same reason: a factor may not judge a value we deliberately vary.

- the **dead-man duration** alternates 20/25 minutes so the vendor automation fires
  at all, so it is judged against the permitted set rather than against the claim;
- the **quarter** — a dispatch session spans every row of its plan, and binding the
  claim to the row broke ownership at the first boundary (claim `08:15`, row `08:30`,
  same dispatch);
- the **commanded power** — the sixty-second controller rewrites it by design (claim
  4.3 kW, live −5.5 kW one minute later), so it is reported and never judged.

### Fixed: every second quarter of a multi-quarter run was skipped

One slot cannot hold both "the quarter that is open" and "the quarter admitted next".
While a quarter was open the slot was occupied, so nothing was admitted for the
boundary after it — and when the slot freed, the selection rule looked strictly
forward and picked the quarter *after* the one that had just opened. Visible in the
hardware ring: the executing quarter jumped from `22:15Z` to `22:45Z` and the quarter
between them never ran.

`AdmittedPlan` freezes the **whole schedule** at admission and the executing row is
**derived** from it, so a boundary is a lookup rather than a hand-off. There is no
slot to occupy and no race to lose. Immutability is stronger than before: the entire
schedule is frozen, so no later publication can alter any row, and withdrawal is
still never inferred from a horizon that structurally cannot describe an open row.

A row that stops being current has *ended*: it is closed and its shortfall recorded,
and the dispatch stops only when nothing follows it.

### Fixed: an ended run's progress reached a fresh publication

The hardware reported `remaining_battery_kwh: 0` and `target_reached` against a
1.11 kWh target while the battery was physically charging at 5.7 kW — the previous
run's 2.531 kWh being compared against a publication it never executed. Quarter
progress now keys on `(claim_id, quarter_start)`, so neither a new arm nor a new row
can inherit the other's measurements.

### Fixed: planned rows below the actuator's resolution

The Dispatch power helper quantises to 0.1 kW, so the smallest energy a quarter can
deliver is **0.025 kWh**. `beta.29` published export rows of 0.01 and 0.02 kWh, which
the actuator can only answer with 0.025 — a 150 % overshoot — or with nothing. Stage A
now marks such a row non-executable with a named reason; it stays fully visible in the
economics, and Stage B refuses it as its own backstop.

### New: the dispatch-start register probe (P0), read-only

Because the register's meaning has never been established, `beta.30` measures it. On
the cadences that already read the device — no new timer, no extra I/O — it records
the raw state and its attributes, the entity's `last_changed` (which is the readback
lag, measured rather than assumed), six candidate interpretations and each one's
delta to the claim's `written_at`, the change since the previous sample, and a phase
derived from observable transitions.

**It chooses nothing.** Candidates are published side by side; the interpretation is a
hardware measurement, not a code assumption. No decision path reads the ring.

### Why the tests did not catch any of this

`LiveSurface` set the register to *"the same reconstruction the ownership layer
performs, from the same instant"*. **A double defined as the inverse of the function
under test cannot fail** — roughly two hundred ownership assertions passed while Live
execution was impossible in the field. `beta.30` adds an anti-tautology guard that
fails if this test module ever derives a fixture value from the production code it
then checks.

### Not changed

Verified byte-for-byte against `v1.0.0-beta.29`: `evaluate`, `absorbing_capacity_kw`,
`safe_discharge_power_kw`, `limit_command`, `action_refusal`, `dispatch_refusal`,
`demand_for`, `carry_forward`, `control_intent_for` and `ownership_of`. The run-level
`TARGET_TOLERANCE_KWH` is still `0.25`.

Stage A's economics are untouched: the optimiser still decides when to charge, when to
export, the quarter targets, the reserve, headroom, prices, forecasts and switching
cost. Stage B still only executes an already-authorised quarter. `serve_load` remains
blocked, the reserve-guard discharge still cannot export, and the Force Charging and
Force Discharging helper families are written for neither Live intent.

**Physical PV curtailment is still not executed.** Stage A continues to model it for
negative export prices; there is no actuator, and adding one needs a fail-safe that
*restores* generation — the opposite of every other actuator here.

### Upgrading

A claim written before `beta.30` records no plan and no claim id, so it cannot be
checked against what is running. Such a claim **fails closed**: the dispatch is left
to the device dead-man rather than adopted under rules this release no longer applies.
`STORAGE_MINOR_VERSION` is unchanged; there is nothing to migrate.

## [1.0.0-beta.29] - 2026-08-27

**The open quarter becomes the execution authority, for both intents.** `beta.28`
got further on real hardware and stopped again: at the **19:45–20:00 export
quarter** the controller computed the correct physics, built the correct Mode 2
START sequence, and the device stayed inactive.

Tracing that one symptom found **five** defects. Four share a root — `beta.27`
declared `CarriedQuarter` the execution envelope and left several gates still asking
run-era questions. Stage A's economics are untouched, and so is every safety
function.

Anyone on `beta.27` or `beta.28` should upgrade.

### Fixed: an export could never start

`authorize_export` required provable ownership unconditionally. Before the first
write there is no dispatch to own and no causal record to match, so the export path
refused **every** START, deterministically, forever — computing the right power,
building the right sequence, and reporting `ownership_not_provable`.

The rule is now the one `evaluate` has used since Phase 4: **inactive, or owned.**

* **starting**, nothing running — ownership and causation are not required, because
  neither exists yet. What protects the site is the foreign-dispatch check, which is
  reachable only when something *is* active;
* **continuing**, a dispatch running — ownership and causation are hard requirements
  again, unchanged and unrelaxed.

The relaxation is confined to `dispatch_active is False`. A running export is gated
exactly as strictly as before.

### Fixed: an open charge quarter whose run had ended produced no command

`beta.27` made the quarter the execution envelope and then asked it only for
`net_export`. A charge still went through the run-level path, which needs a carried
run with an actionable window — so an open **charge** quarter whose parent run had
ended commanded nothing at all. That is the `beta.26` skipped-quarter fault, still
live in every release since, and masked only because charge runs usually span
several quarters and get affirmed by the next publication.

`export_intent_for` is generalised into `quarter_intent_for`, keyed on the quarter's
frozen intent: `net_export` discharges, `grid_charge` charges, everything else
returns nothing. `control_intent_for` keeps its charge-only guarantee untouched and
remains the path for a publication with no quarter schedule.

**A parent run ending, rolling or being absent no longer prevents an already-open
quarter from executing** — for either intent. The exposure stays bounded exactly as
before: one quarter, fifteen minutes, and the energy admitted before it opened.

### Fixed: an export could not start on the first refresh after any stop

Found while implementing the above, and release-blocking on its own. The
control-grade coherence verdict is produced only by the sixty-second tick and set to
`None` by every stop — and the tick cannot run before a START, because it requires
an active dispatch. So an export was refused `sensor_incoherence` on the first
refresh after any stop, **including the previous quarter's own expiry**, with the
next opportunity a full quarter away.

Absence of a verdict is not evidence of incoherence. With no verdict yet, the
question asked is the one `control_coherence` seeds itself from: are the sources
readable at all. A verdict that exists and says unusable still refuses.

Deliberately **not** fixed by advancing the coherence state on the refresh cadence
too: its grace is counted in *ticks*, so a second cadence feeding it would have
shortened a documented 180-second safety bound to something nobody chose.

### Fixed: `refresh_decision` reported the wrong question

It was recorded at the write boundary, *before* authorization ran, so it could not
see the decision even in principle and fell back to Stage B's run-level
`stop_reason`. On the hardware that published `target_reached` for a refresh which
had planned a correct START and been refused — while the quarter diagnostics beside
it correctly showed the target *not* reached. The one fact a reader needed was
absent and a fact about a different question was in its place.

It is now recorded after authorization, in precedence order: the write-boundary
refusal, then the authorization refusal including which condition, then
`stop_reason` **only while actually stopping**, then whether a command was planned.
`wrote` now means permitted rather than merely planned.

### Not changed, and asserted so

The run-level `TARGET_TOLERANCE_KWH` is still `0.25`. It *was* implicated: the
19:45 run's battery target was 0.25 kWh with zero progress, so `demand_for`
declared the target met on the first refresh, which is where the phantom
`target_reached` came from. Under quarter authority the command no longer comes from
that path, so it cannot block execution — and a named regression proves exactly that
at the boundary value, for both intents, rather than assuming it.

Also unchanged, verified byte-for-byte against the published `beta.28`: `evaluate`,
`absorbing_capacity_kw`, `safe_discharge_power_kw`, `limit_command`, `demand_for`,
`carry_forward`, `carry_quarter` and `control_intent_for`. A reserve-guard discharge
still cannot export, `INHIBIT_WOULD_EXPORT` still fires, `serve_load` stays blocked,
and the Force Charging and Force Discharging helpers remain unwritten for Live.

`carry_forward` and `carry_quarter` were deliberately left alone. The 19:45 run
ending is **expected**: Stage A's horizon head is `elapsed_intervals + 1`, so a
19:45 refresh publishes from 20:00 and the 19:45 run cannot affirm itself. That is
the fact `CarriedQuarter` exists for, and fighting it would have been the wrong fix.

### One semantic expectation changed

`test_stage_a_withdrawal_stops_the_charge` — Sequence D, "a reset with no current
charge intent anywhere" — now closes the open quarter as well as withdrawing the
publications. "Anywhere" includes the quarter, because an open quarter is itself an
intent source; that is the point of the fix, and the continuation behaviour has its
own test rather than being conflated with the reset-path test.

### Upgrading

Nothing to do, and no migration: `STORAGE_MINOR_VERSION` is unchanged.

## [1.0.0-beta.28] - 2026-08-27

**The beta.27 hardware hotfix.** `beta.27` was installed on the live installation
and neither of its two headline features worked. Both causes were the same shape: a
new mechanism was added correctly, and the older gate that *selects* it was left
answering the older question. Four omissions, no redesign, and Stage A's economics
untouched.

Anyone running `beta.27` should upgrade. It cannot execute a `net_export` run at
all, and its quarter envelope never opens.

### Fixed: `quarter_schedule` was empty on every run

The hardware reported `"quarter_schedule": []` for every published run, even after a
full quarter refresh — so Stage B admitted no quarter, reported
`quarter_start = null` and `intent = null`, and every sixty-second tick recorded
`no_admitted_quarter`.

`execution_target` took the schedule as an **optional prebuilt list**, and the
production call site never passed it. The rule string describing the list was
published unconditionally beside it, which is exactly why the diagnostics looked
almost right: the schema was there and only the rows were missing.

The rows are now the **input** rather than the result. `execution_target` takes the
solved interval rows and builds the schedule itself, where the intent it depends on
has already been derived — so a caller can no longer assemble it wrongly, or forget
it. A new `quarter_schedule_source` field says whether rows were supplied at all, so
an empty schedule and an unwired call site are never again the same observation in a
download.

### Fixed: `would_export` blocked every intentional export

The hardware reported `inhibit_reason = would_export` and `authorized = false` on a
planned export. **The inhibition itself was correct** — `evaluate` was refusing a
genuine Phase-3 reserve-guard discharge into the house. What was wrong is that the
reserve guard was asked for a command at all on a refresh where Stage A wanted to
export.

Three causes, in a chain:

- `carry_forward` kept its **charge-only default**, so a `net_export` run was never
  carried. It now receives both executable intents.
- With nothing carried, the guard that suppresses the reserve-guard fallback while
  Stage B holds a run was false — so the fallback took the wheel, and it only ever
  discharges. Fixing the first cause fixes this one.
- Both authorisation gates tested the action against an **unconditionally
  charge-only** set, so an export would have been refused `live_charge_only` even
  once it reached them. Worse, `authorize_reset` used the same test — so an export
  could have been started and then never stopped, stranding a running dispatch on
  the device dead-man.

The action gates are now **intent-aware**, through one new function.
`CONTROL_EXECUTABLE_ACTIONS` stays charge-only and unconditional; a second
intent-keyed map unlocks the discharge direction for `net_export` and for nothing
else. That split is the point: widening the unconditional set would have authorised
every discharge, the reserve guard's included.

**No existing safety function changed.** `evaluate`, `absorbing_capacity_kw`,
`safe_discharge_power_kw` and `limit_command` are untouched, `INHIBIT_WOULD_EXPORT`
still refuses a reserve-guard discharge above the measured absorbing capacity, and
`serve_load` is still blocked — now asserted from both directions in tests.

### Diagnostic strings that had gone stale

Four published strings still told a reader that only a grid charge could execute, on
a release that also exports — which made a hardware download ambiguous about whether
a refused export was a defect or the documented design. The scope strings now name
both executable intents and, explicitly, what remains blocked: `serve_load`, the
reserve guard's discharge (which still cannot export), PV curtailment, panel
shutdown and Dispatch modes 6 and 7. Two refusal reasons were renamed from
`live_charge_only` to say what they actually refuse.

### One semantic expectation changed, deliberately

A `beta.25` test asserted that the battery setpoint eases back when production
collapses. Under the quarter objective it does not: a charge aims at the **battery**
figure, and production changes what the charge *costs* rather than what it *is*. That
assertion described the `beta.26` meter-targeting behaviour the asymmetric design
replaced on purpose — and it had continued to pass in `beta.27` only because the
quarter was never admitted. It has been re-aimed at what the release guarantees, and
the grid-consequence direction it used to cover is now asserted separately.

Two other tests were adjusted for the same reason: with the schedule actually
published, the shared fixture's quarter is planned at the full inverter limit, so
its setpoint saturates and could not discriminate. The quarter is given explicit
headroom rather than the assertions being loosened.

## [1.0.0-beta.27] - 2026-08-27

**Quarter-accurate execution, and Live net export.** Everything in `beta.26` plus
two changes: every 15-minute Stage-A interval becomes an explicit execution
envelope with its own energy target, and selling energy back to the grid becomes
executable on the same validated Dispatch surface.

`beta.26` was validated on the real installation: production moved, the controller
recomputed `-3.17 kW` and moved the applied setpoint from `-2.7` to `-3.1`. Two
things did not work, and both are fixed here from the code rather than from a
guess.

### The skipped quarter, and the repeated "no owned run"

A planned 15:00–15:15 charge never physically executed, and every sixty-second
tick through it reported `no_owned_run`. One shared cause, and it is structural
rather than a race.

`carry_forward` has **one** carried slot, and the only way to fill an empty one is
a target whose window *contains* the current instant. But Stage A's horizon head is
`elapsed_intervals + 1`, so **every fresh publication opens at the next boundary**.
At any boundary where the carried run ends, the fresh publication therefore cannot
be admitted until the *following* boundary — and a whole quarter has no carrier at
all. Nothing is armed, because arming needs a command, which needs an intent, which
needed a carried run.

So **any quarter in which a run ends was skippable**, on every installation.

`CarriedQuarter` fixes it by being carried in its own right, admitted one refresh
ahead exactly as runs are, and by being authority enough for the tick on its own.
The 15:00 quarter now survives the 15:00 refresh.

### The progress objective is asymmetric, and the contract already said so

A quarter's target is realised by measuring progress inside it and correcting every
sixty seconds. **Which figure is the objective depends on the intent**, and the
publication contract has always distinguished the two: `battery_target_kwh` is
*"Battery side. Authoritative for a charge"*, and `grid_target_kwh` is *"Meter side.
Present only when the meter is what the plan is aiming at"* — `None` for a charge.

| intent | objective | ceiling |
|---|---|---|
| `grid_charge` | the **battery** figure | the marginal grid import authorised |
| `net_export` | the **actual meter export** | the battery discharge authorised |

A single formula for both would treat a charge's grid authorisation as an amount to
**consume**. On a quarter with a 2.0 kWh battery target, 1.2 kWh of authorisation
and production outperforming its forecast at 8 kW, the asymmetric design meets the
target from production alone and buys **nothing**; the single formula would have
imported 4.8 kW throughout and bought about **1.2 kWh that was not needed**. That
counterexample is now a test timeline rather than an argument.

Four consequences, each one a requirement:

- **production substitutes for planned grid energy** — more sun, same battery
  target, less buying;
- **missing production cannot unlock extra buying** — the ceiling comes from the
  authorisation, never from the battery deficit;
- **falling behind still speeds up**, bounded by production plus the authorisation;
- **free production is still absorbed once the grid budget is spent** — a spent
  *ceiling* is not a reached *objective*.

### A multi-quarter run no longer follows its first quarter's rate

`desired_grid_kw` was published as a run's **first interval** rate while its window
spanned every interval it covered, so quarters two onward of a multi-quarter run
were executed against quarter one's target. Stage A now publishes the per-quarter
schedule it had already solved — no new solve, and **no economics changed**.

### Live net export

`net_export` executes on the same Dispatch surface, in mode 2, with a **positive**
power. The meter is the target: house consumption is supplied *in addition* to the
export and production reduces the discharge required, through the canonical
identity `dispatch = house − pv + export`. Commanding the planned export magnitude
as battery power would under-export by exactly the house load.

The export objective is the **actual** meter export, not the marginal figure. On a
site whose production is already exporting, following the marginal figure would
under-export by precisely the amount the sun was sending anyway.

**No existing safety gate was widened to make this work.** `INHIBIT_WOULD_EXPORT`
refuses any discharge above the measured absorbing capacity, and
`absorbing_capacity_kw` returns zero for a site that is already exporting — both
correct for the reserve-guard discharge they were written for, where energy reaching
the meter is an accident. Rather than thread a condition through them, beta.27 adds a
**separate** authorisation function with its own twenty-one-condition checklist,
reachable only for an admitted `net_export` quarter. `evaluate`,
`safe_discharge_power_kw`, `absorbing_capacity_kw` and `limit_command` are unchanged,
and tests assert they never learned the words *quarter*, *net_export* or *intent*.

An authorised export then continues through the same machinery a Live charge uses:
ownership, the execution lock, the Dispatch Mode 2 builders, the write boundary,
readback verification, the dead-man and the stop sequence. **The Force Charging and
Force Discharging helpers are never written for it.**

`serve_load` stays blocked. It has no published meter target, so there is no
quantity a quarter could measure it against. So do modes 6 and 7, production
curtailment, and every unverified direction — all of them by having no entry in the
intent-to-sign map rather than by being enumerated anywhere.

### Stopping, and what is never carried forward

**Reaching the target stops the dispatch immediately.** The 20/25-minute device
duration is a dead-man *lease*, never an execution entitlement: a dispatch left
armed because a countdown has not expired is how a target gets exceeded.

**A quarter that expires short records the shortfall and carries nothing.** Stage A
decides each quarter independently; letting Stage B accumulate a deficit would be it
claiming an entitlement no economic layer authorised.

Before every write the requested power is additionally bounded by what one control
interval could deliver against the energy actually left, on a **90-second** horizon —
longer than the 60-second tick it guards, because the tick is approximate, a readback
lands after the write, and a tick can be skipped for lock contention. Near a quarter
boundary that can finish a few watt-hours short. **That is the intended direction of
error**: overshooting spends energy the plan never authorised, and falling marginally
short does not. The shortfall is recorded with the binding clamp named.

### An open quarter is economically immutable

Once a quarter starts, its economic authority is frozen for at most fifteen minutes.
Exactly three things end it: the objective is reached, `quarter_end` arrives, or a
safety condition invalidates execution. A parent run ending or rolling forward has
**no effect** on it.

**Withdrawal is never inferred**, and the reason is structural rather than cautious:
Stage A's horizon head is `elapsed_intervals + 1`, so no publication issued after a
quarter starts can ever contain it. Reading its absence as a cancellation would treat
a certainty as evidence — and would cancel every quarter, always. There is no
explicit cancellation signal in the contract today; adding one is a deliberate
non-goal for this release, and a future phase that needs it needs a real contract
field, not a sharper inference.

Stage B keeps full freedom to throttle or stop for physical safety throughout, so
"immutable" bounds the *authorisation* and never the safety response. The exposure is
bounded by construction: one quarter, fifteen minutes, and the energy admitted before
it opened.

### A restart over a running dispatch stops it

The quarter envelope is deliberately **not persisted**, and its measured progress
lives only in memory — so after a restart the energy already delivered inside the
open quarter is unknown. Continuing would risk delivering the quarter twice, and
leaving the inverter running while sending nothing means relying on the vendor
dead-man. So a provably owned active dispatch is **stopped**, reported as
`quarter_progress_unknown`, and the next admitted quarter is awaited. At most the
remainder of one quarter is lost, and Stage A re-plans at the next boundary.

A dispatch whose provenance **cannot** be established still gets **zero writes**.
That rule is unchanged.

### Diagnostics

`_last_tick_reason` was a single mutable string written by the sixty-second tick and
then published beside figures computed during the *quarter refresh* — so a stale
refusal sat next to a freshly successful write with nothing saying the two described
different events. It is replaced by a typed record that carries its own cadence and
is written once, at the end of the evaluation. `controller.last_tick` and
`controller.refresh_decision` are now separate.

`no_owned_run` covered three different situations — *nothing admitted*, *nothing
armed*, and *we cannot prove this is ours* — needing three different responses. It is
now three reasons.

New with every tick: the open quarter's bounds, elapsed and remaining time, and
planned against realised against remaining energy in both domains. New per finished
quarter, bounded to the last twelve: planned and realised figures, the shortfall in
kWh and per cent, how it ended, peak and mean power, whether production helped, which
clamps bound it, and when the target was reached if it was early.

### Upgrading

Nothing to do, and no migration: `STORAGE_MINOR_VERSION` is unchanged.

A publication or persisted record written before beta.27 carries no quarter schedule,
admits no quarter, and the sixty-second tick **degrades to the run** — executing the
same arithmetic that was proven on the hardware. Refusing to correct a run for want
of a quarter would have taken the working charge path away on upgrade.

### Fixed

- `net_export` mapping to `ACTION_DISCHARGE` — needed so a stop can name what it
  stops — would have made an export command fall into the advisory branch and be
  armed on the **Force Discharging helper family**, silently making a helper family
  the physical actuator for the new capability. The branch now keys on whether the
  intent is a validated Live Dispatch intent. An intent carries a surface; an action
  carries only a direction, and two intents can share one.
- With an export quarter's meter target already met, the canonical identity gives
  `house − pv`, which is the power that holds the meter at zero by supplying the
  house from the battery. That is `serve_load`, which this release does not execute.
  The sixty-second tick would have discharged the battery every time an export
  finished early in its quarter.
- The completed-quarter record read the live quarter field, which both callers reach
  after it has already moved on — recording nothing on the stop path, and the *next*
  quarter's targets against the previous one's measured progress on the refresh path.
- A refusal's flight-recorder entry carried only a timestamp and a reason, which is
  the entry a reader most needs explained.
- The restart flag was cleared on two stop paths but not on the one a restart
  actually takes, so it would have stopped the next admitted quarter the moment it
  armed.

## [1.0.0-beta.26] - 2026-08-26

**The Dispatch runtime.** Everything in `beta.25` plus the complete migration of
Live charging onto the real Hillview Dispatch surface. The ownership hotfix ships
separately as `beta.25` so it can be installed and validated on hardware without
also taking this migration.

**The Live actuator family has changed, and the cutover is atomic.** A Live
charge now executes on the real Hillview Dispatch surface in mode 2 with a
negative power. Arm, sustain, stop and the new sixty-second correction all moved
together: there is no state in which a start uses one surface and a stop the
other, because that state cannot be reasoned about.

The Force Charging and Force Discharging helpers are still *read* -- they are two
of the six conflicting families the vendor automation would silently switch off --
and the reserve-guard discharge is still *planned* for shadow reporting. Neither
is a Live path: no release executes a discharge, and it stays refused at both
boundaries.

### Added -- the Dispatch surface, built and gated

- **The real writable Hillview Dispatch entities**, read from the published
  package rather than guessed, with the register each one drives recorded because
  the encoding is what constrains the design. Power is honoured in modes 1, 2, 3
  and 5 only; outside those the package writes the register as a bare `32000`,
  which is zero watts -- so **modes 6 and 7 are not controllable kW primitives at
  all**. That corrects the assumption that mode 7 is a throttled consumption mode.

- **`dispatch.py`, one canonical identity and every sign derived from it**:
  `grid = house - pv - dispatch`, so
  `required_dispatch_kw = house_load_kw - pv_kw - desired_grid_kw`. Deliberately
  the opposite convention from the helper families, which take a positive
  magnitude and carry direction in *which family* was written -- the two never
  meet in one expression.

- **A 0.2 kW deadband with zero-crossing hysteresis.** Quantise first, then
  compare, so -2.00 to -2.04 writes nothing while -2.0 to -2.3 does. The deadband
  alone would still permit chatter around zero, so reversing a sign has to clear
  the band on the far side of it: noise cannot flip the sign, a real reversal can.

- **The clamp hierarchy, with clamp four as the grid-energy authorisation** rather
  than the remaining battery target. `battery_target_kwh` is
  `expected_pv_to_battery_kwh + expected_grid_to_battery_kwh`, a forecast
  composite, so bounding a grid-power controller with it stops absorption the
  moment production runs ahead of forecast and pushes free photovoltaic energy out
  to the meter at an export price the optimizer had already judged worse than
  storing it. The pack stays bounded by headroom and the reserve, which are
  different questions and are asked separately.

- **A two-part interlock, because one part cannot express it.** Direction on this
  surface is a signed *value*, not a choice of entity, so the entity subset test
  that keeps a discharge off the helper families cannot see a wrong-way dispatch.
  `dispatch_refusal` is the check that can: positive power refused, any mode
  outside the executable set refused by exact label. Zero is permitted, and that
  is not a loophole -- it is what the direction gate produces when the grid target
  would require a discharge, and what the cleanup writes.

- **All six conflicting Hillview families** are now declared, not four. The vendor
  automation turns off force charging, force discharging, force import, force
  export, excess export and peak shaving before arming, and a feature Alpha EMS
  does not know about is one it would have silently destroyed.

- `PERMITTED_SERVICES` grows by exactly one, `input_select.select_option`, because
  the mode is an `input_select` whose label the package parses a number out of.
  `input_button.press` is **not** added: turning the enable boolean off already
  triggers the package's own reset.

### Added -- the Stage-A to Stage-B contract

- **`desired_grid_kw`**, signed, per quarter, read off the solved plan's own
  per-interval grid energies. Positive is intended net import. Kept separate from
  `grid_target_kwh`, which is an export *energy* published only for a net-export
  intent -- merging them is precisely the confusion that field's own comment
  guards against.

- **`safety_buy_kwh` and `economic_buy_kwh`**, from the reserve-relaxed
  counterfactual the plan already solves. The difference used to be computed,
  compared against one bucket and discarded. The documented limitation travels
  with the figures: quantities within this run window, not a globally exact
  decomposition, because the relaxed solve may move economic charging elsewhere.

### Added -- the runtime

- **A sixty-second physical controller on the existing safety cadence.** No new
  timer and no faster loop. It reads the frozen `desired_grid_kw` for the quarter
  it is in and moves the setpoint toward it; it never re-runs prices, admits a
  run, re-ranks a window or re-arms the dead-man. Every refusal is recorded, since
  a controller that did nothing and said nothing is indistinguishable from one
  that is not running.

- **One execution lock over every actuator sequence** -- start, live update,
  emergency self-stop, normal stop and the quarter-boundary writes. Until now
  there was a single write path so nothing needed serialising; a Home Assistant
  timer callback is not serialised against a coordinator refresh, and without the
  lock a correction could land between the mode and the enable and arm a dispatch
  against half-written values. The tick acquires non-blocking and **skips rather
  than queues**: a correction computed while a sequence was running describes a
  world that no longer exists by the time the lock frees.

- **Ownership gained a `degraded` state, and it is never called owned.** Marker
  off still means not owned. What the state adds is that causation may still be
  provable, which authorises exactly one write -- `Dispatch enable -> OFF` --
  through a fourth entitlement of its own. `authorize_reset` requires the
  ownership this state has lost, and the start question would refuse for the wrong
  reason.

- **Two fragilities the migration exposed, both real.** The causal record was
  rewritten on every non-stopping refresh, which cleared its `dispatch_start`
  stamp each time; that was survivable only because the old sustain re-issued the
  activation boolean, so the device restamped its own start instant to match.
  beta.25's sustain deliberately leaves the enable alone, so without the fix
  ownership fell to `unproven` on the second refresh and a charge became
  unstoppable while still running. And the dead-man observation was keyed on
  activation for the same reason, so it was taken once at the arm and never again.

### Added -- three safety layers

- **Control-grade sensor coherence**, on its own threshold.
  `BALANCE_MAX_SOURCE_AGE_SECONDS` is 300 and was calibrated for comparing
  accumulated energy; reused as an actuator threshold it accepts a five-minute-old
  reading as the basis for a live setpoint. 90 seconds is derived from measured
  behaviour -- the installation reports source ages of five and twelve seconds
  against a worst skew of nineteen -- so it cannot fire on ordinary jitter.

  The grace period is counted in **physical ticks, never economic refreshes**. Two
  refreshes is about thirty minutes, longer than the twenty-minute dead-man it is
  supposed to sit inside. Three ticks is 180 seconds, and the dead-man is never
  re-armed while incoherent.

- **An emergency self-stop authority that is not ownership.** Marker off still
  means not owned, and the degraded state is never called owned. What the
  authority adds is one write -- Dispatch enable off -- granted only while
  causation survives the marker, and it refuses any widening: each other write
  touches a dispatch that may still be running, and one of them restarts the
  vendor timer. Retries are bounded at three, then the device dead-man finishes.

- **An admitted run can now be revised downward.** Its energy figures were
  immutable, so a Safety Buy admitted conservatively while tomorrow's prices were
  unknown kept delivering after cheaper prices arrived. Two caps on separate
  domains, never one comparison across origins: a fresh publication reports
  remaining energy from the *next boundary* while the frozen remainder is measured
  from the *admitted window start*, and a single `min` across them trims a healthy
  6.0 kWh run to about 4.5. Strictly subtractive -- carry-forward can reduce a run
  or leave it alone, never grow one.

### Testing

3534 passed, 1 skipped. Dispatch runtime: 185. Ownership hotfix: 21. beta.24: 83.
beta.23: 47. Stage-B gate family: 200. Mutation suites: 238, 0 survivors.

None of it is hardware evidence. See *Known and outstanding*.

### Preserved

Stage-A economics are **unchanged**, and that is a finding rather than an
omission: fifteen counterfactuals were written first and all fifteen pass. The
Safety Buy waits for the cheapest feasible quarter and buys early only as far as
feasibility forces, with the dearest quarter carrying the least. A kilowatt-hour
is retained for a 0.40 avoided import over a 0.30 export, and exported when export
really is worth more. The battery is not exhausted in the first sell window when a
better one follows. No export price unlocks a reserve violation, and energy above
the reserve stays discretionary. Negative prices are ordinary numbers in the same
objective, and no module selects an actuator mode from a price sign.

The foreign-dispatch invariant, restart adoption, the claim window, the four-layer
charge-only interlock, G.1/G.2 sustain, headroom, the grid budget, Activity,
`authorize_reset` and `authorize_marker_release` are all unchanged.

### Known and outstanding

- **Not one line of this is hardware-proven.** Every claim above is from the
  automated suite. The staged hardware validation in the release notes exists
  because a controller that writes every sixty seconds has more ways to be wrong
  on a real inverter than a controller that wrote once a quarter.
- **Not hardware-proven.** The ownership fix should be validated first, and the
  first thing to confirm is that the capability refuses while the helper is
  absent.
- Modes 6 and 7, mode-2 positive power, export, and photovoltaic curtailment are
  modelled and tested and remain gated at both boundaries.
- **R2 remains open**: withdrawal is inferred from the absence of an affirming
  publication.
- beta.22's capped-charge observation is still outstanding.

## [1.0.0-beta.25] - 2026-08-26

**A charge could be armed that Alpha EMS could provably never own, sustain or
stop.** Ownership safety only; nothing else changed -- the previous execution
architecture is deliberately untouched, so this release can be installed and
validated on hardware on its own.

Planned as `beta.24.1`. A four-part version is not a shape this project's version
pattern accepts, and the pattern was not going to be widened to accommodate a
name.

### Fixed -- ownership safety

- **The owner marker was not a required entity.** `discover` walks
  `REQUIRED_ENTITIES` and nothing else, and the marker was absent from it, so an
  installation that had never created `input_boolean.alpha_ems_dispatch_owner`
  reported a complete control surface. The arm wrote `turn_on` to a non-existent
  entity, the write reported success, the causal record could never match, and
  every later refresh read `foreign`. The fifteen-minute alternation in the event
  log was the whole cycle: arm at :00, inhibited at :15, device dead-man at :20,
  arm again at :30. A missing or unavailable marker now makes the capability
  unready and Live refuses before anything is planned, naming the entity.

- **The arm is staged, so the activation is no longer in the same stage as the
  claim.** "Activation last" was already true and was not enough: last in a list
  that runs unconditionally is still reached when the first step did nothing. The
  claim is now sent alone and read back, and only a verified claim reaches the
  parameters and the activation. A claim that cannot be read back arms nothing,
  withdraws the causal record and reports `marker_not_verified`. This is a second,
  independent guarantee -- it holds where a capability snapshot cannot help,
  because a service call that succeeds is not a state that changed.

- **The stop is staged the same way.** The deactivation is sent alone and read
  back before anything else is written. Writing the duration helper restarts the
  vendor timer, so a cleanup issued against a dispatch that did not actually stop
  would extend the run it was ending. A stop that cannot be confirmed withholds
  the cleanup, keeps the marker and the record, reports `stop_not_verified`, and
  retries later -- it never publishes a clean or unowned state.

- **The marker is reported as four distinct facts** -- absent, unavailable, off,
  on -- plus `unverified` for a write whose readback disagreed.

### Testing

3343 passed, 1 skipped. Ownership hotfix: 21. beta.24: 83. beta.23: 47.
Mutation suites: 238, 0 survivors.

None of it is hardware evidence.

### Known and outstanding

- **Not hardware-proven.** The first thing to confirm on the installation is that
  the capability refuses while `input_boolean.alpha_ems_dispatch_owner` is absent,
  and only then that a verified claim precedes any parameter write.

## [1.0.0-beta.24] - 2026-08-25

**The first release that can charge your battery -- and stop.** Live execution is
enabled for exactly one action: a Stage-B `grid_charge`. Discharging, exporting and
curtailing remain advisory and are refused at three independent boundaries.

**Charging does nothing until you ask for it twice**: Control Mode set to *Live*,
and command sending enabled in the options. A fresh installation is `off` and an
upgrade changes neither.

### Added

- **Live charge execution, gated by a set rather than a flag.** The release barrier
  is now `CONTROL_EXECUTABLE_ACTIONS`, a frozen set containing one action, with the
  old boolean derived from it. Tracing what flipping that boolean would actually
  permit is what forced this shape: the command source falls back to the Phase-3
  reserve guard whenever Stage B has no charge to make, `authorize` had no direction
  check, and the device-level check only compared a command against its *own*
  family. A single `True` would have authorised reserve-guard **discharges** on the
  first refresh with nothing to buy.

  Charge-only is enforced four times over: Stage B can only express a charge; the
  reserve-guard fallback is suppressed while Stage B holds a run; authorisation
  refuses an action outside the set; and the send site refuses any step naming an
  entity outside the charge family and the owner marker. The last of those is a
  subset test on entity ids -- it reads no action field and trusts no caller, so it
  survives a defect upstream.

- **Ownership becomes provable.** The causal record is completed from the device's
  own dispatch readback rather than guessed at write time, so a charge Alpha EMS
  arms is a charge it can later stop. A restart adopts the run a live dispatch
  belongs to instead of minting a competing one -- and where the persisted evidence
  cannot prove which run is running, it writes **nothing at all** and lets the
  device dead-man end the dispatch, which is the conservative direction and the
  invariant this project has held since Phase 4.

- **The device dead-man is refreshed unconditionally.** Every refresh of an owned,
  active run rewrites the duration and re-issues activation, whether or not the
  requested power moved. An earlier design gated that on a power change, which would
  mean a charge holding steady at 3.0 kW never re-arms, its dead-man is never
  refreshed, and the dispatch expires mid-run while the controller believes it is
  still going. Constant power is the *common* case.

  The power helper itself is written only when the quantised power has actually
  moved, because writing a helper a value it already holds is a service call that
  buys nothing.

- **Whether the timer actually refreshed is measured, not assumed.** Re-activating
  an already-active dispatch is the one physical behaviour that could not be
  verified in advance, so the helper timer's `finishes_at` is read every refresh and
  compared. If it does not advance, the run is stopped and said so -- there is
  deliberately no deactivate-and-reactivate fallback, because the moment to
  improvise an unobserved write pattern is not the moment the controller has just
  discovered its assumption was wrong.

- **Concise Activity for a Live run.** One line when a charge is planned, one when
  it physically starts, one when it ends, each at most once per run:

  ```
  Charge planned - 8.06 kWh - 2.3 kW - 13:00-16:30
  Grid charge started - 8.06 kWh - 2.3 kW
  Charge complete - 8.06 kWh
  ```

  "Started" means an activation write succeeded. Deriving it from the controller
  state would announce a start for an *armed* decision -- computed, sent nothing --
  which on a release that writes is the one claim that must not be wrong. A
  sustaining refresh, a power change, a republication and a revision bump all
  produce silence.

### Fixed

- **A stop was authorised through the machinery built for a start, so no stop could
  reach the inverter.** Found by executing rather than by reading: it lived in the
  gap between "the controller decided to stop" and "the stop reached the wire", and
  every release before this one was correct to have no wire. Measured on every stop
  path -- target reached, withdrawal, safety, dead-man, Live to Shadow, Live to
  Off -- as zero service calls with Force Charging still on.

  Three faults compounded. The reset was planned with the action of *this refresh's*
  command, which is absent on any stopping refresh, so it planned a lone marker
  release -- it would have **released ownership of a live dispatch**. The safety gate
  returns unsafe whenever there is no intent, and a stop has none. And the mode
  check refused a reset after the user had selected Shadow, which is circular: that
  selection *is* the stop request.

  Stops now have their own entitlement. It requires proof of ownership, the action
  from the record of what was armed, and a real stop reason -- and deliberately not
  an intent, a safety verdict, the active mode, the opt-in or the cooldown. A rate
  limit may delay a start; it may never delay a stop. The rule is that a reset may
  be more reachable than a start, but only for a dispatch Alpha EMS can prove it
  owns; foreign and unproven dispatches stay untouchable.

  Selecting *Shadow* or *Off* now stops a charge we own, releases the marker last,
  and clears the causal record only once the stop has actually landed.

- **The reset list was built after the authorisation that permitted it**, so the
  thing authorised was not the thing sent. The operation is now decided first, then
  built, validated and authorised.

- **A safety condition can no longer prevent a stop.** "Do not start this" and "do
  not stop what is already running" look alike and are opposites. An unsafe verdict
  while we own an active dispatch is now itself a stop condition, and the reset
  authorisation never reads the verdict -- so an unsafe world cannot block the
  response to itself.

- **A headroom stop reports as one.** It reported `target_reached`, which told a
  reader the plan had been met when in fact the pack had run out of room. On a sunny
  capped run those are different outcomes and now read differently.

### Preserved

No Stage-A allocation change, no reserve economics change, no optimizer change, no
carry-forward or supersession semantic change, no R2 change, no grid-budget or
headroom arithmetic change, no new permitted service and no `timer.cancel`.
`economic.py`, `reserve.py`, `policy.py`, `battery.py`, `simulation.py`,
`realized.py`, `storage.py` and `control.py` are byte-identical to beta.23, and the
carry-forward, ownership, command-planning and gate functions were compared body by
body against it.

### Known and outstanding

- **Whether re-activation refreshes the device dead-man is unproven on hardware.**
  The software detects failure and stops rather than assuming success. This is the
  first thing to watch on a real Live run.
- **R2 remains open.** Withdrawal is inferred from the absence of an affirming
  publication, so a transient absence would still discard a carried run.
- **beta.22's capped-charge observation is still outstanding**: a naturally
  occurring charge run with a non-null `max_end_energy_kwh`.
- A restart whose persisted evidence cannot prove ownership leaves one charge to the
  device dead-man. Bounded by `control_horizon_minutes`, and deliberate.

## [1.0.0-beta.23] - 2026-08-25

**A carried run ended correctly and could not say why.** Reporting only. The
lifecycle, the economics and the carry-forward rules are unchanged.

**LIVE EXECUTION REMAINS DISABLED.** `CONTROL_EXECUTION_AVAILABLE` is still
`False`. beta.23 is published for continued Phase-2 Shadow observation.

### Fixed

- **Shadow could not report why a run stopped, for any reason at all.** Every
  branch of the controller read `stop_reason=<reason> if owned else None`. Shadow
  never acquires ownership by design, and with the release barrier closed no mode
  can reach it -- so the field was always absent in practice, and the one fallback
  phrase was the only wording the Activity log could ever produce, for all eight
  reasons the controller computes.

  The sharpest form of it was a single decision contradicting itself: the correct
  reason was computed one line above and discarded by the ternary, while the
  target on the adjacent line was kept. That is why a real log line carried the
  8.06 kWh figure and no reason beside it.

  The reason is now reported regardless of ownership. It authorises nothing -- it
  has two readers, the wording layer and the diagnostics payload, and a structural
  test pins that. The physical stop is driven by `reset_required`, which keeps its
  ownership gate and its call-site check.

- **The end reason survived exactly one refresh.** `carried.ended_reason` is set
  only on the refresh a run ends, so a diagnostics download taken later carried
  nothing about it and the answer had to be reconstructed from two snapshots and
  the event ring. `execution.carried.last_ended` now retains the last ended run --
  reason, identity, intent, window, target, realized, remaining and the basis the
  lifecycle machine observed.

  Session-local and deliberately **not** persisted: it records what this session
  saw, and a restart forgets it rather than restating a stale claim as a fact. It
  is written only when a run actually ends, never on an affirmation or an ordinary
  refresh.

- **An unrelated publication could hide the end reason.** The machine reaches the
  withdrawal reason only through the branch it takes when nothing is selectable.
  The Stage-B target list carries every run the plan contains, an `export`
  recommendation included, so on the refresh a charge campaign was withdrawn
  another publication could be actionable, another branch was taken, and the
  verdict the carry machine had *watched* was dropped. Filled in once now, in a
  shell around the state machine, so a branch added later cannot forget it. It
  adds a reason and moves no other field.

- **Lifecycle Activity lines were a paragraph, or an identifier.** The wording was
  one line: the reason or a fallback. In Shadow it printed the fallback inside a
  three-clause sentence; in Live it would have interpolated the raw constant, as
  in "Dispatch stopped: grid_energy_ceiling." Each of the thirteen declared
  reasons now has its own plain phrase, and the line is one short clause -- what
  the battery was doing, why it stopped, and how far it got.

  A Shadow line still states that no command was sent. On a release whose whole
  claim is that it executes nothing, that is the one clause it cannot lose.

### Added

- **The real 8.06 kWh incident as a regression sequence.** On 2026-08-25 a carried
  `grid_charge` run for 8.06 kWh, admitted 12:50 with a window of 13:00-16:30,
  ended at the 15:00 refresh with ninety minutes left and 6.30 kWh outstanding,
  reporting only "Shadow run finished: plan ended." while the Economic Action
  became `export`.

  Nothing was wrong with the decision. The pack had filled 3.43 kWh from
  production, headroom became binding, and the remaining 6.30 kWh no longer
  fitted; Stage A stopped publishing the campaign and the run was withdrawn. The
  export recommendation neither affirmed nor superseded anything -- an export is
  not an executable Stage-B intent, so it was never a candidate. The two events
  were consequences of one cause, not cause and effect.

  The whole sequence is now replayed as a test, through the real controller and
  again through the real coordinator report, and every reason the controller can
  assign is asserted at ownership `none` -- the mode the release actually runs in,
  and the arm the previous stop-reason tests never exercised.

### Preserved

No Stage-A allocation change, no reserve economics change, no grid-budget change,
no ownership change, no carry-forward or supersession semantic change, no actuator
mapping change, no new permitted service, and no storage migration. `const.py`,
`economic.py`, `reserve.py`, `policy.py`, `battery.py`, `simulation.py`,
`realized.py`, `safety.py`, `storage.py`, `alphaess_device.py` and
`alphaess_adapter.py` are byte-identical to beta.22.

### Phase-2 status

**R2 is not solved and is not addressed here.** Withdrawal is still *inferred*
from the absence of an affirming publication, because the Stage-A contract carries
no tombstone. A transient absence -- one refresh without a publication, then the
campaign returns -- would still discard the run identity and reset its progress
and grid budget. Changing that needs its own evidence and its own approval; no
hysteresis, grace period or delayed withdrawal was added.

Still outstanding from beta.22: **a naturally occurring capped charge run with a
non-null `max_end_energy_kwh`**. The corrected headroom cap has never been
exercised on real hardware. Do not manufacture that run by adjusting Stage A.

## [1.0.0-beta.22] - 2026-08-25

**A real Shadow diagnostics download, and the four things it caught.** No new
behaviour, no allocation change, and nothing that brings Live any closer.

**LIVE EXECUTION REMAINS DISABLED.** `CONTROL_EXECUTION_AVAILABLE` is still
`False`. beta.22 is published for continued Phase-2 Shadow observation.

### Fixed

- **`projected_end_energy_kwh` counted expected production twice, and mixed AC
  with DC.** A snapshot published **31.946 kWh for a 22 kWh pack**. The formula
  was `stored + expected_pv + remaining`, and the production term was already
  *inside* the remaining delivery: `battery_target_kwh` is the sum of
  `expected_pv_to_battery_kwh` and `expected_grid_to_battery_kwh`, both built from
  the same run charge, so the production share is a component of the target rather
  than an addition to it. The snapshot reconciled the error to 0.002 kWh.

  It now reads stored energy plus the energy still to deliver, converted from AC
  to DC exactly once using the pack's own efficiency. Where the conversion cannot
  be made the field publishes `null` rather than a figure — mixing the two
  boundaries unannounced is what produced the impossible number. The result is
  **not** clamped to capacity: a projection above the pack ceiling says the plan's
  remaining target does not fit, which is information a reader needs rather than a
  fault to hide.

  Diagnostics-only. The field had one reader and it was the payload.

- **The Stage-B headroom cap subtracted expected production twice**, and this one
  did affect what would be charged. `max_end_energy_kwh` is the optimizer's *own*
  projected stored energy at the end of the run —
  `start_energy_dc_kwh + battery_delta_dc_kwh` off the chosen trajectory — so it
  already contains every kilowatt-hour the run charges. Subtracting the production
  still expected inside the window removed the same energy a second time, and the
  pack finished short by exactly the production the plan meant to store.

  On a 22 kWh pack holding 10 with a landing figure of 18 and five kilowatt-hours
  of production expected in the window, the old cap allowed 0.75 kW and finished at
  **12.85 kWh**; the corrected cap allows 2.108 kW and reaches **18.00 kWh**. The
  error was fail-safe — it could only under-charge — which is why it survived: on a
  sunny capped run it quietly declined most of an approved plan.

  The allowance is a DC stored-energy figure and the cap is compared against an AC
  power, so that crossing is now made once as well. The invariant is asserted
  rather than described: holding the cap lands the pack on `max_end_energy_kwh`
  exactly, from any starting state. Stage B still only ever reduces or stops, and
  an absent ceiling still means unconstrained.

- **The per-quarter reserve requirement was read off the wrong axis.**
  `planning_reserve_kwh` is positioned by horizon offset and beta.21 indexed it
  with the interval's own index, so on a horizon starting at interval 44 every
  published requirement belonged forty-four intervals later and everything past
  the horizon length read `null`. The snapshot showed interval 44 carrying
  12.39 kWh where its requirement was 5.67.

  This one was introduced by beta.21's own observability feature, and the suite
  could not see it: every synthetic horizon starts at interval 0 with a flat
  reserve, so the two axes coincide. The regression now builds a horizon starting
  at 44 with a varying reserve, and it was verified to fail with the bug
  reintroduced. Diagnostics only — the solver always read the array correctly.

- **Two revisions in one payload, both correct, neither labelled.** A snapshot
  showed `execution.revision` of 13 beside `carried.run.revision` of 2. The first
  is the Stage-A publication Stage B admitted, *frozen at admission* — a carried
  run holds that publication by reference for its whole life, so the whole group
  around it is frozen too, `stale_after` included. The second counts material
  changes since admission.

  It reached 13 legitimately: `plan_id` is `sha256(intent | window_start)` over an
  absolute instant, so while a campaign still sits ahead of the horizon front its
  identity is stable and Stage A's revision climbs as the forecast firms. No key
  was renamed and the payload was not reshaped; an `admitted_publication_rule`
  beside them names the frozen group and points at `carried.publication` and
  `carried.run` for the live figures.

### Added

- **Regression coverage for the charge-setpoint tracking question.** A measured
  helper test charged about 1.135 kW against a 1.0 kW setpoint, which raised
  whether the corrected cap — now targeting `max_end_energy_kwh` exactly — needs a
  margin. It does not, and no margin was added.

  The excess is 135 W against the 177 W residual this project's own energy-balance
  model already allows at those power levels, and a whole quarter-hour of it is
  38 % of one step of the state-of-charge sensor, so one sample cannot establish a
  ratio. More importantly the cap is recomputed every refresh from *measured*
  stored energy, so an interval that charges above its setpoint arrives next
  refresh as a fuller pack and the allowance closes by exactly that much. Only the
  final interval is uncorrected, and that exposure is a quarter of one state-space
  bucket for a run on schedule — inside the quantisation margin Stage A already
  publishes as irreducible.

  Two tests pin the reasoning rather than the numbers: that the cap stays
  closed-loop on measured stored energy, and that one interval of tracking error
  stays bounded against the installation's own lattice margin. If the cap ever
  became open-loop, the first fails.

### Preserved

No Stage-A allocation change, no reserve economics change, no grid-budget change,
no ownership change, no carry-forward semantic change, no actuator mapping change,
no new permitted service. `reserve.py`, `policy.py`, `battery.py`,
`simulation.py`, `realized.py`, `safety.py`, `storage.py`, `alphaess_device.py`
and `alphaess_adapter.py` are byte-identical to beta.21, and the dynamic
program's recursion is untouched.

### Phase-2 status

beta.22 is published for continued Phase-2 Shadow observation. The next required
real-world evidence is **a naturally occurring capped charge run with a non-null
`max_end_energy_kwh`** — the corrected cap has never been exercised on real
hardware, because no snapshot so far has carried one. Two or three diagnostics
snapshots taken during the same such run would settle it.

Do not manufacture that run by adjusting Stage A.

## [1.0.0-beta.21] - 2026-08-25

**A configured setting that did nothing, and a reported figure that misled.**
Two narrow fixes, no change to how energy is allocated and no change to Stage B.

**LIVE EXECUTION REMAINS DISABLED.** `CONTROL_EXECUTION_AVAILABLE` is still
`False`. Phase-2 shadow observation of a complete real `grid_charge` window is
still required before any Live commissioning is considered.

### Fixed

- **`grid_charge_margin_eur_per_kwh` never reached the solver.** The option was
  configurable, `solve` accepted it and used it, `build_outcome` forwarded it, and
  the executor function between the coordinator and the solve had no such
  parameter — so the value was dropped and every solve ran at the `0.0` default.
  A user who set a margin got no effect on planning.

  Stock installs were unaffected, because the default *is* zero. That is also why
  it survived a full test suite: every existing margin test calls `solve`
  directly, so all of them passed while the setting did nothing in production.
  The new tests go through the executor path instead, which is the layer that
  dropped it.

  `async_add_executor_job` passes **positionally**, so a missing parameter is a
  silent no-op and a misplaced one is worse — it would have applied the fixed
  trade gain as a per-kWh margin. There is now a structural test that the call
  passes exactly as many arguments as the function declares, and a mutation test
  for each failure separately.

  **Zero remains exactly inert**, asserted across four price spreads on the whole
  interval trajectory and the plan cost rather than on a summary. A positive
  margin now raises the advantage a marginal grid-caused kilowatt-hour must earn
  before it is bought: measured, a twelve-cent gross spread is taken at margin
  `0.00` and refused at `0.10`, while a sixty-cent spread is still taken at
  `0.25`. Export allocation is unaffected — the margin is charged on marginal
  grid-caused *charging*, and selling is not charging.

### Added

- **A bounded per-quarter allocation breakdown for each published run**, in
  diagnostics. Per quarter: the interval index, the action, both prices, battery
  power as an unsigned magnitude with direction carried by the action, battery
  charge and discharge energy, opening stored energy, site grid flows, the
  marginal grid import and export the interval actually caused, its marginal cost
  against doing nothing, that interval's reserve requirement, and — the field
  that resolves the ambiguity — whether the quarter was **absorbing** production.

  This exists because a window and a total cannot distinguish "the campaign spans
  thirteen quarters" from "energy is spread across thirteen quarters", and those
  are different plans. On a real shape the answer was two quarters buying at
  10 kW inside eleven quarters of free production absorption, reported as one
  campaign averaging 3.50 kW.

  Bounded to a shared budget of forty-eight rows across at most eight runs — a
  quarter of the full trajectory, longer than any real campaign — allocated in run
  order so the runs a reader sees first are complete, with any shortfall stated
  rather than silently trimmed. The full 192-row trajectory is still not
  serialised.

- **`peak_power_kw` on each published run**, a maximum over the same intervals
  the run already sums.

### Changed

- **Activity no longer offers a campaign mean as the dispatch intensity.** Where
  the peak differs materially from the mean it now says both — *"peak 8.00 kW,
  campaign average 4.46 kW"* — and where they agree it still says one figure, so
  the line is no noisier than before. Per-quarter detail stays in diagnostics.

### Notes

- **No allocation changed.** The dynamic program's recursion is byte-identical:
  no objective change, no post-processing, no reallocation, no window shortening,
  no change to reserve protection, switching-fee semantics or battery limits. A
  separate investigation established that the optimizer already concentrates
  buying and selling in the best feasible quarters, and that long windows are the
  reserve trajectory and production absorption rather than dilution.
- `execution.py`, `alphaess_device.py`, `alphaess_adapter.py`, `reserve.py`,
  `policy.py`, `battery.py`, `simulation.py`, `realized.py`, `safety.py` and
  `storage.py` are byte-identical to beta.20.
- The permitted service set remains closed at **three**, with no timer service.

## [1.0.0-beta.20] - 2026-08-24

**Phase 1 complete. Phase 2 shadow validation build.** The whole Stage-B command
path now exists, end to end, and is exercised on every refresh: a Stage-A
`grid_charge` target becomes a carried execution run, becomes actionable when its
window actually opens, becomes a charge intent, becomes a complete six-step
AlphaESS command with a positive unsigned magnitude and an upper state-of-charge
cutoff -- and is then refused, whole, at the final barrier.

**LIVE EXECUTION REMAINS DISABLED.** `CONTROL_EXECUTION_AVAILABLE` is still
`False`. No command can reach the inverter, by a constant in the source rather
than by a setting. `applied_kw` is zero, `executed` is false, the owner marker is
never written and ownership is never acquired.

beta.20 is published for one reason: so at least one complete real `grid_charge`
window can be observed in shadow, on real hardware, before any Live commissioning
is considered.

### The defect this release exists to fix

beta.19 shipped a controller that was correct and connected to nothing.
`request_kw` was computed, published, and read by no one -- the command was built
from the Phase-3 reserve-guard plan, which never charges. Flipping the barrier
would have armed a **discharge** while the economic plan asked to buy.

Fixing the wire exposed a second problem that only measurement could find. Ten
consecutive real refreshes showed the controller stuck in `prepared` for ever,
because activation is strictly inside the window -- correctly, since arming this
hardware delivers energy immediately -- while every refresh rebuilds the economic
horizon from the *next* interval boundary. A freshly published target therefore
always opens fifteen minutes from now, and can never open its own window.

The run whose window opens is the one accepted a refresh earlier. So Stage B now
carries it.

### Added

- **Carried execution runs.** Stage B mints its own `run_id`, stable for the life
  of a run, and Stage A is unchanged. Progress, the cumulative grid attribution
  and the ownership record all key on it rather than on the publication -- which
  matters, because Stage A's `plan_id` is `sha256(intent | window_start)` and so
  churns every fifteen minutes as the horizon rolls. Keying on it would have reset
  every one of those figures every quarter.

  Each refresh the carried run is *affirmed* if the fresh publication holds a run
  of the same intent whose window overlaps the accepted one. Rolling movement
  always overlaps; a campaign Stage A has moved to tonight does not, and the
  carried run is withdrawn. The accepted window is never moved by a later
  publication -- that is precisely what makes activation reachable.

  Four bounds keep a carried run short: withdrawal on the first non-affirming
  refresh, a freshness deadline re-anchored on each affirmation, its own window
  end, and the grid ceiling. A restart discards the carried run and keeps the
  ownership record, because those are different questions.

- **A cumulative grid-energy ceiling.** `expected_grid_to_battery_kwh` was parsed
  and compared against nothing. It is now a hard ceiling, enforced in the terms
  Stage A published it in: an attribution estimate that credits production first
  and the grid second, monotonic by construction, accruing nothing across a
  measurement gap, and attributing more to the grid where readings disagree so the
  cap binds earlier rather than later.

  Headroom does not cover this. The headroom cap bounds stored energy at window
  end, not how much of it was bought -- if production disappoints, the ceiling
  correctly *rises* and the controller would fill it from the grid.

- **A commissioning grid-charge budget** on the Control page, and the
  execution-enable consent beside it. The budget is a tightener only: zero means
  the tightener is **off**, never a ban on charging, and it can only ever bind
  earlier than Stage A's own ceiling.

- **Diagnostics a first Live day can actually be read from.** The Stage-A
  publication and the carried run are published side by side under different
  names, because conflating them was the whole bug. Plus the cumulative grid
  estimate with its cap and remainder, the quantised physical power, and the
  device's own dispatch readback -- there was previously nothing in that block to
  compare a request against.

### Fixed

- **Stage B is the command source for a grid charge.** Evaluated before the
  command is built, so the reserve guard cannot already hold the pending command
  when the window opens. Everything else keeps the Phase-3 behaviour byte for
  byte: Stage B returns an intent only for `grid_charge`, and the reserve guard
  never emits a charge, so the two can never compete for the same action.

- **No activation before the window opens.** Selection still looks one interval
  ahead -- it must, or nothing is ever selected -- but activation is strictly
  inside the window, and they are now separate questions asked by separate
  methods. The pre-window power is no longer diluted across window-plus-lead.

- **The charge cutoff is an upper bound.** It was the configured discharge floor,
  for both directions: roughly 21 % written as a charge cutoff while the pack sat
  at 61 %. A charge now takes the applicable ceiling, truncating downward because
  for an upper bound that is the conservative direction, and a charge with no
  establishable ceiling is **refused** rather than given a substituted default.

- **Progress accumulates across quarter boundaries.** `QuarterAccumulator` zeroes
  at every boundary, so reading its open quarter as run progress sawtoothed -- and
  near a window's end, with a small denominator, asked for about 35 kW. Closed
  quarters are now accumulated into a run total, and a revision bump no longer
  re-baselines delivery already made.

- **Ownership can be established, and a run can be stopped.** The ownership
  evidence was hardcoded to `None`, so `owned` was unreachable and the causal
  record was never written; `plan_reset` and `plan_release_marker` were defined,
  tested and never called, so nothing could stop a run or release the marker that
  arming sets. All of it is wired now: the record is persisted *before* the
  writes, the marker is released as the *last* step of a stop, and the claim is
  cleared only once the stop has actually landed -- so an interrupted stop leaves
  enough evidence to retry rather than dropping to `unproven`, which is never
  touched again.

- **The write boundary cannot send the wrong direction.** A third interlock
  validates the planned entity list itself rather than the intention that built
  it, and refuses a malformed command in full -- there are no partial writes. The
  cooldown gate is direction-aware; comparing raw kW made a 3 kW charge followed
  by a 2 kW discharge read as a decrease.

- **A command can no longer name a power the inverter cannot hold.** With no
  headroom cap in force, a run late in its window asks for
  `remaining / remaining_hours`, which grows without bound; a real campaign
  reached 21.68 kW against a 20 kW register. Unclamped, the safety gate refused
  the command outright -- so a late charge did not run at the maximum, it did not
  run at all.

- **The export-absorption limit governs a discharge only.** It is the capacity the
  house can absorb, and its purpose is to stop discharged energy reaching the
  grid. A charge cannot export, so clamping one against it would have silently
  under-delivered an approved plan for a reason that does not apply to it.

- **The coordinator survives a failed write.** The send site dereferenced a block
  that is `None` in every reachable state, and sat outside the safe wrapper -- so
  the first authorized send would have taken down the whole refresh. `applied_kw`
  now comes from the power actually written rather than from the request, and a
  failed write costs the write rather than the refresh loop that would retry it.

- **The dead-man is bounded by the remaining window** as well as by the configured
  horizon, so a command armed in a window's last quarter no longer carries a
  timeout that outlives it.

- **Activity no longer says an actuator is missing when one exists.** The
  capability comparison was computed once, plan-wide, and stamped onto every run,
  with the sentence *"No actuator in this release can do that"* hard-coded. It is
  now per run and keyed on the reason that actually fired.

### Honest limits

- **Withdrawal is inferred, not signalled.** Stage A publishes no withdrawal
  tombstone: a run it has dropped and a run it has rolled forward are both simply
  absent from the next publication. The overlap test is an inference from
  instants -- a good one, and deliberately not described as more than that. No
  prices, no ranking and no second solver were added to Stage B to compensate.

- **The real measured grid attribution has not been observed yet.** Its arithmetic
  is unit-tested and its monotonicity holds under hostile inputs, but in shadow no
  energy moves, so its behaviour on real measured production and battery power is
  a Phase-2 observation rather than something this release proves.

- **A supersession costs one interval.** The old run ends and resets on the
  refresh that fails to affirm it, and a new one is admitted no earlier than the
  refresh after -- so a reset always lands before a new claim. That ordering was
  chosen over admitting and ending together.

### Notes

- Stage A economics are untouched. `economic.py`, `reserve.py`, `policy.py`,
  `battery.py`, `simulation.py`, `realized.py` and `safety.py` are byte-identical
  to beta.19.
- The permitted service set remains closed at **three**. No timer service was
  added: hardware testing established that deactivating the dispatch stops the
  action and clears the vendor timer on both control surfaces.
- The storage version deliberately does not move. beta.19 defined the causal
  record but never wrote one, so no stored document contains the older shape.

## [1.0.0-beta.19] - 2026-08-24

**Stage B, the physical execution controller — built, wired in, and executing
nothing.** It reads a Stage-A target, measures what has actually been delivered,
works out the power that would finish the job inside the window Stage A chose, and
stops there. `CONTROL_EXECUTION_AVAILABLE` remains `False`, so no command can reach
the inverter and no actuator is reachable from the controller's path.

That split is deliberate. This release introduces the controller; a separately
approved beta.20 will change the one constant that lets it act. The alternative —
shipping the actuator and removing the barrier together — would have meant the
release that introduces the write is also the release that removes the thing
preventing it, and a defect in either would be harder to isolate.

### Added

- **`execution.py`** — the controller, and **pure**: no Home Assistant import, no
  price module, no economics. It is handed every economic quantity as data.

  The rule it exists to hold is worth stating precisely, because two things that
  look like one rule are not:

  - the **rolling controller may raise power**. Being behind schedule on an
    already-approved target inside its own window is exactly what it is for, and
    refusing to catch up would quietly under-deliver a plan Stage A chose. It
    raises the *rate*, never the *amount*;
  - the **PV/headroom cap may only lower** what the rolling controller asked for.
    It is applied afterwards and can reach zero, which means stop.

  Progress is **measured**, never `setpoint × elapsed`: a clamp, a limit, a cloud
  or a full pack each make what arrived differ from what was asked for, and a
  controller trusting the request compounds its own error every refresh. Two bases
  are published rather than reconciled — an integral of measured battery power
  within the quarter, and a state-of-charge difference that survives a restart.
  Where they disagree the disagreement is the information.

- **Two-factor ownership.** Stage B may modify or stop only a dispatch it can prove
  is its own, and the AlphaESS surface cannot prove that: every arming path is
  driven by helper *values*, so a dashboard-armed dispatch and a service-armed one
  leave byte-identical state. The project's own position has been that matching
  parameters is *worse* than no evidence, because the person watching Shadow is
  exactly the person who would set those same figures by hand.

  So ownership rests on two things outside that surface, and **both** are required:
  an owner marker (`input_boolean.alpha_ems_dispatch_owner`, turned on as the first
  step of arming and off as the last step of resetting) and a persisted causal
  record tying the claim to the dispatch the inverter reports. A marker alone is
  `unproven`; a record alone is `foreign`; neither is `owned`.

  | dispatch | marker | record | verdict |
  |---|---|---|---|
  | running | on | matches | **owned** — controllable |
  | running | off | — | **foreign** — never modified, never reset |
  | running | on | missing or contradictory | **unproven** — also never touched, but reported as a fault |
  | not running | on | — | stale marker, cleared safely |

  The marker costs **no new permitted service**: `turn_on` and `turn_off` were
  already in the closed set of three. `OWNERSHIP_PROVABLE` stays `False`, because
  what changed is not that the vendor surface became provable — it is that Alpha
  EMS stopped depending on it.

- **An explicit stop path**, `plan_reset()`, separate from arming rather than a
  branch inside it. The two run in opposite orders for opposite reasons: arming
  settles its parameters before switching on, and resetting switches off first, so
  an interrupted reset leaves the dispatch *off*. **Setting power to zero is not a
  stop** — a dispatch left armed at zero still holds a duration, a cutoff and a
  timer, and the next run would inherit them, so a short run following a long one
  would silently acquire the long one's dead-man.

- **An `execution` diagnostics block**, and it is the surface this release is meant
  to be validated from. Every Stage-A expectation sits beside what actually
  happened — expected production against measured, expected house load against
  measured, expected grid contribution against measured — so a deviation is
  readable rather than something a reader has to compute. `applied_kw` is `0.0` and
  `executed` is `false` on every path.

- **A START/STOP Activity lifecycle**, and nothing else. A six-hour run produces
  two lines, not twenty-four: routine quarter-by-quarter corrections are silent by
  construction, because the dedup compares the *intent* rather than the run's start
  instant. Shadow gets its own event kinds (`would_start`, `would_stop`) on the
  *advice* side of the vocabulary, so a Shadow line cannot be filed as execution
  even by mistake — `started` and `stopped` remain execution kinds and remain
  refused while the barrier stands.

### Changed — the Stage-A contract, additively

Four fixes, all published figures, none of which changes a plan. Every one is an
aggregate or projection of something the solve had already computed for the plan it
had already chosen, and a suite proves inertness: same objective, same runs, same
reserve behaviour, same terminal behaviour, and still **three solves**.

- **Freshness is anchored to the issue instant, not to the window.** beta.18
  derived `stale_after` from `window_start`, which made it useless for the one job
  its name describes: a run eighteen hours out carried a freshness deadline
  eighteen and a half hours out, so a target could be stale by any ordinary meaning
  of the word and still be inside it. `issued_at` is now published beside it, the
  window remains a separate fact, and **`stale_after` is enforced** — a stale target
  may not start, and an owned run whose target goes stale is stopped.

- **`first_power_kw` is published**, because `initial_average_power_kw` is the run's
  *mean* and always was, despite the name. The old field keeps its name and its
  value; the honest figure sits beside it rather than quietly replacing it.

- **The charge-window balance.** For every `grid_charge` target Stage A publishes
  expected production, expected house load, expected production *reaching the
  battery*, the expected grid contribution as a **maximum**, and `charge_source`.

  The middle one is the point: **expected production is not production available to
  the battery.** The house consumes throughout the window and its share is taken
  first, so a fifteen-kilowatt-hour afternoon with five kilowatt-hours of load
  offers substantially less than fifteen to the pack. Publishing the gross figure
  would have invited Stage B to preserve headroom against energy the house was
  always going to eat.

- **The headroom constraint** — `required_headroom_kwh`, `max_end_energy_kwh` and
  `headroom_until`, and this is what keeps economics out of Stage B entirely.

  The problem: an old cheap-grid charge target is still live while substantial
  production is forecast before a later window. Charging the pack full early
  displaces production the plan meant to absorb. Deciding *how much* headroom is
  worth keeping is an economic question — so Stage A answers it, publishes the
  answer, and Stage B does arithmetic against it. Stage B never reads a price,
  never identifies an export window, and never computes a headroom of its own. If
  honouring the constraint would need a *different economic decision* rather than
  merely less execution, it reduces or stops and waits for a fresh revision.

  **`null` means unconstrained, never zero** — zero would forbid the pack from
  filling at all, and reading absence as zero is a one-character mistake with the
  opposite effect.

- **An owned dispatch no longer inhibits Alpha EMS.** Strictly additive: the gate
  is handed an ownership *verdict* rather than the evidence, `dispatch_owned`
  defaults to `false` so every existing path keeps beta.18's behaviour, and a
  foreign dispatch still stops everything exactly as before. This was the second of
  the two blockers named in `CONTROL_EXECUTION_AVAILABLE`'s own documentation —
  until now Alpha EMS would have inhibited itself the moment it armed anything.

- **The control mode reads "Live" instead of "Active"**, while the stored value
  stays `active`. The integration had no `entity` translation block at all, so
  every enum state rendered as its raw value; there is one now, in English and
  Dutch. The value does not move, because a restored entity and every stored
  document already use it.

- **`STORAGE_MINOR_VERSION` 4 → 5**, additive. The learning document gained an
  optional `execution` key holding the published revision of each target and the
  causal ownership record. Both must survive a restart: a revision that reset to
  one on every reboot would tell Stage B that every target it had been tracking for
  hours was brand new, and a causal record that did not survive would make an owned
  dispatch indistinguishable from a stranger's. Absent on every earlier document,
  and read as "nothing was running" rather than as a claim. **Progress is
  deliberately not persisted** — it is re-measured from evidence, because a restart
  must never replay a target from the beginning.

### Not in this release

**No Live execution.** `CONTROL_EXECUTION_AVAILABLE` is `False`, `PERMITTED_SERVICES`
is 3, and a runtime proof registers real handlers for every service the integration
may call, runs the whole controller across a simulated day in the most permissive
mode this release can reach, and records **zero** writes — including zero
acquisitions of the owner marker.

**No export or curtailment actuation.** `serve_load` and `net_export` are computed
and diagnosed only. Their physics is pinned — 1.3 kW of net export against 0.9 kW of
house load needs 2.2 kW of battery — but nothing drives them, and `export` still has
no primitive in the capability layer. That contradiction is named rather than
papered over, and is future work. PV curtailment remains absent entirely: no
negative-price rule, no hidden heuristic.

### Two gates that must close before Live

Neither is solved here, and neither is a defect in beta.19 — it sends nothing. Both
are conditions on the release that *would* send something.

**The vendor timer.** The reset sequence assumes the AlphaESS package's own
automation clears the dispatch timer when the activation boolean goes off. That
cannot be established from source, and `timer.cancel` is not a permitted service.
Before beta.20, on the real installation: `activate` off must stop the dispatch,
leave the vendor timer inactive, leave no stale timer behind, and let the next
dispatch start clean. If it does not, beta.20 stays blocked — widening the service
set needs its own review rather than a quiet addition.

**One-interval-ahead arming, and this one is measured rather than merely
unverified.** The economic horizon begins at the *next* interval boundary, so the
controller treats a window opening within one planning interval as actionable —
without that it selects nothing, ever. Measured on the current implementation:
fifteen minutes before a window opens, the controller reaches `armed` and computes a
real request.

Arming an AlphaESS dispatch starts it immediately. So **as it stands, flipping the
barrier would begin delivering battery energy before the Stage-A window opens.** In
Shadow that is harmless and invisible; in Live it would be a plan executed early.

Preparing a command before a window and delivering energy inside one are different
things, and beta.19 does not yet distinguish them. **beta.20 is blocked until it
does**, and the correction belongs there rather than here — it changes when energy
physically moves, which is exactly what this release is not permitted to decide.

### Two defects found during implementation

Both were found by walking a simulated day rather than by a unit test, and both are
now pinned.

- **The controller was acting on the previous refresh's plan.** The execution
  targets were built *after* the control report, so Stage B read a target one
  quarter stale — with a `plan_id` and `revision` describing a plan it was not
  executing. Nothing failed loudly; the figures were simply wrong.
- **Strict window containment selected nothing, ever.** The economic horizon begins
  at the *next* interval boundary, so a run planned at 09:00 opens at 09:15 — and a
  controller asking "does this window contain now?" sat idle beside a perfectly
  good target through an entire simulated day. A dispatch armed now runs through the
  coming interval, so imminence within one planning interval is the correct reading,
  and it is the same gate the Activity surface already used.

### Verification

Tests **2940 → 3073**, 1 skipped, 0 failed. Mutation suites extended with 21 Stage-B
mutations, zero survivors. Ruff clean, format clean, `git diff --check` clean. Entity
count **13**, `Economic Action` at exactly **8** attributes, `PERMITTED_SERVICES` **3**,
`reserve.py` unchanged, both storage majors unchanged, no learning reset.

## [1.0.0-beta.18] - 2026-08-22

**Stage A completion.** The economic planner gains the one thing it was missing
before physical execution could be built on top of it: a target a controller can
act on without guessing which side of the meter a number refers to. Plus a per-kWh
requirement on bought energy, and realised figures beside the forecast ones.

**Nothing is executed.** `CONTROL_EXECUTION_AVAILABLE` remains False, there is no
Stage B, and no actuator exists for any of this.

**A serious pre-existing defect was found in this pass and is fixed in it.** The
terminal condition refused the dearest quarters of the day whenever they were the
last ones it could see. See *Removed* below.

### Added

- **`grid_charge_margin_eur_per_kwh`** — an additional economic requirement, in
  euros per kilowatt-hour, on energy a charge actually causes to be bought.
  **Default 0.0, which is exactly the previous behaviour**, so upgrading changes
  nothing until it is deliberately set.

  It exists because the existing `minimum_trade_gain_eur` is a *fixed* amount and
  does not scale. Measured on the released beta.17 optimizer: once a trade cleared
  the fixed gain, the volume behind it was unconstrained, and a **14.230 kWh**
  round trip was planned while earning **0.0371 EUR per grid-caused kWh**. The two
  settings are different quantities and both remain — a fixed amount a run must
  earn to be worth starting, and a per-kWh requirement on the energy it buys.

  Charged on **marginal grid-caused charging**, which is what makes four exemptions
  structural rather than written:

  | not charged | why |
  |---|---|
  | ambient production absorption | causes no import beyond the idle baseline, so the basis is zero |
  | the sun's share of a mixed quarter | only the grid share is the basis |
  | discharging to supply the house | not charging at all |
  | charging to protect the reserve | the objective compares `(violation, cost)` lexicographically, so no cost can outrank keeping the house supplied — proven at margins up to 10 000 EUR/kWh |

  **It is not a degradation model.** The boundary is strict: a trade is taken while
  its net benefit per kWh *exceeds* the margin, and one exactly equal to it is
  indifferent and not taken. On the measured shape the flip is at
  **0.037129 EUR/kWh**.

- **`execution_target`** in the diagnostics economic block — one entry per planned
  run, and the contract a future Stage B will be written against. **Consumed by
  nothing.** Three properties are the point, and each was a defect waiting to
  happen:

  - **Two boundaries, two fields.** `battery_target_kwh` is at the battery;
    `grid_target_kwh` is at the meter and is present only for a net export. The
    existing per-run `energy_kwh` changes meaning with the action, and on the live
    installation 1.3 kW of intended export needs **2.2 kW** of battery against
    0.9 kW of house load — a consumer handed one generic figure would command 1.3
    and deliver 0.4.
  - **Absolute instants.** `window_start` / `window_end` / `stale_after`, never
    horizon indices. An index moves every quarter, which is exactly what made the
    beta.16 Activity log repeat itself.
  - **Identity that survives replanning.** `plan_id` is `(intent, start instant)`,
    so a run keeps its identity as its remaining energy shrinks; `revision`
    increments only when the target moves beyond one state-space bucket, so
    floating-point drift cannot cause control jitter.

  `intent` is `grid_charge`, `serve_load`, `net_export` or `hold` — separating a
  load-serving discharge from a net export, which the action label only half does.
  There is deliberately **no `curtail_pv` intent**: no actuator can decline
  production, and offering one would advertise a capability that does not exist.

- **`realized`** in the same block — what today actually cost, from measured flows
  at the prices recorded for the same intervals. Import cost, export revenue, net
  cash flow, both grid energies, battery movement, and load avoidance.

  **No new storage.** The evidence was already being recorded per interval and per
  day: house baseline, production, state of charge, and grid import and export.
  This is a multiplication over data already on disk, so the storage version is
  **unchanged**.

  **Deliberately not published: `trade_profit_eur`.** Attributing a discharged
  kilowatt-hour to a particular earlier charge needs an inventory convention —
  weighted average, first-in-first-out — and a battery has no physical ordering
  that makes either true rather than conventional. Beside figures that are
  measured, such a number would borrow a precision it has not got.

  Energy already in the pack is **opening inventory of unknown provenance** and is
  never priced. There is no cost basis anywhere, and a structural test proves the
  optimizer cannot import the module at all — "never sell below what this cost" is
  economically wrong, because energy that cost 0.20 is a sunk cost and selling it
  at 0.18 is correct when it makes room for something cheaper.

### Removed

- **The hold-end terminal floor.** The dynamic battery reserve is now the only
  physical floor the economic optimizer is given.

  The old rule ended the horizon no lower than doing nothing would have. That
  reads like a rule against dumping the battery, and it is not one. With no
  surplus production ahead, "doing nothing" is a *flat* line, so the requirement
  became **"never end lower than you are now"** — a prohibition on net discharge.
  When the most valuable quarters of the day are the last ones visible, selling
  into them ends lower, so it did not sell.

  It also **ratcheted**. The floor was recomputed from the current state of charge
  at every refresh, so a charge raised it, the next refresh inherited the raised
  value, and the pack was locked out of late value for the rest of the day. Rolled
  forward across an evening the enforced floor climbed **19.5 → 20.5 → 21.5 →
  22.0 kWh** and stayed there.

  Measured on a 19-quarter horizon ending in four quarters at 1.20 EUR/kWh from
  19.5 kWh:

  | | cost | sold into the peak |
  |---|---|---|
  | the old floor | **+0.87 EUR** | 1.17 kWh |
  | the reserve alone | **−5.52 EUR** | 8.29 kWh |

  Rolled forward over fourteen quarters of an evening peak, the old floor sold
  **0.000 kWh** into the dearest quarters and *bought* 0.450 kWh inside them; the
  reserve alone sells **6.665 kWh** and buys nothing.

  **What replaces it: nothing.** No continuation value, no salvage term, no
  boundary bridge, no second floor. The requirement it was standing in for already
  existed and is authoritative — the pointwise dynamic reserve, enforced at
  **every** interval rather than only at the end. The reason that is sufficient is
  a fact about the forecasts rather than a hope: the reserve's own demand forecast
  legitimately outlives the price horizon, because production is forecast for
  today *and tomorrow* while prices are only priced as far as they have published.
  On the live installation that is **143 reserve intervals against 47 priced
  ones**, so the requirement is at its most substantial exactly where the prices
  stop — 15.7 kWh in summer and 19.4 kWh in winter, against a 4.25 kWh configured
  floor. Energy above the requirement is discretionary by construction, and the
  objective is now free to trade it.

  An earlier investigation reached the opposite conclusion, and the mistake is
  worth recording because it is easy to repeat: its harness passed a **constant**
  reserve. A flat floor has nothing holding the tail of the horizon up, so removing
  the terminal bound appeared to empty the pack — dumping that the real pointwise
  profile prevents. Both the sweep and a mutation test now use the real recursion
  over demands that extend past the prices.

  **Safety: no new violations.** Across 200 seeded adversarial worlds — randomised
  price levels, production, load, publication time and forecast error, rolled
  forward with the optimizer and the scorer on separate information sets — the
  total reserve shortfall is **identical** to the old rule's, to the kilowatt-hour.
  Every remaining shortfall belongs to a world that starts *below* a saturated
  requirement, where no terminal rule can help and all candidates behave alike.
  Economically the same sweep gives the old rule **+39.694 EUR** of total regret
  against an oracle and the reserve alone **−19.629 EUR**; they choose identically
  on 175 of 200 worlds, and on the three where the old rule comes out ahead it does
  so by at most **0.4542 EUR**.

  **Where it loses, stated plainly.** The old floor is not worse everywhere. On a
  summer evening peak *with tomorrow's prices already published*, it finishes
  **0.63 EUR ahead** over fourteen quarters — it sells 3.843 kWh into the dear
  quarters against 4.318 kWh, and the extra it holds turns out to be worth more
  than the sale. Being able to see past the peak is exactly the case where "hold
  something back" is sound advice, and the sweep agrees: the old rule wins 3 of 200
  worlds. What it cannot do is tell that case apart from the one where the peak is
  the last thing it can see, and there — tomorrow unpublished, the ordinary
  situation every evening before publication — it pays **+3.7701 EUR against
  −4.0894**, having sold **nothing** into the peak and bought 6.835 kWh inside it.
  A rule that is right when it can see ahead and badly wrong when it cannot is not
  a rule worth keeping over one that is close to right in both.

  A boundary bridge — a floor at the reserve's own requirement at the price
  boundary — was tested as a candidate and is **bit-identical** to having no
  terminal floor at all, on every case tried. It is not adopted, because a second
  constraint that restates an existing one is a second place to keep correct.

### Changed

- **The terminal instrumentation is absent rather than zero.**
  `terminal_plan_cost_eur`, `terminal_plan_import_kwh`,
  `terminal_near_field_cost_eur` and `terminal_first_run_changed` are now `null`.

  They priced the removed constraint by re-solving with it relaxed. With no
  constraint the relaxed solve *is* the desired solve, so the difference would be
  identically zero — and publishing a zero would say the constraint is free, which
  is a different claim from there being no constraint. The fields are kept so that
  documents written by beta.16 and beta.17 still read back with the values they
  recorded; nothing is migrated to make old and new look continuous.

  A **fourth solve is no longer performed**, which is where the release's solve
  time comes back from.

### Unchanged, and re-verified

Import valuation still uses the source's all-in purchase price; export still uses
the reconstructed return price with the configured feed-in adjustment and VAT only
when enabled. Production opportunity cost is still priced through forgone export
revenue — the crossover is measurable at 0.18 EUR/kWh — and **no explicit term was
added**, because that would double-count. Remaining production is still read per
quarter. Today-plus-tomorrow semantics, the reserve model, Safety Buy, the bucket
selector, the beta.15 clamp and mode refresh, and the beta.16 Activity behaviour
are all untouched.

**13 entities, `Economic Action` at exactly 8 attributes.** Everything added is
diagnostics or one option.

Also pinned this release: both live physics cases. 3.7 kW of battery charging
against 1.1 kW of house and 0.63 kW of production draws **4.170 kW** at the meter;
2.2 kW of battery discharge against 0.9 kW of house delivers **1.300 kW** of export
while 1.3 kW of discharge delivers only 0.400.

### Verification

Tests **2817 → 2940**. Mutations **147 → 167** across four suites, zero
survivors.

**Three solves per refresh instead of four**, and that is measurably cheaper rather
than notionally so. Measured on one machine against the fourth solve reproduced
exactly as beta.17 performed it: **307 ms → 224 ms**, so 83 ms and 27 % of the
solve work per refresh goes away. Table build 13.1 ms; the realised layer 0.100 ms
per day. No new state dimension, no history replay.

The terminal removal carries its own evidence: seven regressions on a horizon whose
reserve outlives its prices, nine mutations including restoring the old floor and
flattening the reserve to a constant, a seven-scenario deterministic campaign, and
the 200-seed adversarial sweep above.

## [1.0.0-beta.17] - 2026-08-22

**beta.16's first live day produced four things that looked like defects in the
optimizer. Measured properly, three were defects in what beta.16 said about
itself, and the fourth was a number beta.16 had documented incorrectly.** The
objective is unchanged, the terminal condition is unchanged, and no rule was added
to any decision the search already makes.

One thing does change what the optimizer decides: it can now use the whole
inverter.

### Changed

- **The battery can reach its configured power.** A quarter-hour at 10 kW is
  2.3717 kWh at the pack, which is 9.487 of beta.16's 0.25 kWh state-space
  buckets — so nine buckets were reachable, ten needed 10.54 kW, and **the top
  5.13 % of the inverter was unusable in both directions**. It is now reachable
  exactly: 10.0000 kW charging and discharging.

  The fix is a *rounding* of the bucket rather than a refinement of it — divide a
  maximum-power quarter into a whole number of buckets — and that distinction is
  why it is affordable. beta.16 costed the refinement (0.25 → 0.10 kWh: reaches
  only 9.87 kW and takes 461 ms instead of 80 ms) and rejected it correctly, then
  drew the wrong conclusion. Rounding costs nothing: the reference installation
  now solves on **84 states instead of 88**, slightly faster, and because the
  bucket is still constant within a solve every surviving move is still exactly
  linear — the invariant the whole pricing table rests on is untouched.

  **Nobody's installation gets worse.** The bucket is chosen by a search under
  three hard constraints: neither direction may lose representable power, the
  bucket must stay between 0.15 and 0.40 kWh so energy resolution cannot collapse,
  and the state count may grow by at most a tenth. Across fourteen
  configurations — 10 to 50 kWh, 3 to 20 kW, 85 % to 95 % efficiency, symmetric
  and asymmetric power — **twelve improve and two keep the beta.16 lattice
  unchanged.** A 22 kWh / 5 kW pack is one of the two: the obvious alignment would
  take its charge side to exactly 5 kW while pushing its discharge side from
  5.1 % short to 10.0 % short, and that trade is refused rather than taken. Which
  rule produced the lattice is published, because two installations can now
  legitimately differ.

  Worth €0.08–0.33 on a short expensive window, measured.

  **Expect a small discontinuity in your history across this upgrade.** Where the
  lattice changed, economic energies and euro figures are quantised on a slightly
  different grid, so a plan from before and a plan from after can differ by up to
  one bucket — about 0.26 kWh on the reference installation — for no reason other
  than the grid. Stored documents are **not** rewritten to hide it: the bucket has
  always been recorded, and beta.17 records the rule and both directional peaks
  beside it, so a figure can be interpreted in the terms it was computed under.
  Making old and new values look continuous would be a fabrication.

- **An Activity line says which boundary each figure belongs to.** The live line
  read:

  ```
  export to the grid 0.95 kW, 0.27 kWh during 18:30-19:30
  ```

  Every number in it was true and the sentence was still misleading: 0.95 kW was
  the **battery** in the first interval, 0.27 kWh was what reached the **meter**
  across the whole run, and the remainder covered the house. A reader who
  multiplies gets nonsense; a reader who does not still cannot tell which is
  which. It now reads:

  > plans to export to the grid during 18:30-19:30: 0.95 kW average (0.95 kWh
  > from the battery), of which 0.27 kWh reaches the grid and the rest covers the
  > house.

  The mean power is used rather than the first interval's, so the arithmetic
  closes. Every beta.16 anti-spam property is unchanged: one announcement per run
  within a quarter-hour of its start, no repetition, no back-dating, at most one
  entry per refresh, advisory wording throughout.

- **The terminal condition's cost is no longer reported as money.**
  `terminal_protection_cost_eur` is now `terminal_plan_cost_eur`, and it says in
  the payload what it is: a *whole-horizon plan* difference, not realised cost.

  The rename is the fix for a real reporting defect. The live installation
  reported about €3.9 and it read as €3.9 lost. But a plan is rebuilt every
  quarter-hour and only its **first interval** is ever executed, so a difference
  in the tail is discarded before it can happen. Rolling the horizon forward —
  re-plan each quarter, execute one interval, roll the state through the same
  physics — the released rule and every alternative to it land within **€0.10 per
  day** of each other. The figure overstated by roughly fortyfold.

  Two honest figures replace it: **`first_run_changed`**, which is whether the
  bound altered the interval about to be executed, and `near_field_cost_eur` over
  the first hour. On one synthetic shape the bound does reach the next interval
  with a single day of prices and does not with two — and the whole-horizon figure
  is identical either way, which is exactly why it could not answer the question.

### Added

- **`horizon.tomorrow_prices`, `horizon.reserve_basis` and
  `horizon.bridge_requirement_kwh`** — the pre-publication regime, made
  measurable and consumed by nothing.

  `tomorrow_prices` comes from what the source has published. There is no
  publication time anywhere in this integration and there must not be: whether
  tomorrow is visible is a fact about the data, and answering it from the hour of
  the day would break on the first day the source is late.

  `reserve_basis` is Phase 7's own verdict on its tail, and it is the reason a
  terminal condition exists at all. The reserve's recursion starts from zero
  deficit at its last interval, so **its requirement decays to the configured
  floor plus one interval's demand at whatever point the forecast stops** — 4.72
  against a 4.40 kWh floor, in summer and winter alike — and it reports
  `truncated` to say so. A reserve that asks for almost nothing at midnight cannot
  protect the night after it.

  `bridge_requirement_kwh` asks the same recursion what tonight's end would need
  if the forecast ran a day longer. It is published to be read rather than obeyed,
  because the answer rules itself out as a constraint: **15.7 kWh in summer, 33 to
  61 kWh in winter, against a 22 kWh pack.** A bound larger than the battery is
  not a bound.

- **`solver.max_representable_charge_kw` and `..._discharge_kw`**, beside the
  configured limits and the rule that chose the lattice. beta.16 published only
  the larger of the two, which on a 15 kWh / 7.5 kW pack read 7.4620 kW — half a
  per cent short of nameplate and entirely reassuring — while concealing a
  discharge side that reached 6.5666 kW. The asymmetry reaches **29.7 %** on a
  3 kW installation, so beta.16's "roughly five per cent, in both directions" was
  this installation's number and not a general one.

- **`pv.remaining_today`** — the sum of the remaining quarter forecasts the plan
  was actually built on, with a tolerance. Published to be compared against the
  production source's own remaining-today figure: a difference beyond tolerance
  points at the site selection or the interval mapping rather than at either
  forecast. The quarter-level series stays authoritative, because an aggregate
  cannot say *when* production arrives and when is most of what the optimizer
  needs.

### Fixed

- **A flexible-load entity missing at startup no longer warns.** Home Assistant
  brings integrations up in an arbitrary order, so the configured EV sensor is
  routinely absent for the first refresh or two after a restart, and the warning
  described the startup sequence rather than the configuration. It now logs at
  debug for the first three refreshes, and warns immediately if the entity was
  readable and then disappeared.

  **The safety rule is untouched:** a missing reading is `None` and never zero,
  baseline learning pauses from the very first absence, and the rejection reason
  and the invalid-interval count are unchanged. Only the log line moved.

### Unchanged, and verified as such

The four behaviours that were in doubt are now regressions rather than arguments.
Each was suspected of being a defect on the live installation; each is the cost
objective doing its job:

- **Sale timing is jointly optimal over quantity, power, quarter and household
  avoidance.** Given four dear quarters that can absorb the energy, all four run
  at the largest representable power. Given only *one*, an earlier sale into a
  moderate window becomes rational — a single quarter can only carry one
  quarter-hour of power. Given only 2 kWh, it serves the house rather than
  exporting, because 0.25 avoided beats 0.20 earned. With import at 0.50 and
  export at 0.08, less than a third of what the battery gives up reaches the grid.
- **A losing round trip is refused.** Buy at 0.20, sell at 0.15, rebuy at 0.25:
  no trade. Replace the middle with a genuine 0.60 peak and it trades hard. Import
  is an all-in price and export is a compensation, and every euro in the payload
  reconciles against that asymmetric pair per interval.
- **A reserve-driven buy stops at the requirement.** At requirements of 8, 12,
  15.5 and 19 kWh from a 5 kWh start, peak state of charge lands on the
  requirement with an overshoot of **+0.00 kWh** every time — never the 22 kWh
  ceiling. "Safety buy" remains a *label*, attributed by re-solving with the
  reserve relaxed; there is no separate mechanism, and none is needed because
  reserve feasibility already outranks cost lexicographically.
- **The reserve is obeyed at every interval.** A requirement spiking to 18 kWh in
  mid-horizon is met exactly while the surrounding intervals sit far lower. A plan
  checking only its endpoint would sail through it.

Also unchanged: **the terminal condition**, on the evidence above. **Phase 7** — no
change to `reserve.py`; the bridge figure is a caller-side measurement using the
existing three-argument entry point. **13 entities**, 12 sensors and 1 select, with
`Economic Action` still carrying exactly 8 attributes. **Zero actuation** —
`CONTROL_EXECUTION_AVAILABLE` remains False, `PERMITTED_SERVICES` is still three,
the service callers are unchanged, and there is still no export, curtail, Force
Export, Force Import or PV Switch primitive. The **beta.15 clamp** and its
immediate mode refresh. Every **beta.16** reporting and Activity property.

No season flag, no publication clock, no maximum-power rule, no
one-trade-per-day rule, and no headroom rule was added. Each would be a weaker
restatement of something the search already proves exactly, and a weaker
restatement can only disagree with the optimum.

### Storage

`FORECAST_STORAGE_MINOR_VERSION` **1.6 → 1.7**, additive with two renames:
`tpc`/`tpi` become `tplc`/`tpli`, and `tfrc` is new. **The reader accepts both
spellings**, so a beta.16 document loads with every figure intact, and a beta.15
document still loads with the newer fields absent rather than invented. No
config-entry migration, no option change, no learning-history reset.

### Verification

Tests **2715 → 2817**. Three new files: the lattice guarantee, the rolling-horizon
harness and the behaviour proofs. Mutations **139 → 147** across four suites, zero
survivors.

The rolling harness is the one that matters, and it is the slowest thing in the
suite by design — it is the only test that can tell a plan-level artefact from
realised money, which is the distinction beta.16 got wrong.

## [1.0.0-beta.16] - 2026-08-22

**The optimizer's decisions were right; the numbers it published about them were
not.** A full-horizon diagnostics download made four things read as defects that
were not defects at all — and hid two that were. beta.16 makes the reporting true
and stops the Activity log repeating the same plan every quarter of an hour.

The optimizer's objective is unchanged. **The terminal condition is deliberately
unchanged**; what it costs is now measured and published instead.

### Changed

- **A charge run now says where its energy came from.** "charged 4.48 kWh" read as
  "bought 4.48 kWh". It was not: most of it was the sun, and the
  `grid_import_kwh` printed beside it was **site** import including house load —
  a third quantity again. Every run now also reports
  `marginal_grid_import_kwh`, `marginal_grid_export_kwh` and a `charge_source` of
  `production`, `mixed` or `grid`.

  Exact, not apportioned: the optimizer already computes each interval's idle
  counterfactual, so what a run *caused* is a difference rather than an estimate.
  The boundary for "production" is one state-space bucket, below which a grid
  contribution is unrepresentable and claiming one would be over-claiming.

- **`expected_value_eur` per run is now `net_cash_flow_eur`, and there is a real
  economic figure beside it.** The old field was a negated cash flow with no
  counterfactual, so every charge run was negative *by construction* — and a
  discharge that exactly covered house load read `0.00` while avoiding the entire
  import bill. `marginal_cost_eur` is what the run cost against leaving the
  battery alone through the same intervals. **Negative means it saved money.**

- **The hold baseline prices the same physical world the plan does.** It used to
  freeze the battery, so the baseline *sold* the surplus production the plan was
  held to *bank* by the terminal bound, and stored energy carries no price. The
  published gain was understated by roughly the export value of everything
  absorbed. Both are now priced on one ambient trajectory — the same walk the
  terminal bound already used, so there is a single definition of "doing nothing".

  The objective never read this figure, so no plan changes; only what is reported
  about it.

- **Run count and switch count are now both visible.** Seven runs read as seven
  trades; the switching fee had been charged three times. One physical discharge
  carries both the `discharge` and `export` labels as house load rises and falls
  beneath it — the label flips, the direction does not. Each run now reports its
  `direction` and whether it `charged_switching_fee`, and each plan reports
  `direction_changes`.

- **Solar absorption no longer splits one paid charging campaign.** A sunny
  quarter inside a charging window draws nothing extra from the grid, so it is
  ambient — but it used to count as plain idle, which **broke the run**, and the
  next purchasing quarter paid `minimum_trade_gain_eur` again. On a partly-sunny
  cheap afternoon a single campaign could pay several fees.

  Absorption is now transparent to a charge campaign, and to nothing else: it *is*
  a charge, so it cannot continue a discharge run, and a **true** idle interval
  still breaks a run exactly as before.

  This one does change the chosen plan, and it is the only change in this release
  that does.

- **Activity announces a run once, when it is about to happen.** This was the
  worst of it. The live log showed:

  ```
  11:45 -> charge 11:45-15:00
  12:00 -> charge 12:00-15:00
  12:15 -> charge 12:15-15:30
  ```

  Technically honest and unreadable. Three causes, all fixed: the run's identity
  was keyed on a horizon index that advances every refresh while a run is under
  way; midnight rebased every index by a whole day with no change in meaning; and
  the figures were bucketed and hashed, so a hundredth of a kilowatt across a
  boundary spoke while a fifth inside one stayed silent.

  A run is now identified by `(direction, start instant)` — immune to horizon
  shifting, to midnight, and to a label flipping mid-discharge. It is announced on
  the first refresh within **one planning interval** of starting, and then stays
  silent. Content is compared against the announced value with deadbands taken
  from existing constants rather than invented percentages: one state-space bucket
  of energy, the smallest power the device accepts, one planning interval of time.

  A run that materially changes gets one `changed`; one dropped before its window
  opens gets one `cancelled`; one whose window elapses gets one `ended`. A run
  whose window has already closed is **never** announced retrospectively. At most
  one entry per refresh, and a run already under way after a reload gets exactly
  one line.

- **`cancelled` is now an advice event rather than an execution event.** beta.14
  classified it as execution, on the reading that cancelling is something done to
  a command in flight. Withdrawing *advice* that never began is plainly advice, and
  the one-message-per-run design has to be able to retract an announcement or
  leave it standing as a lie. `started` remains the sole execution kind and is
  still refused outright.

### Added

- **`terminal_protection_cost_eur` and `terminal_protection_import_kwh`.** The
  terminal condition — end the horizon no lower than doing nothing would have —
  stops the optimizer emptying the pack in the last priced interval merely because
  nothing after it is priced. It does that job. It is also not free: on a
  synthetic two-day shape it cost €1.77 and forced 9.49 kWh of grid import that a
  plan seeing one more day would not have bought, and its signature is a
  maximum-power purchase in the final quarters.

  A fourth solve with the bound relaxed to the configured floor now prices it,
  exactly as the reserve's own protection cost has been priced since beta.14.
  **The bound itself is unchanged and the published plan is the bounded one** — the
  figure exists so a decision about it can rest on live evidence rather than on a
  synthetic shape.

- **`max_representable_power_kw` in the solver diagnostics.** Roughly five per cent
  of nameplate peak power is unreachable in both directions: a 10 kW charge for a
  quarter is 2.3717 kWh DC, which is 9.487 state-space buckets. Nine buckets need
  9.487 kW and are reachable; ten need 10.54 kW, which the clamp reduces, so the
  move is correctly discarded.

  Quantisation, not a clamp fault and not a configured limit. Published so it is
  visible and cannot silently worsen. **Not fixed in this release, deliberately:**
  refining the grid costs solve time as the inverse square of the bucket, and the
  targeted alternative breaks the linearity invariant the per-delta pricing table
  rests on — for a few per cent of peak power in the rare case where power binds.

- **`marginal_*` figures on every interval**, from which every run figure sums. No
  apportioning anywhere.

### Unchanged, and verified as such

- **The optimizer's objective and its decisions.** Two behaviours that were in
  doubt are now regression-tested rather than argued:
  - with 20 kWh of forecast production arriving and 7 kWh of headroom, the plan
    buys **nothing** from the grid at 0.10 EUR/kWh — and forbidding grid charging
    changes the answer by **€0.0000**. Remove the production and it immediately
    buys at 7.4 kW. No "reserve room for the sun" rule was added, because the cost
    objective already expresses it exactly;
  - given eight candidate cheap quarters of which only two are cheapest, it
    exports first to *make* headroom and then buys at the largest representable
    power in **exactly those two quarters**. Energy, power and quarter selection
    are jointly optimal; no maximum-power rule, no single-window rule, no
    one-trade-per-day rule, and no season rule.
- **The terminal condition**, as above.
- **Phase 7.** The dynamic reserve is consumed exactly as before.
- **The beta.15 safe-discharge clamp** and the beta.15 immediate mode refresh.
- **13 entities**, `Economic Action` with exactly 8 attributes, no new sensor and
  no new setting.
- **Zero actuation.** `CONTROL_EXECUTION_AVAILABLE` remains **False**,
  `PERMITTED_SERVICES` is still three, the service callers are unchanged, and
  there is still no export primitive, no PV Switch, no Force Export and no Force
  Import.
- **No publication-gap hedge was added.** No clock rule, no Frank-specific timer.
  A horizon holding only today's prices already ends at the hold endpoint, so the
  terminal condition is the hedge — now with its cost visible.
- **Raw quarter prices remain the only economic input.** No derived zone or
  optimal-period entity exists to consume, let alone is consumed.

### Storage

`FORECAST_STORAGE_MINOR_VERSION` **1.5 → 1.6**, additive: economic snapshots gain
`tpc`, `tpi` and `dc`. A beta.15 document reads back with every other field intact
and the new ones absent rather than invented. No config-entry migration, no
learning history reset.

### Verification

Tests **2628 → 2715**. Two new files: the reporting corrections and the
announcement policy. Fourteen new mutation tests, 139 across all four suites, zero
survivors.

Zero actuation is proved at runtime as well as structurally: eight consecutive
quarter-hours with both opt-ins on, in `active` mode, with real registered
handlers for all three permitted services — the plan available, Activity lines
filed, no line repeated, and not one service call. A test that proved silence
while the plan was unavailable would prove nothing, so the positive half is
asserted first.

The announcement policy is exercised against fixed instants — the module reads no
clock of its own, and a structural test enforces that — so every case including the
ten-refreshes-during-a-running-charge regression is deterministic.

One implementation defect was found and fixed during the work, by an existing
test: the first version of the absorption change let a sunny quarter continue a
*discharge* run, which would have suppressed the fee a genuine reversal owes.

## [1.0.0-beta.15] - 2026-08-22

**A discharge that is too big for the house is now made smaller instead of
refused.** Before this release, a recommendation to discharge 1.1 kW into a house
absorbing 0.99 kW produced `inhibited` / `would_export` and no discharge at all.
It now produces a 0.8 kW command — the largest the meter says can be absorbed
without pushing energy onto the grid.

Nothing about export safety was weakened, and nothing became executable.
`CONTROL_EXECUTION_AVAILABLE` remains **False**.

### Changed

- **`would_export` clamps a non-exporting discharge instead of refusing it
  whole.** The order is fixed and each step matters: measure the absorbing
  capacity at the meter, take the configured margin off the **capacity**, clamp
  the requested command to what remains, quantise **downwards** to a helper step,
  then recompute the commanded energy. On the live case that is
  `0.99 → 0.891 → 0.8 kW`; the 0.9 kW step is rejected because it would exceed
  the margined bound.

  The clamp lives *upstream* of the gate rather than inside it. The gate still
  never scales a command — it is handed one that is already safe, and passes it.
  That keeps the one rule this layer has had since Phase 4: the safety layer may
  only ever **subtract**.

- **`would_export` still refuses, and refuses exactly as it did.** When no
  representable command survives the clamp, the *original unreduced request* is
  passed to the gate, which refuses it whole with the same reason and the same
  figures as beta.14. Reducing it to zero inside the clamp would have moved the
  refusal into the wrong module and reported the wrong cause.

  Nothing representable survives when the site has no absorption, when it is
  already exporting, when the safe power falls below the device's two-step
  minimum, when the meter is missing or stale, or when any other gate condition
  refuses first.

- **`eligible` now means "a safe command remains", not "the requested command was
  safe".** A safely reduced command is `eligible`, not `inhibited`. That is the
  semantic change, and it is the one worth reading twice: the state answers
  *would something safe happen*, and the diagnostics say how much it was reduced.

- **The safety bound has exactly one definition.** `safe_discharge_power_kw` is
  what the clamp reduces to *and* what the gate checks, so the number a command
  was reduced to and the number it is judged against cannot drift apart. Two
  expressions of one safety bound is one too many — the same reasoning that made
  `split_grid_energy` the sole grid-residual authority in Phase 8.

- **The commanded energy is recomputed from the reduced power**, over the same
  duration. `allowed_energy_ac_kwh` is untouched, so the energy given up appears
  in `undelivered_energy_ac_kwh` — 0.075 kWh on the live case. The duration is
  **not** extended to compensate: it is a dead-man margin rather than a delivery
  window, and stretching it would outlive the planning interval the next refresh
  supersedes it in. The cutoff, the action and the energy-limit flag are all
  untouched: this is a reduction, never a substitution.

- **The export-check diagnostics report every stage.** `requested_power_kw`,
  `absorbing_capacity_kw`, `safety_margin_percent`, `safe_capacity_kw`,
  `safety_limited`, `limited_power_kw`, `final_command_power_kw`, the three meter
  readings the bound came from, the `inhibit_reason`, and the ordering rule
  spelled out. Three powers rather than one, because a single
  `commanded_power_kw` could not distinguish a command that was never limited
  from one reduced to exactly the bound. `DeviceCommand` gains
  `requested_power_kw` and `safety_limited` beside it.

### Fixed

- **A Phase-4 safety *policy* that was deliberately conservative, not a beta.14
  defect.** The wholesale refusal has been the behaviour since Phase 4 shipped in
  beta.8. Phase 8 made its practical cost visible: a `make_headroom`
  recommendation — sell or discharge now to leave room for forecast production —
  was repeatedly refused outright whenever household load was modest, which with
  low load could persist for hours.

  Two of the three real diagnostics samples recorded in `test_export_capacity.py`
  now yield a safe reduced command (0.3 kW and 0.6 kW) where beta.14 refused them
  outright. The third, taken while the site was already exporting a kilowatt of
  sunshine, is still refused. That change of historical behaviour is intentional
  and is pinned in that file rather than left to be discovered: each surviving
  command sits strictly below the *measured* absorbing capacity, which is the
  property that made the refusal safe and keeps the reduction safe.

- **The export-check diagnostics block had no test at all.** A pre-existing gap,
  and the reason a rename inside it could have shipped silently. Its key set is
  now pinned.

- **Changing the control mode is re-evaluated immediately again.** Selecting a
  mode used the *debounced* refresh, which carries a ten-second cooldown. Once
  that timer was armed — by a quarter-hour tick, say — a mode change applied the
  mode but deferred the re-evaluation to the end of the cooldown, so the published
  Control State could sit up to ten seconds behind the control the user had just
  operated.

  Now the undebounced `async_refresh`, for exactly the reason
  `AlphaEmsCoordinator._handle_started` already gave for the startup path: a user
  pressing a control is the last thing that should be rate-limited against a
  background timer. The mode was never lost — only its re-evaluation was late.

  This surfaced as an intermittent test failure rather than a report, because the
  symptom depends on whether the cooldown happened to be armed. The regression
  now arms it deliberately, so it fails every time against the debounced form
  instead of once in a while: verified 10/10 failing before the fix and 100/100
  passing after.

### Unchanged

- **The Phase-8 optimizer.** The desired plan, the capability plan, the reason,
  the capability gap and every euro figure are byte-identical across two refreshes
  whose absorbing capacity differs by more than a kilowatt. The safety layer may
  subtract capability; it does not reach backwards into the planner.
- **The Phase-8 `export` action is still advisory and still has no actuator.**
  The clamp exists so grid export does *not* occur, so it cannot be the vehicle
  for an action that wants it. `gap_reason` still reports `no_primitive`, and
  `economic_value_forgone_eur` still reflects the missing export primitive
  honestly. No Force Export, no Force Import, no PV Switch, no new service caller
  — `PERMITTED_SERVICES` is still exactly three and the two callers are unchanged.
- **The reserve and the floor.** The configured minimum state of charge, the
  cutoff, `allowed_energy_ac_kwh` and every Phase-4 device constraint bound a
  clamped command exactly as they bound an unclamped one. `would_export` is the
  last condition evaluated, so every higher-priority refusal still wins.
- **The 10 % export margin**, its default, and its absence from the options form.
  No new setting. The margin was audited and found to be applied in the right
  place — to the capacity, before any rounding.
- **Entity count: 13.** No new entity, no new attribute, no new Activity line.
  Activity remains about economic-plan changes and reads nothing from the safety
  layer.
- **Both storage versions.** `DeviceCommand` is diagnostics-only; nothing here is
  persisted, and no migration is involved.

### Verification

- Tests **2479 → 2628**. One new file, `test_safe_discharge_clamp.py`, with 113
  tests: the bound's single definition, the live case and its companion, the
  only-ever-subtract invariants swept over a grid of capacities and requests, the
  energy bookkeeping, every surviving refusal, the low-load ladder, meter-noise
  stability, and the whole pipeline driven through the real coordinator.

- **Sixteen new mutation tests**, all killed. Removing the margin, applying it to
  the command instead of the capacity, rounding the safe command up, rounding it
  to nearest, skipping the clamp, clamping above the request, raising a
  sub-minimum command to the device minimum, keeping the stale commanded energy,
  extending the duration to compensate, clamping on an absent meter, deleting
  `would_export`, bounding the clamp by forecast house load, bounding it by a
  planned residual, treating the desired export as executable, letting the clamp
  reach back into the planner, and flipping the release barrier.

- **The sign question was asked directly** rather than reasoned about: the same
  magnitude presented as export never yields a larger bound than as import, and a
  *charging* battery is never credited as absorption.

- **Cost: 2.3 microseconds per refresh** — 0.16 µs for the bound and 2.2 µs for
  the clamp, against a 185 ms Phase-8 solve. No second solve, no I/O, no
  event-loop work. The control context is now assembled once and the single
  changed field replaced, so the bound and the gate provably describe the same
  instant.

- **No hysteresis mechanism was added, and the measurements say none is needed.**
  ±150 W of noise around the live working point moves the command between four
  adjacent steps, every one of them below the bound. The `eligible`/`inhibited`
  boundary sits at 222 W of absorption — 768 W below that working point — so
  flipping across it takes a load switching off, not meter noise. And the existing
  write cooldown already rate-limits any restart to one planning interval while
  exempting every reduction.

## [1.0.0-beta.14] - 2026-08-21

**Alpha EMS now works out what it would do with your battery to save money, and
still does nothing.** A new sensor reports the action it wants, the action
implemented actuators could produce, and the reason nothing is sent. Every figure
beta.13 published for the same inputs is unchanged.

If your battery recommendation, planned power, usable energy, dynamic reserve or
control state changes after this upgrade, that is a bug. Phase 8 Stage A decides;
executing a decision is Stage B, and none of it is in this release.

### Added

- **Economic Action**, one new sensor. Its state is the **desired** action —
  `hold`, `charge`, `discharge`, `export`, `curtail_pv` or `safety_buy` — and
  `capability_action` beside it is what actuators that actually exist could
  produce. Both, always: a state reading `export` with no way to tell whether
  anything could happen would be worse than no state at all.
  `execution_blocked_reason` is the third fact, and while the release barrier
  stands it is the only value it can take.

  Sensor count goes from eleven to twelve; entity count from twelve to thirteen.
  Nothing else moved.

- **A finite-horizon optimizer**, solved by backward induction over
  `(interval, energy bucket, run state)`. The objective is the pair
  `(reserve violation, economic cost)` compared **lexicographically**, so reserve
  feasibility dominates economics without a second mechanism and without a mode
  switch: when no violation is avoidable the first term ties and the order becomes
  pure cost. There is no fallback, and that is the point — an earlier draft
  degraded to a profit solve once the reserve was unreachable, which meant a
  deficit made the optimizer *freer* rather than more careful.

- **Two solves, not one solve and a downgrade.** `desired` optimises over every
  action the physics allows, including export and photovoltaic curtailment, for
  which no actuator exists at all. `capability` is a separately computed plan over
  the actions that do have a primitive. Keeping them apart is what lets the
  optimum stay undistorted by which actuators happen to exist, and what makes
  `economic_value_forgone_eur` — the euro cost of the missing primitives —
  meaningful rather than tautological.

- **Safety buy is a label, not a mechanism.** A third solve, with the reserve
  relaxed to the configured floor, is compared against the desired plan: the
  charging that disappears is the charging the reserve was responsible for. No
  price threshold could make that distinction, because a cheap interval and a
  reserve deadline coincide constantly.

- **A terminal condition that forecasts nothing.** `E(n) >= E_hold(n)`, where
  `E_hold` is the Phase-3 hold trajectory — the plan may not leave the battery
  worse off than doing nothing would have. Stored energy is assigned **no price**,
  so no claim is made about prices after the horizon; the bound exists only to
  stop the optimizer emptying the pack into the last priced interval because the
  data ran out. It is a bound rather than a prohibition: the action space is
  continuous in buckets, so the evening sale still happens and only its final
  depth is limited.

- **A precomputed physics table, measured from the clamp.** Every reachable
  transition is built once per refresh by asking `battery.apply_request`, so the
  optimizer performs **no efficiency arithmetic at all** and no hardware limit is
  ever compared against a second time. The two conversion ratios are *measured*
  from a calibration probe rather than derived from
  `round_trip_efficiency_percent`, which is why they agree with the simulator to
  fourteen decimal places instead of to within a modelling assumption.

- **Every euro figure comes from grid AC energy, without exception.**
  `import_price × grid_import_kwh − export_price × grid_export_kwh`, with the
  residual split supplied by `battery.split_grid_energy` and by nothing else. Each
  interval and each run carries all six energies separately — one DC, two
  battery-side AC, two grid-side AC and the curtailment — because a euro figure is
  only meaningful against the boundary it was measured at.

- **`minimum_trade_gain_eur`, the single economic knob.** Charged once per
  discretionary battery-action *run* inside the objective, which is why the run
  state is a dimension of the search. It suppresses the micro-cycle a per-kWh cost
  cannot — a tenth of a kilowatt-hour at a wide margin still earns only a few
  cents — and it is emphatically **not** a degradation model. Reserve-protection
  charging still happens below it, with no exemption rule, because reserve
  feasibility already has priority.

- **Two explicit opt-ins, both default off**: charging from the grid, and selling
  from the battery. Unlike the execution enable, both are offered in the options
  form, and the difference is that both change the *published plan*: turning grid
  charging on moves the action, the value forgone and every per-run figure, in a
  release that sends nothing.

- **An `economics` options page**, the fourth. Three fields, flat keys, and the
  same merge-from-existing-options rule as its three siblings, so editing a
  threshold never disturbs a source selection.

- **An `economic_plan` diagnostics section**, the twentieth. A dict key rather
  than a list entry, so the sixteen-entry ceiling every list in the payload is
  held to is untouched. At most eight planned runs, each with all five energy
  boundaries; never the per-interval trajectory, which would be a hundred and
  ninety-two rows and which, truncated, would read as a short horizon rather than
  as a clipped payload.

- **Grid limits, stated as unknown.** The integration has no way to learn a
  connection or contractual limit, so the advisory peaks in provenance are bounded
  by the inverter and the battery only, and the payload says out loud that a
  reported peak may exceed what the connection can carry. Executing nothing is
  what makes that safe.

- **An Activity surface**, one logbook line per material change. Filed against the
  entity as well as the domain, so it appears on the sensor's own history.
  Change-triggered on a *coarse* fingerprint — the action, the window, and the
  power and energy rounded to material thresholds — so ninety-six refreshes
  against an unchanged answer produce one line and a plan that shifts by a watt
  produces none. It is **write-only**: nothing in the integration subscribes, no
  figure is derived from it, and an installation without the recorder produces
  identical numbers.

  The four kinds it can emit all describe *advice*: `planned`, `changed`, `ended`
  and `refused`. The two that describe execution, `started` and `cancelled`, are
  refused outright while the barrier stands — a line reading "charge started" on a
  release that sends no command would be a lie about the hardware, and that is the
  one failure mode this surface must not have.

- **Economic evidence**, a fifth change-fingerprinted snapshot family. Scalars
  only, and it exists for one reason: prices, load, production and the reserve are
  already persisted, so the arithmetic is reproducible — but a threshold the user
  changed, or an opt-in they turned on, lives in the config entry, which keeps no
  history. Turning grid charging off would otherwise make every earlier plan
  unverifiable.

- **Measured grid import and export are now recorded per interval** in the
  learning store, alongside load, production and state of charge. Read by nothing
  in this release. Phase 9 needs measured grid flows to score a plan against what
  actually happened, and a scoring pass cannot be built retroactively over history
  that was never kept.

### Changed

- **`charge` means buying from the grid.** The economic action `charge` and the
  `allow_grid_charging` opt-in refer specifically to Alpha EMS *choosing to
  purchase* energy. They are not about energy moving into the pack.

  **Physical battery charging from ambient production is not an economic charge
  action.** A charge whose grid import does not exceed the idle baseline draws
  nothing from the meter — it is the same ambient behaviour the Phase-5 simulator
  models as `absorb_surplus` — so when production naturally enters the battery
  while Alpha EMS takes no economic action, `Economic Action` reads **`hold`**. It
  creates no economic action run, pays no `minimum_trade_gain_eur` switching cost,
  needs no opt-in, remains part of the physical trajectory, and can still create
  future economic value through the energy it stored.

  The permission is now measured against the idle baseline in **both** directions,
  exactly as the export side already was. This is a ratified refinement of the
  approved Phase-8 contract, not a preference: see *Fixed* below, where the
  direction-only reading made the release non-functional in its default
  configuration.

- **The terminal bound is reproduced on the solver's own state space.** HoldPolicy
  remains the conceptual counterfactual, but the enforced bound is the
  idle-with-absorption endpoint expressed on the same bucketed grid and physical
  model the optimizer searches over, and reachable by construction. What is
  published is what is enforced, and `terminal_basis` reads
  `hold_trajectory_end_on_bucket_grid` so the continuous reference value can never
  be silently confused with the bucketed constraint.

  A ratified refinement of the approved Phase-8 contract, which specified the
  continuous `plan.reference.end_energy_kwh` literally. A terminal requirement the
  solver's state space cannot represent makes an otherwise valid sunny horizon
  artificially infeasible; the regression proving both halves of that is retained.
  The user's configured floor is unmodified either way.

- **The reserve requirement is quantised *up* to a 0.25 kWh bucket** for planning,
  and capped at the pack. Up, because protecting at most one bucket too much is
  the safe error while ignoring up to one bucket of shortfall is not — and because
  a requirement on a bucket boundary, with every state also on one, makes a
  sub-bucket violation **unrepresentable** rather than merely ignored. The
  measured state of charge snaps *down*, and the user's configured floor is
  quantised **not at all**: it is enforced by the clamp, and moving it is exactly
  what Phase 7 exists to refuse. `quantisation_margin_kwh` publishes the bound.

- **Forecast-history storage minor version 1.4 → 1.5**, additive. Partitions gain
  `eco` and index rows gain `ecofp`. **Learning-store minor version 2.3 → 2.4**,
  additive: day records gain `gi` and `gx`. A document written by any earlier
  release reads unchanged, and an installation without battery planning writes
  neither.

### Fixed

- **Two defects found by this release's own test suite, both of which made the
  default configuration non-functional on any sunny day.** They are two halves of
  one mistake: the optimizer's "do nothing" and the simulator's "do nothing" were
  not the same trajectory.

  The charge permission was measured on *direction* alone, so absorbing production
  the house could not use required the grid-charging opt-in — off by default. With
  it off the model believed the pack never absorbs anything. The terminal bound,
  meanwhile, came from the continuous hold trajectory, which does absorb. On a
  four-interval sunny horizon the bound therefore sat above every reachable state,
  every state was infeasible, and the whole plan reported `available: false` — under
  the reason `economic_horizon_empty`, about a horizon that was four intervals
  long. A wrong reason is worse than an unused one, so
  `economic_terminal_unreachable` now exists as a named guard even though the clamp
  makes it structurally unreachable.

- **The export permission leaked into the capability plan.** Measured on direction
  alone, unavoidable photovoltaic spill made every sunny state illegal, which
  silently collapsed the desired plan onto the capability plan and reported a value
  forgone of zero. Battery-caused export is now measured against the idle
  baseline. On the eight-interval fixture the two plans separate correctly
  afterwards: €2.0897 desired against €0.2741 achievable, €1.8156 forgone.

- **`capability_gap_reason` reported a spurious `forecast_infeasible`** whenever
  the desired action was labelled `safety_buy` and the capability action was a
  plain `charge` — the same charge, in the same intervals, for the same reason.
  The two are now compared on the underlying *direction*.

- **A 670 ms solve.** `split_grid_energy` was called once per state transition,
  roughly nine hundred thousand times for one refresh. The AC energies for a given
  change in stored energy are identical from every bucket, because the clamp
  rejects any move it had to reduce — so the per-interval outcomes are now
  precomputed by bucket delta, thirty-four thousand calls instead. All three
  solves over a full ninety-six-interval day now take about 185 ms, on top of a
  14 ms table build, in the executor.

### Verification

- Tests **2256 → 2479**. Six new files: the model and its hand-computed
  arithmetic, the action vocabulary, the published surface, the evidence layer, the
  phase boundaries and the mutations.

- **Every euro figure in the load-bearing tests is recomputed by hand.** The
  eight-interval fixture is sized so the arithmetic can be done on paper: four
  cheap intervals then four expensive ones, one kilowatt of house load, no
  production. Doing nothing costs €0.50 exactly; buying and load-shifting costs
  €0.2259; buying and selling earns €1.5897 — each asserted against a sum computed
  from the per-interval grid energies rather than against the plan's own total.

- **Thirty-four mutation tests**, each a plausible refactor rather than an
  absurdity. Five of them were real mistakes made and caught while building this
  phase: the charge permission on direction alone, the terminal bound left
  unclamped, the export permission on direction alone, the reserve falling back to
  a profit solve, and the capability gap compared on the raw action.

- **The safety ordering is proved to be lexicographic rather than steep.** Avoiding
  a one-bucket shortfall is made to cost over a thousand euros and is still
  chosen, so no finite weight a reviewer might write can reproduce the behaviour.

- **The boundary contract has a test that a plausible total cannot pass.** Changing
  the round trip from 90 % to 80 % moves the DC movement behind a commanded AC
  discharge and leaves the priced grid quantity alone; if the two were ever
  confused, that test fails at 80 % while passing at 90 %.

- **Stage A's zero-actuation promise is enforced statically**, in the same style as
  the Phase-3, Phase-4 and Phase-7 boundary tests: neither Phase-8 module imports
  Home Assistant, a source, a store or the control layer; neither calls a service;
  neither names an inverter helper, a flash-backed register or a grid-rate
  actuator; the permitted-service set is still exactly three; and
  `next_activity`'s signature is pinned to three arguments so an execution event
  cannot be logged without a visible decision.

- **Performance is guarded.** A ninety-six-interval solve is asserted to complete
  well inside the refresh budget, so the 670 ms regression cannot come back
  silently.

## [1.0.0-beta.13] - 2026-08-21

**Alpha EMS now works out how much energy the battery ought to be holding, and
still does nothing differently because of it.** A new sensor reports the
requirement; the recommendation, the planned power, the usable energy and the
control state are exactly what beta.12 published for the same inputs.

If your battery recommendation changes after this upgrade, that is a bug. Phase 7
identifies need. Deciding what to do about it — buying at a cheap hour, holding,
selling, or a safety buy — is Phase 8, and none of it is in this release.

### Added

- **Dynamic Battery Reserve**, one new sensor, in **kWh**. It answers a single
  physical question: how much stored energy must be present so the battery never
  runs short of the net demand it can physically serve, over the forecast horizon.
  The state of charge that implies travels as an attribute, because energy is the
  quantity the model conserves and a percentage is a reading of it.

  Entity count goes from eleven to twelve. Nothing else moved.

- **A backward recursion with the configured minimum as its base case.**
  `R[n] = floor`, then `R[i] = max(floor, R[i+1] + demand(i) − credit(i))`. Both
  terms are read as the energy difference `battery.apply_request` produced, so the
  module performs **no efficiency arithmetic at all** — the AC-to-DC direction is
  not merely tested but unrepresentable, and the discharge power limit is applied
  by the clamp rather than copied.

  Demand above what the inverter could deliver in a quarter-hour raises no
  requirement, because it is grid demand whatever the state of charge. It is
  reported separately instead.

- **The requirement is not monotone, and it is not the peak.** It is low while
  replenishment is imminent and high once it has passed. At midday on a sunny day
  the answer is the floor and the *peak* is the coming night — publishing the peak
  would reserve energy the sun is about to supply, and at a cheap overnight hour it
  would tell a later phase to buy kilowatt-hours it does not need.
  `peak_required_reserve_kwh` is kept as a diagnostic and is never the reserve.

- **Two counterfactuals, published every refresh**, so the one assumption this
  phase makes is measured rather than argued:
  `required_same_interval_only_kwh` (the requirement if forecast surplus cannot
  replenish the battery across intervals) and `required_pv_blind_kwh` (no
  production at all). `replenishment_dependency_kwh` is the difference between the
  first and the authoritative figure — on a sunny midday it is large, and that is
  the signal the figure beside it is optimistic.

- **A `reserve` diagnostics section**, the nineteenth. A dict key rather than a
  list entry, so the sixteen-entry ceiling every list in the payload is held to is
  untouched. No per-interval array appears anywhere: a hundred and ninety-two
  requirements truncated to sixteen would read as a short horizon rather than as a
  clipped payload.

- **Reserve evidence**, a fourth change-fingerprinted snapshot family. Scalars
  only. It exists for one reason: both forecasts are already persisted, so the
  arithmetic is reproducible — but capacity, the floor, the power limits and the
  efficiency live in the config entry, which keeps no history. Raising a minimum
  state of charge would otherwise make every earlier belief unverifiable.

### Changed

- **The "same-interval PV netting only" decision is superseded.** Forecast surplus
  may now offset accumulated deficit across intervals, capped by the inverter's
  charge power, converted at the charge boundary, floored so it can never exceed
  the deficit it repays, and flagged when capacity would have prevented it.

  This is arithmetically equivalent to assuming forecast surplus becomes usable
  future battery energy, and it is a **forecast rather than an observation** — it
  does not prove the real inverter will store that surplus. It is documented as
  superseded rather than quietly replaced, because without it the requirement
  degenerates to *all* remaining net demand to the end of the forecast: a figure
  that grows when the forecast horizon lengthens, and therefore a property of the
  forecast rather than of the battery. On the reference installation that
  degenerate figure is 21.3 kWh against a 22 kWh pack, on a sunny August day with
  30 kWh of production forecast.

  `pv_absorption.modelled` is recorded beside the requirement and read by nothing.
  On the reference installation it flipped from true to false inside fifteen
  minutes because a dispatch began, while both forecasts stood still — so a
  requirement that consulted it would have jumped for no physical reason, and an
  earlier belief would not be reproducible.

- **Forecast-history storage minor version 1.3 → 1.4**, additive. Partitions gain
  `rsv` and index rows gain `rsvfp`. A document written by any earlier release
  reads unchanged, and an installation without battery planning writes neither.

### Fixed

- **A pre-existing test defect, not a product one.**
  `test_solcast_capability_recovery` read diagnostics on the real clock after
  driving a refresh on a frozen one, so `pv.forecast_today` was looked up by a date
  the driven refresh had never produced a forecast for. The two agreed only while
  the real date happened to be the driven one: the file passed on the day it was
  written and began failing two days later. Both clocks are now pinned to one
  instant. No assertion was weakened.

### Verification

- Tests **2087 → 2256**. Five new files: the reserve model and its oracle, the
  published surface, the evidence layer, the mutations and the phase boundaries.

- **The definition is proved against an independent oracle rather than restated.**
  For six horizon shapes, starting at the requirement is handed to the Phase-3
  simulator — which applies every limit through the clamp and imports nothing from
  the reserve — and the pack touches its floor without crossing it while the only
  grid import is the demand the inverter could never have delivered in time. One
  step below the requirement, import strictly increases. Those tests pass only
  with surplus absorption modelled, and needing that flag *is* the proof of what
  the figure assumes.

- **Twenty-three mutation tests**, each a plausible refactor rather than an
  absurdity. Three of them were real mistakes made and caught while building this
  phase: publishing the peak as the requirement, measuring the capacity bound as a
  cumulative excursion (which labelled a correct answer a lower bound), and
  fingerprinting the answer instead of the inputs (which stored a document every
  quarter-hour and broke the rule that an unchanged refresh costs no I/O).

- **The blindness contract is enforced in three layers**: the signature of
  `build_reserve` is pinned to limits, a floor and demands; no identifier naming a
  price, a control mode, a dispatch state, absorption, Excess Export or Peak
  Shaving appears anywhere in the calculation; and `reserve` is now on the
  price-neutrality module list, a guard that existed before this module did.

### Unchanged

- `CONTROL_EXECUTION_AVAILABLE` is still `False`. Zero new service callers, and a
  reserve shortfall in `active` mode still issues no command at all.

- **`battery.py` is not modified.** The tripwire asserting that `dynamic_reserve`
  does not exist is still green, so this phase is structurally incapable of raising
  the floor the policy obeys. `build_plan` still calls `static_reserve`, and
  `effective_min_soc_percent` still equals the user's configured minimum.

- No config-entry version change, so nothing to migrate. No new configuration
  option: the existing minimum state of charge remains the one hard floor.

- The learning store is untouched at schema 2.3.

### Known limitations

- **The requirement is a point estimate with no margin for forecast error.**
  `ForecastUncertainty` exists and stays deliberately unread — widening a plan by
  measured error is Phase 9. On the reference installation the measured load bias
  is currently *negative*, meaning the model under-predicts, so the requirement may
  be biased low. The figure and the diagnostics both say so.

- **On a persistently non-absorbing installation** — Excess Export or Peak Shaving
  left enabled — the authoritative figure is indefinitely optimistic, because
  forecast surplus structurally never reaches the battery.
  `required_same_interval_only_kwh` is the applicable figure there. Phase 7 never
  substitutes it automatically: doing so would make the requirement depend on a
  live control flag and stop it being reproducible.

- **Winter behaviour is designed and labelled but unobserved.** With no surplus to
  credit the requirement is the whole horizon's net demand, `horizon_basis` reads
  `truncated`, and the figure is a lower bound. That is honest and it cannot be
  confirmed against a real December before December.

- The forecast and the measured production still sit on different, undeclared
  electrical boundaries. The requirement inherits that ambiguity; it is recorded,
  not corrected.

- Unrelated and retained rather than fixed here: the `pv.mapping` block reports
  `rows_received 96`, `rows_mapped 48` and `rows_out_of_range 96`, which looks like
  a merged per-day counter double-counting across two days. It is a report rather
  than a calculation, affects no value, and belongs to Phase 5.

## [1.0.0-beta.12] - 2026-08-20

**Alpha EMS now knows what electricity costs, and does nothing differently
because of it.** Prices are read, normalised, cross-checked, reported and stored
as evidence. No price value reaches a battery decision, a policy, a simulation or
a command — and that is enforced structurally rather than by comparing behaviour.

If your battery recommendation changes after this upgrade, that is a bug. It
should read exactly as it did before prices arrived.

### Added

- **Quarter-hour import, wholesale and export prices**, read from a Frank Quarter
  Prices integration you have already installed and selected. Both published days
  are re-read every refresh and mapped onto the same chronological interval
  identity load and generation already use, so 92-, 96- and 100-quarter days are
  handled by measurement rather than assumption.

- **Three prices, and only one of them is a measurement of the same kind.** The
  source publishes a wholesale price and an all-in purchase price per interval. It
  publishes **no export price at all** — the upstream endpoint has no such field —
  so the export figure is *reconstructed* from the wholesale price plus the
  adjustment configured on the source's own entry, and every interval carries a
  label saying which rule produced it.

  The asymmetry is load-bearing rather than pedantic. Sourcing markup plus energy
  tax is a fixed **0.129 EUR/kWh floor** on the import side and absent from the
  export side, so on a negative wholesale interval **importing still costs money
  while exporting earns a negative amount**. Import and export are not two signs
  of one number, and no single price field can answer both questions.

- **A price block in diagnostics** — eighteen sections now. Counts, coverage,
  edges, both cross-checks, the horizon, the capability probed now *and* as
  recorded at the last refresh, and the reason the next day is absent. Never the
  price series itself: ninety-six values truncated to the payload's sixteen-item
  ceiling would read as a short day rather than as a clipped payload.

- **Price evidence in the existing forecast store**, change-triggered by content
  fingerprint. Four floats an interval — wholesale, its tax, the all-in import
  price and the reconstructed export price — plus the two fixed components once
  per day with a flag if they ever vary within one.

  There is deliberately no outcome half. A price has no "what actually happened"
  to be scored against; it *was* the price. What cannot be recovered afterwards is
  **which future prices were visible when a plan was made** — they get revised and
  republished, so a later phase reading today's series has no way to tell what
  nine o'clock knew. That hindsight bias can only be avoided in advance.

- **An economic price horizon**, defined now so a later phase inherits one
  definition rather than inventing its own: the end of the last interval in the
  *contiguous* run of known prices. Contiguity is deliberate — knowing prices on
  both sides of a hole is not knowing them continuously — and intervals beyond a
  gap stay visible in their own count rather than disappearing.

### Changed

- **`_entry_loaded` is deleted**, along with the `ConfigEntryState` import that
  existed only to serve it. That probe asked whether a consumed integration's
  config entry was `LOADED`, and it was the cause of the beta.9 defect: an
  integration's usability has nothing to do with which phase of setup its entry
  happens to be in when you look. A test now asserts the symbol appears nowhere in
  the package, so reintroducing the pattern means reintroducing the import.

  `frank_available` is established from facts instead — an entry selected, that
  entry present, the price entities resolvable through the registry by unique id,
  their state readable.

- **Entities are resolved by unique id, never by name.** The source builds every
  unique id as `{entry_id}_{key}` and documents that as a stable contract, so
  resolving through the registry isolates the selected entry *by construction* —
  a Dutch and a Belgian entry can never be combined — and survives you renaming
  the entity. No entity id is hard-coded anywhere.

- Forecast-history minor version `1.2` → `1.3`, a backward-compatible addition.
  Every earlier document reads unchanged and nothing is migrated or discarded.

- The economics guard is widened from two decision modules to four. With a real
  normalised price series in the build, the interesting claim is no longer "no
  optimiser has been written" but "the data exists and still cannot reach a
  decision".

### The publication gap is normal operation

Between market midnight and the next day's publication — normally around 13:00 to
14:00 market time — a healthy installation reports **today complete and tomorrow
absent**. That is the source working exactly as designed, and it is reported with
its own reason rather than as a fault. Treating it as one would mark every
installation degraded for a good part of every day.

Three outcomes stay distinguishable, and only the source's own availability
signal separates them:

| Signal | Next-day entity | Meaning |
|---|---|---|
| `off` | unavailable | **normal** — not published yet |
| `on` | unavailable | abnormal — the source claims a day it is not carrying |
| `on` | available, empty | abnormal — claimed and empty |

Alpha EMS draws **no** conclusion from the clock, in either direction:
publication can be late, and if the source already reports the day before 13:00 it
is consumed. Market timezone is recorded as context and decides nothing.

The midnight rollover is the source's to perform. There is no copy operation on
this side, no retained stale day, and no synthesised day-after-next.

### Verification

Tests **1940 → 2087**.

The primary fixture is a **captured live contract** from a running installation,
headed with the source version, commit and capture date. It keeps the fields Alpha
EMS deliberately does not read — `duration_minutes` and `per_unit` — and tests
assert they stay unread, because a fixture holding only what the parser wants
cannot catch a parser reading the wrong thing. That is exactly how the beta.10
defect shipped.

What that fixture proves is stated precisely and not overstated: it proves Alpha
EMS reads *the shape that was observed*. It does **not** prove Alpha EMS reads the
source — CI cannot see that repository. Only the runtime cross-check, comparing the
current interval against the two figures the source publishes, can fail when the
*source* changes, and that is the check that runs on your installation.

Nineteen mutation tests, each a plausible refactor rather than an absurdity. The
one worth naming: on the captured installation `feed_in_adjustment` and
`sourcing_markup_price` are **the same number**, so reaching for the wrong one
reconstructs the export price correctly and passes every check including the live
cross-check. Only synthetic fixtures that keep the three components distinct can
catch it, so every synthetic block does.

Everything the live capture could not reach is covered synthetically and labelled
as such, never upgraded to live-verified: the unpublished next-day shape, an hourly
source, 92- and 100-quarter days, Home Assistant running outside the market
timezone, an explicit upstream feed-in field, and two-country isolation.

### Unchanged

No new entities. No new configuration options — the two feed-in settings are read
from the source's own entry rather than duplicated here, because the return-price
figure on your dashboard is derived from them and a second copy would drift. No
config-entry version change, so nothing to migrate.

Obtaining prices calls **no service at all** — not "no forbidden service", none.
Both days come from published entity state, so the permitted service-caller set is
untouched and there is no call site through which Alpha EMS could make the source
fetch. That is structural, not a promise.

Execution remains structurally unavailable: `CONTROL_EXECUTION_AVAILABLE` is
still `False`, ownership is still unproven, `SHADOW` writes nothing and `ACTIVE`
cannot execute. A fully healthy price source driven through `ACTIVE` is asserted
to produce zero service calls. The meter-based export rule is still the sole
export basis.

Both known findings stand: the small energy-balance boundary residual is still a
limitation with no tolerance widened, and the +1394 W gross-fault sample is still
recorded and still not a control input.

### Known limitations

- **Coverage below 1.0 is normal for some installations, not a fault.** The source
  publishes a *market* day — midnight to midnight in the market's own zone — while
  Alpha EMS plans a Home Assistant civil day. If you run Home Assistant outside
  the market timezone those are different spans, so part of your local day is
  priced by a market day that may not be published yet. It is reported, never
  repaired by extrapolation.

- **Freshness is observed, not reported.** The source does not publish its own
  last-update instant on any entity, so what Alpha EMS records is when the state
  machine last wrote. A different fact, and labelled as the different fact it is.

- The source refreshes at second 3 of each quarter and Alpha EMS reads at second
  5. A two-second margin against a network fetch, so a slow refresh means reading
  the previous quarter's series. Harmless here — mapping is by instant, so a stale
  read is an older series rather than a misaligned one — and recorded in
  provenance so a later phase does not assume the current quarter is present.

## [1.0.0-beta.11] - 2026-08-20

**Hotfix: Solcast site discovery found nothing on an account with two sites.**
beta.10 fixed the capability check, and this fixes the layer immediately behind
it. Diagnostics reported the source usable *and* discoverable while showing
`site_count: 0` and `no_solcast_sites_discovered`, so the site selector never
appeared and planning stayed PV-blind.

### Fixed

- **The diagnostic response was read at the wrong nesting level.** Both Solcast
  actions wrap their result: `query_forecast_data` returns a list under `data` and
  `diagnostic` returns a mapping under it. beta.10 unwrapped the first and read the
  second at the top level, so every field came back absent at once — no sites, no
  estimate key, no version, no API figures. That "everything empty simultaneously"
  pattern is the signature of looking in the wrong place rather than of a source
  with nothing to say, and it is now recorded as a named `response_shape` so a
  future change of convention cannot present the same way.

  The flat shape is still accepted. It costs one branch and it means a future
  convention change degrades rather than silently losing PV entirely.

- **Why the tests missed it.** The Solcast fake was written from a human-readable
  transcription of a diagnostics download rather than from the raw action response,
  so it reproduced the parser's own assumption and could only ever confirm it. The
  fake now returns the wrapped shape with the full live field set — including
  fields Alpha EMS deliberately does not read — so the whole suite exercises the
  real response.

### Verification

Tests 1897 → **1940**. New coverage for the response shape itself, and for the
site-identity rules that have to survive it: one site through nine, two sites
sharing a display name, unicode and blank names, source ordering, renames, and
capacity changes. Plus mutations for the wrong nesting level, unwrapping a list as
a payload, treating empty discovery as success, storing display names as identity,
and mislabelling the aggregate versus per-site query mode.

End-to-end: sites discovered → selection resolved and persisted → rows mapped →
the battery simulation receiving production, with `intervals_pv_aware > 0` and
`forecast_pv_kwh > 0`. beta.10's live diagnostics showed both at zero, which was
correct given zero sites and is what this asserts is no longer the case. The
PV-blind path is asserted unchanged for installations without a forecast.

### Unchanged

No new entities, no new configuration options, no storage or config-entry version
change. Execution remains structurally unavailable — `SHADOW` writes nothing,
`ACTIVE` cannot execute, and that is re-asserted with a fully recovered source and
PV-aware planning in place. The export check remains the meter-based rule alone.
Both known findings stand: the small energy-balance boundary residual, and the
+1394 W gross-fault sample.

## [1.0.0-beta.10] - 2026-08-20

**Hotfix for a live beta.9 defect: the PV forecast never started.** On a real
installation, after a full Home Assistant restart, Alpha EMS refused to read a
Solcast source that was demonstrably working — the site selector never appeared in
the options form and planning stayed PV-blind. Every restart reproduced it.

### Fixed

- **Solcast capability was decided from something unprovable.** beta.9 required
  the Solcast config entry to be in state `LOADED`. Solcast registers its actions
  at component level, so they are visible while its config entry is still setting
  up — and Alpha EMS takes its first refresh during its own setup, which can win
  that race. One diagnostics download therefore reported both actions registered
  *and* the entry not loaded, captured in a single call.

  That state was never load-bearing. Calling a registered action is safe by
  definition, and a failure was already caught and reported as a failed call
  rather than guessed at in advance. So `entry_loaded` is **removed** rather than
  forced true, and capability now comes from facts that can be demonstrated: an
  entry is selected, the stored id names an entry that exists, and the two
  read-only actions are registered.

  `solcast_entry_not_loaded` is replaced by `solcast_entry_not_found`, which is
  provable — Solcast removed, or removed and re-added under a new id. A missing
  *diagnostic* action is now named separately from a missing *query* action,
  because one costs the site list and the other costs the forecast.
- **A reading taken during setup could stand for a quarter of an hour.** Refreshes
  are driven by the quarter-hour tick rather than an interval, so anything read
  before the sources had published held until the next boundary. A refresh now
  also runs once Home Assistant reports itself started, after every integration
  has had its chance to load.

  This is also why the battery plan reported `missing_soc` beside a live state of
  charge of 96 %. The refusal itself was right — with no state of charge there is
  nothing to apply the model to — but it should not have been unrevisitable.
- **The one-time site-membership write reloaded the entry from inside a refresh.**
  Writing the options fires this entry's own update listener, and doing that
  inline tore the coordinator down halfway through the refresh that had just
  resolved the answer. The write is deferred and re-checks before writing, so a
  user answering the question themselves is never overwritten by a default
  resolved from discovery.
- **Diagnostics could contradict itself, and now cannot.** `solcast_available`
  asked whether the entry was loaded and answered at download time, while the `pv`
  block carried a capability from the last refresh — two definitions and two
  instants, printed as though they described the same thing. There is one
  definition now, the capability block is probed live, the last refresh's snapshot
  is kept beside it under its own name, and the refresh instant is published. An
  invariant test asserts the pair the user saw is unrepresentable.

### Not a defect

`pv.actual_today.intervals_recorded: 0` immediately after a restart is expected: no
complete quarter had elapsed, and a partly observed quarter cannot be recorded
without inventing the unobserved remainder. Both cases now have tests, so which is
which is asserted rather than argued.

### Unchanged

No new entities, no new configuration options, no storage or config-entry version
change. Execution remains structurally unavailable, `SHADOW` still writes nothing,
and `ACTIVE` still cannot execute — asserted again from the recovery path, driving
a recovered source through active mode and confirming zero service calls. No
mutating Solcast action exists anywhere in the package. The two known findings
stand: the small energy-balance boundary residual is still a limitation with no
tolerance widened, and the +1394 W gross-fault sample is still recorded and still
not a control input.

### Verification

Tests 1844 → **1897**. Every new test fails on beta.9 behaviour, and the beta.9
probe is reproduced verbatim beside the new one so the disagreement is visible
rather than asserted. Twenty-one mutations shaped like the original mistake —
reinstating the state check, inferring from runtime data, dropping the existence
check, assuming the actions exist, caching the refusal — plus a structural guard
that forbids reading *any* config-entry internals to decide capability, since
inferring it from someone else's setup state is the category error this defect was.

CI green on the release SHA: Lint, Tests, Hassfest, HACS validation.

## [1.0.0-beta.9] - 2026-08-20

**Phase 5: Solcast PV Forecast Integration.** Alpha EMS was completely blind to
the sun. It read your PV sensor for the energy-balance check and nothing else,
which had a visible consequence: on a sunny afternoon it recommended discharging
the battery to cover a load the panels were already covering.

It now reads a forecast from the Solcast integration you already have and nets
expected production against predicted load *before* the battery is asked for
anything. No new policy and no new objective: the existing rule is simply shown
the right number.

**Nothing is polled, and no API allowance is consumed.** Two read-only Solcast
actions, both served from that integration's own cache. Every mutating one appears
nowhere in the source, and a test proves it.

**Execution is still unavailable.** Phase 4's barrier is untouched: no command
reaches your inverter, `active` still cannot execute, and an active-mode refresh
with a forecast in hand still makes exactly zero service calls.

### Added

- **A PV forecast on the existing quarter-hour identity.** Half-hourly source
  periods are split piecewise-constant, so the quarters sum to exactly the period
  energy and no intra-period shape is invented. The period length is *measured*
  from consecutive timestamps rather than assumed — every row this project has
  seen was thirty minutes, which is precisely why assuming it would be untestable.
  Daylight saving needs no special case, because mapping is by instant: the
  repeated autumn hour produces two distinct intervals and the spring gap has no
  rows.
- **One new setting: which Solcast sites are yours.** A Solcast account can hold
  sites that have nothing to do with this system, and folding those into your plan
  would be silently wrong. **Options → Sources** now lists your sites by name and
  asks you to tick the ones that feed this AlphaESS system. On upgrade every site
  found is selected and written down once; a site added later is reported as
  available but never joins your plan on its own. Stable identifiers are stored,
  so renaming a site changes nothing.

  You are never asked which site is AC- or DC-coupled, or which feeds the hybrid.
  Most people cannot answer that reliably, and a guessed answer recorded as fact
  would be worse than the honest unknown stored instead.
- **Measured production, recorded per quarter-hour**, on the same machinery as
  house load and subject to the same coverage rule. A missing interval is missing,
  never zero.
- **Forecast-versus-actual evidence**, with both sides kept raw. Every interval
  carries a code saying why it could or could not be compared — no forecast, no
  reading, night, one declared site quiet, not yet elapsed — because a single
  residual that folds those together is not evidence of anything.
- **Tenth and ninetieth percentile bands at interval resolution.** They cost
  nothing to fetch and cannot be recovered once a day has passed.
- **A `pv` diagnostics section**, and a `pv_aware` attribute on `Battery
  Recommendation` — the one fact needed to read the recommendation that cannot
  live in prose, because prose cannot be automated against.

### Fixed

- **The export safety check was under-protective, and live data caught it.** It
  compared a proposed discharge against your house load alone, which is wrong
  whenever the panels are already covering that load. Three real shadow-mode
  samples show it: at 15:33 the house drew 2071 W against 3132 W of PV, so the
  site was exporting a kilowatt and the absorbing capacity was the 22 W of import
  actually recorded — and the check read 2071 W and passed.

  Capacity is now measured where export is defined: `grid import − grid export +
  battery discharge`. A forced discharge first displaces import and only spills
  onto the grid once import reaches zero, so that expression is the bound, derived
  rather than tuned. Subtracting PV from house load would also have caught all
  three, but the meter needs no PV term at all — no assumption about how your
  arrays are wired, no exposure to the vendor's filter on the PV signal, and no
  daylight rule for a sensor that legitimately reads zero all night. It also
  bounds loads your house-load sensor cannot see, which on this installation is
  about 1.4 kW.

  The check still refuses whole commands and still never scales one. Two
  conditions were *added* rather than replaced — an unreadable meter and a stale
  one both refuse — so it is strictly tighter than before.
- **PV finally has the sanitisers the other sources always had.** It had a bare
  non-negative check and no plausibility ceiling, so a spike to a million watts
  was accepted and inflated the energy-balance allowance — making the check most
  permissive exactly when the entity was most obviously wrong — while half a watt
  of noise below zero was thrown away as unreadable. Both are now the right way
  round.
- **Stale interface wording.** The PV source is no longer described as "recorded
  for the energy-balance check only", and the forecast option no longer says it
  merely validates that the source is reachable.

### Changed

- **The projected state of charge is realistic where your inverter is storing
  surplus, and says so where it is not.** With **Excess Export** on, that feature
  deliberately sends production to the grid rather than the battery — so the
  projection reports itself as a lower bound instead of overstating your battery.
  The same applies while Peak Shaving is on or a dispatch is running. The PV-blind
  disclaimer was made conditional rather than deleted: it exists because a visibly
  wrong figure costs more trust than it buys, and a partly covered horizon is
  still partly wrong.
- **Storage: learning history 2.2 → 2.3, forecast history 1.2 → 1.3.** Both minor.
  Every earlier document is read unchanged and simply written back in the newer
  form; nothing migrates and nothing is discarded. The config entry stays at
  version 2, so no reinstall is needed and no history is lost.

### Not in this release

No prices, no cheap-hour buying, no reserve sized for tomorrow's weather, no
overnight carry-over, no arbitrage, and no self-learning correction of any kind.
Expected production is an input to the plan, never a reason to buy or sell. A
forecast that turned out badly cannot change the next one — asserted both
behaviourally and structurally.

### Verification

Tests 1545 → 1844, including sixteen new mutations that break a Phase-5 invariant
and prove a test notices. Writing them found three real defects, each fixed here:
a hole in the source series silently stretched every row across the gap and
fabricated generation; the selection origin reported a user decision on the very
refresh that had resolved it automatically; and an unreadable Excess Export
boolean was indistinguishable from one switched off, which is the unsafe direction
for the surplus question. A fourth was caught by an existing test the moment the
device read was added — it could take down a whole refresh.

Phases 1 to 4 are asserted unchanged rather than assumed: the whole integration is
driven twice at the same instant, once with a forecast and once without, and every
Phase-1 and Phase-2 figure is compared along with the stored baselines themselves.
`test_pv_independence.py` and `test_stale_zero_pv.py` pass unmodified.

## [1.0.0-beta.8] - 2026-08-20

**Phase 4: AlphaESS Actions & Control.** The integration now builds the complete
path from a battery decision to a real inverter command — translation, every
safety check, the exact helper values in the exact order — and then cannot walk
it.

**No command reaches your inverter in this release.** That is enforced by a
build-time constant, not by a setting: the executor refuses on its own, and a
whole-integration test drives every mode against a healthy control surface and
asserts the count of service calls is exactly zero.

Phases 1, 2 and 3 are behaviourally unchanged, asserted rather than assumed: the
nine existing entities are driven twice at the same instant, once with control
running and once with it off, and compared figure for figure.

### Added

- **Two entities, and only two.** `Control Mode` (`off` / `shadow` / `active`,
  starting at `off`) and `Control State` (`inhibited`, `eligible`, `idle`,
  `off`). Everything the pipeline computes — which parts of your control surface
  were found, what the inverter is doing, the intent, the quantised command, the
  ordered command list, the event trail — is in eight flat attributes and in
  diagnostics. A safety gate with twenty-five ways to refuse must not become
  twenty-five rows on a dashboard.
- **Shadow mode, which runs the real pipeline.** The same translation, the same
  safety gate and the same command planner `active` uses, with nothing written.
  Its verdict is the real verdict, so its diagnostics answer "would this have
  been safe, and what exactly would it have sent". `inhibited` and `eligible` are
  deliberately distinct states: the first means a safety check refused, the
  second that none did and only the release barrier stopped a command.
- **Safety eligibility separated from permission to execute.** Twenty-five
  conditions, none of which reads the control mode — so shadow and active return
  identical verdicts, proven over the full condition table. Authorization is a
  second, narrower stage that can only ever subtract.
- **The gate refuses; it never reduces.** A command that would push energy onto
  the grid is refused whole rather than trimmed to fit the house load. There is
  no magnitude on the verdict at all, so nothing downstream could mistake it for
  a smaller command. A gate that trimmed a request would have made a decision,
  and deciding belongs to Phase 3.
- **Two control settings**, under Options → Control: the command duration (a
  dead-man timeout, not a delivery window) and an export safety margin.
- **Energy-balance instrumentation**, all diagnostics-only: the sign of each
  failure, the mean signed residual, failures and accumulated overshoot per
  power band, and a windowed failure count that can see an alternating fault the
  consecutive counter cannot.
- **A state-of-charge coherence instrument**, also diagnostics-only: over a
  closed interval, did the stored energy move the way the measured battery power
  said it would? It measures what your sensor can actually resolve rather than
  assuming it.

### Honest about what it cannot do

- **It cannot execute, and enabling that later is not a formality.** Nothing in
  the AlphaESS control surface records *who* armed a dispatch — a dispatch set
  from a dashboard and one set by a service call leave byte-identical helper
  values, registers, timers and read-backs. So Alpha EMS cannot prove a running
  dispatch is its own, and matching power, cutoff or duration is not proof: the
  person most likely to have armed exactly the figures Alpha EMS would have sent
  is you, watching the shadow recommendation. That blocks stopping a command
  *and* continuing one beyond a single interval.
- **There is no stop path, deliberately.** A stop whose authorisation cannot be
  established is worse than none, because the next person to read it inherits an
  open safety question dressed as working code.
- **`off` means Alpha EMS attempts no control.** It does not mean your inverter
  reverts. In this release the distinction cannot arise, because nothing here can
  start a dispatch — but it is stated rather than glossed, because a later release
  will have to choose.
- **Nothing today is worth executing anyway.** The shipped recommendation is the
  discharge that covers your predicted load, which a self-consumption inverter
  already does — and does better, because it tracks load continuously while a
  fixed-power command cannot. The pipeline exists so it can be watched for months
  before anything depends on it.

### Changed

- **The energy-balance residual is no longer a candidate control interlock.** An
  earlier design would have blocked control on a gross-fault verdict. Live data
  disproved it: where the house-load figure comes from the inverter's own grid
  register and the balance check reads a separate meter, the residual reduces to
  the difference between those two meters — the battery term cancels identically
  and the state of charge never appears. Two real samples showed it: **+1394 W**
  (12.35× its allowance, an unmetered load) and **−10149 W** (during a 10.18 kW
  charge ramp). Neither is a broken sensor; both would have blocked control.
  **No tolerance was widened and no threshold tuned** — the allowance produces
  exactly the figures it always did, both samples are still labelled gross faults,
  and both still warn. Control simply does not consult them.

### Notes for anyone upgrading

- Nothing is required, and nothing changes until you ask it to. There is no
  migration, no new required setting, and the control mode starts at `off`.
- To watch it: **Settings → Devices & Services → Alpha EMS Manager**, set
  **Control Mode** to `shadow`, and read **Control State** plus the `control`
  block in diagnostics.
- Alpha EMS uses the AlphaESS package's own helpers and its own tested write
  sequence; it never writes a Modbus register directly, and it never writes any
  setting the inverter keeps in flash memory.
- Tests: 1303 → 1545. Nineteen mutation tests deliberately break a control
  invariant and prove a test notices — which is how two gaps in this suite were
  found and closed.

## [1.0.0-beta.7] - 2026-08-20

**Phase 3: Battery Decision & Simulation.** The integration now works out what
the battery *should* do and simulates the consequence.

**It still controls nothing.** No command is sent to the battery, no service is
called, and nothing executes the plan. The recommendation is published so it can
be watched for weeks before any later phase is allowed to act on it, and that is
enforced rather than intended: a test reads the real sources and asserts no
Phase-3 module imports a network client or calls a service.

Phase 1 and Phase 2 are behaviourally unchanged. The learning-history schema
gains one optional array and its *minor* version moves 2.1 → 2.2; the major
version, the forecast-history schema and the config-entry schema are all
untouched, so learned days, forecast evidence, entity IDs and unique IDs survive
the upgrade with no migration and no remove-and-re-add.

### Added

- **Three entities, and only three.** `Battery Recommendation` (`hold`, `charge`
  or `discharge`, with its reason in the attributes, and `unknown` when a battery
  figure is missing); `Planned Battery Power` in kW, positive for charging, and
  labelled as the interval *average* it is rather than an inverter setpoint; and
  `Usable Battery Energy`, the energy genuinely available above the minimum state
  of charge. Everything else the layer computes — the simulated trajectory, where
  the floor would bite, the per-band split, the comparison against leaving the
  battery alone, the projected state of charge — is diagnostics-only. A
  ninety-six-interval plan has no business in an entity attribute.
- **Battery planning settings, in their own Options page.** Usable capacity,
  minimum state of charge, maximum charge and discharge power, and round-trip
  efficiency. A second page rather than five more fields on a form that already
  had thirteen. Every label states which side of the inverter it refers to,
  because entering a manufacturer's AC "usable energy" figure where a DC capacity
  belongs is a five-per-cent error that nothing downstream can detect.
- **Minimum state of charge is a hard floor.** No recommendation and no simulated
  interval ever crosses it. It is kept deliberately distinct from the *policy*
  reserve — identical in this release — so that a later phase can raise the
  reserve dynamically without ever being able to lower the floor the user chose.
- **A recorded state of charge, once per quarter-hour.** The one physical
  observation a battery plan depends on that cannot be reconstructed from
  anything else: a prediction can be recomputed from the stored forecast and the
  stored settings, but where the battery actually was last Tuesday cannot. It is
  additive evidence for a later phase and never a learning input — adding,
  removing or corrupting it cannot move learned days, confidence, the baseline,
  the forecast or any forecast-error figure, and a dedicated test file holds that
  down. Absent on every day recorded by an earlier release, and read as absent
  rather than as zero.
- **A what-if comparison.** Every refresh simulates two futures — the battery left
  alone, and the recommendation followed — and reports the difference. That is
  the shape a later phase will put prices against.

### Honest about what it cannot see

- **The simulation has no solar production term.** Forecasting production is a
  later phase, and inventing one would be exactly the fabrication this project
  avoids. So the simulation answers a narrow question — given the predicted
  household load and no other generation, what happens to the battery — and every
  figure derived from it is labelled a battery-only counterfactual. On a sunny day
  the real state of charge will be higher than the projection and the simulated
  grid import well above reality. **That is why the projected state of charge is
  not published as an entity**; a visibly wrong entity costs more trust than it
  buys. `Usable Battery Energy` and `Battery Recommendation` do not depend on
  production and are unaffected.
- **`Usable Battery Energy` is an upper bound.** A single efficiency figure
  flatters a real inverter at low power, and the inverter's own standby draw is
  not modelled. Both biases point the same way. Neither is guessed at.
- **Nothing here charges the battery.** Every reason to would need information
  this release does not have — surplus production, a price, a storm warning, an
  arbitrage spread — and the inverter already absorbs solar surplus by itself. The
  charge path is fully built, constrained and simulated so later phases have
  somewhere to land, and a test asserts that no policy shipped today uses it.
- **A missing battery figure is reported, never guessed.** Capacity and the two
  power limits have no default, because nothing can derive them. Without them the
  three battery entities read `unknown` and name the figure that is missing;
  learning and forecasting are untouched.

### Fixed

- **`ForecastUncertainty.mae_by_band` was mutable.** The public interface promises
  that everything it returns is frozen and copied, but this was a plain
  dictionary on a frozen dataclass: the reference could not be swapped, while a
  caller could edit a band average in place and pass the altered object on. It is
  now a read-only mapping.
- **A test that could not fail.** `test_the_public_interface_exposes_only_what_it_promises`
  compared a *subset*, so a new public name — or a decision accidentally exposed
  on the interface — would have passed a test whose name says it would not. It now
  compares an exact set over the names the module actually defines.

### Notes for anyone upgrading

- Nothing is required. The integration loads, learns and forecasts exactly as
  before; only the three new entities read `unknown` until the battery figures are
  entered.
- To enable the battery planning: **Settings → Devices & Services → Alpha EMS
  Manager → Configure → Battery planning**. Enter the usable capacity, the two
  power limits, and adjust the minimum state of charge if 20 % is not what you
  want. The entities become live on the next quarter-hour without a restart.
- Tests: 1026 → 1303. Every safety invariant was demonstrated to fail against a
  deliberately broken implementation before being accepted as evidence.

## [1.0.0-beta.6] - 2026-08-20

Closes Phase 2. `beta.5` shipped the forecast evidence layer; its first real
midnight rollover exposed four defects in it, and this release fixes all four,
repairs the record the first one damaged, and proves the whole scoring pipeline
end to end at exact values instead of at tolerances.

Nothing about the forecast itself changes. No threshold, tolerance, weighting or
learning rule was touched, and the four Phase-1 sensors publish identical
numbers. The config-entry schema stays at v2 and the learning-history schema at
v2, so learned days, source selections, entity IDs and unique IDs all survive.
The forecast-history schema stays at major v1 — its *minor* version moves from 1
to 2 for one added optional field — so every prediction and every matched actual
`beta.5` wrote is read back unchanged. No remove-and-re-add, and no battery is
controlled.

### Fixed

- **A missing quarter was read as the meaning of "baseline" changing, and it
  excluded the whole day from every statistic — permanently.** This is why the
  19 August rollover advanced the learning side correctly while
  `Forecast Error Yesterday` stayed `unknown`: the day was matched, then flagged
  `definition_changed`, and a flagged day is never scored.

  The flag is meant to catch a real change of quantity — a flexible-load source
  selected or removed part-way through a day, which makes the morning's baseline
  and the afternoon's two different things that no single prediction can be
  scored against. It was judged from the per-interval flexible-load expectation
  recorded on the day. But that expectation is only written for quarters that
  were actually *accepted*: a quarter that never reached coverage, or that fell
  inside a Home Assistant restart, is never recorded at all and keeps the "no
  flexible load" default it was padded with. On any installation with a charger
  configured, one such quarter therefore looked exactly like the charger being
  switched on mid-day — and a restart is guaranteed to produce one, including the
  restart that installs an update.

  A gap is a gap. The per-interval status codes already describe it precisely,
  and it says nothing whatever about what "baseline" meant. The judgement is now
  made over the intervals that were actually observed, so a day with a data gap
  is scored on the intervals it has, while a genuine mid-day change of
  configuration is still excluded. A day with no observation at all now makes no
  claim either way: it has no comparable interval regardless, and asserting a
  definition change on top of that invents the one reason a maintainer would go
  and investigate.

- **A corrected matching rule now reaches the days already matched.** Matching
  only ever looked at days that had never been matched, so the day the defect
  above damaged would have carried its wrong verdict for as long as the record
  survived. Matching is a pure recomputation from a stored prediction and a
  retained learning record, so a day whose match predates the current rules is
  re-derived — while both of those inputs are still on disk, and never
  otherwise. The predictions themselves are never touched: they are the
  evidence, and only the reading of them is restated. On upgrade this repairs
  19 August in place rather than merely sparing the days after it.

- **A host whose clock ran years ahead deleted every stored prediction array.**
  Retention is measured against a reference clamped to one day past the newest
  recorded day, precisely so a Pi without a real-time clock cannot define "now"
  and take the history with it. The clamp was inert: forecasts were recorded
  before retention ran, so the bogus future day was already inside the set the
  clamp measures against. One refresh under a five-year excursion reduced the
  entire history to daily summaries. The same ordering defect was found and fixed
  in the learning history in `beta.4`; the test that was supposed to cover it
  here exercised the retention function directly and so never touched the path a
  refresh actually takes.

- **`Forecast Error 7 Days` published energy it had not measured.** Until the
  window holds about two full days, the error *rate* is withheld — below that it
  is whichever handful of intervals happened to resolve, and a new
  installation's noise would read as forecast quality. But the two energy totals
  were being dropped to zero along with it, so the sensor advertised
  `predicted_kwh: 0.0` and `actual_kwh: 0.0` beside an `intervals_compared` of
  ninety-six: a claim that the house consumed nothing, from the one sensor whose
  entire purpose is to refuse that substitution. The sample size and both
  energies are facts about the window and are now always reported; only the rate
  waits. A window with genuinely nothing in it reports no energy at all rather
  than zero.

- **Non-finite numbers are refused wherever a document is read.** Nothing this
  integration writes can produce a `NaN` or an infinity, but a hand-edited or
  externally damaged file can, and one would travel through every mean, total and
  forecast into a sensor state — comparing false against every guard that might
  have caught it. Measured energies, predictions, matched actuals and summary
  rows now all reject them as missing data. One damaged summary row no longer
  voids the sound rows beside it.

### Changed

- **Diagnostics explains an excluded day instead of counting it.** A flag count
  says a day was dropped, not why. Each excluded day is now reported with the
  facts that excluded it — its interval count, how much of it carried a valid
  measurement, the timezone its record was written in, its flexible-load total,
  and the baseline definition, shape and timezone of each prediction made for it.
  The list is newest-first and capped; the counts beside it stay complete.
- **Diagnostics and the two sensors can no longer appear to disagree.** The
  rolling statistics in a download are deliberately ungated — a maintainer wants
  the figure whatever its sample size — which meant a payload could show a WAPE
  of 25 % next to an entity reading `unknown` with nothing explaining which was
  wrong. Neither was. The payload now also carries what the entities actually
  publish and the threshold that separates the two.
- **The matching rules carry their own version,** separate from the model
  version, recorded on every daily summary. Two comparisons produced under
  different rules must never be pooled into one error series, and a later phase
  reading the history has to be able to tell.

### Notes for anyone upgrading

- `Forecast Error Yesterday` becomes a real number as soon as one prior day has
  been validated. `Forecast Error 7 Days` waits for about two full days of
  compared intervals, by design, and reports its sample size honestly while it
  waits.
- On the first refresh after the update, a day excluded by the old rule is
  re-matched if its prediction and its learning record are both still retained.
  Diagnostics reports it under `matching.restated_last_refresh`. A day that
  *stays* excluded was excluded correctly.
- Tests: 964 → 1026. Every new regression test was demonstrated to fail against
  the `beta.5` behaviour before the fix and to pass after it.

## [1.0.0-beta.5] - 2026-08-19

The first Phase-2 release: **Load Forecasting & Forecast-Error Logging**.

Phase 1 learned what the house uses and predicted what it will use. Until now
those predictions vanished the moment a newer one replaced them, so the obvious
question -- *was it right?* -- had no answer anywhere. This release keeps the
evidence.

Nothing about the forecast itself changes. No threshold, tolerance, weighting or
learning rule was touched, and the numbers the four existing sensors publish are
identical to `beta.4`. The storage schema stays at v2 and the config-entry schema
at v2, so upgrading preserves learned history, source selections, entity ids and
unique ids. No remove-and-re-add is required, and no battery is controlled.

### Added

- **Immutable forecast records.** Every forecast the model issues is kept
  exactly as it stood at the moment it was made -- the per-quarter prediction,
  which intervals were genuinely modelled and which were extrapolated from a
  neighbour, how many learned days stood behind it, the weekday/weekend
  decision, the confidence at the time, and the version and parameter
  fingerprint of the model that produced it. A record is never overwritten by a
  later one: a prediction that turned out to be wrong is evidence, not a mistake
  to be tidied away.
- **Records are written when the forecast changes, not on every refresh.** The
  Phase-1 model is a pure function of history that cannot change between one
  midnight and the next, so a per-refresh policy would have written ninety-six
  identical copies a day. A content fingerprint reduces that to the two
  predictions that genuinely differ: the one made the day before, and the one
  made on the day itself. Volatile context -- the issuance time, the confidence
  percentage, the energy-balance score -- is recorded on the record but excluded
  from the fingerprint, because the balance score is resampled every minute and
  would otherwise defeat the whole policy. Withheld forecasts are recorded too,
  with the reason: a model that never spoke must not later look like a model
  that was never wrong.
- **Matching against what actually happened.** Once a day can no longer gain
  intervals, the measured **baseline** -- household load minus any configured
  flexible load, the same quantity the model predicts -- is matched to the
  prediction interval by interval. Comparing against raw measured load instead
  would charge the model for energy an EV drew, which is precisely the load the
  baseline exists to exclude. A quarter that was never measured stays missing;
  it is never read as zero consumption, and a day is only ever scored on the
  intervals where a prediction and a trustworthy measurement both exist.
- **Two new sensors, and only two.** `Forecast Error Yesterday` in kWh, signed
  so that positive means the model predicted more than the house used; and
  `Forecast Error 7 Days` as a rolling weighted percentage,
  `sum(|error|) / sum(actual)`. Both read `unknown` until there is something
  honest to report, and neither is an "accuracy" score -- `100 - error` goes
  negative on a bad week and invites comparison with unrelated systems. The
  integration now publishes six entities. Everything else -- the record
  inventory, error broken down by look-ahead, by time of day and by
  modelled-versus-extrapolated interval, the matching health and the storage
  health -- is diagnostics only.
- **Per-interval fill provenance on the forecast.** `filled_intervals` said how
  many intervals were extrapolated from a neighbour but never which, and the
  filling step overwrites the values in place, so the information was destroyed
  at the moment it was created. A per-interval mask now travels with the
  forecast. It changes no predicted value; it exists because comparing error on
  modelled versus extrapolated intervals cannot be reconstructed after the fact
  and would otherwise have to be recovered by running a second copy of the model
  elsewhere.
- **A separate, partitioned, versioned store for the evidence.** A small
  always-loaded index plus one document per calendar month of predicted days.
  Home Assistant rewrites a whole document on every save, so a single year-long
  file would put roughly a megabyte through the disk on each write and would
  lose the entire history to one corrupt byte. Writes are atomic, and the common
  case -- a refresh reproducing the forecast it produced fifteen minutes ago --
  performs no disk access at all. Around 3.5 kB per day, so about 1.3 MB at the
  365-day steady state.
- **Retention aligned with the learning history.** Raw quarter-level evidence is
  kept for 365 days, the same window as the learning data it can be correlated
  with; past that point the inputs behind a forecast are gone, so the raw arrays
  could no longer explain *why* it was wrong. Reduced daily summaries -- about
  200 bytes each, and enough to rebuild the rolling figures -- are kept for
  years.
- **A stable interface for later phases.** `api.py` exposes the current
  forecast, any historical prediction as it was issued, and measured forecast
  uncertainty, as frozen and copied structures. Nothing outside it may reach
  into the storage internals, and a test enforces that statically over the real
  source files.

### Fixed

- **A day-ahead and a day-of prediction could collapse into one record.** The
  fingerprint covered content but not look-ahead, so on a settled household --
  where the model often says the same thing on both days -- only one record
  survived. That left the day-of side of any look-ahead comparison
  systematically empty of exactly the days the model found easy. Look-ahead is
  now part of the fingerprint. It costs nothing in churn, because it is constant
  for a whole civil day.

### Safety

- **Matching is suspended while the learning history cannot be read.** An
  unreadable learning document degrades to an empty history so that setup can
  continue, which is right for availability -- but every interval then reads as
  missing. Matching against that would write immutable records permanently
  asserting that nothing was measured, for days whose measurements are almost
  certainly intact on disk. This is the `beta.4` write-after-failed-read defect
  one layer up, and worse, because these records are final by design. Nothing is
  lost by waiting: matching is a pure recomputation from persisted data, so the
  days simply resolve after a restart that reads the history successfully.
- **Nothing in the evidence layer can fail a refresh.** Learning and both
  forecasts do not read any of it, so a storage failure degrades the evidence
  and leaves the four Phase-1 sensors alone.
- **The clock-excursion and failed-read rules are reproduced, not re-learned.**
  Pruning clamps its reference against known history, so a host without a
  real-time clock cannot define "now" and delete the retention window behind it;
  and a store that could not be read refuses to write for the rest of the
  session.
- **Days whose two sides are not comparable are kept but never scored.** A
  changed timezone, a changed day length, a flexible-load source added or
  removed, or a day with no record at all: the prediction and the measurement
  are both true facts, and only their comparability is void. Index-matching two
  different day shapes would line an 18:00 prediction up against a 17:00
  measurement and look entirely plausible doing it.

### Changed

- The entity contract now documents **six** sensors rather than four. Existing
  entity ids and unique ids are unchanged.

## [1.0.0-beta.4] - 2026-08-19

The final Phase-1 hardening release. Three defects reported from live `beta.3`
operation are fixed, and a full audit of the learning and data foundation found
a further eleven -- including one that could destroy a year of learned history.
**No tolerance, learning threshold or forecast threshold was changed.** Every fix
corrects logic that was already wrong, rather than widening a limit that was
catching something real.

The storage schema stays at v2 and the config-entry schema at v2, so upgrading
preserves learned history, source selections, entity ids and unique ids. No
remove-and-re-add is required.

### Fixed

- **A PV sensor resting at 0 W overnight blocked the energy-balance check for
  the whole night.** The AlphaESS PV template stops republishing while
  generation is zero, so its report age reached three hours and every sample was
  skipped as `stale_source` -- 185 of 189 skips on the reference installation,
  while the identity being blocked closed to within 1 W. A PV source whose
  current value is *exactly* zero now takes no part in the timing comparison,
  because the term it contributes to the identity is exactly zero however old
  the reading is. Nothing else is relaxed: a stale positive PV, an unreadable
  PV, and a stale battery, grid or house-load source are all judged as before,
  and there is deliberately no tolerance band around zero. The exemption is
  self-terminating -- a sensor that starts generating publishes a new value by
  definition -- and it can only ever produce a *failure* at sunrise, never a
  spurious pass. Exemptions are counted per entity in diagnostics.
- **Coverage counted intervals that had not happened yet.** Diagnostics divided
  valid intervals by the full civil length of every retained day, including the
  day in progress, so a perfectly healthy installation reported 25 % coverage at
  06:00 and recovered by itself at midnight. Coverage is now measured against
  intervals that have actually elapsed, with finalised days still measured
  against their whole 92/96/100-interval length. The confidence score was never
  affected -- it is computed over learned days only, which are by definition
  finalised -- and it does not move.
- **A retained day with no usable data could distort the weekday/weekend
  split.** A day contributing zero valid baseline intervals still counted toward
  `MIN_DAYS_FOR_DAY_TYPE`, so it could engage the day-type split on the strength
  of contributing nothing, narrow the model to that one day type, and lower
  `model_days` below what the same history pooled would support. Unusable days
  now take part in no decision at all.
- **A forecast could be withheld while reporting no reason for it.** Once two
  days of a type existed the split engaged, but `model_days` counted only
  *learned* days of that type -- so two partial weekend days produced
  `model_days: 0`, an unavailable Saturday forecast, and `unavailable_reason:
  null`, alongside a diagnostics payload claiming 96 of 96 modelled intervals.
  It was also non-monotonic in data: deleting one of the two partial days
  restored the forecast, so acquiring history removed one. The split now engages
  on learned days of the type, matching what `MIN_DAYS_FOR_DAY_TYPE` has always
  been documented to mean, and an unavailable forecast always carries a reason.
- **A single future-dated day deleted the entire retention window.** The
  clock-excursion guard in `prune()` clamps against the newest stored day, but
  `get_or_create` inserted the new day *before* pruning -- so the clamp measured
  itself against a set that already contained the future date and could never
  fire. A host without a real-time clock, or one whose clock is stepped before
  NTP corrects it, therefore dropped every learned day, and the debounced save
  wrote the empty document to disk within the minute. Pruning now happens before
  insertion, and the reference is clamped to one day past known history.
- **A failed read could overwrite an intact learning document.** An unreadable
  store degrades to an empty history so setup can continue, but that empty
  history was then written straight back to disk on the next unload or shutdown,
  turning one transient I/O error into permanent loss. Writes are suspended for
  the session after a failed load, and diagnostics report
  `storage.writes_suspended`.
- **A timezone change split the write path across two calendars.** Both
  accumulators capture the zone at setup while the storage layer creates records
  in whatever zone is current, and Home Assistant does not reload config entries
  when its timezone changes -- so until the next restart, quarters were filed
  hours away from where they belonged, into days that still looked complete. The
  entry now reloads on a timezone change, and intervals are indexed in the zone
  their day was recorded in.
- **A quarter could report itself as finalised while storing nothing.**
  `record_interval` silently dropped an out-of-range index. It now reports the
  drop, which is counted and named as `interval_outside_stored_day`.
- **A large forward clock step blocked the event loop.** A host starting in 1970
  and stepped to the present asked the accumulator to close roughly two million
  quarter-hour buckets in one synchronous loop -- about twelve seconds of
  blocked event loop and several hundred megabytes of results that were all
  going to be rejected anyway, since every quarter in a gap that long already
  fails the sample-gap test. Accumulation now restarts beyond a day.
- **A house-load source stuck at exactly 0 W raised the confidence score while
  degrading the forecast.** Such a day is fully covered and fully valid, so it
  counted as learned and lifted both maturity and coverage, while dragging every
  slot mean toward zero -- and `_stability`, the one component whose job is to
  notice that daily totals disagree, filtered zero totals out. It no longer
  does.
- **`modelled_intervals` claimed neighbour-filled intervals had been modelled.**
  It was overwritten with the day length on every published forecast, making it
  a constant and hiding exactly what it was added to show. It now reports what
  was really blended, alongside a new `filled_intervals`.
- **Tomorrow's sensor published model metadata for a forecast it was
  withholding.** Today's attributes were gated on availability; tomorrow's were
  not, so a template saw all five look-back windows and a day-type decision
  behind a sensor reading `unknown`.
- **A stale Solcast selection made the options form reject every submission.**
  The dropdown validates against the live entry list, so once Solcast was
  removed -- or removed and re-added under a new id -- the stored value was no
  longer selectable and the form failed schema validation before any field error
  could be produced. The user could not change any unrelated setting. The Frank
  dropdown already guarded against this; Solcast now does too.
- **The flexible-load sensor could be set to the house-load sensor.** Since
  `baseline = max(measured - flexible, 0)`, that makes the baseline exactly zero
  for every interval of every day -- valid, complete, counted as learned, and a
  confident 0 kWh forecast that nothing downstream could distinguish from a
  house using no energy. It is refused at selection time in both flows.
- **An implausible house-load reading widened the energy-balance allowance.**
  The learning path rejected a reading above `MAX_PLAUSIBLE_LOAD_W`, but the
  balance path accepted it into `ac_power`, making the check most permissive
  exactly when the house-load entity was most obviously wrong. Both paths now
  apply the same rule.
- **A dead battery, PV or grid entity failed silently.** Those three are read
  through `_read_power`, which logs nothing -- unlike house load, which is on
  the learning path and reports its own problems. An unreadable one therefore
  left the balance check with no verdict to give, forever, and the only symptom
  was `unavailable_samples` climbing without naming which of four sources was
  missing. Unreadable sources are now counted per entity in
  `unavailable_source_counts` and warned about once, with the warning stating
  plainly that learning is unaffected.
- **`open_quarter_coverage` on the Learning Days sensor could only ever read
  about 0.0.** Attributes are captured when the coordinator writes state, and it
  writes at the quarter tick plus five seconds -- so the open quarter was always
  five seconds old when the figure was taken. The number was true and useless,
  and read as a fault. It is gone from the entity and kept in diagnostics, where
  the payload is built on demand and the figure means something.

### Added

- **Rejected-quarter attribution.** Every route to a rejected quarter ends in
  "coverage too low", so the bare count could not distinguish a normal restart
  from an entity that had been publishing kWh instead of W since the day it was
  selected -- and learning could stall with nothing in the log to explain it.
  Diagnostics now carry `rejected_quarters_by_reason`, `last_rejected_quarter`
  and `last_rejected_reason`, and the flexible load carries
  `intervals_without_valid_data_by_reason`. Reasons distinguish a missing
  entity, an unavailable state, a non-numeric state, a missing unit, a non-power
  unit, an implausible value, an out-of-range interval, and genuinely thin
  coverage. A source fault warns at most once per reason per throttle window;
  thin coverage with a healthy source logs at debug, because a message that
  fires after every restart teaches the user to ignore the channel.
- **Coverage populations reported separately** -- `learning.completed_days` for
  finalised days, `learning.current_day` for the day in progress,
  `learning.occurred_intervals` as the shared denominator, and a
  `coverage_basis` note -- so `learning.baseline_coverage` and
  `confidence.coverage` can no longer be read as the same figure.
- **A day-total cross-check in diagnostics.** `daily_validation` compares this
  integration's integrated measurement against the vendor's own daily counter,
  with the difference in kWh and per cent and the coverage caveat alongside. It
  is diagnostic only and structurally incapable of being anything else: nothing
  in the learning path reads the validation entity. It reports factual states
  rather than a pass/fail verdict, because no defensible tolerance separates
  "agrees" from "disagrees" between two different measurement methods.
- **`storage.reset_by_schema_migration`**, so a discarded pre-v2 document is
  distinguishable from a fresh install. The flag existed but was never set or
  read, leaving only a log line that has usually rotated away by the time anyone
  asks.

### Changed

- **Both forecasts share one prepared view of the history.** The observation
  bucketing depends only on the records and the reference day, so building it
  once per target repeated the most expensive part of every refresh -- around
  90 ms at a year of history, twice, on the event loop every quarter of an hour,
  and several times that on a Raspberry Pi. Results are unchanged.
- Days older than the longest look-back window are no longer counted as model
  inputs. `_mean` already ignored them, so reporting them overstated the history
  behind a forecast.
- Removed `QuarterAccumulator.mark_unavailable`, `poll` and `open_slot_start`,
  and `PowerFlows.has_house_load`, none of which were called from anywhere.

### Testing

610 tests at `beta.3`, 762 at `beta.4`. Every fix above has a regression test
that fails against `beta.3` and passes here, across
`tests/test_stale_zero_pv.py`, `tests/test_coverage_semantics.py`,
`tests/test_empty_day_isolation.py`, `tests/test_rejection_visibility.py`,
`tests/test_beta4_audit_regressions.py`,
`tests/test_source_availability_visibility.py` and
`tests/test_midnight_finalization.py`.

## [1.0.0-beta.3] - 2026-08-18

A Phase-1 bugfix beta, from an investigation of energy-balance failures observed
during live Home Assistant testing of `1.0.0-beta.2`. **No tolerance was changed,
no learning threshold was changed, and no forecast threshold was changed.** The
balance failures under investigation turned out to be legitimate detections; what
was wrong was how they were *reported*.

### Fixed

- **A gross-fault warning could be discarded permanently, leaving only the
  reassuring wording for a genuinely broken configuration.** Both energy-balance
  messages shared one throttle key. A moderate residual warned, a passing sample
  re-armed the sustained-failure debounce, and the gross fault that followed
  inside the one-hour throttle window was dropped — and could never be re-raised,
  because only a passing coherent sample re-arms the one-shot flag and a real
  fault never produces one. The user was left reading "Learning is unaffected"
  while the log never mentioned checking source entities or sign conventions. The
  two wordings now throttle independently.
- **`energy_balance.last_warning` reported warnings that were never logged.** The
  timestamp was stamped before the throttled log call, so a suppressed message
  still advanced it. Anyone reading diagnostics then searched the log for an entry
  that did not exist — which is exactly how the live evidence for this
  investigation became ambiguous. `_ThrottledLogger.warning()` now reports whether
  it emitted, and the timestamp is only stamped when it did.
- **`learning.learned_days` in diagnostics disagreed with the Learning Days
  sensor.** It was recomputed with `store.learned_days()` and no `before`
  argument, so it counted the in-progress day from the moment its baseline
  coverage crossed `MIN_DAY_COMPLETENESS` — around 19:15 on a clean day. A
  download taken that evening reported one more learned day than the entity
  showed. Diagnostics now reads the value the coordinator publishes, so the two
  cannot diverge. `coordinator.learned_day_dates()` carried the same unfiltered
  form and was corrected with it.
- **`energy_balance.active_balance_mode` could assert an operating mode for a
  snapshot that produced no verdict.** It re-read the state machine instead of
  reading the last sample as its own documentation claimed, so a partial snapshot
  — which `evaluate_balance` deliberately refuses to judge — was still given a
  mode label. It is now lifted from the last sample and is `null` when there is
  none.

### Added

- **Energy-balance failure attribution in diagnostics**, so a minority of
  failures on an otherwise healthy system can be diagnosed from recorded data
  instead of argument: `passed_samples_by_mode`, `failed_samples_by_mode`,
  `skipped_due_to_skew`, `skipped_due_to_stale_source`,
  `least_recently_reported_source_counts`, `worst_skew_seconds`,
  `worst_residual_w`, `worst_relative_error`, `worst_excess_sample` and
  `last_failed_sample`.

  Failures confined to *converting* modes point at the inverter's DC/AC boundary;
  failures confined to *low-power* modes point at a roughly constant offset
  between two instruments; failures spread across every mode point at a real
  configuration error. The three call for completely different action and were
  previously indistinguishable.
- Skipped samples now record **which** gate fired and **which entity** reported
  least recently. A high incoherent-skip rate previously said only that the
  sources disagreed about when they were describing, without naming the source
  holding the comparison back.
- `worst_excess_sample` retains the largest overshoot of an allowance rather than
  the largest residual, because a residual is only meaningful against the
  allowance it broke: 300 W is healthy at 10 kW and a fault at 300 W.

### Unchanged, and intentional

- **The energy-balance tolerance model is untouched.** All ten operating modes a
  real installation enters — grid to house, grid to house and battery, PV to
  house, PV to house and battery, PV and grid to house and battery, battery to
  house, battery to house and grid, PV export, battery export and near-zero
  crossings — pass with realistic residuals, so the observed failures are not the
  model being too strict. The allowance is deliberately near-absolute at low power
  (46 W at 200 W of load, 58 W at 600 W) and grows with conversion (~580 W when PV
  and a battery are both active), so a roughly constant boundary offset above
  about 60 W fails overnight and passes all afternoon. Widening it to absorb that
  would blind the check at every power level in order to explain one regime.
- **Energy balance still cannot reject a learning interval or a learned day.** It
  feeds diagnostics and the confidence score and nothing else; `_ingest()`,
  `record_interval()`, `DayRecord.is_learned` and `build_forecast()` reference it
  nowhere. A balance failure has no effect on day qualification.
- **`modelled_intervals` does not increase during the active day.** Only days
  strictly before the reference are model inputs, so the figure is fixed for the
  whole civil day. A behavioural slot needs two observations, so with two prior
  days the modelled set is their intersection — which is why a partial install
  evening plus one complete day yields exactly the count of that evening's slots.
- **`model_days: 1` alongside an available forecast is honest reporting, not a
  fault.** A partial install day pairs its slots with the following complete day,
  so a forecast can publish before a second day is fully learned.
- Baseline remains `max(measured - flexible, 0)`, and an interval with a
  configured but unreadable flexible load still has no valid baseline.

### Compatibility

No config-entry migration, no storage-schema change, no entity-ID or unique-ID
change. Storage schema stays at version 2 and the config-entry schema at
version 2. Learned history, retained intervals, learning-day count, confidence
state and every source selection are preserved across the update. The
energy-balance attribution counters are session-scoped and deliberately not
persisted, so they start empty after the update and after any restart.

## [1.0.0-beta.2] - 2026-08-18

A Phase-1 bugfix beta, from a defect found during live Home Assistant testing of
`1.0.0-beta.1`. No new functionality, no behaviour change to learning, storage or
the forecast model itself.

### Fixed

- **Diagnostics could report a house-load forecast that the entity had
  deliberately withheld.** On a live installation two days after install, the
  Today entity correctly read `unknown` with `model_days: 0` while diagnostics
  for the same refresh reported `today_total_kwh: 4.546`. The withholding was
  correct — a single learned day and two partial days cannot model a full day —
  but `DayForecast.remaining_kwh()` summed whatever intervals happened to blend
  without consulting the availability rule the entity uses. Same-day adaptation
  called it unconditionally, so an unpublishable baseline still produced a
  confident-looking day total for anything reading it directly.
- Forecast publication and diagnostics now apply one availability rule, so the
  two can no longer disagree about whether a forecast exists.
- Same-day adaptation is no longer reported as applied when there is no
  publishable baseline to adapt against.

### Added

- Diagnostics now explain **why** a forecast is unavailable, under
  `forecast.forecast_today` and `forecast.forecast_tomorrow`:
  `available`, `unavailable_reason`, `total_kwh`, `model_days`, `usable_days`,
  `modelled_intervals`, `interval_count`, `day_type`, `day_type_pooled` and
  `windows_used_days`. Reasons are `no_history`, `insufficient_model_days`,
  `insufficient_baseline_coverage` and `forecast_not_built`.
  This bug was only visible because two numbers disagreed; a withheld forecast
  is normally healthy, and `unknown` alone could not be told apart from a fault.
- `forecast.today_remaining_kwh` and `forecast.today_available`.

### Unchanged, and intentional

- **Expected House Load Tomorrow remains unavailable until enough usable
  historical model data exists.** This is deliberate and is not a defect. A
  behavioural slot needs at least two observations before any look-back window
  will use it, so a single prior day can never produce a forecast, and a
  whole-day figure is only published once most of the day can actually be
  modelled. The alternative is a fabricated prediction.
- `model_days` is deliberately distinct from the Learning Days sensor. Learning
  Days counts every day complete enough to learn from; `model_days` counts the
  days actually backing a *published* forecast, and is therefore 0 while a
  forecast is withheld.

### Compatibility

No configuration, storage-schema, entity-id or unique-id changes. Learned history
is preserved; no remove and re-add is required. HACS Custom Repository users can
update from `1.0.0-beta.1` normally.

## [1.0.0-beta.1] - 2026-08-17

**First public beta.** This is the Phase 1 foundation: the integration observes,
learns and forecasts household demand. It issues no commands to the battery and
makes no charge, discharge or trading decisions.

This is a **beta**, not a stable release. The learning and forecast model has not
yet completed enough real-world full-day validation to be called stable — see
[Known limitations](#known-limitations-1) below.

The learning foundation was rebuilt around measured household power; the advisory
battery/trading layer of 0.1.0 was removed and will be redesigned on top of this
foundation in a later phase.

### Added

- Time-weighted integration of an instantaneous house-load **power** sensor into
  quarter-hour intervals, replacing delta-sampling of a cumulative daily counter.
- Flexible-load separation. An optional EV charger power entity is integrated
  independently, and the learned demand curve is the **baseline**
  (`max(measured - flexible, 0)`) rather than raw measured load. Measured energy
  is always retained as ground truth.
- Daylight-saving-safe persistence. Intervals are keyed by chronological index
  from local midnight, so a civil day stores 92, 96 or 100 real intervals and
  the repeated fall-back hour is kept twice instead of overwriting itself.
- Multi-window forecast model (7/30/90/180/365 days) with weight
  renormalisation, weekday/weekend separation and damped same-day adaptation.
- Learning-confidence score derived from maturity, baseline coverage, recency,
  stability and optional energy balance.
- Configurable battery and grid **sign conventions**, normalised to one internal
  convention.
- Optional energy-balance sanity check, surfaced through diagnostics. Coherence
  gating skips samples whose sources are more than 90 s apart in time, and a
  warning requires three consecutive coherent failures, so asynchronous source
  updates no longer produce false alarms.
- Energy-balance tolerance derived from physics rather than a flat percentage:
  a fixed allowance for inverter auxiliary draw and rounding, plus a
  conversion-loss term scaling with DC-side power, plus a metering term scaling
  with AC-side power. Tighter than a flat 15 % above roughly 700 W, and correctly
  more forgiving in low-power battery conversion.
- Diagnostics record the operating mode, the full normalised flow breakdown, the
  allowed residual and which tolerance term dominated, so a reported imbalance
  can be diagnosed without reproducing it.
- Dutch translations alongside English.
- A test suite (511 tests) covering measurement, DST, flexible loads,
  persistence, config flows, translations, the energy-balance tolerance model and
  external-API isolation.
- Config-entry version 2 with a migration guard that refuses to load a 0.1.0
  entry rather than starting up with no usable sources.
- Continuous integration: Ruff lint and format checks, the full test suite,
  Home Assistant `hassfest` validation and HACS validation on every push and
  pull request.
- `docs/ARCHITECTURE.md`, a developer reference for the learning, persistence
  and energy-balance layers.
- Issue and pull-request templates, and an explicit `ruff.toml` so a Ruff
  upgrade cannot silently change which rules apply.

### Changed

- Minimum supported Home Assistant is now **2025.1.0**. The rewrite uses
  `entry.runtime_data`, generic `ConfigEntry` typing and coordinator
  `config_entry` support, none of which exist in 2024.1.
- Entity set reduced from 55 to 4.
- Storage moved from a single global key to one document per config entry, and
  the schema is now versioned at 2.

### Removed

- The advisory battery and trading layer: `recommendation`, `reserve_satisfied`,
  the trade engine, reserve model, PV forecast correction and safety-buy logic.
  These remain in Git history and are deferred to a later optimisation phase,
  which will be rebuilt on the corrected learning foundation.

### Fixed

- Interval identity no longer keyed on the wall-clock slot (`hour * 4 + minute //
  15`). On a daylight-saving fall-back day the repeated hour overwrote itself, so
  the stored day total no longer matched the sum of its intervals. Intervals are
  now keyed by chronological index and both occurrences of the repeated hour are
  retained.
- Elapsed-interval counting uses a monotonic chronological index. A wall-clock
  index moved backwards through the fall-back fold and re-counted energy that had
  already been consumed.
- A missing flexible-load (EV) reading invalidates the baseline for that interval
  instead of being read as "no charging". Assuming zero folded a charging session
  into the learned baseline, which is precisely what the split exists to prevent.
- An unavailable or non-numeric source reading normalises to "no data" rather
  than to zero, so an outage is recorded as missing coverage instead of teaching
  the model that the house consumed nothing.
- Energy-balance warnings no longer fire on asynchronous source updates. The
  previously reported `supply 9 W vs demand 1197 W` alarms were entirely a
  timing artefact of sources that do not share a clock.
- `hacs.json` declared Home Assistant 2024.1.0 while the code used APIs
  introduced well after it, so an install on that core would have failed
  immediately.

The following were found and fixed during the pre-release audit of this beta:

- **The forecast could publish a fabricated day.** Intervals never observed were
  filled with the mean of those that were, so on a fresh install where only the
  evening had been seen twice the evening rate was painted across all 96
  intervals — 38 kWh reported against a real 14 kWh, presented as available. A
  forecast is now published only once most of the day has genuinely been
  observed, and the few remaining holes are filled from the nearest observed
  interval rather than the whole-day mean.
- `model_days` counted days that were never learned, so it disagreed with the
  Learning Days sensor.
- Today's forecast sensor published `forecast_total_kwh: 0.0` as an attribute
  while its state was correctly `unknown`, so a template reading the attribute
  received a plausible-looking zero-kWh prediction.
- The options flow became permanently unsubmittable if Frank Quarter Prices was
  removed and re-added: the stored entry id was no longer a valid choice, so no
  setting could be changed and the only escape was deleting the entry and losing
  all learned history. It now aborts with an explanation.
- Entity pickers filtered on `device_class`, but validation deliberately accepts
  an entity on its unit. A template sensor with `unit_of_measurement: W` and no
  `device_class` — exactly the AlphaESS Modbus source this integration targets —
  was accepted by the validator yet invisible in the dropdown.
- Downloading diagnostics for an entry that was not loaded raised
  `AttributeError` and returned HTTP 500. That included the migration-error state
  this release's guard produces, which is precisely when diagnostics are needed.
- Removing a config entry orphaned its learning history in `.storage` forever.
  The document is now deleted with the entry, and the migration message no longer
  implies history carries over to a replacement entry.
- A stored timezone that no longer resolves raised out of the forecast builder on
  every refresh, leaving all four sensors permanently unavailable with no
  recovery path.
- A damaged stored day with a missing or zero interval count was reinterpreted as
  a short but fully covered day, inflating the learned-day count and the
  confidence score instead of being discarded. An implausibly large count is also
  rejected rather than allocated.
- A single forward clock excursion could delete the entire learning history,
  because retention pruned against whatever date was handed in.
- A negative reading from an inverted PV or house-load sensor produced a negative
  energy-balance allowance, reporting a snapshot whose identity closed exactly as
  a failure. Such a reading is now treated as unusable rather than as a quantity.
- An unrecognised sign-convention value selected the *inverse* of the shipped
  default rather than the default itself.
- A source flapping between a value and `unavailable` defeated the log throttle
  entirely, producing one warning per read indefinitely.
- Diagnostics reported a flexible-load source as available while simultaneously
  reporting its power as null and counting its intervals as invalid.

### Known limitations

- **This is a beta.** Real-world learning and forecast validation is still
  ongoing. The model has not yet been observed across enough complete days —
  including a daylight-saving transition and a full seasonal spread — to justify
  a stable release.
- **Forecast entities may read `unknown` for some time after installation, and
  this is correct.** The model needs enough valid learned history before it will
  predict anything, and it deliberately does not fabricate a value to avoid an
  empty state. Learning confidence rises slowly by design: about 30 days of
  history is roughly 63 % maturity.
- A day only counts as learned at 80 % *baseline* coverage. An EV sensor that
  reports `unavailable` while idle, rather than a numeric `0`, will invalidate the
  baseline for every idle interval and prevent days from being learned. Prefer a
  sensor that reports zero, or leave the EV field empty.
- One sustained, coherent energy-balance residual on the maintainer's own system
  remains unexplained (roughly 154 W on 740 W of supply). It is under
  investigation, is reported as a moderate measurement-boundary effect rather
  than a configuration error, and cannot affect learning — the balance check is a
  quality signal only and can never reject an interval. It does slightly reduce
  the reported confidence score.
- Solcast PV forecast data is validated and surfaced in diagnostics but does not
  yet influence the load model.
- A 0.1.0 config entry cannot be migrated automatically: the two configuration
  models share no keys. Remove the integration and add it again. The old
  `.storage` file (`alpha_ems_manager_learning`) is left untouched and can be
  deleted by hand.
- Learning history written during Phase-1 development under storage schema v1 is
  discarded on load with a warning; it cannot represent a fall-back day.
- Still prediction only. No write commands are issued to AlphaESS.

## [0.1.0] - 2026-06-14

### Added

- Initial scaffold of the Alpha EMS Manager integration.
- Config flow to select all source entities (house load, PV, Frank prices,
  battery), including optional east/west PV and battery SoC sensors.
- `DataUpdateCoordinator` that reads source sensors on a 1-minute interval.
- Self-learning household load profile per 15-minute interval, bucketed by
  season and weekday/weekend, using an exponential moving average.
- Persistent storage of the learned profile across restarts.
- Scaffold reserve calculation combining learned load and PV forecast.
- Sensors: predicted daily load, predicted remaining load, required reserve,
  PV forecast today/tomorrow, battery current energy, recommendation.
- Binary sensor: reserve satisfied.
- Diagnostics support.
- HACS compatibility.

### Notes

- AlphaESS write commands are intentionally **not** implemented in this release.

[Unreleased]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.39...HEAD
[1.0.0-beta.39]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.38...v1.0.0-beta.39
[1.0.0-beta.38]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.37...v1.0.0-beta.38
[1.0.0-beta.37]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.36...v1.0.0-beta.37
[1.0.0-beta.36]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.35...v1.0.0-beta.36
[1.0.0-beta.35]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.34...v1.0.0-beta.35
[1.0.0-beta.34]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.33...v1.0.0-beta.34
[1.0.0-beta.33]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.32...v1.0.0-beta.33
[1.0.0-beta.32]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.31...v1.0.0-beta.32
[1.0.0-beta.31]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.30...v1.0.0-beta.31
[1.0.0-beta.30]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.29...v1.0.0-beta.30
[1.0.0-beta.29]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.28...v1.0.0-beta.29
[1.0.0-beta.28]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.27...v1.0.0-beta.28
[1.0.0-beta.27]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.26...v1.0.0-beta.27
[1.0.0-beta.11]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.10...v1.0.0-beta.11
[1.0.0-beta.10]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.9...v1.0.0-beta.10
[1.0.0-beta.9]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.8...v1.0.0-beta.9
[1.0.0-beta.8]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.7...v1.0.0-beta.8
[1.0.0-beta.7]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.6...v1.0.0-beta.7
[1.0.0-beta.6]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.5...v1.0.0-beta.6
[1.0.0-beta.5]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.4...v1.0.0-beta.5
[1.0.0-beta.4]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.3...v1.0.0-beta.4
[1.0.0-beta.3]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.2...v1.0.0-beta.3
[1.0.0-beta.2]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.1...v1.0.0-beta.2
[1.0.0-beta.1]: https://github.com/Bennie-JC/ha-alpha-ems-manager/releases/tag/v1.0.0-beta.1
[0.1.0]: https://github.com/Bennie-JC/ha-alpha-ems-manager/releases/tag/v0.1.0
