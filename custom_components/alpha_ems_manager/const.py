"""Constants for the Alpha EMS Manager integration.

Data fusion, household-load learning, forecasting, a battery decision, and the
control pipeline that would carry it out. No arbitrage and no optimisation -- and
no execution either: ``CONTROL_EXECUTION_AVAILABLE`` is what makes the last of
those a fact rather than an intention.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alpha_ems_manager"
NAME: Final = "Alpha EMS Manager"

# Default config-entry title. The device is named after the entry title, so the
# default yields entity ids such as ``sensor.alpha_ems_expected_house_load_today``.
DEFAULT_INSTANCE_NAME: Final = "Alpha EMS"

# ``select`` joins ``sensor`` in Phase 4. It is the integration's first
# writable entity, and it writes only to this integration's own runtime
# state -- never to a battery.
PLATFORMS: Final = ["sensor", "select"]

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

# --- Photovoltaic generation --------------------------------------------------

#: Implausibly high instantaneous PV generation, in watts. A domestic rooftop
#: above this is not a domestic rooftop, so a reading beyond it is a sensor
#: glitch rather than an unusually good day.
#:
#: This ceiling exists because PV had none. House load went through
#: ``sanitize_load_w``, which refuses a value above ``MAX_PLAUSIBLE_LOAD_W``,
#: while PV went through a bare non-negative check -- so a spike to a million
#: watts was accepted, inflated the balance allowance and made the energy-balance
#: check most permissive exactly when the PV entity was most obviously wrong.
MAX_PLAUSIBLE_PV_W: Final = 50_000.0

#: PV generation is never negative, but a PV *sensor* legitimately can be: an
#: inverter draws a little standby power after dark, and on an installation whose
#: PV figure is a sum across several strings and an AC meter, that shows up as a
#: few watts below zero.
#:
#: Readings inside this band are clamped up to zero. Anything more negative is an
#: invalid sample, not zero generation -- and the band is deliberately narrow so
#: a sign-inverted sensor, which reads thousands of watts negative at midday, is
#: refused outright instead of being quietly clamped to a plausible zero.
PV_NEGATIVE_NOISE_FLOOR_W: Final = -50.0

# --- Persistence --------------------------------------------------------------

#: Storage schema version.
#:
#: v1 was the original development format: a fixed 96-entry list keyed by
#: wall-clock slot. That shape cannot represent a 100-quarter fall-back day, so
#: it was replaced before any real history accumulated. v1 documents are
#: discarded with a warning rather than misread -- see ``LearningStore``.
STORAGE_VERSION: Final = 2
#: Minor version, bumped for a *backward-compatible* addition to the layout. The
#: major version is what decides whether a document can be read at all, so a
#: minor bump reads every earlier document unchanged and simply writes the newer
#: one back. History is never migrated and never discarded by one.
#:
#: * **2.1** -- up to v1.0.0-beta.6.
#: * **2.2** -- v1.0.0-beta.7. Day records gained an optional per-interval
#:   battery state-of-charge array. Absent on every earlier document, and read as
#:   "no samples" rather than as zeros.
#: * **2.3** -- v1.0.0-beta.9. Day records gained an optional per-interval
#:   measured PV array, on exactly the same terms: absent on every earlier
#:   document, read as "no samples" rather than as zeros, and read by nothing on
#:   the learning path.
STORAGE_MINOR_VERSION: Final = 3
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

# Phase 3: battery planning. The first *numeric* configuration keys in the
# project -- every key above selects an entity, a sign convention or a boolean.
#
# They carry a unit suffix, which the entity keys do not need. Internal constants
# already do this (``MAX_PLAUSIBLE_LOAD_W``, ``TODAY_ADAPT_MIN_BASELINE_KWH``,
# ``BALANCE_CONVERSION_LOSS_FRACTION``) and a numeric key without one is
# ambiguous in the worst possible place: ``battery_min_soc`` could mean 20 or
# 0.2, and this value is a hard safety floor.
#
# Nothing here can be derived from the sensors already configured. A percentage
# state of charge says nothing about how many kilowatt-hours a percent is worth,
# and a power limit cannot be inferred from a capacity without assuming a C-rate
# -- which would be inventing a hardware property and would silently produce a
# plan the inverter cannot execute.

#: Usable capacity, **DC side**. See ``battery.py`` for why the boundary matters.
CONF_BATTERY_CAPACITY_KWH: Final = "battery_capacity_kwh"
#: The user's hard floor. Never crossed by the simulator; see ``BatteryReserve``.
CONF_BATTERY_MIN_SOC_PERCENT: Final = "battery_min_soc_percent"
#: Maximum charge and discharge power, **AC side**.
CONF_BATTERY_MAX_CHARGE_KW: Final = "battery_max_charge_kw"
CONF_BATTERY_MAX_DISCHARGE_KW: Final = "battery_max_discharge_kw"
#: Round-trip efficiency, **AC to AC**, as a percentage.
CONF_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: Final = (
    "battery_round_trip_efficiency_percent"
)

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

# --- Phase 3: battery planning ------------------------------------------------

#: Default hard floor, in percent of capacity.
#:
#: Conservative for a value the decision engine treats as inviolable, and
#: comfortably above the ~10 % on-grid discharge floor common on vendor
#: inverters, so the EMS constraint binds before the hardware's own does. A user
#: lowering it is then an explicit choice rather than an inherited accident.
#:
#: Zero is a legal setting: it means "no EMS reserve", and the inverter's own
#: floor still protects the cells. Refusing it would be an arbitrary restriction
#: of exactly the kind this project does not make.
DEFAULT_BATTERY_MIN_SOC_PERCENT: Final = 20.0

#: Default round-trip efficiency, AC to AC, in percent.
#:
#: Corroborated independently: the symmetric split this implies is
#: ``1 - sqrt(0.90) = 5.13 %`` per boundary crossing, against
#: ``BALANCE_CONVERSION_LOSS_FRACTION = 0.05`` above, which was derived from
#: inverter conversion loss for the energy-balance tolerance model. The two
#: agree to 0.13 %. Stated here so a future tuning of either cannot silently
#: separate them.
DEFAULT_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: Final = 90.0

#: Upper limit on the configurable state of charge, in percent.
#:
#: Not configurable in Phase 3: no Phase-3 policy charges, so a user-facing
#: ceiling would be a field nothing exercised. The charge clamp is written and
#: unit-tested against this constant so Phase 5 can promote it to an option
#: without touching the clamp.
BATTERY_MAX_SOC_PERCENT: Final = 100.0

#: Accepted range for a configured capacity, in kWh. The ceiling exists for the
#: same reason ``MAX_PLAUSIBLE_LOAD_W`` does: a mistyped value must be refused
#: rather than producing a confidently wrong plan.
MIN_BATTERY_CAPACITY_KWH: Final = 0.1
MAX_BATTERY_CAPACITY_KWH: Final = 200.0

#: Accepted range for a configured charge or discharge power limit, in kW.
MIN_BATTERY_POWER_KW: Final = 0.1
MAX_BATTERY_POWER_KW: Final = 50.0

#: Accepted range for round-trip efficiency, in percent. The floor is not
#: cosmetic: it catches a user entering ``0.90`` where ``90`` belongs, which
#: would otherwise model a plausible-looking 90 %-loss battery.
MIN_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: Final = 50.0
MAX_BATTERY_ROUND_TRIP_EFFICIENCY_PERCENT: Final = 100.0

#: How far outside 0..100 a state-of-charge reading may sit and still be clamped
#: back rather than refused. Sensor noise around either end is real; anything
#: further out is not a number, it is an unreadable source. The same reasoning as
#: ``EV_NEGATIVE_NOISE_FLOOR_W``, and deliberately narrow for the same reason.
SOC_NOISE_BAND_PERCENT: Final = 1.0

#: Version of the Phase-3 decision policy. Recorded on every decision so a later
#: phase cannot pool decisions made under different objectives into one series --
#: the same role ``FORECAST_MODEL_VERSION`` plays for predictions.
BATTERY_POLICY_VERSION: Final = 1

# --- Phase 3: reserve provenance ----------------------------------------------

#: Where an effective minimum state of charge came from. Phase 3 only ever
#: produces the first; Phase 7 adds the second.
RESERVE_CONFIGURED: Final = "configured"
RESERVE_DYNAMIC: Final = "dynamic"

# --- Phase 3: battery actions -------------------------------------------------

#: What the decision layer recommends. ``NO_DECISION`` is deliberately distinct
#: from ``HOLD``: "hold because that is best" and "hold because I know nothing"
#: are different facts, and a later phase needs them apart. It is made safe by
#: an invariant rather than by hope -- ``NO_DECISION`` always carries zero
#: allowed energy, so it is behaviourally identical to ``HOLD``.
ACTION_HOLD: Final = "hold"
ACTION_CHARGE: Final = "charge"
ACTION_DISCHARGE: Final = "discharge"
ACTION_NO_DECISION: Final = "no_decision"

#: The three states the published recommendation entity can take. ``NO_DECISION``
#: is *not* among them: it renders as ``unknown``, the same way an unresolved
#: forecast error does.
BATTERY_ACTION_OPTIONS: Final = (ACTION_HOLD, ACTION_CHARGE, ACTION_DISCHARGE)

# --- Phase 3: request modes ---------------------------------------------------

#: The direction of a battery request. Direction and magnitude are separate so a
#: signed power cannot exist internally: a negative "discharge" would otherwise
#: *add* energy to the pack, and a simultaneous charge and discharge would
#: destroy energy while leaving the grid residual untouched.
MODE_IDLE: Final = "idle"
MODE_CHARGE: Final = "charge"
MODE_DISCHARGE: Final = "discharge"

# --- Phase 3: decision reasons ------------------------------------------------

#: Why the decision layer said what it said. A bounded vocabulary, like the
#: ``FLAG_*``, ``STATUS_*`` and ``REJECT_*`` key spaces: an open string would
#: become unqueryable within two releases.
REASON_AT_RESERVE: Final = "at_reserve"
REASON_BELOW_RESERVE: Final = "below_reserve"
REASON_COVER_FORECAST_LOAD: Final = "forecast_load_and_available_energy"
REASON_NO_FLEXIBILITY: Final = "no_flexibility_available"
REASON_MISSING_SOC: Final = "missing_soc"
REASON_MISSING_CAPACITY: Final = "missing_capacity"
REASON_MISSING_POWER_LIMITS: Final = "missing_power_limits"
REASON_INVALID_EFFICIENCY: Final = "invalid_efficiency"
REASON_FORECAST_UNAVAILABLE: Final = "forecast_unavailable"
REASON_POLICY_HOLD: Final = "policy_hold"

# --- Phase 3: constraint names ------------------------------------------------

#: Which limit bound a request. Reported per interval and tallied per
#: trajectory; a bounded key space, so the tally cannot grow with runtime.
CONSTRAINT_MIN_SOC: Final = "min_soc"
CONSTRAINT_MAX_SOC: Final = "max_soc"
CONSTRAINT_MAX_CHARGE_POWER: Final = "max_charge_power"
CONSTRAINT_MAX_DISCHARGE_POWER: Final = "max_discharge_power"

BATTERY_CONSTRAINTS: Final = (
    CONSTRAINT_MIN_SOC,
    CONSTRAINT_MAX_SOC,
    CONSTRAINT_MAX_CHARGE_POWER,
    CONSTRAINT_MAX_DISCHARGE_POWER,
)

#: Decimal places for persisted and reported battery energies.
#:
#: Two, not four. An integer-percent state-of-charge sensor on a 10 kWh pack
#: quantises the starting energy to 0.1 kWh, so the seed carries about +/-0.05 kWh
#: of unavoidable error -- five hundred times any accumulated arithmetic drift.
#: Reporting four decimals would advertise 0.1 Wh resolution on a quantity known
#: to 50 Wh.
BATTERY_KWH_PRECISION: Final = 2
#: Decimal places for a reported state of charge. The source is integer percent.
BATTERY_SOC_PRECISION: Final = 1
#: Decimal places for a reported planned power.
BATTERY_KW_PRECISION: Final = 3

#: Binding intervals described individually in diagnostics, newest first. The
#: tallies beside them are always complete; this bounds only the per-interval
#: detail, and it must stay at or below the sixteen-entry ceiling every
#: diagnostics list is held to.
MAX_BINDING_INTERVALS_REPORTED: Final = 16

# --- Phase 4: the execution barrier -------------------------------------------

#: Whether real execution is permitted to leave this release at all.
#:
#: The release barrier, and the reason ``async_execute`` is unreachable rather
#: than merely disabled. The whole active path is built, imported and tested,
#: and this constant is the single thing standing between it and the inverter.
#:
#: Flipping it is **not** sufficient to enable real control. Two prerequisites
#: are unresolved, and both are recorded here so they cannot be forgotten:
#:
#: 1. **Provenance.** Nothing in the control surface records who armed a
#:    dispatch, so Alpha EMS cannot prove it created one. Without that proof a
#:    stop or a continuation could act on a dispatch a person started by hand.
#: 2. **Continuation.** Because any active dispatch is therefore treated as
#:    foreign, a dispatch Alpha EMS itself armed would inhibit it at the next
#:    refresh -- so no multi-interval command is expressible yet.
CONTROL_EXECUTION_AVAILABLE: Final = False

# --- Phase 4: control modes ---------------------------------------------------

#: What the user asks the control layer to do.
#:
#: ``off`` does nothing at all. ``shadow`` runs the complete real pipeline --
#: the same intent translation, the same safety gate and the same command
#: planner the active path uses -- and writes nothing, so its diagnostics answer
#: "would this have been safe, and what exactly would it have sent". ``active``
#: adds only the final authorization step.
CONTROL_MODE_OFF: Final = "off"
CONTROL_MODE_SHADOW: Final = "shadow"
CONTROL_MODE_ACTIVE: Final = "active"

CONTROL_MODE_OPTIONS: Final = (
    CONTROL_MODE_OFF,
    CONTROL_MODE_SHADOW,
    CONTROL_MODE_ACTIVE,
)

#: What the control pipeline actually did this refresh.
#:
#: ``inhibited`` and ``eligible`` are deliberately distinct: the first says the
#: safety gate refused, the second says it did not and only the execution
#: barrier stopped the write. Watching the second is the entire point of shadow
#: mode. ``idle`` means the gate passed and there was nothing to send, which is
#: what a hold is. No state describes a completed write, because this release
#: cannot perform one; a later phase adds that option when it can.
CONTROL_STATE_OFF: Final = "off"
CONTROL_STATE_INHIBITED: Final = "inhibited"
CONTROL_STATE_ELIGIBLE: Final = "eligible"
CONTROL_STATE_IDLE: Final = "idle"

CONTROL_STATE_OPTIONS: Final = (
    CONTROL_STATE_OFF,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_IDLE,
)

# --- Phase 4: configuration keys ----------------------------------------------

#: How long a command tells the device to hold, in minutes.
#:
#: A dead-man margin, **not** a delivery window. The plan is rebuilt once per
#: quarter-hour and each rebuild supersedes the last, so in normal operation
#: this never expires; it exists so a stopped Alpha EMS cannot leave a dispatch
#: running indefinitely. It must therefore exceed the planning cadence, which is
#: why the accepted range starts above it rather than at the device minimum.
CONF_CONTROL_HORIZON_MINUTES: Final = "control_horizon_minutes"
#: How far below the live house load a discharge command must stay, in percent.
CONF_CONTROL_EXPORT_MARGIN_PERCENT: Final = "control_export_margin_percent"
#: Whether the user has enabled real execution.
#:
#: Read but deliberately absent from the options form in this release: with
#: ``CONTROL_EXECUTION_AVAILABLE`` false it cannot change behaviour, and an
#: option that does nothing is worse than no option. The key exists so the
#: plumbing is exercised by tests today.
CONF_CONTROL_EXECUTION_ENABLED: Final = "control_execution_enabled"

DEFAULT_CONTROL_HORIZON_MINUTES: Final = 20
DEFAULT_CONTROL_EXPORT_MARGIN_PERCENT: Final = 10
DEFAULT_CONTROL_EXECUTION_ENABLED: Final = False

#: Accepted range for the dead-man margin, in minutes. The floor is one planning
#: cadence plus one device step: a shorter command would lapse before the next
#: refresh could renew it, leaving the battery unmanaged for most of every
#: interval.
MIN_CONTROL_HORIZON_MINUTES: Final = QUARTER_MINUTES + 5
MAX_CONTROL_HORIZON_MINUTES: Final = 60

#: Accepted range for the export margin, in percent.
MIN_CONTROL_EXPORT_MARGIN_PERCENT: Final = 0
MAX_CONTROL_EXPORT_MARGIN_PERCENT: Final = 50

# --- Phase 4: the device contract ---------------------------------------------
#
# Read off the control surface rather than assumed. Every figure here is a
# property of the helpers Alpha EMS writes, so each is a quantisation a command
# must respect -- and every one of them resolves *downwards*, so a command can
# only ever deliver less energy than the decision layer allowed.

#: Resolution of the power helper, in kW.
CONTROL_POWER_STEP_KW: Final = 0.1
#: Upper bound of the power helper, in kW.
CONTROL_MAX_POWER_KW: Final = 20.0

#: Window, in watts, inside which the control surface reads a battery as having
#: reached its cutoff and tears the dispatch down after ten seconds.
#:
#: Not a figure Alpha EMS chose. A command whose *actual* battery power lands
#: inside this band is indistinguishable from a finished one, so the smallest
#: command worth issuing has to sit clear of it.
CONTROL_HOLD_MONITOR_WINDOW_W: Final = 50.0

#: Smallest power Alpha EMS will command, in kW.
#:
#: Two helper steps, which is four times the teardown window above. One step
#: would be only twice it, and a dispatch tracks the commanded power rather than
#: matching it exactly, so a single-step command could sit inside the band while
#: behaving perfectly correctly. Below this the command is refused rather than
#: rounded up: the undelivered energy is at most one step over one interval, and
#: the inverter's own behaviour already covers it.
CONTROL_MIN_POWER_KW: Final = 2 * CONTROL_POWER_STEP_KW

#: Resolution and accepted range of the duration helper, in minutes.
CONTROL_DURATION_STEP_MINUTES: Final = 5
CONTROL_MIN_DURATION_MINUTES: Final = 5
CONTROL_MAX_DURATION_MINUTES: Final = 480

#: Accepted range of the cutoff state-of-charge helper, in percent.
CONTROL_CUTOFF_MIN_PERCENT: Final = 4
CONTROL_CUTOFF_MAX_PERCENT: Final = 100

#: Percent represented by one bit of the device's cutoff register.
#:
#: The control surface converts percent to register bits by truncation, so a
#: requested cutoff always lands at or slightly *below* the figure asked for --
#: the direction that would permit a marginally deeper discharge. One extra
#: percent is added to compensate, which is provably sufficient because the
#: worst-case truncation loss is smaller than one percent.
CONTROL_CUTOFF_PERCENT_PER_BIT: Final = 0.392

#: Decimal places a commanded power is reported and written with.
CONTROL_POWER_DECIMALS: Final = 1

#: Recent control events described individually in diagnostics, newest first.
#: Held to the same sixteen-entry ceiling as every other diagnostics list.
MAX_CONTROL_EVENTS_REPORTED: Final = 16

#: Minimum gap between two commands that start or increase battery movement.
#:
#: One planning interval, because the plan is rebuilt once per interval and
#: anything faster would throttle a decision that has not changed. A command
#: that *reduces* movement is exempt, since reducing can only reduce risk.
CONTROL_COOLDOWN_SECONDS: Final = QUARTER_SECONDS

# --- Phase 4: why control was inhibited ---------------------------------------
#
# A bounded vocabulary, like every other reason space here. Each names one
# condition, and exactly one is ever reported: the first that failed.

#: A helper or read-back the adapter needs is not in the state machine.
INHIBIT_MISSING_CONTROL_ENTITY: Final = "missing_control_entity"
#: It exists, but is unavailable or unknown.
INHIBIT_CONTROL_ENTITY_UNAVAILABLE: Final = "control_entity_unavailable"
#: The control surface's own restart and communication-loss reset is absent or
#: switched off. Without it an interrupted sequence could outlive Alpha EMS.
INHIBIT_NO_FAILSAFE_AUTOMATION: Final = "no_failsafe_automation"
#: Another feature of the control surface is driving the battery. Alpha EMS
#: stands down rather than switching a user's feature off behind their back.
INHIBIT_EXCESS_EXPORT_ACTIVE: Final = "excess_export_active"
INHIBIT_PEAK_SHAVING_ACTIVE: Final = "peak_shaving_active"
#: A dispatch is running. Alpha EMS cannot prove it created it, so it belongs to
#: someone else and is neither modified nor cancelled.
INHIBIT_DISPATCH_ACTIVE: Final = "dispatch_active"
#: The battery hardware facts are incomplete.
INHIBIT_BATTERY_NOT_CONFIGURED: Final = "battery_not_configured"
#: No plan, an unusable plan, or a plan that reached no decision.
INHIBIT_NO_PLAN: Final = "no_plan"
INHIBIT_PLAN_UNAVAILABLE: Final = "plan_unavailable"
INHIBIT_NO_DECISION: Final = "no_decision"
#: The plan describes a different interval, a different day, or is too old.
INHIBIT_STALE_PLAN_INTERVAL: Final = "stale_plan_interval"
INHIBIT_STALE_PLAN_DAY: Final = "stale_plan_day"
INHIBIT_STALE_PLAN_AGE: Final = "stale_plan_age"
#: A reading Phase 4 depends on is unusable, or too old to act on.
INHIBIT_SOC_UNUSABLE: Final = "soc_unusable"
INHIBIT_SOC_STALE: Final = "soc_stale"
INHIBIT_BATTERY_POWER_UNUSABLE: Final = "battery_power_unusable"
INHIBIT_BATTERY_POWER_STALE: Final = "battery_power_stale"
INHIBIT_HOUSE_LOAD_UNUSABLE: Final = "house_load_unusable"
INHIBIT_HOUSE_LOAD_STALE: Final = "house_load_stale"
#: A discharge was recommended with nothing available above the floor.
INHIBIT_AT_OR_BELOW_FLOOR: Final = "at_or_below_floor"
#: The command does not fit the device contract.
INHIBIT_POWER_BELOW_DEVICE_MINIMUM: Final = "power_below_device_minimum"
INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM: Final = "power_above_device_maximum"
INHIBIT_CUTOFF_OUT_OF_RANGE: Final = "cutoff_out_of_range"
INHIBIT_DURATION_OUT_OF_RANGE: Final = "duration_out_of_range"
#: The grid meter could not be read, or read too long ago to be evidence about
#: now. The absorbing capacity a discharge is checked against is measured at the
#: meter, so without it there is no bound and the command is refused.
INHIBIT_GRID_UNUSABLE: Final = "grid_unusable"
INHIBIT_GRID_STALE: Final = "grid_stale"
#: The commanded discharge would push energy onto the grid. The gate refuses the
#: whole command; it never reduces it to fit, because a gate that scales a
#: request has made a decision of its own.
INHIBIT_WOULD_EXPORT: Final = "would_export"

CONTROL_INHIBIT_REASONS: Final = (
    INHIBIT_MISSING_CONTROL_ENTITY,
    INHIBIT_CONTROL_ENTITY_UNAVAILABLE,
    INHIBIT_NO_FAILSAFE_AUTOMATION,
    INHIBIT_EXCESS_EXPORT_ACTIVE,
    INHIBIT_PEAK_SHAVING_ACTIVE,
    INHIBIT_DISPATCH_ACTIVE,
    INHIBIT_BATTERY_NOT_CONFIGURED,
    INHIBIT_NO_PLAN,
    INHIBIT_PLAN_UNAVAILABLE,
    INHIBIT_NO_DECISION,
    INHIBIT_STALE_PLAN_INTERVAL,
    INHIBIT_STALE_PLAN_DAY,
    INHIBIT_STALE_PLAN_AGE,
    INHIBIT_SOC_UNUSABLE,
    INHIBIT_SOC_STALE,
    INHIBIT_BATTERY_POWER_UNUSABLE,
    INHIBIT_BATTERY_POWER_STALE,
    INHIBIT_HOUSE_LOAD_UNUSABLE,
    INHIBIT_HOUSE_LOAD_STALE,
    INHIBIT_AT_OR_BELOW_FLOOR,
    INHIBIT_POWER_BELOW_DEVICE_MINIMUM,
    INHIBIT_POWER_ABOVE_DEVICE_MAXIMUM,
    INHIBIT_CUTOFF_OUT_OF_RANGE,
    INHIBIT_DURATION_OUT_OF_RANGE,
    INHIBIT_GRID_UNUSABLE,
    INHIBIT_GRID_STALE,
    INHIBIT_WOULD_EXPORT,
)

# --- Phase 4: why execution was not authorized --------------------------------
#
# Separate from the inhibit vocabulary above, and separate on purpose: a hazard
# and a permission are different facts. The safety gate answers the first and
# knows nothing about the control mode; this answers the second and knows
# nothing about hazards beyond whether the gate passed.

#: The safety gate refused. Carries the gate's own reason alongside.
REFUSE_UNSAFE: Final = "unsafe"
#: The user has not selected active control.
REFUSE_MODE_NOT_ACTIVE: Final = "mode_not_active"
#: Active is selected, but execution has not been enabled.
REFUSE_EXECUTION_NOT_ENABLED: Final = "execution_not_enabled"
#: This release cannot execute at all. See ``CONTROL_EXECUTION_AVAILABLE``.
REFUSE_EXECUTION_UNAVAILABLE: Final = "execution_unavailable"
#: A previous command is too recent for this one to start or increase it.
REFUSE_COOLDOWN: Final = "cooldown"
#: There was nothing to send.
REFUSE_NO_COMMANDS: Final = "no_commands"

CONTROL_REFUSAL_REASONS: Final = (
    REFUSE_UNSAFE,
    REFUSE_MODE_NOT_ACTIVE,
    REFUSE_EXECUTION_NOT_ENABLED,
    REFUSE_EXECUTION_UNAVAILABLE,
    REFUSE_COOLDOWN,
    REFUSE_NO_COMMANDS,
)

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

# Phase 4. Two, and only two: one control the user sets, and one state that
# says what the control pipeline did and why. Every command, capability check,
# gate condition, residual and read-back value stays in attributes and
# diagnostics -- a gate with twenty-five inhibit reasons must not become
# twenty-five entities.
SELECT_CONTROL_MODE: Final = "control_mode"
SENSOR_CONTROL_STATE: Final = "control_state"

# Phase 3. Three, and only three: everything else the decision layer computes --
# the reduced trajectory, the per-band split, the binding-constraint tally, the
# what-if comparison and the PV-blind projection -- is diagnostics-only.
SENSOR_BATTERY_RECOMMENDATION: Final = "battery_recommendation"
SENSOR_BATTERY_PLANNED_POWER: Final = "battery_planned_power"
SENSOR_BATTERY_USABLE_ENERGY: Final = "battery_usable_energy"

# --- Logging ------------------------------------------------------------------

#: Minimum seconds between two repetitions of the same aggregated warning.
LOG_THROTTLE_SECONDS: Final = 3600

# --- Phase 5: photovoltaic forecast -------------------------------------------
#
# The forecast is read from a Solcast integration the user has already installed
# and configured. Alpha EMS never talks to any network service: it calls two
# read-only Home Assistant actions and consumes their responses. Everything below
# is either the vocabulary of that boundary, or the vocabulary of the evidence
# recorded on the other side of it.

#: Which rooftop sites in a Solcast account belong to *this* AlphaESS
#: installation. Stable ``resource_id`` values, never display names: a site can be
#: renamed and must remain the same site.
#:
#: This is the one configuration key Phase 5 adds, and it exists because nothing
#: already stored can express it. A Solcast account may hold a second property or
#: a neighbour's array, and consuming the aggregate unconditionally folds those
#: silently into the plan. The user is asked exactly one question -- which sites
#: belong here -- and is never asked to classify a site as AC- or DC-coupled,
#: hybrid-side or grid-inverter-side. That correspondence is not reliably known to
#: a user, and a guessed topology in provenance is worse than a declared unknown.
CONF_SELECTED_SOLCAST_SITE_IDS: Final = "selected_solcast_site_ids"

#: Sentinel identifier for the aggregate series, which is what Solcast returns
#: when no site is named. Deliberately not a plausible ``resource_id``.
PV_AGGREGATE_SITE: Final = "__aggregate__"

#: Whether the stored selection was resolved automatically on first discovery, or
#: chosen by the user. A default and a decision are different facts.
PV_SELECTION_ORIGIN_AUTO: Final = "auto_initial"
PV_SELECTION_ORIGIN_USER: Final = "user"

#: Which query produced a series. Load-bearing rather than tidy: if Solcast's own
#: aggregation ever disagreed with a per-site sum, this is what makes a stored
#: snapshot attributable to one or the other.
PV_QUERY_MODE_AGGREGATE: Final = "aggregate"
PV_QUERY_MODE_PER_SITE: Final = "per_site"

#: How the percentile bands were combined.
#:
#: ``comonotonic_sum`` is the honest name for adding each site's tenth-percentile
#: outcome together: it assumes every site has its bad day simultaneously, which
#: is a *more* conservative bound than the true joint tenth percentile and is
#: therefore safe to compute -- but it is not a calibrated interval. A later
#: reserve calculation that treated it as one would size against a scenario far
#: rarer than one day in ten. The label costs one string and stops that mistake at
#: the source.
PV_PERCENTILE_SOURCE_AGGREGATE: Final = "source_aggregate"
PV_PERCENTILE_COMONOTONIC_SUM: Final = "comonotonic_sum"

#: Bumped when the thirty-to-fifteen-minute mapping rule itself changes, so
#: evidence recorded under an older rule is identifiable rather than pooled.
PV_MAPPING_VERSION: Final = 1

#: The electrical boundary of the *actual* PV figure on this class of
#: installation: a sum of DC string power and an AC meter. Declared rather than
#: corrected. Forecast-versus-actual therefore carries a structural conversion
#: difference which is physics, not forecast error, and Phase 9 must be told so
#: rather than learning a correction for it.
PV_ACTUAL_BOUNDARY_MIXED: Final = "mixed_dc_strings_and_ac_meter"
#: Solcast does not document whether its estimate sits at the AC or the DC
#: boundary. Recording the ambiguity is more honest than picking one.
PV_FORECAST_BOUNDARY_UNSPECIFIED: Final = "unspecified"
#: Which selected site corresponds to which AlphaESS subsystem. Solcast partitions
#: a roof by orientation and AlphaESS partitions it by electrical coupling, so
#: equal site counts prove nothing. Never guessed, and never asked.
PV_ELECTRICAL_CORRESPONDENCE_UNKNOWN: Final = "unknown"

#: Why no usable PV forecast could be produced. Never an empty series presented as
#: a forecast of zero.
PV_UNAVAILABLE_NOT_CONFIGURED: Final = "pv_forecast_not_enabled"
PV_UNAVAILABLE_NO_SOLCAST_ENTRY: Final = "solcast_entry_not_selected"
PV_UNAVAILABLE_ENTRY_NOT_LOADED: Final = "solcast_entry_not_loaded"
PV_UNAVAILABLE_SERVICE_MISSING: Final = "solcast_query_service_missing"
PV_UNAVAILABLE_SERVICE_FAILED: Final = "solcast_query_failed"
PV_UNAVAILABLE_NO_SITES_DISCOVERED: Final = "no_solcast_sites_discovered"
PV_UNAVAILABLE_EMPTY_SELECTION: Final = "no_solcast_site_selected"
PV_UNAVAILABLE_NO_ROWS: Final = "solcast_returned_no_rows"
PV_UNAVAILABLE_UNUSABLE_ROWS: Final = "solcast_rows_unusable"
PV_UNAVAILABLE_PERIOD_REFUSED: Final = "source_period_not_a_multiple_of_fifteen"

PV_UNAVAILABLE_REASONS: Final = (
    PV_UNAVAILABLE_NOT_CONFIGURED,
    PV_UNAVAILABLE_NO_SOLCAST_ENTRY,
    PV_UNAVAILABLE_ENTRY_NOT_LOADED,
    PV_UNAVAILABLE_SERVICE_MISSING,
    PV_UNAVAILABLE_SERVICE_FAILED,
    PV_UNAVAILABLE_NO_SITES_DISCOVERED,
    PV_UNAVAILABLE_EMPTY_SELECTION,
    PV_UNAVAILABLE_NO_ROWS,
    PV_UNAVAILABLE_UNUSABLE_ROWS,
    PV_UNAVAILABLE_PERIOD_REFUSED,
)

#: Per-interval comparability of a forecast against what was measured. Each case
#: has its own code rather than collapsing into one residual, because telling them
#: apart is Phase 9's whole job.
PV_STATUS_VALID: Final = "valid"
PV_STATUS_FORECAST_MISSING: Final = "forecast_missing"
PV_STATUS_ACTUAL_MISSING: Final = "actual_missing"
PV_STATUS_NOT_ELAPSED: Final = "not_elapsed"
PV_STATUS_NIGHT: Final = "night"
PV_STATUS_PV_BLIND: Final = "pv_blind"
PV_STATUS_PARTIAL_SITES: Final = "partial_sites"
PV_STATUS_PROVENANCE_CHANGED: Final = "provenance_changed"

PV_INTERVAL_STATUSES: Final = (
    PV_STATUS_VALID,
    PV_STATUS_FORECAST_MISSING,
    PV_STATUS_ACTUAL_MISSING,
    PV_STATUS_NOT_ELAPSED,
    PV_STATUS_NIGHT,
    PV_STATUS_PV_BLIND,
    PV_STATUS_PARTIAL_SITES,
    PV_STATUS_PROVENANCE_CHANGED,
)

#: Day-level flags. ``selected_sites_changed`` is a hard barrier -- evidence
#: either side of it is never pooled -- while ``available_sites_changed`` is
#: informational, because a site appearing in Solcast without joining the
#: selection changes nothing about what was forecast.
PV_FLAG_SELECTED_SITES_CHANGED: Final = "selected_sites_changed"
PV_FLAG_SELECTED_MODEL_CHANGED: Final = "selected_model_changed"
PV_FLAG_AVAILABLE_SITES_CHANGED: Final = "available_sites_changed"
#: Dampening, auto-dampening and actuals blending are three ways Solcast can alter
#: its own output. One flag, because the consequence is identical: evidence either
#: side of the change is not poolable.
PV_FLAG_SOURCE_CORRECTION_CHANGED: Final = "source_correction_changed"
PV_FLAG_TIMEZONE_CHANGED: Final = "timezone_changed"
PV_FLAG_SHAPE_MISMATCH: Final = "shape_mismatch"
#: The measured figure sat at a plateau near the inverter's AC limit while the
#: forecast kept climbing. Raised, never corrected: on a big day forecast exceeds
#: actual by design, and Phase 9 must not learn a correction for physics.
PV_FLAG_CLIPPING_SUSPECTED: Final = "clipping_suspected"

PV_DAY_FLAGS: Final = (
    PV_FLAG_SELECTED_SITES_CHANGED,
    PV_FLAG_SELECTED_MODEL_CHANGED,
    PV_FLAG_AVAILABLE_SITES_CHANGED,
    PV_FLAG_SOURCE_CORRECTION_CHANGED,
    PV_FLAG_TIMEZONE_CHANGED,
    PV_FLAG_SHAPE_MISMATCH,
    PV_FLAG_CLIPPING_SUSPECTED,
)

#: The Solcast domain, and the only two of its actions Alpha EMS may ever call.
#: Both are registered response-only, both read cached data, and neither consumes
#: the account's API allowance.
SOLCAST_DOMAIN: Final = "solcast_solar"
SOLCAST_SERVICE_QUERY_FORECAST: Final = "query_forecast_data"
SOLCAST_SERVICE_DIAGNOSTIC: Final = "diagnostic"
SOLCAST_PERMITTED_SERVICES: Final = (
    SOLCAST_SERVICE_QUERY_FORECAST,
    SOLCAST_SERVICE_DIAGNOSTIC,
)

#: Every mutating Solcast action, named here so a structural test can prove none
#: of them appears anywhere in this package. The same technique as the
#: flash-backed helper deny-list in Phase 4: naming the forbidden thing once, in
#: one place, is what lets a test assert it is named nowhere else.
SOLCAST_FORBIDDEN_SERVICES: Final = (
    "update_forecasts",
    "force_update_forecasts",
    "force_update_estimates",
    "clear_all_solcast_data",
    "set_options",
    "set_dampening",
    "set_hard_limit",
    "remove_hard_limit",
)

#: The source period must be a whole number of planning intervals. Fifteen
#: minutes is not a tolerance to be widened; it is the resolution the whole
#: project is built on.
PV_SOURCE_PERIOD_STEP_MINUTES: Final = QUARTER_MINUTES
