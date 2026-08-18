# Architecture

Developer reference for the Alpha EMS Manager integration. The
[README](../README.md) describes what the integration does for a user; this
document describes how it is put together and, where it matters, why.

Read this before changing the learning, persistence or energy-balance layers —
several sections record decisions that a reasonable-looking change would silently
undo.

## Scope

Phase 1 is **observation only**. It measures household consumption, learns a
baseline demand profile and forecasts it. It issues no commands to the battery,
makes no charge, discharge or trading decisions, and schedules nothing.

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

## Entity contract

Exactly four sensors, unique IDs `{entry_id}_{key}`, all on one service device
named from the entry title. Names are literal English with **no**
`translation_key`: Home Assistant derives the entity ID from the translated name,
so a translation key would give a Dutch user Dutch entity IDs. This matches the
sibling Frank Quarter Prices integration.

Neither forecast sensor sets `state_class` — a prediction must not become a
long-term statistic.

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
foundation, not inside it. Things Phase 1 deliberately keeps possible without
implementing: battery minimum SOC and usable capacity, charge/discharge power
limits, grid import/export limits, efficiency and degradation modelling,
reason/status outputs, and EV scheduling using the flexible-load series already
being recorded.
