"""Constants for the Alpha EMS Manager integration.

Phase 1 scope: data fusion, household-load learning and forecasting.
No battery control, no arbitrage, no optimisation.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alpha_ems_manager"
NAME: Final = "Alpha EMS Manager"

# Default config-entry title. The device is named after the entry title, so the
# default yields entity ids such as ``sensor.alpha_ems_expected_house_load_today``.
DEFAULT_INSTANCE_NAME: Final = "Alpha EMS"

PLATFORMS: Final = ["sensor"]

# --- Learning resolution ------------------------------------------------------

#: Length of one learning bucket, in minutes.
QUARTER_MINUTES: Final = 15
#: Length of one learning bucket, in seconds.
QUARTER_SECONDS: Final = QUARTER_MINUTES * 60
#: Number of wall-clock quarter slots in a nominal civil day.
#:
#: Slot index is always derived from local wall-clock time
#: (``hour * 4 + minute // 15``), so the index range is 0..95 even on DST
#: transition days. A spring-forward day simply never observes some slots and a
#: fall-back day observes an hour of slots twice; see ``storage.DayRecord``.
#:
#: It is *not* a day length. ``DayRecord.interval_count`` counts real
#: quarter-hours and is 92, 96 or 100; this constant only serves as that field's
#: nominal default and as the ``2 * SLOTS_PER_DAY`` bound that keeps a corrupt
#: stored length from allocating unbounded lists. Every production caller --
#: ``get_or_create`` and ``from_dict`` -- passes a real count, and nothing may
#: use this value where a civil day's true length is meant.
SLOTS_PER_DAY: Final = 96

# --- Quarter measurement quality ---------------------------------------------

#: Longest gap between two consecutive source samples that is still integrated.
#:
#: Within this window the previous power reading is held constant (left-hand
#: Riemann integration, which is the physically correct reading of a
#: change-driven power sensor). Anything longer is recorded as *missing*
#: coverage and contributes no energy at all, so an unavailable source can never
#: manufacture load.
MAX_SAMPLE_GAP_SECONDS: Final = 300

#: Longest span the accumulator will walk forward in one step, in seconds.
#:
#: Not a measurement rule -- every quarter inside a gap this long has already
#: failed the MAX_SAMPLE_GAP_SECONDS test and can only be rejected -- but a
#: bound on the work done producing those rejections. A host without a
#: real-time clock starts Home Assistant in 1970 and is stepped to the present
#: once timesyncd reaches a server, which asked the accumulator to close two
#: million quarter-hour buckets in a single synchronous loop: about twelve
#: seconds of blocked event loop and several hundred megabytes of results that
#: were all going to be discarded. Beyond a day, accumulation simply restarts
#: at the new instant.
MAX_CATCHUP_SECONDS: Final = 86_400

#: Safety sampling interval. Guarantees the accumulator advances even when the
#: source entity is quiet. Exactly one timer is registered per config entry.
SAFETY_SAMPLE_SECONDS: Final = 60

#: Minimum fraction of a quarter that must be covered by valid samples before
#: the quarter is accepted for learning.
#:
#: Note what does *not* cost coverage: a gap up to ``MAX_SAMPLE_GAP_SECONDS``
#: contributes in full, because holding the last reading across it is the correct
#: reading of a change-driven sensor. So a missed 60 s sampler tick, or any
#: tolerated gap wholly inside the quarter, leaves coverage at 1.0.
#:
#: The threshold therefore governs two cases only: a gap that straddles a quarter
#: boundary, and the partial quarter in progress when the integration starts.
#: 0.80 accepts a quarter that lost up to three minutes at one end, while
#: rejecting one that was mostly unobserved -- which must never be learned as a
#: low-consumption period. Any single *untolerated* gap inside a quarter already
#: costs more than 33 %, so it lands below this threshold regardless.
MIN_QUARTER_COVERAGE: Final = 0.80

#: Minimum fraction of a day's occurred quarters that must carry a valid
#: *baseline* value before the day counts as a learned day.
#:
#: Baseline validity needs both a usable measured reading and, when a flexible
#: load is configured, a usable flexible-load reading. A day whose EV sensor was
#: down therefore keeps its measured history but does not count as learned.
MIN_DAY_COMPLETENESS: Final = 0.80

#: Implausibly high instantaneous household load, in watts. Readings above this
#: are treated as sensor glitches and recorded as missing coverage.
MAX_PLAUSIBLE_LOAD_W: Final = 60_000.0

# --- Flexible loads -----------------------------------------------------------

#: Implausibly high EV charging power, in watts. A 22 kW three-phase AC charger
#: is the realistic domestic ceiling; this leaves generous headroom above it.
MAX_PLAUSIBLE_EV_W: Final = 50_000.0

#: EV charging power is consumption, so the canonical value is never negative.
#: Readings inside this noise band are clamped up to zero; anything more
#: negative is treated as an *invalid sample*, not as zero charging. Silently
#: reading a negative as zero would let a mis-selected sensor quietly inflate
#: the baseline, which is exactly the contamination this feature prevents.
EV_NEGATIVE_NOISE_FLOOR_W: Final = -10.0

# --- Persistence --------------------------------------------------------------

#: Storage schema version.
#:
#: v1 was the original development format: a fixed 96-entry list keyed by
#: wall-clock slot. That shape cannot represent a 100-quarter fall-back day, so
#: it was replaced before any real history accumulated. v1 documents are
#: discarded with a warning rather than misread -- see ``LearningStore``.
STORAGE_VERSION: Final = 2
STORAGE_MINOR_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = f"{DOMAIN}.{{entry_id}}.learning"

#: Config-entry schema version. v1 was the previous integration's source model,
#: which shares no configuration keys with this one and cannot be mapped onto
#: it. See ``async_migrate_entry``.
CONFIG_ENTRY_VERSION: Final = 2

#: A key that only ever existed in the v1 configuration model, used to identify
#: a legacy entry precisely rather than by absence of the new keys.
LEGACY_CONF_MARKER: Final = "cumulative_house_load_sensor"

#: Maximum number of calendar days of learning history retained.
MAX_HISTORY_DAYS: Final = 365

#: Debounce delay for persisting learning history, in seconds. Quarters finalise
#: every 15 minutes, so this batches writes without risking meaningful loss.
STORE_SAVE_DELAY: Final = 60

# --- Forecast model -----------------------------------------------------------

#: Historical look-back windows, in days, used by the combined forecast.
FORECAST_WINDOWS: Final = (7, 30, 90, 180, 365)

#: Base weight per window. The windows deliberately overlap: a day inside the
#: 7-day window also sits in every longer window, which is what gives recent
#: observations their extra influence. Weights are renormalised over whichever
#: windows actually have enough data.
FORECAST_WINDOW_WEIGHTS: Final = {
    7: 0.35,
    30: 0.27,
    90: 0.20,
    180: 0.12,
    365: 0.06,
}

#: Minimum number of valid observations of a slot inside a window before that
#: window is allowed to contribute to the blended forecast.
MIN_OBSERVATIONS_PER_WINDOW: Final = 2

# --- Day typing ---------------------------------------------------------------

DAY_TYPE_WEEKDAY: Final = "weekday"
DAY_TYPE_WEEKEND: Final = "weekend"

#: Minimum number of valid days of a given day type before the day-type split is
#: trusted. Below this the model falls back to all-day statistics, which keeps a
#: three-day-old installation from overfitting to one observed Saturday.
MIN_DAYS_FOR_DAY_TYPE: Final = 2

# --- Today adaptation ---------------------------------------------------------

#: Fraction of the observed over/under-consumption ratio that is carried into
#: the remainder of the day. 0.5 halves the correction, so one unusual appliance
#: cycle cannot dominate the rest of the forecast.
TODAY_ADAPT_DAMPING: Final = 0.5

#: Clamp applied to the damped adaptation ratio.
TODAY_ADAPT_RATIO_MIN: Final = 0.6
TODAY_ADAPT_RATIO_MAX: Final = 1.6

#: Adaptation is suppressed before this many quarters of the day have completed,
#: and while the modelled baseline so far is below the energy floor. Both guards
#: stop tiny early-morning baselines from producing enormous ratios.
TODAY_ADAPT_MIN_ELAPSED_SLOTS: Final = 8
TODAY_ADAPT_MIN_BASELINE_KWH: Final = 0.5

# --- Confidence ---------------------------------------------------------------

#: Time constant, in days, of the saturating maturity curve
#: ``1 - exp(-valid_days / tau)``.
CONFIDENCE_DAYS_TAU: Final = 30.0

#: Number of trailing days examined by the recency component.
CONFIDENCE_RECENCY_DAYS: Final = 7

#: Relative weights of the data-quality components. ``balance`` is dropped and
#: the rest renormalised when no energy-balance samples are available.
CONFIDENCE_QUALITY_WEIGHTS: Final = {
    "coverage": 0.35,
    "recency": 0.20,
    "stability": 0.25,
    "balance": 0.20,
}

# --- Energy balance -----------------------------------------------------------

# The balance identity is checked against a three-term allowance rather than a
# single relative tolerance, because the residual has three physically distinct
# causes that scale differently. See ``evaluate_balance()`` for the formula.
#
# A flat relative tolerance gets this wrong in both directions. It is too tight
# in a low-power conversion mode -- a battery discharging 300 W DC to deliver
# 240 W AC is a perfectly healthy 20 % "error" -- and far too loose at high
# power, where 15 % of 10 kW is a 1.5 kW allowance that would happily hide a
# mis-selected entity.

#: Fixed allowance, in watts, independent of the flows.
#:
#: Covers the two constant contributions. First, the hybrid inverter's own
#: auxiliary draw: control electronics, BMS and fans on the AlphaESS SMILE range
#: consume some tens of watts that are supplied from PV, battery or grid but
#: never appear as house load. Second, per-register rounding across the four
#: sources, which is a few watts at the 1 W resolution these report.
#:
#: 40 W is also within a couple of watts of the effective absolute allowance of
#: the previous rule (15 % of a 250 W floor, so 37.5 W), which is deliberate:
#: overnight and standby behaviour is known-good and must not change.
BALANCE_BASE_ALLOWANCE_W: Final = 40.0

#: Conversion-loss allowance as a fraction of the DC-side power.
#:
#: PV and battery power are measured on the DC side of a hybrid inverter, while
#: house load and grid are AC-side quantities. Every watt that crosses that
#: boundary is taxed by the conversion stage, so the identity cannot close to
#: better than the inverter's efficiency. Datasheet peak efficiency for this
#: class of inverter is around 97 %, but the efficiency curve degrades sharply
#: below roughly 10 % of rated power -- exactly where a domestic battery spends
#: much of its day. 5 % of DC throughput covers that region when combined with
#: the fixed allowance above, while staying well below the ~20 % that would be
#: needed to explain a grossly mis-selected entity.
BALANCE_CONVERSION_LOSS_FRACTION: Final = 0.05

#: Metering allowance as a fraction of the AC-side power scale.
#:
#: The grid figure and the house-load figure need not come from the same device:
#: the grid side is typically a P1 or CT meter and the house load is derived
#: inside the inverter. Two independent instruments integrating over different
#: windows disagree by more than their individual accuracy class (~1-2 % for a
#: class 1 CT). 3 % absorbs that and the residual sub-skew timing noise that
#: survives the coherence gate.
#:
#: Where this comes out tighter than the flat 15 % it replaces depends on how much
#: conversion is happening, because the DC term is added on top. For a
#: non-converting flow -- grid straight to house with the battery idle -- the
#: crossover is around 350 W. Where PV and battery both convert, so DC power is
#: roughly twice AC power, it is around 2 kW. Above those points the new model is
#: strictly stricter; below them it is deliberately more forgiving, which is the
#: entire point: that is the regime where inverter efficiency genuinely falls off.
BALANCE_METERING_TOLERANCE: Final = 0.03

#: Per-flow threshold, in watts, above which a flow counts as active for the
#: purpose of labelling the operating mode. Set just above sensor noise and
#: standby so an idle system reports ``idle`` rather than a spurious mode.
BALANCE_MODE_ACTIVE_W: Final = 25.0

#: How far past its allowance a residual must go before the warning is allowed
#: to suggest a wrong entity or sign convention rather than a measurement
#: boundary effect.
#:
#: Both conditions must hold. The multiple alone would escalate a 60 W residual
#: at 3 a.m.; the floor alone would escalate a legitimately large conversion
#: loss at 10 kW. A genuine sign inversion or a missing source doubles or
#: removes a kilowatt-scale term, so it clears both by a wide margin -- an
#: inverted battery sign on a 1.3 kW discharge lands about thirteen times over
#: its allowance.
BALANCE_GROSS_FAULT_MULTIPLE: Final = 3.0
BALANCE_GROSS_FAULT_FLOOR_W: Final = 500.0

#: Absolute floor, in watts, used as the denominator of the *reported* relative
#: error. Reporting only: the pass/fail verdict comes from the allowance above.
#:
#: Set near a typical household standby draw so that a 20 W disagreement between
#: two near-zero flows is reported as an 8 % error rather than a 100 % one, which
#: keeps the figure in the logs and diagnostics readable at night.
BALANCE_ABSOLUTE_FLOOR_W: Final = 250.0

#: Number of most recent balance samples retained for the quality score.
BALANCE_SAMPLE_WINDOW: Final = 200

#: Largest spread between the participating sources' most recent reports before
#: a balance sample is considered temporally incoherent and skipped.
#:
#: The sources do not share a clock. Modbus-backed registers are commonly polled
#: on per-register intervals ranging from about one second to a minute, while a
#: P1 meter typically publishes every second or two, so at any instant the
#: newest and oldest reports can legitimately be most of a minute apart. Ninety
#: seconds therefore admits normal operation -- including a register polled only
#: once a minute -- while still catching a source that has genuinely stopped
#: reporting relative to the others.
BALANCE_MAX_SOURCE_SKEW_SECONDS: Final = 90.0

#: Oldest acceptable report age for any participating source. Matches
#: ``MAX_SAMPLE_GAP_SECONDS``: a source silent for five minutes is treated as
#: not reporting at all, and the balance identity says nothing useful about it.
BALANCE_MAX_SOURCE_AGE_SECONDS: Final = 300.0

#: Consecutive *coherent* failures required before a user-facing warning.
#:
#: Balance is sampled once a minute, so three consecutive failures means the
#: imbalance has persisted for around three minutes. A load step -- a kettle
#: switching on before the battery and grid meters have caught up -- resolves in
#: seconds and can produce at most one failing sample, whereas a wrong sign
#: convention or a mis-selected entity fails every single sample. This is what
#: separates the two, and it is the mechanism that stops transient warnings.
BALANCE_SUSTAINED_FAILURES: Final = 3

# --- Phase 2: forecast history ------------------------------------------------

#: Forecast-history schema version. Deliberately independent of
#: ``STORAGE_VERSION``: the learning history and the forecast evidence have
#: different lifecycles, and a change to one must not force a migration of the
#: other.
FORECAST_STORAGE_VERSION: Final = 1
#: Minor version, bumped for a *backward-compatible* addition to the layout.
#: The major version is what decides whether a document can be read at all, so a
#: minor bump reads every earlier document unchanged and simply writes the newer
#: one back. History is never migrated and never discarded by one.
#:
#: * **1.1** -- v1.0.0-beta.5. The original layout.
#: * **1.2** -- v1.0.0-beta.6. Summary rows gained ``mr``, the matching-rule
#:   generation that produced them. A row without it is generation 1, which is
#:   exactly what every beta.5 row is.
FORECAST_STORAGE_MINOR_VERSION: Final = 2

#: Index document: schema version, the month partitions that exist, and the
#: small daily summary rows. Always loaded.
FORECAST_INDEX_KEY_TEMPLATE: Final = f"{DOMAIN}.{{entry_id}}.forecast_index"
#: One partition per calendar month of *target* days. Home Assistant's ``Store``
#: rewrites a whole document on every save, so a single year-long file would put
#: roughly a megabyte through the executor on every issuance. Partitioning keeps
#: a write to about a hundred kilobytes and confines a corrupt document to one
#: month instead of the entire history.
FORECAST_MONTH_KEY_TEMPLATE: Final = f"{DOMAIN}.{{entry_id}}.forecast.{{month}}"

#: Debounce delay for forecast-history writes, in seconds. Issuance is rare --
#: see the fingerprint policy in ``forecast_history.py`` -- so this exists to
#: coalesce the two targets of a single refresh rather than to batch a stream.
FORECAST_STORE_SAVE_DELAY: Final = 10

#: Days of raw quarter-level forecast evidence retained, aligned deliberately
#: with ``MAX_HISTORY_DAYS``. Within this window a forecast record can still be
#: correlated with the learning history that produced it; beyond it those inputs
#: are gone, so the raw evidence could no longer answer *why* a forecast was
#: wrong -- only that it was.
FORECAST_RAW_RETENTION_DAYS: Final = MAX_HISTORY_DAYS

#: Days of daily summary rows retained. Around 200 bytes each, so a decade costs
#: well under a megabyte; the bound exists so the document cannot grow without
#: limit on an installation that runs for years.
FORECAST_SUMMARY_RETENTION_DAYS: Final = 3650

#: Hard ceiling on immutable snapshots kept for one target day.
#:
#: Issuance is change-triggered, and the Phase-1 model produces at most two
#: distinct forecasts per target (day-ahead and day-of), so this is never
#: reached in normal operation. It bounds the damage if a future input starts
#: oscillating: without it, a source flapping every quarter would write 96
#: records a day. A breach is logged rather than silently truncating, because a
#: silent cap reads as full coverage when it is not.
FORECAST_MAX_SNAPSHOTS_PER_TARGET: Final = 32

#: Decimal places used for persisted energies and for the fingerprint input.
#: Matches the learning store's precision -- 0.1 Wh -- so a rounding difference
#: can never make two identical forecasts fingerprint differently.
FORECAST_KWH_PRECISION: Final = 4

#: Version of the forecasting model itself, bumped whenever a change alters the
#: numbers ``build_forecast`` produces. Recorded on every snapshot so a later
#: phase cannot pool error statistics across two incompatible model generations
#: and read the discontinuity as a behavioural change in the household.
FORECAST_MODEL_VERSION: Final = 1

#: Version of the *matching* rules -- how a stored prediction is paired with
#: what the house actually did, and which days are judged incomparable.
#:
#: Distinct from ``FORECAST_MODEL_VERSION``, which versions the numbers the
#: forecast contains. This versions the numbers the *comparison* contains, and
#: it exists so a correction to a matching rule reaches the days already
#: matched under the old one. Matching is a pure recomputation from the stored
#: snapshot and the retained learning record, so re-deriving an outcome is
#: idempotent and loses nothing -- while leaving it alone would keep a verdict
#: the current release knows to be wrong, permanently, in the one dataset this
#: project cannot regenerate.
#:
#: * **1** -- v1.0.0-beta.5. Judged the baseline definition from every entry of
#:   ``DayRecord.ev_expected``, including the padded entries of intervals that
#:   were never recorded, so on an installation with a flexible load a single
#:   missing quarter excluded the whole day as ``definition_changed``.
#: * **2** -- v1.0.0-beta.6. Judges it from the observed intervals only.
FORECAST_MATCHER_VERSION: Final = 2

#: Rolling window, in days, behind the published forecast-error sensor.
FORECAST_ERROR_WINDOW_DAYS: Final = 7

#: Windows reported in diagnostics. Bounded at 90 days so a diagnostics download
#: loads at most four month partitions.
FORECAST_METRIC_WINDOWS: Final = (7, 30, 90)

#: Fewest compared intervals before a rolling metric is published at all.
#: Roughly two full days. Below it the figure is dominated by whichever few
#: intervals happened to resolve, and publishing it would invite a user to read
#: a fresh installation's noise as forecast quality.
FORECAST_MIN_INTERVALS_FOR_METRIC: Final = 192

#: Per-interval outcome status codes. A fixed, bounded key space: an interval is
#: described by exactly one of these, and the set cannot grow at runtime.
#:
#: The distinction between the three failure codes is the point. "No usable
#: actual" is not one situation: a quarter that never reached coverage, a
#: quarter whose flexible-load reading was unusable, and a quarter that never
#: happened call for completely different readings of the same missing number.
STATUS_VALID: Final = "0"
#: No usable measured reading: the quarter never reached ``MIN_QUARTER_COVERAGE``,
#: or Home Assistant was not running for it.
STATUS_MEASURED_MISSING: Final = "1"
#: Measured energy exists, but a configured flexible load had no usable reading,
#: so the baseline for that interval is not defined. The measured ground truth
#: is intact; only the quantity the forecast predicts is missing.
STATUS_FLEXIBLE_MISSING: Final = "2"
#: The interval had not elapsed when the day was finalised. Unreachable in
#: normal operation -- only days already in the past are finalised -- so this
#: exists for a clock stepped backwards across a finalisation.
STATUS_NOT_ELAPSED: Final = "3"

#: Reasons a finalised target day is excluded from every derived metric. The
#: record is still kept: the prediction and the actual are both true facts, and
#: only their *comparability* is in doubt.
#:
#: A day carrying any of these must never enter an error statistic, because each
#: one means the two sides of the comparison are describing different things.
FLAG_NO_RECORD: Final = "no_record"
FLAG_SHAPE_MISMATCH: Final = "shape_mismatch"
FLAG_TIMEZONE_CHANGED: Final = "timezone_changed"
FLAG_DEFINITION_CHANGED: Final = "definition_changed"

#: Behavioural slot bands used to report where in the day the error sits.
#: Quarter-hour resolution is too fine to read as a table and too noisy to act
#: on; four bands are enough to see a pattern and few enough to publish.
FORECAST_SLOT_BANDS: Final = (
    ("night", 0, 24),  # 00:00-05:59
    ("morning", 24, 48),  # 06:00-11:59
    ("afternoon", 48, 72),  # 12:00-17:59
    ("evening", 72, 96),  # 18:00-23:59
)

# --- Configuration keys -------------------------------------------------------

CONF_NAME: Final = "name"

CONF_BATTERY_SOC_ENTITY: Final = "battery_soc_entity"
CONF_BATTERY_POWER_ENTITY: Final = "battery_power_entity"
CONF_BATTERY_POWER_SIGN: Final = "battery_power_sign"

CONF_HOUSE_LOAD_ENTITY: Final = "house_load_entity"
CONF_DAILY_HOUSE_LOAD_ENTITY: Final = "daily_house_load_entity"

#: Optional flexible-load source. EV charging is the first supported flexible
#: load; nothing about the storage or forecast layers is EV-specific, so further
#: flexible loads can be added later without another schema change.
CONF_EV_POWER_ENTITY: Final = "ev_power_entity"

CONF_HAS_PV: Final = "has_pv"
CONF_PV_POWER_ENTITY: Final = "pv_power_entity"

CONF_GRID_POWER_ENTITY: Final = "grid_power_entity"
CONF_GRID_POWER_SIGN: Final = "grid_power_sign"

CONF_FRANK_ENTRY_ID: Final = "frank_entry_id"

CONF_USE_PV_FORECAST: Final = "use_pv_forecast"
CONF_SOLCAST_ENTRY_ID: Final = "solcast_entry_id"

# --- Sign conventions ---------------------------------------------------------

#: Battery power sign options. The AlphaESS test system reports a *negative*
#: battery power while charging, but that is not assumed globally.
SIGN_BATTERY_NEGATIVE_IS_CHARGE: Final = "negative_is_charge"
SIGN_BATTERY_POSITIVE_IS_CHARGE: Final = "positive_is_charge"
BATTERY_SIGN_OPTIONS: Final = (
    SIGN_BATTERY_NEGATIVE_IS_CHARGE,
    SIGN_BATTERY_POSITIVE_IS_CHARGE,
)
DEFAULT_BATTERY_POWER_SIGN: Final = SIGN_BATTERY_NEGATIVE_IS_CHARGE

#: Grid power sign options.
SIGN_GRID_POSITIVE_IS_IMPORT: Final = "positive_is_import"
SIGN_GRID_NEGATIVE_IS_IMPORT: Final = "negative_is_import"
GRID_SIGN_OPTIONS: Final = (
    SIGN_GRID_POSITIVE_IS_IMPORT,
    SIGN_GRID_NEGATIVE_IS_IMPORT,
)
DEFAULT_GRID_POWER_SIGN: Final = SIGN_GRID_POSITIVE_IS_IMPORT

# --- Consumed integration domains ---------------------------------------------

#: Domains Alpha EMS Manager *reads entities from*. It never talks to their APIs.
DOMAIN_FRANK: Final = "frank_quarter_prices"
DOMAIN_SOLCAST: Final = "solcast_solar"

# --- Entity keys --------------------------------------------------------------

SENSOR_EXPECTED_LOAD_TODAY: Final = "expected_house_load_today"
SENSOR_EXPECTED_LOAD_TOMORROW: Final = "expected_house_load_tomorrow"
SENSOR_LEARNING_CONFIDENCE: Final = "learning_confidence"
SENSOR_LEARNING_DAYS: Final = "learning_days"

# Phase 2. Both are measurements of past error rather than predictions, so both
# carry a state class -- unlike the two forecast sensors above.
SENSOR_FORECAST_ERROR_YESTERDAY: Final = "forecast_error_yesterday"
SENSOR_FORECAST_ERROR_WINDOW: Final = "forecast_error_7d"

# --- Logging ------------------------------------------------------------------

#: Minimum seconds between two repetitions of the same aggregated warning.
LOG_THROTTLE_SECONDS: Final = 3600
