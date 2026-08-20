# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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
