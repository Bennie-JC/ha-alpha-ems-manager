# Architecture

Developer reference for the Alpha EMS Manager integration. The
[README](../README.md) describes what the integration does for a user; this
document describes how it is put together and, where it matters, why.

Read this before changing the learning, persistence or energy-balance layers —
several sections record decisions that a reasonable-looking change would silently
undo.

## Scope

Phases 1 and 2 are **observation only**. Phase 1 measures household consumption,
learns a baseline demand profile and forecasts it. Phase 2 records each forecast
and matches it against what actually happened. Neither issues a command to the
battery, makes a charge, discharge or trading decision, or schedules anything.

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

## Entity contract

Exactly six sensors — four from Phase 1, two from Phase 2 — unique IDs
`{entry_id}_{key}`, all on one service device named from the entry title. Names
are literal English with **no** `translation_key`: Home Assistant derives the
entity ID from the translated name, so a translation key would give a Dutch user
Dutch entity IDs. This matches the sibling Frank Quarter Prices integration, and
it is why the two new sensors carry no translation entries either.

Neither forecast sensor sets `state_class` — a prediction must not become a
long-term statistic.

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

Optimisation, reserve calculation and any battery control belong on top of this
foundation, not inside it. Things Phases 1 and 2 deliberately keep possible
without implementing: battery minimum SOC and usable capacity, charge/discharge
power limits, grid import/export limits, efficiency and degradation modelling,
reason/status outputs, and EV scheduling using the flexible-load series already
being recorded.

Phase 3 should consume `api.py` and nothing deeper. `ForecastUncertainty` exists
so a reserve can be sized from measured forecast error rather than a guessed
margin — note that its fields are `None` when there is no evidence, and `None`
means "no evidence", never "no error".

An adaptive phase should read the stored evidence through the same boundary, and
should check `model_version` and `model_params_hash` before pooling records: two
different values there describe two different models.
