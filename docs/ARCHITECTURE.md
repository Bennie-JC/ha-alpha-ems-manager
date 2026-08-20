# Architecture

Developer reference for the Alpha EMS Manager integration. The
[README](../README.md) describes what the integration does for a user; this
document describes how it is put together and, where it matters, why.

Read this before changing the learning, persistence or energy-balance layers —
several sections record decisions that a reasonable-looking change would silently
undo.

## Scope

Phases 1, 2 and 3 are **observation only**. Phase 1 measures household
consumption, learns a baseline demand profile and forecasts it. Phase 2 records
each forecast and matches it against what actually happened. Phase 3 decides what
the battery *should* do and simulates the consequence. **None of them issues a
command to the battery, schedules anything, or calls a service.** Phase 3's
recommendation is published so it can be watched for weeks before Phase 4 is
allowed to act on it; nothing executes it.

Phase 2 adds no feedback loop. It *records* forecast error; nothing reads that
error back into the model. Adaptive correction belongs to a much later phase, and
keeping the boundary sharp is what makes the recorded evidence trustworthy: the
history is a measurement of the model, not a product of it.

The previous 0.1.0 release contained an advisory battery and trading layer —
`recommendation`, `reserve_satisfied`, a trade engine, a reserve model, a PV
forecast correction and safety-buy logic. All of it was removed. It remains in
Git history and will be redesigned on top of this learning foundation in a later
phase. Do not reintroduce any of it here.

## Development workflow

```bash
pip install -r requirements-test.txt
python -m pytest        # pytest.ini supplies everything
ruff check .
ruff format --check .
```

To run against a real Home Assistant, copy `custom_components/alpha_ems_manager/`
into `config/custom_components/` and restart.

Minimum supported Home Assistant is **2025.1.0**, driven by `entry.runtime_data`,
generic `ConfigEntry` typing and coordinator `config_entry` support. Keep
`hacs.json`, the README and this file in agreement.

## Module map

| File | Role |
|---|---|
| `const.py` | Every tunable constant and configuration key, each with the reasoning for its value. |
| `normalization.py` | Unit conversion and sign normalisation. The only place a source's convention is interpreted. |
| `quarter.py` | `QuarterAccumulator`: time-weighted integration of one power signal into quarter-hour results. Home-Assistant-free. |
| `storage.py` | `DayRecord` and `LearningStore`. Interval identity, DST-safe day shape, persistence, retention. |
| `forecast.py` | The multi-window statistical model and same-day adaptation. Pure. |
| `confidence.py` | The confidence score and its component breakdown. Pure. |
| `energy_balance.py` | Optional sanity check on the flow identity. Pure. |
| `forecast_history.py` | Phase 2 record model: immutable snapshots, day outcomes, fingerprinting, matching rules. Home-Assistant-free. |
| `history_store.py` | Partitioned, versioned persistence for forecast evidence. |
| `metrics.py` | All derived forecast-error statistics. Pure, and persists nothing. |
| `forecast_recorder.py` | Orchestration: issue, match, prune, read back. |
| `api.py` | The frozen read-only interface later phases consume. |
| `validation.py` | Entity validation used by the config and options flows. |
| `coordinator.py` | Runtime orchestration: listeners, timers, both accumulators, ingest, derived values. |
| `config_flow.py` | Five-step config flow and a single-page options flow. |
| `sensor.py` | The four sensors. |
| `diagnostics.py` | Everything that does not justify an entity. |
| `__init__.py` | Entry lifecycle, config-entry migration guard, missing-source guard. |

The pure modules carry the interesting logic deliberately: they can be tested
against synthetic timelines without a Home Assistant instance, which is why the
DST and forecast tests are fast and exhaustive.

### Phase-5 modules

| Module | Role |
|---|---|
| `pv_forecast.py` | the production model, the mapping, both fingerprints, provenance and the evidence pair. Pure |
| `solcast_source.py` | the read-only source boundary. Fetches values and decides nothing |

### Phase-3 modules

| Module | Role |
|---|---|
| `battery.py` | physical model, limits, and the single clamp. Pure |
| `simulation.py` | interval stepper and reduced trajectory. Pure |
| `policy.py` | objectives. Pure, and enforces nothing |
| `plan.py` | one refresh's decision, and the only Phase-3 consumer of `api` |

## Source selection

Two styles, chosen per integration:

- **Entity selectors** for AlphaESS, the grid meter and the EV charger. The
  AlphaESS Modbus package is YAML template sensors with no config entry and no
  device, so there is nothing to discover. Nothing is auto-bound.
- **Config-entry pickers** for Frank Quarter Prices (`frank_quarter_prices`) and
  Solcast (`solcast_solar`), which are real config-entry integrations.
  Referencing the entry survives entity renames.

There are **no hard-coded entity IDs** anywhere in the integration. Keep it that
way.

## The three load quantities

```
measured   = whatever the house-load source reports (EV included)
flexible   = whatever the optional EV source reports
baseline   = max(measured - flexible, 0)
```

`measured` is ground truth and is always stored. `baseline` is always derived,
never stored, so the relationship stays auditable and a future change of
definition does not invalidate history.

Baseline is *valid* only when `measured` exists and, if a flexible source is
configured, `flexible` exists too. A missing EV reading invalidates the baseline
for that interval rather than being read as "no charging" — assuming zero would
fold a charging session into the baseline, which is precisely what this exists to
prevent.

The forecast learns **baseline**. A future optimiser must not reserve battery
energy to cover a load it may itself schedule.

## Measurement

`QuarterAccumulator` integrates one power signal with left-handed integration:
the latest reading is held until the next arrives, which is the correct reading
of a change-driven sensor. Gaps longer than `MAX_SAMPLE_GAP_SECONDS` contribute
neither energy nor coverage. An interval is accepted at `MIN_QUARTER_COVERAGE`,
measured against the *full* interval so a partially observed one can never
qualify.

House load and EV get **separate accumulator instances** advanced by the same
`_sample()` call, so unsynchronised sources need no special handling and nothing
is ever approximated as `power × 0.25`.

Three triggers, all registered through `entry.async_on_unload`:

```
state change on house load / EV  ─┐
60 s safety sampler              ─┼─→ _sample() ─→ both accumulators ─→ _ingest()
:00/:15/:30/:45 + 5 s            ─┘                                        │
                                                                    async_request_refresh()
                                                                            │
                                                              _async_update_data()
                                                                            │
                                                          forecast + confidence → sensors
```

## Interval identity and DST

This is the part most likely to be broken by a well-meaning change.

A day stores its **real** number of intervals: 92 on a spring-forward day, 96
normally, 100 on a fall-back day, from `expected_quarters_for()`. Interval `i`
begins at `utc_midnight(day) + i × 15 min` — an absolute instant, so every real
quarter has a distinct ordered identity.

The **behavioural** wall-clock slot (0..95) is *derived* from that instant, never
used as the storage key. On a fall-back day chronological intervals 8–11 and
12–15 both map to behavioural slots 8–11, and both are kept; both contribute to
the statistics for those slots.

Rules to preserve:

- Never key persisted identity on `hour * 4 + minute // 15`. An earlier design
  did, and the repeated hour silently overwrote itself — the day total no longer
  matched the sum of stored intervals.
- Never add a `timedelta` to an aware local datetime to advance time. Python does
  wall-clock arithmetic there and skips or repeats an hour. Work in UTC.
- Never subtract two aware datetimes that share a `tzinfo` object and expect
  DST-correct elapsed time; CPython short-circuits to naive arithmetic. Convert
  to UTC first.
- Elapsed/remaining calculations use the chronological index from
  `_elapsed_intervals()`, which is monotonic. A wall-clock index moves backwards
  through the fold and re-counts consumed energy.
- Forecasts are generated for the target day's real length, not a fixed 96.

`tests/test_dst_persistence.py` exists to catch regressions in all of the above.

## Persistence

`Store` under `.storage/alpha_ems_manager.<entry_id>.learning`, **schema
version 2**.

```jsonc
{
  "days": {
    "2026-10-25": {
      "tz": "Europe/Amsterdam",   // recorded explicitly, never re-inferred
      "n": 100,                   // real interval count for this civil day
      "m": [0.25, null, ...],     // measured kWh, chronological, null = gap
      "e": [0.0, null, ...],      // flexible kWh (omitted when never configured)
      "x": [1, 1, ...]            // flexible-load expected flag (omitted with "e")
    }
  },
  "balance": {"ok": 0, "total": 0},
  "last_finalized": "2026-10-25T00:00:00+00:00"
}
```

Writes are debounced by `STORE_SAVE_DELAY` and flushed on unload and on Home
Assistant stop. Retention is `MAX_HISTORY_DAYS`, pruned when a new day is
created.

Two rules protect the history, and both exist because they were once broken:

- **Prune before inserting.** `prune()` clamps its reference to at most one day
  past the newest stored day, so a clock excursion cannot define "now". That
  clamp is inert if the new day has already joined the set being clamped
  against — which is what `get_or_create` used to do, so a single future-dated
  record deleted the entire retention window and the debounced save wrote the
  empty document to disk within the minute.
- **Never write after a failed read.** `async_load` degrades an unreadable
  document to an empty history so setup can continue; without a guard, that empty
  history is written back over the file on the next unload or shutdown, turning
  one transient I/O error into permanent loss. `corrupt` suspends all writes for
  the session and is reported as `storage.writes_suspended`.

A timezone change is handled by reloading the entry. Both accumulators capture
the zone once, at `async_start`, while the storage layer stamps each day with the
zone it was written in — and Home Assistant does not reload config entries when
its timezone changes, so the two halves of the write path otherwise ran on
different calendars until the next restart. Intervals are additionally indexed in
the record's own zone rather than the live one, and `record_interval` reports an
out-of-range index instead of dropping it silently.

`_LearningStoreBackend._async_migrate_func` discards any pre-v2 document with a
warning. There is no faithful mapping from the v1 wall-clock-slot format, and
reading it as chronological intervals would corrupt every DST day. Only this
integration's own document is affected.

## Config-entry versioning

`CONFIG_ENTRY_VERSION = 2`. The 0.1.0 model shares **no** keys with this one, so
`async_migrate_entry` returns `False` for version 1 and logs what to do. That is
deliberately louder than loading: before the guard existed, a legacy entry set up
cleanly, created four sensors, registered no listener and learned nothing
silently.

`async_setup_entry` additionally raises `ConfigEntryError` when no house-load
entity is configured, which covers any other route to a source-less entry.

## Forecasting

Five overlapping windows (`FORECAST_WINDOWS`) blended per behavioural slot with
`FORECAST_WINDOW_WEIGHTS`, renormalised over whichever windows have at least
`MIN_OBSERVATIONS_PER_WINDOW` observations. Weekday/weekend split engages once
there are `MIN_DAYS_FOR_DAY_TYPE` days of a type; below that all days are pooled.

Observations are bucketed once per forecast in `_collect_observations()`, so the
per-window lookups stay cheap over a year of history.

### Honesty rules — do not relax these

An `unknown` forecast is a correct answer. Two guards enforce that, and both were
added after the alternative shipped and produced a fabricated day.

1. **A forecast is published only when at least `MIN_DAY_COMPLETENESS` of the
   target day's intervals actually blended.** Below that, `source_days` is set to
   0 so `available` is false. Without this, a fresh install that had only ever
   seen the evening reported 38 kWh against a real 14 kWh.
2. **`_fill_unknown_intervals()` fills from the nearest observed interval, never
   from the whole-day mean.** The mean is actively misleading for the case this
   exists to handle: a flexible-load sensor that goes unavailable overnight
   invalidates the same early-morning slots every day, and a mean that includes
   the evening peak overstated the night by more than half while the day still
   counted as learned. Distance is linear, not circular, so a leading gap inherits
   the first known interval rather than a 23:45 peak.

`source_days` counts **learned** days only, so the published `model_days`
attribute agrees with the Learning Days sensor. Unlearned past days still
contribute the intervals they did measure — a valid interval is real data — they
just do not count as days the model was built from.

`tests/test_forecast_honesty.py` pins all of the above.

Same-day adaptation is damped (`TODAY_ADAPT_DAMPING`) and clamped
(`TODAY_ADAPT_RATIO_MIN/MAX`), and suppressed before
`TODAY_ADAPT_MIN_ELAPSED_SLOTS`.

## Coverage semantics

Four different populations answer four different questions, and conflating them
is how a healthy installation came to report 25 % coverage at 06:00 and recover
by itself at midnight.

| Reported as | Population | Denominator |
|---|---|---|
| `learning.*` | every retained day | intervals that have **elapsed** (`elapsed_quarters_for`) |
| `learning.completed_days.*` | finalised days only | their full civil length, 92/96/100 |
| `learning.current_day.*` | the day in progress | quarters that have **closed** |
| `confidence.*` | learned days only | their full civil length |

`elapsed_quarters_for` needs no branch for the three cases: a past day returns its
whole civil length, a future day returns zero, and the running day returns what
has actually closed. Daylight saving falls out of the same arithmetic, because the
count is absolute time since the day's UTC midnight clamped by
`expected_quarters_for`.

The running day is judged by nothing. It cannot be a learned day, cannot enter the
confidence score, and cannot be an input to its own forecast until midnight
finalises it — so measuring it against a full civil day reported the unlived
remainder of today as missing data.

## Rejection attribution

Every route to a rejected quarter ends in the same place — the interval failed to
reach `MIN_QUARTER_COVERAGE` — but the causes are unrelated, and a bare count
could not distinguish a normal restart from an entity that had been publishing
kWh instead of W since the day it was selected.

`describe_power_problem()` classifies a reading exactly as `normalize_power_w`
accepts it, and the coordinator adds the reasons only it can see. Counters are
keyed by a fixed set of literals, so the mapping cannot grow with runtime:

`source_entity_missing`, `state_unavailable`, `state_not_numeric`,
`unit_missing`, `unit_not_power`, `value_implausible`,
`interval_outside_stored_day`, `insufficient_sample_coverage`.

Logging is throttled per reason, so a new kind of fault is never rate-limited by
an older one. `insufficient_sample_coverage` logs at debug rather than warning:
it is the expected outcome of every restart, and a message that fires every time
teaches the user to ignore the channel.

## Confidence

```
confidence = 100 × maturity × quality
maturity   = 1 - exp(-learned_days / CONFIDENCE_DAYS_TAU)
quality    = weighted mean of coverage / recency / stability / balance
```

Coverage is **baseline** coverage, because baseline is what the forecast learns.
Measured coverage is reported separately for diagnostics so a gap can be
attributed to the right source. A component with no data drops out and the rest
renormalise. The multiplication is what stops a long-but-gappy history scoring
highly.

A day is *learned* at `MIN_DAY_COMPLETENESS` baseline coverage, so a day whose EV
sensor was down keeps its measured history but does not count.

The in-progress day is excluded everywhere, via `learned_days(before=today)`: a
day can only be judged once it can no longer gain intervals. Every consumer must
go through the value the coordinator publishes rather than recomputing it.
Diagnostics called `learned_days()` without `before` and so counted today from the
moment its baseline coverage crossed the bar — around 19:15 on a clean day — and a
download taken that evening reported one more learned day than the Learning Days
sensor showed. `coordinator.learned_day_dates()` had the same unfiltered form.
`tests/test_diagnostics.py` now asserts the two agree.

### Model history and the day rollover

Only days strictly before the reference are model inputs, so `modelled_intervals`
is **fixed for the whole civil day** and does not creep up as today fills in. A
behavioural slot needs `MIN_OBSERVATIONS_PER_WINDOW` observations, so with two
prior days the modelled set is their *intersection*.

That has a consequence worth knowing before reading a fresh installation's
numbers: a partial install day still pairs its slots with the following complete
day, so a forecast can publish on rather less than a second complete day — and in
that band it publishes with `model_days: 1`, because the newest day is not yet
learned. `model_days: 1` alongside an available forecast is therefore honest
reporting, not a fault. `tests/test_day_rollover.py` pins the whole transition,
including the exact 19-interval overlap the live installation reported.

## Energy balance

A quality signal only: `_sample_balance()` feeds `BalanceMonitor` and the
persisted `BalanceStats`, and nothing else. It can never reject a learning
interval, and `tests/test_balance_robustness.py` asserts that directly.

Three layers, in the order they apply:

1. **Availability** — a partial snapshot returns `None` and is counted as
   `unavailable_samples`. No verdict.
2. **Coherence** — `measure_coherence()` compares the participating sources'
   `last_reported` timestamps (falling back to `last_updated`, which does not
   advance when a value repeats and would make a steady sensor look stale).
   Too much skew, or too old, and the sample is *skipped*: excluded from the
   pass rate in both numerator and denominator.

   **One source is exempt, under one condition.** A PV source whose current
   value is *exactly* zero contributes no timestamp at all. The age gate asks
   whether a source's value may have changed since it was last published; for a
   term that is exactly zero, the answer cannot change the verdict, because the
   contribution to the identity is exactly zero however old the reading is.

   The residual risk is self-announcing rather than hidden. If real generation
   `P` started while the sensor stayed silent at zero, substituting zero makes
   `supply` short by exactly `P`, so the sample *fails* by `P`. The exemption can
   never manufacture a pass, which is the only direction that matters.

   The scope is deliberately narrow, and narrow in two separate ways:

   - **Exactly zero, with no band around it.** A stale `5 W` injects five
     fabricated watts of supply, and any tolerance drawn around zero would be a
     threshold with no physical quantity behind it. A stale *positive* reading is
     worse still: it contributes a real term that can cancel a genuine fault in
     either direction, which is precisely what this check exists to catch.
   - **PV only, not "any zero flow".** The arithmetic argument is source-
     agnostic; the *need* is not. Night is a predictable eight to twelve hours in
     which generation is genuinely zero and a change-driven source has no reason
     to republish, so PV alone accumulates a sustained skip rate — 185 of 189
     skips on the reference installation. Battery idle, a null grid reading and a
     zero house load are transient states, not a nightly regime, so relaxing them
     would buy no measurable coverage while widening the surface on which a
     genuinely dead source could pass unnoticed. House load in particular is the
     learning target; its silence is information.

   An *unreadable* source is never a zero: `unavailable`, `unknown` or a bad unit
   yields no verdict at all, so the exemption is not even consulted. The
   exemption is self-terminating — a sensor that starts generating publishes a
   new value by definition, so at sunrise it is simultaneously fresh and non-zero
   — and every exemption is counted per entity in
   `energy_balance.quiescent_zero_source_counts`, because a relaxation that
   leaves no trace is one nobody can audit.

   `tests/test_stale_zero_pv.py` pins both halves: that the exemption fires where
   it should, and that it cannot be stretched to cover anything else.
3. **Debounce** — `BALANCE_SUSTAINED_FAILURES` consecutive coherent failures are
   required before a warning. This, not the coherence gate, is what suppresses
   transients: a load step can fail one sample while all four timestamps look
   fresh, because the lag is inside the sensors rather than in their publishing.

`BalanceMonitor` is session state and deliberately **not** persisted, so a
restart never inherits a failure run that may already be resolved.

### The tolerance model

The verdict is an **absolute allowance in watts**, built from the three physical
reasons the identity cannot close exactly. It is not a percentage.

```
dc_power = pv + battery_charge + battery_discharge     # gross, not netted
ac_power = max(supply, demand)

allowed  = BALANCE_BASE_ALLOWANCE_W                    # 40 W  inverter aux + rounding
         + BALANCE_CONVERSION_LOSS_FRACTION * dc_power # 5 %   DC<->AC conversion
         + BALANCE_METERING_TOLERANCE       * ac_power # 3 %   two instruments

pass     = abs(residual) <= allowed
```

Why not a flat relative tolerance, which is what this replaced: PV and battery
are **DC-side** quantities on a hybrid inverter while house load and grid are
**AC-side** ones, so the residual scales with *conversion*, not with total power.
A flat 15 % was simultaneously too tight in a low-power conversion mode — a
battery delivering 240 W AC from 300 W DC is a healthy 20 % "error" — and far too
loose at high power, where 15 % of 10 kW hid a 1.5 kW mis-selected entity.
`test_the_new_model_catches_a_fault_the_old_rule_let_through` pins that gain.

`dc_power` sums PV and battery rather than netting them because PV charging a
battery is converted twice, by the MPPT stage and the battery DC-DC stage.
Charging and discharging are mutually exclusive, so the battery is not
double-counted.

Below the old 250 W floor the two models agree to within a couple of watts, which
is deliberate: overnight behaviour was known-good.

`relative_error` is still computed and logged, but only as a readable figure. It
is **not** the gate.

Do not widen these without re-checking `test_balance_tolerance_model.py`, which
derives each threshold from inverter efficiency and meter accuracy and asserts
both directions — that realistic residuals pass at 100 W, 1 kW and 10 kW, and
that sign inversions, missing sources and implausible efficiencies still fail.

### Warning wording

`gross_fault_suspected` splits the two messages. A residual both
`BALANCE_GROSS_FAULT_MULTIPLE` times its allowance **and** over
`BALANCE_GROSS_FAULT_FLOOR_W` tells the user to re-check entities and sign
conventions. A merely moderate one attributes itself to measurement boundaries
and says learning is unaffected — because telling someone to re-check a correct
entity is worse than saying nothing.

The two wordings carry **separate throttle keys**. Sharing one let the reassuring
message rate-limit the escalated one for a full hour: a moderate residual warned,
a passing sample re-armed `BalanceMonitor.should_warn()`, and the gross fault that
followed inside the throttle window was discarded — permanently, because only a
passing coherent sample re-arms the one-shot flag and a real fault never produces
one. `_ThrottledLogger.warning()` therefore returns whether it emitted, and
`balance.last_warning` is stamped only when it did; otherwise diagnostics reported
a warning timestamp for a log line that was never written.

### Failure attribution

A pass rate cannot say *why* a minority of samples failed, and the three
candidate explanations call for completely different action. `BalanceMonitor`
therefore records, per session:

| Field | What it distinguishes |
|---|---|
| `passed_samples_by_mode` / `failed_samples_by_mode` | Failures confined to converting modes (DC/AC boundary) versus low-power modes (a constant inter-instrument offset) versus all modes (a real configuration error). |
| `skipped_due_to_skew` / `skipped_due_to_stale_source` | Sources describing different instants — normal for Modbus registers on separate poll intervals — versus a source that has stopped publishing. |
| `least_recently_reported_source_counts` | *Which* entity holds the comparison back, so a high skip rate is actionable. |
| `worst_excess_sample` | The largest overshoot of an allowance, not the largest residual: 300 W is healthy at 10 kW and a fault at 300 W. |
| `worst_residual_w`, `worst_relative_error`, `worst_skew_seconds` | Peak magnitudes, including the evidence for or against the 90 s skew gate itself. |

Mode labels come from `infer_balance_mode()`, so the key space is bounded by the
subsets of three sources and three sinks and cannot grow with runtime. These are
counters only — no per-sample history is retained, and nothing reaches an entity
attribute.

Why this matters more than a wider tolerance: the allowance collapses to a near
absolute floor whenever nothing is converting — 46 W at 200 W of load, 58 W at
600 W, 76 W at 1.2 kW — while reaching ~580 W when PV and a battery are both
active. A roughly *constant* boundary offset above about 60 W therefore fails
overnight and passes all afternoon, producing exactly the shape of a high pass
rate punctuated by short failure runs. Widening the tolerance to absorb it would
blind the check at every power level in order to explain one regime.
`tests/test_balance_attribution.py` pins that arithmetic, including the ~3.5 kW
crossover for a 150 W offset with nothing converting.

### Known open item

The live system produced a sustained, coherent `supply 740 W vs demand 586 W`.
It fails under **every** flow decomposition that could have produced it, and
explaining it would need a ~10-13 % conversion allowance, so it was not tuned
away. The grid source is a separate P1 meter while house load, PV and battery all
come from the AlphaESS inverter, which makes a boundary mismatch the leading
hypothesis — but the AlphaESS Modbus semantics are not documented here and the
original warning recorded only the two totals. `BalanceSample.as_dict()` records
the full flow breakdown, the mode and the allowance, and the attribution counters
above record which modes and which source, so the next occurrence is diagnosable
from diagnostics alone.

**Second occurrence, 2026-08-20, and it behaved exactly as designed.** Mode
`pv+grid->house+battery`, `supply 661 W`, `demand 506 W`, residual `+155 W`
against an allowance of `95 W`. The allowance reconstructs exactly from the
model — `40 + 0.03 x 661 + 0.05 x 703 = 95 W` for `703 W` of gross DC flow — and
the reported `23 %` is `155 / max(661, 506, 250)`, so the arithmetic and the
attribution are both sound. The **sign** is the informative part: `supply -
demand` is *positive*, meaning measured input exceeds accounted output, which is
what an unmeasured conversion loss and an inverter's own auxiliary draw look
like. A negative residual in that mode would be the suspicious one, because it
would mean energy arriving from nowhere.

`155 W` at `703 W` DC is more loss than a conversion stage alone explains, and
that is consistent with the leading hypothesis rather than against it: two
instruments on different electrical boundaries differ by a roughly *constant*
offset, which is a negligible fraction of a busy afternoon and a large fraction
of a quiet one. The check then cleared by itself — 374 of 378 eligible samples
passed, a 98.9 % rate with `consecutive_failures` back to zero — which is the
debounce doing its job rather than a fault going away.

No threshold was changed. Widening the base allowance to cover a `155 W` offset
would blind the check at every power level in order to explain one regime, and it
would hide the wiring or sign error the check exists to catch.

**Nothing in Phase 2 depends on this result.** The balance score reaches exactly
one thing — the confidence component — and confidence is recorded *on* a snapshot
as provenance, excluded from the fingerprint, and read by no scoring, matching,
retention or storage decision. A balance failure therefore cannot invalidate,
alter or suppress a forecast comparison.
`tests/test_forecast_scoring_proof.py` pins that twice: once by scoring an
identical day with a 0 % pass rate and comparing every figure, and once
statically, by asserting that no scoring module imports the balance check at all.

## Phase 2: the forecast evidence layer

Phase 1 produced a forecast and threw it away the moment a newer one replaced
it. Phase 2 keeps it, together with what the house went on to do, so the question
*was it right, and why not* has an answer later.

### The property everything rests on

`build_forecast()` is a pure function of `(records, reference, target, tz)`. The
in-progress day is excluded from its own forecast — `collect_forecast_inputs()`
keeps only `0 < age <= horizon` — and the only writer that touches a *past* day
mid-run is the midnight close of the previous day's last quarter.

**So between one midnight and the next, the forecast cannot change.** Ninety-six
refreshes rebuild the same arrays from an unchanged input set.

`tests/test_forecast_issuance.py::test_the_forecast_is_constant_within_a_civil_day`
pins this. If it ever fails, the issuance policy needs redesigning rather than
patching: it would mean the model is moving in a way nothing is recording.

### Issuance policy

Change-triggered, deduplicated by a content fingerprint. A snapshot is written
when — and only when — the forecast's content and provenance differ from the last
one kept for that target day.

```
fingerprint = sha256(target day, tz, look-ahead, interval count,
                     available + reason, predicted array, fill mask,
                     model_days, usable_days, day type, pooled, windows,
                     modelled/filled counts,
                     model version, model parameter hash, baseline definition)
```

Deliberately **excluded**: the issuance instant, the confidence percentage and
the energy-balance score. Balance is resampled every sixty seconds, so including
it would write a snapshot per refresh and defeat the entire policy. Those fields
are recorded *on* the snapshot; they simply do not decide whether one is written.

Deliberately **included**, and the one exception to "content only": the
look-ahead. A prediction made a day ahead and one made on the day are different
observations even when they carry identical numbers, because the question they
answer differs. Excluding it collapsed the two into one record whenever the model
happened not to move — common on a settled household — and left the day-of side
of any look-ahead comparison systematically empty of exactly the days the model
found easy. It costs nothing in churn: look-ahead is constant for a whole civil
day.

Under the Phase-1 model this yields exactly **two records per target day**: H-1
issued while the target was "tomorrow", and H-0 issued on the day itself, after
the model gained a learned day.

Consequences that fall out for free: a reload, a restart, the first refresh after
setup and duplicate coordinator callbacks all recompute the same fingerprint and
write nothing. Duplicate protection is structural, not defensive.

`FORECAST_MAX_SNAPSHOTS_PER_TARGET` caps a runaway future input at 32 per day and
**logs when it bites** — a silent cap reads as full coverage when it is not.

### What is recorded, and what is refused

A withheld forecast **is** recorded, with its `unavailable_reason` and no array.
Without that, an installation whose first month published nothing would later
look like a model that was never wrong.

The snapshot is never the *adapted* Today figure. Same-day adaptation blends
measured energy into the remainder of the day, so that total is a hybrid of
prediction and reality and is not a like-for-like prediction of anything. The
unadapted baseline forecast is what gets stored and scored.

Arrays are copied, not referenced. `DayForecast` is a mutable dataclass rebuilt
every refresh; holding a reference would make "immutable snapshot" a comment
rather than a property.

### Matching

At the first refresh of a new civil day, every unmatched past day is matched.
The ordering already works: `_handle_quarter_boundary` samples **synchronously**,
closing 23:45 into yesterday's record, before scheduling the refresh.

The actual is `DayRecord.baseline_at(index)` — `max(measured - flexible, 0)`,
`None` when either half is untrustworthy. That is deliberately the same quantity
the model predicts; scoring a baseline forecast against raw measured load would
charge the model for energy an EV drew.

Per-interval status codes are a fixed, bounded key space:

| Code | Meaning |
|---|---|
| `0` | valid baseline measurement |
| `1` | no usable measured reading |
| `2` | measured present, flexible-load reading unusable |
| `3` | interval had not elapsed (clock stepped backwards) |

`1` and `2` are kept apart because they call for different action — check the
house-load sensor, or check the charger — and collapsing them would throw away
the only clue.

A day is **kept but never scored** when its two sides are not comparable:
`no_record`, `shape_mismatch`, `timezone_changed`, `definition_changed`. The
prediction and the measurement are both true facts; only their comparability is
void. Index-matching two different day shapes would line an 18:00 prediction up
against a 17:00 measurement and look entirely plausible doing it.

`definition_changed` is judged from the evidence rather than the configuration:
the record's own `ev_expected` flags say what was expected of each interval, so a
flexible load switched on at noon is caught even though the configuration looks
consistent by the time the day is finalised.

**Only observed intervals have an opinion, and that distinction is load-bearing.**
`ev_expected` is written by `record_interval()`, which the coordinator calls once
per *accepted* quarter — so an interval that never reached coverage, or that fell
inside a restart, keeps the `False` its list was padded with. Reading those
padded entries as "no flexible load was configured then" is what made a single
missing quarter on a day *with* a charger look like the definition changing at
that quarter: `any(expected) and not all(expected)` was true and the whole day
was excluded from every statistic, permanently. That was the beta.5 defect that
left 19 August 2026 unscored on the live system after the upgrade restart.

A gap is a gap. The status codes above describe it exactly, and it says nothing
about what "baseline" meant. So the judgement is made over the intervals with a
measurement, and a day with no observation at all makes no claim either way — it
has no comparable interval regardless, and adding a reason that was never
established is the reason a maintainer would go and investigate.

| Situation | Verdict |
|---|---|
| Charger configured all day, quarters missing to a restart | comparable — scored on the observed intervals |
| Charger selected at noon | `definition_changed` |
| Charger removed between issuance and the day being measured | `definition_changed` |
| No charger at all, quarters missing | comparable |
| Nothing observed at all | no flag; `unmatched` for want of a comparison |
| Entry reloaded, options unchanged | comparable |
| Model version or parameter hash changed | comparable — recorded on the snapshot, never a matching flag |

### Restating a match

`FORECAST_MATCHER_VERSION` versions the *matching* rules, separately from
`FORECAST_MODEL_VERSION`, which versions the numbers a forecast contains. Every
summary row records the generation that produced it; a row without the field is
generation 1, which is exactly what every beta.5 row is.

A matching rule that turns out to be wrong is not only wrong going forward. The
days already matched carry its verdict and nothing revisits them, because
finalisation only looks at days that were never matched at all. On a dataset that
accrues one irreplaceable day at a time that leaves a permanent scar for every
rule ever corrected, so a day whose row predates the current generation is
**re-derived**. Snapshots are never touched — they are the evidence; only the
match, a derived reading of them, is restated.

It is safe only because of how narrowly it is bounded. Re-derivation needs:

- retained snapshots — past the raw-retention horizon there is no prediction left
  to re-derive against, and a re-derivation there would replace a real comparison
  with an empty one;
- a **retained learning record** — without it `build_outcome()` would honestly
  return `no_record`, and writing that over a sound match would destroy the
  evidence the sweep exists to preserve;
- the same suspension rule and the same per-refresh budget as finalisation.

Once every row carries the current generation the sweep finds nothing and costs
nothing: it reads the always-loaded index and the learning history already in
memory. `tests/test_beta6_audit_regressions.py` pins each bound, including that
the sweep runs once and does not rewrite the document afterwards.

### Lifecycle

Derived, never stored. The only state committed to disk is `finalized_at`.

```
pending      -> has a prediction, and the day has not finished
unresolved   -> the day has finished and it is still unmatched
validated    -> matched, comparable, and something survives to compare
unmatched    -> matched, but nothing comparable came out of it
```

A stored state field would be a second source of truth, and the first time the
two disagreed it would be the stored one that got believed — a record labelled
`validated` whose actual is null. Both callers go through `lifecycle_state()`, so
a diagnostics count and a scored day cannot tell different stories.

### The suspension rule — do not remove this

**Nothing is matched while `LearningStore.corrupt` is true.**

That store degrades an unreadable document to an empty history so setup can
continue, which is right for availability — but then `baseline_at` returns `None`
for every interval of every day. Matching against it would write immutable
records stating that every measurement was missing, for days whose data is very
probably intact on disk.

This is the beta.4 write-after-failed-read defect one layer up, and worse: the
learning document survives that failure, while these records are final by design.

Nothing is lost by waiting. Matching is a pure recomputation from persisted data,
so the days stay unmatched and resolve on the next refresh after a successful
read. `tests/test_forecast_matching.py` pins both halves.

### Persistence

Separate documents from the learning history, on their own schema version, so
neither can damage the other — and the learning document is already discarded
outright by its own migration guard.

```
alpha_ems_manager.<entry_id>.forecast_index        # always loaded, small
alpha_ems_manager.<entry_id>.forecast.<YYYY-MM>    # one per month of target days
```

The index carries one lightweight row per target day: interval count, the
fingerprints already kept, `finalized_at`, and the reduced summary facts. Because
the fingerprints live there, **the hot path performs no disk access at all** —
a refresh reproducing the previous forecast is two string comparisons.
`test_an_unchanged_refresh_touches_no_storage_at_all` enforces that by making
`Store.async_load` raise.

Month partitioning exists because `Store` rewrites a whole document on every
save: a year-long file would be about a megabyte through the executor on each
write, and one corrupt byte would cost the lot. Writes are atomic — forecast
evidence cannot be regenerated the way a lost quarter can.

Both learning-store safety rules are reproduced: **never write after a failed
read** (per document, so a corrupt month costs one month), and **clamp the prune
reference** to at most one day past the newest stored target.

The clamp only works if pruning runs **before** issuance, and in beta.5 it did
not. Issuing first put the bogus future target inside the very set the clamp
measures against, so one refresh under a host whose clock was years ahead — a Pi
without a real-time clock, before NTP corrects it — dropped every retained
prediction array in the history. This is the beta.4 `get_or_create()` ordering
defect one store along, and the store-level test that was meant to cover it
called `async_prune()` directly and so never exercised the real path.

Order per refresh, and it matters in this order:

```
prune (day change only) -> issue -> finalise -> restate -> schedule save
```

The clamp costs one day of lag when the newest stored target is far behind the
reference: that first pass declines to expire anything, and the next day-change
prunes correctly against recorded time. That is the intended behaviour — a
reference far ahead of the recorded history is indistinguishable from a wrong
clock until real days start arriving.

### Retention

| Class | Kept | Why |
|---|---|---|
| Raw per-quarter arrays | 365 days | Exactly `MAX_HISTORY_DAYS`, so a record can always be correlated with the learning history that produced it. Past that the inputs are gone and the arrays could no longer explain *why* a forecast was wrong. |
| Reduced daily summaries | 3650 days | About 200 bytes. Sufficient statistics — summed absolute error, summed actual, compared count — so MAE, bias and WAPE can be rebuilt for any window. |

An **unfinalised** day is never pruned: dropping its prediction would leave a
record nothing can ever answer.

Roughly 3.5 kB/day, so about 1.3 MB at the 365-day steady state.

### Metrics

Nothing derived is persisted. Predictions and actuals are the facts; every
statistic is recomputed. A stored `mae` field would freeze one definition into
the history and would eventually disagree with the data beside it.

Sign convention, fixed once: `error = predicted - actual`. **Positive means the
model over-predicted.**

Published: MAE, mean signed error, and WAPE = `sum(|error|) / sum(actual)` over
the window. RMSE and the breakdowns are diagnostics only.

**Never computed:** a per-interval percentage error. Quarter-hour baseline load is
routinely a few hundredths of a kilowatt-hour at four in the morning, so MAPE
divides by something arbitrarily close to zero and one interval swamps any
average it enters. It is not stabilised with a floor — a floor is a threshold
with no physical quantity behind it — it is simply not computed. Likewise there
is no `100 - error` accuracy figure: it is unbounded below and invites comparison
with unrelated systems.

A day-level percentage *is* safe, because the denominator is a whole day of
demand, and it returns `None` when that day measured nothing.

`FORECAST_MIN_INTERVALS_FOR_METRIC` (192 — roughly two full days) is a
**minimum-sample rule on the rate, and only on the rate**. Below it, MAE, bias
and WAPE are withheld, because the figure would be whichever handful of intervals
happened to resolve and a fresh installation's noise would be read as forecast
quality. The sample size and the two summed energies are *facts about the window*
rather than judgements of the model, so they are reported throughout. beta.5
dropped them to their `0.0` dataclass defaults instead, which had the rolling
sensor advertising `predicted_kwh: 0.0` and `actual_kwh: 0.0` beside an
`intervals_compared` of ninety-six — a claim that the house consumed nothing,
published by the one sensor whose whole purpose is to refuse that substitution.
`None` now means no comparison, at every layer.

`Forecast Error Yesterday` has no such threshold: one validated prior day is one
real comparison, and it is published as soon as there is one.

Diagnostics reports the rolling statistics **ungated** — a maintainer wants the
figure whatever its sample size — so it also carries what the entities actually
publish, under `quality.published`, together with the threshold that separates
the two. Without that a download showed a WAPE of 25 % next to an entity reading
`unknown`, with nothing in the payload saying which was wrong. Neither was.

Every loader refuses non-finite numbers. Nothing this integration writes can
produce a `NaN`, but a hand-edited or externally damaged document can and
Python's `json` accepts the literals — and `NaN` compares false against every
threshold, so the completeness guards would wave it straight through into a
sensor state.

Day-level comparison uses the **intersection** of intervals valid in the actual
and predicted in the snapshot. Comparing a whole-day prediction against a partly
observed day reports the unmeasured hours as a forecast that came in high, which
is a systematic bias manufactured out of a sensor outage.

### Context, and keeping it from becoming a dumping ground

Provenance travels in namespaced, versioned blocks declared by a
`ContextProvider`: a key, a version and an exact field set. Anything undeclared
raises. Phase 2 registers exactly one provider, `load_model`. A later phase adds
its own key without touching it, and unknown blocks read from disk are
**preserved verbatim** so a downgrade is not destructive.

`model_params_hash()` fingerprints the constants that shape a forecast. Without
it, one future tuning change would silently split the historical error series in
two and a later phase would read the discontinuity as the household changing its
habits.

### The Phase-3 boundary

`api.py` is the only module a later phase may import. `forecast_history`,
`history_store`, `forecast_recorder` and `metrics` are implementation — if Phase 3
reads a partition dictionary directly, the storage layout can never change again
without breaking battery logic. `tests/test_api_boundary.py` enforces this
statically over the real source files, the same way
`tests/test_no_external_polling.py` enforces the network boundary.

Phase 3 respects it: `plan.py` is the only Phase-3 module that imports `api`, and
none of the four imports a private module. It also does **not** publish its plan
here. `api.py` reports what was predicted and how that prediction performed;
adding a decision to it would mean deleting the grep guard that keeps it
descriptive, and Phase 3 needs no public surface because nothing may consume it
yet. **When Phase 4 needs one it should get its own module.**

`load_forecast_from` was added to this surface because `current_forecast` reads
the *last published* coordinator data, which is one refresh stale for anything
running inside the refresh that produces it. It converts and copies; it decides
nothing.


## Phase 3: battery decision and simulation

Phase 3 answers one question: *given what is legitimately known now, what should
the battery do, what constrains it, and what would happen next.* It controls
nothing, and that is enforced rather than intended —
`tests/test_phase_three_boundaries.py` reads the real sources and asserts no
Phase-3 module imports a network client or calls a service.

Four pure modules, none of which imports Home Assistant, so every rule can be
tested against synthetic state exactly as `forecast_history` and `metrics` are:

| Module | Responsibility |
|---|---|
| `battery.py` | the physical model, and **the one place a limit is enforced** |
| `simulation.py` | walking intervals forward; decides nothing |
| `policy.py` | what the battery *should* do; enforces nothing |
| `plan.py` | one refresh's decision and the evidence behind it |

### The electrical boundary — the expensive decision

`energy_kwh`, `soc_percent` and `capacity_kwh` are **DC-side**: the state of the
pack. Charge and discharge power, the grid residual and every household energy
are **AC-side**.

**Efficiency is applied exactly once, when energy crosses that boundary, and
never in state-of-charge arithmetic.**

This is the most expensive thing here to get wrong. It is baked into a value the
user typed *and* into what gets stored, it is the frame every other quantity is
defined against, and a self-consistent model with the boundary flipped passes
every round-trip test that can be written while carrying a few per cent of
systematic bias — comfortably inside the noise of an integer-percent
state-of-charge sensor. So it is in the field labels (“Usable battery capacity
(DC)”, “Maximum charge power (AC)”), in diagnostics, and in one bit-exact test:

> 10 kWh of AC energy at 90 % round trip raises stored DC energy by exactly
> `9.486832980505138` kWh, and discharging all of it returns exactly `9.0` kWh AC.

One configured round-trip percentage is split symmetrically,
`eta_c = eta_d = sqrt(eta_rt)` — the only split that reproduces the measured round
trip while staying agnostic between directions, and no user knows the halves
separately. The resulting 5.13 % per crossing agrees with
`BALANCE_CONVERSION_LOSS_FRACTION` (0.05) to within 0.13 %, which is an
independent check on the default. The model nonetheless stores
`charge_efficiency` and `discharge_efficiency` as **two** fields, because
photovoltaic charging never crosses the AC boundary at all — Phase 5 needs
asymmetry, and Phase 9 may want to learn them.

#### Two known optimistic biases — do not present these figures as exact

Both push the same way, so `Usable Battery Energy` is an **upper bound**:

1. A constant efficiency flatters a real inverter at low power. The balance
   tolerance model above documents ~20 % conversion loss in that regime, where
   this assumes 5.13 %.
2. Inverter auxiliary draw is not modelled at all — roughly 0.0125 kWh per
   interval, about the size of the modelled loss on an overnight discharge.

Neither is modelled, because both would mean inventing a third and fourth
unverifiable hardware property. They are documented, reported in diagnostics, and
are the first Phase-5/9 candidates. A 20 % floor dwarfs both in practice.

### Two floors, and why they are not one

| Concept | Phase 3 | Enforced by | May be crossed? |
|---|---|---|---|
| `configured_min_soc_percent` | the user's setting | **the clamp** | never |
| `effective_min_soc_percent` | equal to it | the **policy** | Phase 8 only, never below configured |

Numerically identical today, so the user-visible promise holds exactly. Kept apart
because Phase 7 will raise the effective reserve dynamically and Phase 8 must be
able to say “a price spike justifies dipping into the reserve, but never below the
floor the user set”. Merging two names later is free; splitting a persisted one is
not.

`BatteryReserve` is built only through a factory, so the `max()` that protects the
user's floor lives inside `static_reserve` rather than at a call site — Phase 7
adds `dynamic_reserve` beside it and cannot forget. The clamp reads only the
configured floor; the policy reads only the effective one; both halves are
asserted structurally.

### A request carries a mode, not a sign

Two defects in the first draft of this model made that non-negotiable, and both
were reproduced numerically before the design was accepted:

* **A negative requested power created energy.** `min(-1.0, max_discharge)`
  returns `-1.0`, the non-negativity guards sat on the available energy rather
  than the request, and −0.25 kWh AC became **+0.2635 kWh DC** — an effective
  efficiency of 1.054. A negative is exactly what arrives if a caller passes a
  raw battery-power sensor.
* **Charging and discharging in one interval destroyed energy invisibly.** Two
  independently firing rules asking 4 kW each leave the grid residual *exactly*
  equal to the load — identical to doing nothing — while stored energy falls by
  `1/eta − eta` = 0.10541 kWh. Over ninety-six intervals that is **10.1 kWh, an
  entire pack**, with a perfectly balanced grid trace. Phase 1 *assumed* this
  away; Phase 3 enforces it.

`BatteryRequest` therefore carries a mode and a non-negative magnitude, making
both unrepresentable rather than checked. Signed power exists only where
`Planned Battery Power` is published — and that sign is the plan's own, unrelated
to the configured `battery_power_sign`, which describes the user's sensor and is
resolved away long before.

### The single clamp

`battery.apply_request` is the only thing that reduces a request. No policy,
simulator, entity or coordinator may re-implement a limit: a second copy is a
second thing to keep in step, and the first time the two disagreed it would be
the copy that got believed.

* the **power** limit is applied in AC terms — the side the nameplate and the
  grid residual are denominated in;
* the **energy** limit is applied in DC terms — the state of charge is the pack's
  physical state and the available window is a window in it;
* the conversion back to AC happens *after* the energy clamp, so the two
  conversions are exact inverses and an allowed power fed back reproduces itself.

Clamping the AC energy instead would be arithmetically equivalent — measured
across 200 000 magnitudes the two differ by at most 9e-16 kWh and neither ever
exceeds the available energy. `_clamp_energy` is a **backstop**, measured to be
one: neutralised and swept over 7840 combinations with 200 repeated applications
each, it never fired. It is kept because it is two comparisons and it turns an
invariant that happens to hold into one that cannot stop holding.

`tests/test_phase_three_boundaries.py` enforces the single clamp structurally, by
looking for the limit names inside a comparison or a `min`/`max` call rather than
anywhere in the file — reporting a limit is not enforcing one.

### Interval duration is derived, never passed

Every quarter-hour is fifteen minutes. Daylight saving changes how many a civil
day *contains* — 92, 96, 100 — never how long one lasts. `INTERVAL_HOURS` is
derived from `QUARTER_MINUTES` and there is no parameter, because accepting one
invites `1.0`, `900` or, worst, `0.91666` “for the short DST hour”.

That is also why the trajectory starts at the **next** whole interval: every
interval it walks is a whole one, so the partial interval in progress never needs
a different duration. The *recommendation* is separate — it is for the interval
now in progress and needs no trajectory.

The horizon runs to the end of tomorrow, because Phase 2 already publishes
tomorrow's forecast. That costs nothing and exercises the multi-day path Phase 10
needs from the first release.

### PV blindness — a battery-only counterfactual

There is **no photovoltaic term**. Predicting production is Phase 5, and inventing
one here would be the fabrication this project exists to avoid. Two consequences
have to be stated wherever the figures appear:

* for a household with solar, **simulated grid import substantially exceeds
  reality** — the real array is covering load the model cannot see;
* a whole-day state-of-charge projection is wrong on any sunny afternoon.

Which is why **`Projected Battery SoC` is diagnostics-only**, carries an explicit
PV-blind note, and is not an entity. A visibly wrong entity costs more trust than
it buys. Phase 5 makes it publishable.

Read as a conditional — “given the predicted baseline load and no other
generation, where does the battery end up and when does the floor bite” — this is
not a defect but the definition, and it is exactly what Phase 7 needs to size a
reserve.

### The grid residual is unsigned

```
net             = load_ac + charge_ac − discharge_ac     # local only
grid_import_kwh = max(0, net)
grid_export_kwh = max(0, −net)
```

Shaped like `split_grid_power`. A signed field would reintroduce the reasoning
`PowerFlows` was built to end, and grid sign is the one thing this project has a
*tested* opinion about — it is a user-facing option precisely because it is the
field's most common error. Import and export are also priced differently, so any
cost layer must split them anyway.

No grid limit is modelled. Adding one later makes the clamp a **fixed-point
problem**, because a grid clamp constrains the battery request which changes the
residual.

### Policy, and why nothing charges

An objective is a pure callable of `(state, demand)` with an identity and a
version. Two ship: `HoldPolicy` (the reference trajectory the what-if measures
against, and what Phase 8 will price) and `ReserveGuardPolicy`.

**No Phase-3 policy asks to charge.** Every reason to would need information this
phase does not have — surplus production is Phase 5, a cheap half-hour Phase 6, a
storm Phase 7, an arbitrage spread Phase 8 — and the inverter already absorbs
photovoltaic surplus by itself. The charge path exists, is clamped and is
simulated so those phases have somewhere to land and so what-if works, and a test
asserts over the real `SHIPPED_POLICIES` that none of them emits one.

`ReserveGuardPolicy` is **not an optimisation**, and describing it as one would be
a lie: with no forecast of production and no prices, “reduce grid import”
collapses to “discharge to cover load”, which the inverter already does. What it
adds is the reserve boundary made explicit and computable — where the floor bites,
how much is genuinely available above it, and a decision path that provably cannot
cross it. `policy_version` travels on every decision so a later, genuinely
optimising policy is never pooled with this one.

`ForecastUncertainty.interval_margin_kwh` is deliberately **not** consumed: widening
a plan by measured error is reserve sizing, which is Phase 7. It is reported in
diagnostics so Phase 7 has it to hand.

### Deciding, and declining to decide

Two failures are kept apart:

* a **missing hardware fact** — no state of charge, capacity, power limit, or an
  impossible efficiency — means there is nothing to reason with:
  `ACTION_NO_DECISION`, and the recommendation entity reads `unknown`;
* a **missing forecast** means the battery is known and the load is not, so
  holding is a real answer: `ACTION_HOLD` with the reason
  `forecast_unavailable`, and the trajectory is withheld.

`Usable Battery Energy` survives the second case, which is why it was chosen over
a projected state of charge: it needs no forecast, so a young installation still
gets the number the minimum-SoC setting controls.

`NO_DECISION` is made safe by an invariant rather than by hope — it always carries
zero allowed energy, so it is behaviourally identical to a hold and semantically
distinct. That invariant was found to be **untested** by deliberately breaking it,
which is why the test exists.

### Configuration

Five keys, in a second Options page behind a menu. Sources and hardware figures
are edited on different occasions, and appending five numbers to a form that
already had thirteen would have buried them. Keys stay **flat**: sections would
deliver nested values, which the effective-configuration rule, the
unknown-key-preservation rule and the cleared-optional rule all read flat.

| Key | Default | Boundary |
|---|---|---|
| `battery_capacity_kwh` | **none** | DC |
| `battery_min_soc_percent` | 20 | — |
| `battery_max_charge_kw` | **none** | AC |
| `battery_max_discharge_kw` | **none** | AC |
| `battery_round_trip_efficiency_percent` | 90 | AC→AC |

Capacity and the two power limits have **no default**, because nothing can derive
them — a percentage sensor says nothing about how many kilowatt-hours a percent is
worth, and a power limit cannot be inferred from a capacity without assuming a
C-rate. Absent means Phase 3 declines and names the missing field. Required in the
config flow, so a new installation is complete; optional in Options, so an
installation upgrading from beta.6 can fill them in when it has the figures.

`max_soc_percent` stays an internal constant at 100: nothing in Phase 3 charges,
so a user-facing ceiling would be an untested field. The charge clamp is written
and tested against it so Phase 5 can promote it without touching the clamp.

Validation adds one rule, and it is a real one rather than a narrowing of the
range: `min_soc < max_soc`, so a floor leaves some usable energy. **0 % is legal**
— it means no EMS reserve, and the inverter's own floor still protects the cells.
The efficiency range starts at 50 % to catch `0.90` typed where `90` belongs.

**No config-entry version bump.** Additive keys with defaults supplied at read
time need no migration.

### Measured state of charge

One additive optional array on `DayRecord`, `s`, following `e`/`x` exactly:
sampled at each interval boundary, omitted from the document when empty, and read
as “no samples” on every earlier document. `STORAGE_MINOR_VERSION` 2.1 → 2.2; the
major version does not move, so nothing migrates.

Sampled rather than integrated, because a state of charge is a *level*: it does
not pass through `QuarterAccumulator`. When several quarters close together after
a restart, **only the one that just ended takes the sample** — where the battery
was two hours ago is genuinely unknown, and writing the current reading across a
backlog would be inventing history.

It is recorded for one reason: a plan is a pure function of the stored forecast,
the stored configuration and the state of charge at the time, so any plan can be
*recomputed* — but where the battery actually was cannot be recovered from
anything. Every day without it is a day the physical model can never be checked
against reality. That is also why Phase 3 persists **no plan documents** and adds
no storage layer of its own.

**The invariant, and it has its own file.** Adding, removing or corrupting
state-of-charge samples must not move `completeness`, `is_learned`, `baseline_at`,
the forecast, the confidence score, Learning Days, or any Phase-2 figure. It is
additive evidence for a later phase, never a learning input.
`tests/test_soc_persistence.py` holds that down, including a day whose battery
reported happily all day while the house-load sensor was down — which must still
not count as learned.

### Entities

Three, taking the integration to nine.

| Entity | State | Classes |
|---|---|---|
| **Battery Recommendation** | `hold` / `charge` / `discharge`; `unknown` for no decision | `device_class: enum`, no state class |
| **Planned Battery Power** | signed kW, positive = charge, **interval average** | kW, measurement, **no** device class |
| **Usable Battery Energy** | kWh above the reserve, an **upper bound** | kWh, `energy_storage`, measurement |

`Planned Battery Power` is an *average*, not an inverter setpoint: in the last
partial interval before the floor a real device delivers full power for part of it
and nothing after. Naming it for what it is avoids Phase 4 reading it as a
setpoint and under-delivering. No device class, for the same reason
`Forecast Error Yesterday` has none — a signed quantity must not be offered to the
Energy dashboard.

Attributes are capped at eight flat values with no mappings, against a closed
allow-list. Everything else — the reduced trajectory, the per-band split, the
binding-constraint tally, the what-if comparison, the projection — is
diagnostics-only. A ninety-six-interval array would breach the sixteen-entry
ceiling every diagnostics list is held to, and would be written to the recorder on
every state change.

### Failure isolation

Phase 3 is additive, so a fault in it costs only Phase 3. `_build_battery_plan`
is wrapped exactly as `_async_record_forecast_evidence` is: an exception degrades
three entities to `unknown`, names itself in diagnostics, and leaves learning,
both forecasts and both forecast-error sensors untouched. Tested in both
directions — a corrupt forecast history does not take the battery down either.

## Phase 4: control

Phase 4 builds the whole path from a decision to an inverter command, and then
**cannot walk it**. Every stage is real: the intent, the safety gate, the vendor
mapping, the ordered command list, the authorization. The last step is
unreachable, by a single build-time constant.

That is not caution for its own sake. Two things are unresolved, and either alone
would be enough (see *Ownership*, below and *Known open items*). It is also worth
saying plainly what the layer would achieve today if it could act: the shipped
policy asks for the discharge that covers the interval's forecast load, which is
what a self-consumption inverter already does by itself — and does better, because
it tracks load continuously while a fixed-power dispatch cannot. The pipeline is
built now so it can be watched for months before anything depends on it, and so
that the phase which finally has a reason to act — a price signal — inherits a
proven translation layer rather than writing one under pressure.

    Phase 3 decision → ControlIntent → SafetyGate → plan_commands()
                                    → ExecutionAuthorization → (nothing)

### Modules

| Module | Pure? | Holds |
|---|---|---|
| `control.py` | yes | `ControlIntent`, `translate()` — the projection from a decision |
| `safety.py` | yes | `ControlContext`, `evaluate()`, `authorize()` |
| `alphaess_device.py` | yes | the entire vendor mapping: helper ids, quantisation, ordered command list |
| `alphaess_adapter.py` | no | reads the state machine; the only module that could call a service |
| `soc_coherence.py` | yes | instrumentation comparing state of charge against battery power |

The split between `alphaess_device` and `alphaess_adapter` is what makes the
mapping testable: everything that decides *which helper, which number, which
order* is pure, and the impure part only fetches values. Shadow mode and the real
path run identical code up to the final call.

### Eligibility is not permission

Two questions that look alike and are not:

* `evaluate()` asks **would this be safe**. It never reads the control mode, so it
  returns the same verdict in shadow as in active.
* `authorize()` asks **may this be sent**. It knows nothing about hazards beyond
  whether the gate passed.

An earlier draft merged them, with `mode_not_active` as the first gate condition.
Shadow then stopped at that condition and reported it — telling the user nothing
about whether the command would have been safe, which is the only question shadow
exists to answer. Splitting them cannot weaken anything, because `authorize()`
requires `verdict.safe` before considering anything else, and
`tests/test_control_pipeline.py` asserts that over the full cross-product of the
condition table, both modes and both enable states.

The gate **never scales a command.** A request that cannot be sent safely is
refused whole, with one precise reason. There is deliberately no magnitude on
`SafetyVerdict` at all, so nothing downstream could mistake it for a smaller
command. A gate that trimmed a request to fit would have made a decision, and
deciding is Phase 3's job.

### Ownership — why real execution is still unreachable

The control surface has exactly one arming path per direction, driven entirely by
helper *values*. A dispatch armed from a dashboard and one armed by a service call
leave byte-identical helper states, register values, timer and read-backs.
**Nothing records a writer.**

So matching power, cutoff, duration or mode is not evidence of ownership. It is
worse than no evidence: the person most likely to have armed exactly the figures
Alpha EMS would have sent is the one watching the shadow recommendation, so a
parameter-match test would be most confident precisely when it was most likely to
be wrong. A call context does not help either — it cannot separate this
integration from any other automation, and a restart discards it.

The consequence is not confined to stopping a dispatch. *Continuing* one needs the
same proof, so until ownership can be established **no command spanning more than a
single interval is expressible**: Alpha EMS would arm at one refresh and inhibit on
its own dispatch at the next.

Therefore, and asserted rather than intended:

* `owned` is a constant `False`, and no module derives it — `ast`-checked.
* Any active dispatch inhibits, with no reference to its parameters.
* **There is no stop path.** A stop whose authorization cannot be established is
  worse than none, because the next reader inherits an open safety question
  dressed as working code.

### What `off` promises

`off` means **this integration attempts no control**. It does not mean an inverter
reverts. In this release the distinction cannot arise — nothing here can start a
dispatch, so there is never one of ours to revert — and the select's own
description says so rather than calling it an emergency stop. A later release that
can execute has to choose which of the two it promises, and it cannot promise the
second until ownership is solved.

### Energy to power and duration

The decision layer's actionable primitive is `allowed_energy_ac_kwh`, and its
`average_power_kw` carries a warning against reading it as a setpoint. Both are
respected, and the mapping is still legitimate:

* the device cannot be told an energy — it takes a power and a duration, so *some*
  mapping is unavoidable;
* the window a command acts over is the **planning cadence**, not the duration:
  each refresh supersedes the last, so the duration never expires in normal
  operation;
* over a fixed window there is exactly **one** constant power delivering a given
  energy. One admissible value means no choice, and no choice means no decision.

What the warning does forbid is commanding full power and letting the device's own
cutoff stop it — that would deliver the right energy by accident. So the cutoff is
a backstop, and `energy_limit_bound` is carried from `decision.constraints` so a
read-back that stopped at the cutoff can be told from one that stopped because the
clamp bound.

**Every quantisation resolves downwards**, giving one provable promise:

> `device_power_kw(e, h) * h <= e` for every input, with equality exactly when the
> energy is a whole number of power steps.

Swept over five thousand energies in one watt-hour steps. The cost is bounded: at
most one 0.1 kW step over one interval. The cutoff is raised one percent instead,
because the device's register *truncates* percent to bits and would otherwise stop
a fraction below the floor the user set — swept over every integer floor.

The verification loop inside `device_power_kw` is a **measured backstop that does
not fire**: zero walk-backs across that sweep, because the only interval this
integration uses is a quarter hour, `0.25` exactly, and multiplying by a power of
two is lossless. It is kept so the promise does not quietly depend on that.

### Export safety

A forced discharge sets the **battery** rate, so whatever the house cannot absorb
leaves through the meter — and the dispatch path does not honour the inverter's
configured feed-in limit. That limit is read and reported; it is never written,
because it is a flash-backed grid-safety setting.

So export is prevented in software: a discharge above the live house load, less a
configurable margin, is refused whole. Not trimmed to fit. `Max Feed to Grid`
appears in diagnostics so a reader can see the limit the dispatch path ignores.

### Charge is a battery rate, not a grid rate

The control surface offers both, and they are not interchangeable:

| Actuator | The slider means | So |
|---|---|---|
| Force Discharging | battery discharge rate | grid export = value − house load |
| Force Charging | battery charge rate | grid import = value + house load |
| Force Export | grid feed-in rate | battery discharge = value + house load |
| Force Import | grid import rate | battery charge = value − house load |

A battery decision maps only to the first two, so the number commanded is the
number the decision layer computed, in the units it computed it in. Mapping onto
one of the grid-rate actuators would be wrong by the size of the house load. A
structural test asserts neither appears anywhere in the package.

### Hold means two opposite things

Phase 3's `ACTION_HOLD` means *do not move the battery*. The control surface's
Hold flag means *keep forcing it after the cutoff is reached*. Letting those two
words meet would be a genuine hazard, so the second is only ever spelled
`device_hold_flag`, and it is always left off — which gives the device one more
automatic route back to normal operation when the battery stops moving.

The same monitor is why there is a minimum commandable power. It reads a battery
inside a ±50 W band for ten seconds as finished and tears the dispatch down, and a
dispatch tracks the commanded power rather than matching it — so the minimum is
two helper steps, four times that band, and anything smaller is refused rather
than rounded up.

### Flash memory

The dispatch registers are not flash-backed; the schedules, their persistent
cutoffs, the feed-in limit, the PV capacity and the grid-safety settings are. Only
the dispatch helpers are ever written, and a structural test reads the real
sources to confirm the others are not so much as *named* — including the two that
differ from the ones Phase 4 does use by a single word
(`discharging_cutoff_soc` against `force_discharging_cutoff_soc`).

### Fail-safe, and the recovery paradox that does not arise

Recovery needs no write, for three independent reasons: the duration expires by
itself; the control surface resets its own dispatch on Home Assistant start; and
it resets again when the inverter connection drops. Alpha EMS deliberately keeps
no copy of that mechanism — a second copy of a safety mechanism is a second thing
to keep in step — and instead **refuses to consider a command when it is absent or
switched off**. At setup it observes and reports; it never writes.

### The energy-balance residual is not a control gate

An earlier design gated control on `gross_fault_suspected`. Live data disproved
it. Where the house-load figure is derived from the inverter's own grid register
while the balance check reads a separate meter, substituting one into the other
cancels every term:

    residual  ≡  meter_grid − inverter_grid   (+ a PV filter lag)

The battery power cancels identically. The state of charge never appears. So the
residual is a two-grid-meter comparison, and its magnitude is not evidence about
either reading a command depends on. Two live samples make it concrete, and
`tests/test_balance_is_not_a_control_gate.py` reproduces both from their real flow
values:

* **+1394 W** (2026-08-20, `grid->house+battery`, allowance 112.9 W, 12.35×,
  labelled a gross fault). Reconstructs exactly as `2139 − 745`: a load drawing
  through the meter that the inverter's register does not see.
* **−10149 W** (2026-08-19, allowance 913.3 W, during a 10.18 kW charge ramp).
  The same difference, from latency between two sources that both timestamp
  promptly — which the coherence gate structurally cannot catch.

Neither is a broken sensor; both would have blocked control. **No tolerance was
widened and no threshold tuned** — the allowance still produces exactly the figures
it always did, both samples are still labelled gross, and they still warn. What
changed is only that control no longer consults them.

What was added instead is evidence: the *sign* of each failure (which every
existing statistic discarded by taking an absolute value first), the mean signed
residual, failures and accumulated overshoot per AC-power band, and a windowed
failure count that can see the alternating fault the consecutive counter cannot.
A fixed difference between two instruments shrinks against the allowance as power
rises; a mis-selected or mis-signed source grows with it. That is the feature that
could eventually tell them apart. All of it is diagnostics-only.

### What could gate control instead

`soc_coherence.py` asks the one question that constrains the two readings a battery
command actually depends on: over a closed interval, did the stored energy move the
way the measured power said it would? A stuck, mis-scaled or sign-inverted sensor
fails it; a disagreement between two grid meters cannot, because neither is
involved.

It is **instrumentation only**, and the honest reason is that its resolution is a
property of the installation rather than something to assume — so it measures the
smallest movement it actually sees and reports that. Promoting it to a gate is a
question for a later phase with months of that evidence in hand. Two limits are
worth recording now: the battery power is sampled at the boundary rather than
integrated, because no integral exists; and below the sensor's own resolution the
comparison is *inconclusive*, which is not the same as passing.

### Entities

Two. `select.alpha_ems_control_mode` is the control a user sets, and it is an
entity rather than a configuration field because reaching for it should not mean
opening a dialog. `sensor.alpha_ems_control_state` says what the pipeline did:
`inhibited` (the gate refused), `eligible` (it did not, and only the barrier
stopped a command), `idle` (the gate passed and there was nothing to send), or
`off`.

`inhibited` and `eligible` being distinct is the whole value of shadow mode.
Everything else — the capability report, the read-back snapshot, the intent, the
quantised command, the ordered command list, the event trail — is in eight flat
attributes and in diagnostics. A gate with twenty-five ways to refuse must not
become twenty-five rows on a dashboard.

There is deliberately no "last control action" entity. Its state would be a
timestamp, its history *is* the thing it reports, and the recorder already stores
every state change of the control state with its attributes.

### Failure isolation

A third additive layer, isolated the way the two before it are: its own throttle
key, its own `try`/`except`, its own degraded return. A control fault costs the two
control entities and nothing else, and a Phase-3 fault does not take the control
layer down with it. Both directions are tested.


## Phase 5: the PV forecast

Alpha EMS was blind to the sun. It read the PV entity for the energy-balance check
and nothing else, which had a visible consequence rather than a theoretical one:
on a sunny afternoon the recommendation was to discharge the battery to cover a
load the panels were already covering, and the export gate agreed to it.

### Modules

| Module | Pure? | Role |
|---|---|---|
| `pv_forecast.py` | yes | the model, the thirty-to-fifteen-minute mapping, both fingerprints, provenance, the snapshot/outcome evidence pair and the scoring |
| `solcast_source.py` | no | capability discovery, site discovery, the two read-only calls, response parsing |

### Energy is the primitive, and the unit is read once

The source publishes **average power in kilowatts** per period. Conversion happens
once, at the boundary, and everything downstream is kilowatt-hours per
chronological quarter-hour — the same unit and the same index as
`LoadForecast.intervals`. That identity *is* the compatibility contract later
phases depend on: any consumer that handles one handles the other with no
alignment code.

Reading that figure as interval energy is the single most plausible mistake
available, and on a thirty-minute source it doubles every number. It has its own
mutation test, as does reading the offset-aware timestamp as UTC — which moves the
whole day, two hours on this installation, from index 48 to index 56.

### Piecewise-constant, and the period is measured

A period covering two quarters gives each quarter the same average power, so the
two sum to exactly the period's energy. A curve between periods would invent
intra-period shape the source never published, and would not conserve energy.

The period length is measured from consecutive timestamps. Every row this project
has seen was thirty minutes, which is exactly why assuming it would be untestable:
a resolution change would silently halve or double every stored series.

Two subtleties that cost real defects while being written:

* **A row covers its own period and never more than the gap to the next row.**
  Rows cannot overlap. Without the second half of that rule, a hole in the series
  made the measured gap sixty minutes and every row was then stretched across an
  hour — fabricating generation for precisely the intervals the source had
  declined to describe.
* **Two rows an hour apart cannot be told from a half-hourly series with one row
  missing.** There is no way to know from the response, so the measured period is
  *reported* rather than guessed at. Reporting it is the whole reason it is
  measured.

DST needs no special case, because mapping is by instant: the repeated autumn hour
yields two distinct chronological indices sharing one wall-clock slot, and the
spring gap simply has no rows.

### Missing is missing

`None` means no forecast for that interval, and it is never zero. Zero is a
forecast of no generation — true after dark and a fault at noon — and collapsing
the two makes the whole evidence layer meaningless.

### Site membership is declared, never inferred

A Solcast account can hold sites that have nothing to do with the AlphaESS system
a config entry manages. Consuming the aggregate unconditionally folds those into
the plan, and no amount of provenance recovers a number that was already wrong
when it was summed. So Phase 5 adds exactly one configuration key,
`selected_solcast_site_ids`, and asks exactly one question: which sites belong
here.

The user is **never** asked to classify a site as AC- or DC-coupled, hybrid-side
or grid-inverter-side. Solcast partitions a roof by *orientation* and AlphaESS
partitions it by *electrical coupling*, so equal site counts prove nothing — and
that correspondence is not reliably knowable to a user. It is recorded as
`electrical_correspondence: unknown`, and a structural test proves no
configuration key and no translated string asks about it.

Three states are kept distinct because they are three different facts:

| Stored | Meaning |
|---|---|
| nothing | resolve to every discovered site and **write that down once** |
| a set | use it exactly; a site the source no longer offers stays in it, reported as missing |
| an empty set | a named unavailability. Falling back to everything would overrule a decision |

Persisting the default is the load-bearing part. Resolving "all of them" afresh
every refresh would mean a site added to Solcast next year silently joining this
installation's plan, which is the failure the option exists to prevent.

Two fingerprints, and **neither contains the display name**: `selected_sites_identity`
over the identifier set, and `selected_sites_model` over
`(resource_id, capacity, capacity_dc, azimuth, tilt, loss_factor)` plus the
source's own exclusions. A rename is the same roof; a re-tilt is not. `loss_factor`
is in the model key because it scales every figure the source returns — an earlier
draft omitted it, so a site rescaled from 0.9 to 0.85 would have looked identical.

### Aggregate and subset

All sites selected uses the source's own aggregate in one request. A strict subset
queries each declared site and sums them per interval, with P10, P50 and P90 summed
independently — none derived from another. Neither path costs API allowance, which
is what makes the subset path usable at all on an account with ten calls a day.

A declared site missing an interval never contributes a zero. The sum of what
reported is kept, tagged `partial_sites` with the contributing count, and excluded
from scoring. It is a known *under*-estimate — the benign direction, because
understated production raises net demand while export protection comes from the
meter.

**Percentile sums are not calibrated percentiles.** Adding each site's
tenth-percentile outcome assumes every site has its bad day simultaneously, which
bounds the aggregate *more* conservatively than the true joint tenth percentile.
Safe to compute, but a reserve calculation that treated it as a calibrated band
would size against a scenario far rarer than one day in ten. Hence
`percentile_aggregation`, which is `comonotonic_sum` for our sums and
`source_aggregate` where the source did its own and we do not know its convention.

### The read-only boundary

Two actions, `query_forecast_data` and `diagnostic`. Both response-only, both
served from the source integration's cache, neither consuming allowance. The
service-caller guard was widened from one module to two — deliberately, and with
three companions that together say more than the single-caller rule did:

1. Every call site names its domain and action **statically**, resolved through
   the constants. This is the mirror image of the Phase-4 adapter, whose single
   call site passes variables from a planned command; between them neither module
   can reach an arbitrary domain, and they fail closed in opposite directions.
2. **No function takes a domain or an action as an argument**, which is what stops
   a helpful `_call(domain, service, data)` appearing later and becoming the
   escape hatch.
3. Every mutating Solcast action is named once in `const` and proven to appear
   nowhere else.

API keys are dropped at the boundary: only named fields are read out of the
response at all, so "no key material is exposed" is a property of the code rather
than a promise. A test asserts it against a response that carries one.

### The plan sees net demand

Production is netted against load in AC energy **before** anything is converted,
and floored — so at most one of net demand and surplus is non-zero and the
single-direction-per-interval invariant is preserved structurally. Netting after
conversion destroys energy invisibly, for the same reason `BatteryRequest` refuses
a signed power.

The seam is one property. `IntervalDemand.power_kw` derives from net demand, and
`ReserveGuardPolicy` already asks for the discharge that covers the demand it is
shown — so when the sun covers the house it asks for nothing, entirely by its
existing rule. **No policy gained an objective**, and with no forecast the value is
the raw baseline exactly as before.

### Surplus absorption is ambient, conditional, and cannot become a command

The inverter storing surplus is environment, not intent. Three properties keep it
that way:

* it applies **only** where the policy asked for nothing, so no interval carries
  both a requested direction and an ambient one;
* it goes through `battery.apply_request`, so the power limit, the headroom and
  the one-way efficiency apply once, where they are implemented;
* `ControlIntent` derives from the *policy's* action, and a structural test
  asserts the control layer cannot even name a trajectory.

**It is not unconditional physics, and the approved design was wrong about that.**
The vendor control surface says so in its own notes: with **Excess Export** on, PV
below the inverter's AC limit is directed to house load and feed-in and the battery
is charged with *zero*. Peak Shaving arms its own dispatch, and the dispatch
vocabulary contains modes that forbid charging. What does *not* gate it is the
charging/discharging settings helper, whose four options cover grid charging and
timed discharge only — which is why baseline self-consumption is real in the
default configuration.

So it is predicated on observable state, and the distinction that matters is
between a helper that is **absent** and one that is **unreadable**:

| Observed | Absorption |
|---|---|
| a feature is on, or a dispatch is running | not modelled |
| a feature exists and cannot be read | not modelled — it could be suppressing it invisibly |
| the features do not exist here | **modelled** — nothing can be suppressing them |

Reading absence as ignorance would leave every installation without the vendor
package permanently pessimistic about its own battery, which is wrong rather than
cautious. When absorption is not modelled the surplus becomes simulated export,
which projects a *lower* state of charge and never claims stored energy the
inverter is sending to the grid.

Neither feature boolean is in `REQUIRED_ENTITIES`, so neither appears in
`missing` or `unavailable` — which originally made an unreadable Excess Export
indistinguishable from one switched off, the unsafe direction. `DeviceCapability`
now carries both raw states the way it already carried the failsafe state.

This reads the inverter's state in **every** control mode, including `off`.
Reading is not controlling: `off` still means no writes and no attempted control,
and the off-mode report says so explicitly rather than leaving the old claim to
become untrue.

### The export gate is measured, not reconstructed

Beta.8 compared a proposed discharge against house load alone, which is
under-protective whenever production already covers that load. Three live
shadow-mode samples show it — at 15:33, 2071 W of house load against 3132 W of PV,
so the site was exporting a kilowatt and the absorbing capacity was the 22 W of
import recorded, while the gate read 2071 W and passed.

    capacity = max(0, grid_import - grid_export + battery_discharge)

A forced discharge first displaces import and only spills onto the grid once
import reaches zero, so that expression is the bound — derived, not tuned.
Subtracting PV from house load would also have caught all three, but the meter
needs no PV term at all: no answer to the mixed DC/AC boundary question, no
exposure to the vendor's low-pass filter on the PV signal, and no daylight rule
for a sensor that legitimately reads zero all night. It also bounds loads no
house-load sensor sees — about 1.4 kW on this installation.

Two conditions were **added** rather than replaced: an unreadable meter and a
stale one both refuse. So the gate is strictly tighter than before, 25 conditions
to 27, and it still refuses whole commands and still never scales one.

### Evidence, and the correction that is not computed

Snapshots and outcomes live in the **existing** forecast-history partitions under
PV-namespaced keys (`pvs`, `pvo`, and `pvfp` in the index row). A third
partitioned store would have duplicated seven hundred lines of partitioning,
atomic writes, write ordering, corruption suspension and the month sweep — and
Phase 3 rejected exactly that reasoning for plan storage. The counter-argument,
different failure blast radii, is weaker here: load and PV evidence for the same
day are analysed together, and the load snapshots already share a partition with
their own outcomes.

Issuance is change-triggered by content fingerprint, which folds in provenance
identity — so a forecast carrying the same numbers after the declared set changed
is still a different issuance. The source updates a handful of times a day at
best, so this bounds growth naturally rather than by a tuned cap.

Scoring **classifies and computes nothing**. Eight per-interval codes rather than
one residual, because a figure that folds "the forecast was wrong", "the sensor
was down", "it was night", "no forecast was ever obtained" and "one declared site
went quiet" into one number is not evidence of anything. A PV-blind interval is
never scored: comparing a forecast that was never obtained against a real reading
manufactures error out of an outage, which is the mistake the load-side scoring
already refuses for a partly observed day.

Metrics are derived from the two stored sides on demand — a stored statistic is a
second source of truth, and the first time it disagreed it is the stored one that
would be believed. The **signed** error is kept beside the absolute one, because
the sign is the whole diagnostic: a structural conversion difference or clipping
biases one way and forecast noise does not.

No correction exists, and it is unreachable rather than merely absent.
`score_pv_day` never receives a forecast and the snapshot it does receive is
frozen; `build_forecast` cannot name an outcome, a metric or an actual series.

### Boundaries recorded rather than solved

| Fact | Status |
|---|---|
| the measured figure sums DC strings and an AC meter | declared, `actual_pv_boundary` |
| the source does not state its own boundary | declared, `forecast_boundary: unspecified` |
| which site feeds which subsystem | `unknown`, never guessed and never asked |
| clipping | flagged where the AC limit is readable, suppressed where it is not, corrected never |
| the source's own dampening and actuals blending | recorded; either turning on means "raw" is no longer raw |

On a big day the forecast exceeds the actual **by design**, because an inverter
cannot pass more than its limit however bright it is. A later phase must not learn
a correction for physics.

### Daylight is advisory

`get_astral_event_date` rather than the `sun` entity, which exposes only the
*next* events and so cannot describe tomorrow. It never modifies a value and sits
on no safety path. What it buys is the best available detector for a whole class of
timezone and offset bugs — generation forecast in the dark — caught on an
installation rather than only in a test. When the window cannot be determined the
detector is suppressed rather than a window invented.

### The three disclaimer branches

The PV-blind note was pinned by a test on purpose, and the honest response to the
limitation changing is to change the words rather than delete them: a projection
published without its limitation costs more trust than it buys.

| Condition | What it says |
|---|---|
| no forecast | PV-blind, as before, verbatim |
| forecast and absorption modelled | PV-aware, with the covered-interval count |
| forecast and absorption suppressed | PV-aware, and a **lower bound** |

`Battery Recommendation` gains one attribute, `pv_aware`, taking it from eight to
nine. "Eight" was a convention rather than a rule, and this is the one fact needed
to read the recommendation that cannot live in the prose beside it, because prose
cannot be automated against.

## Entity contract

Exactly nine sensors — four from Phase 1, two from Phase 2, three from Phase 3 — unique IDs
`{entry_id}_{key}`, all on one service device named from the entry title. Names
are literal English with **no** `translation_key`: Home Assistant derives the
entity ID from the translated name, so a translation key would give a Dutch user
Dutch entity IDs. This matches the sibling Frank Quarter Prices integration, and
it is why the two new sensors carry no translation entries either.

Neither forecast sensor sets `state_class` — a prediction must not become a
long-term statistic. Nor does `Battery Recommendation`: a device class of `enum`
permits none, and a long-term statistic over a category means nothing.

The two Phase-2 error sensors invert both halves of that rule, deliberately. They
**do** set `state_class`, because they measure error that has already happened
and a record of it belongs in long-term statistics. They set **no**
`device_class`: `forecast_error_yesterday` is a signed difference, and an energy
class would offer it to the Energy dashboard beside real consumption.

Both read `unknown` rather than `0` when there is nothing to report. Zero is the
value of a perfect forecast.

Attributes are small scalars only. Never expose a per-interval profile; the
recorder writes every attribute on every state change.

`tests/test_entity_contract.py` freezes this.

## Boundaries

The integration reads the Home Assistant state machine and config-entry registry
and nothing else. No HTTP client is imported, `requirements` is empty, and the
coordinator has no `update_interval`. `tests/test_no_external_polling.py`
enforces this both statically and at runtime.

## Future phases

Things Phases 1 to 5 deliberately keep possible without implementing: grid
import/export limits, degradation modelling, and EV scheduling using the
flexible-load series already being recorded.

What each next phase needs, and where it plugs in:

| Phase | Needs | Seam |
|---|---|---|
| ~~**5** Solcast PV~~ | *shipped in beta.9* | production is a second series on the same index; the stepper still takes a sequence of demands. Asymmetric efficiency remains available and unused |
| **6** Frank prices | a price series joins | prices are a *policy* input; what-if already compares trajectories, so this adds a cost function over them |
| **7** Dynamic reserve | raise the floor, never the user's setting | `dynamic_reserve` beside `static_reserve`, computing `max(configured, dynamic)` **inside** the factory. `interval_margin_kwh` is already reported, and the P10/P90 series is now recorded per interval — but read `percentile_aggregation` first: a per-site sum is not a calibrated band |
| **8** Economic optimisation | replace the objective, keep the simulator | the policy interface; `HoldPolicy` is already the counterfactual to price against, and the soft reserve is what makes “dip but never below the floor” expressible |
| **9** Adaptive feedback | provenance and joins | recorded state of charge joins the Phase-2 snapshot by chronological index and target day; plans are recomputable; `policy_version` prevents pooling generations; separate efficiency fields let them be learned |
| **10** Multi-day | a longer horizon | the simulator is horizon-agnostic and already walks today plus tomorrow |

### Known open items — Phase 4

**Ownership cannot be established.** Nothing in the control surface records who
armed a dispatch, so Alpha EMS cannot prove a running one is its own. This blocks
both stopping and continuing a command, and is therefore the primary prerequisite
for real execution — ahead of any price signal. Do not solve it by comparing
parameters.

**The command latency is computed, not measured.** A command would act for roughly
one interval less about ten seconds (the refresh offset, the control surface's own
two-second settle, and service latency), delivering about 98.9 % of the allowance.
Shadow mode cannot measure this, because nothing is sent. It stays an estimate.

**The two features that drive the battery independently** — Excess Export and Peak
Shaving — re-arm their own dispatch on a five-minute dead-man. Either being on
inhibits Alpha EMS, so control and those features are mutually exclusive. That is
a product decision, deliberately resolved in the user's favour: Alpha EMS stands
down rather than switching off a setting somebody chose.

**The residual carries a PV filter term.** The house-load template uses a
low-pass-filtered PV figure while the balance check reads the raw one, so a third
term joins the two-meter difference during PV transients. Diagnostics-only, like
the rest.

### Capability is established from facts, never from another entry's state

beta.9 decided whether the Solcast source could be read by asking whether its
config entry was in state ``LOADED``. That was a category error, and it produced a
live false negative on every Home Assistant restart: Solcast registers its actions
at component level, so both are visible while its config entry is still setting
up, and Alpha EMS takes its first refresh during its own setup. One diagnostics
download reported both actions registered and the entry not loaded, in a single
call.

The rule now, asserted from the syntax tree: **the probe may not read any config
entry internals.** Existence is a fact about configuration and is fair game;
setup state, runtime data and everything else say nothing about whether a
registered action can be called. ``ConfigEntryState`` is not imported in the
module at all, so reaching for it again is a visible decision rather than an easy
one.

What replaced it is provable: an entry is selected, the stored id names an entry
that exists, and the actions are registered. Failure is handled where it actually
occurs -- a call that raises is caught and reported as a failed call, which is
strictly better information than a guess made in advance.

### A setup-time reading is provisional

The first refresh happens during this entry's own setup, and refreshes are then
driven by the quarter-hour tick rather than an interval. Anything read at setup --
before the AlphaESS Modbus sensors have published, or before a consumed
integration has loaded -- therefore stood for up to fifteen minutes. That is what
made the beta.9 symptom look permanent, and it is also why the battery plan
reported a missing state of charge beside a live reading of 96 %.

A refresh now also runs on ``async_at_started``, which fires immediately when Home
Assistant is already running so a reload behaves like a cold boot. It uses
``async_refresh`` rather than ``async_request_refresh``: the requesting form is
debounced, and a startup refresh colliding with a user action inside the cooldown
would collapse the two -- with the survivor possibly being the one taken *before*
the user acted.

One consequence worth knowing: a reload now performs two refreshes rather than one,
so per-refresh tallies such as duplicate issuances double. Nothing is issued twice;
the same content fingerprint is simply recognised twice.

### Two instants, said out loud

Diagnostics mixes live probes with snapshots from the last refresh, and beta.9
printed a stale capability beside a live availability flag computed by a *different
rule*. The pair contradicted itself and there was nothing in the payload to say
why.

Now there is one definition of availability, the capability block is probed at
download time, the last refresh's capability is kept beside it under its own name,
and the refresh instant is published. An invariant test asserts the two cannot
disagree.

### Known open items — Phase 5

**Which selected site feeds which AlphaESS subsystem is unknown.** Solcast divides
a roof by orientation and AlphaESS divides it by electrical coupling, so equal
counts prove nothing. Membership is declared by the user and that is all that is
asked; the correspondence is recorded as unknown. Do not solve it by asking the
user, and do not solve it by inferring from capacities — the raw per-site
capacities are stored, so a later phase that finds a sound derivation can classify
retroactively.

**The per-site query path is implemented against the documented contract rather
than against an observed response.** The aggregate path is verified live; the
subset path is exercised only against a fake. Every snapshot records `query_mode`,
so a future disagreement between the two is attributable rather than mysterious.
Confirming it needs one read: per-site queries over a window already fetched as an
aggregate, comparing the sums. If the percentile bands do not agree,
`percentile_aggregation` stops being a formality.

**Forecast and measured production sit on different electrical boundaries** — DC
strings plus an AC meter on one side, an unstated boundary on the other. A
persistent difference between them is a conversion property of the installation.
Recorded, never corrected.

**The source can correct its own output in two ways**, dampening and actuals
blending, and both were off on the installation this was built against. Either
turning on means the series Alpha EMS reads is no longer raw, and a later adaptive
phase would then be learning on top of somebody else's learning. Both are in
provenance for exactly that reason.

`ForecastUncertainty` exists so a reserve can be sized from measured forecast
error rather than a guessed margin — note that its fields are `None` when there is
no evidence, and `None` means "no evidence", never "no error".

An adaptive phase should read the stored evidence through the same boundary, and
should check `model_version` and `model_params_hash` before pooling records: two
different values there describe two different models.
