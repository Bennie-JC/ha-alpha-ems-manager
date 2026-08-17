# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.2...HEAD
[1.0.0-beta.2]: https://github.com/Bennie-JC/ha-alpha-ems-manager/compare/v1.0.0-beta.1...v1.0.0-beta.2
[1.0.0-beta.1]: https://github.com/Bennie-JC/ha-alpha-ems-manager/releases/tag/v1.0.0-beta.1
[0.1.0]: https://github.com/Bennie-JC/ha-alpha-ems-manager/releases/tag/v0.1.0
