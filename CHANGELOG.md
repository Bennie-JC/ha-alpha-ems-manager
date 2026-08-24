# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.11...HEAD
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
