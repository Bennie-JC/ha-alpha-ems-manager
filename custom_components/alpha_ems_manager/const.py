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
#: * **2.4** -- v1.0.0-beta.14. Day records gained optional per-interval measured
#:   grid import and export arrays, on exactly the same terms again. They exist
#:   because **what an economic plan actually cost is irrecoverable afterwards**:
#:   Phase 8 can compute what a plan should cost from prices it holds, but the
#:   realised flows exist nowhere else, and every day without them is a day whose
#:   economics can never be reconstructed. Read by nothing that decides anything,
#:   including the optimizer -- an optimizer learning from its own recorded
#:   outcomes would be Phase 9 wearing Phase 8's clothes.
#: * **2.5** -- v1.0.0-beta.19. The document gained an optional ``execution``
#:   key beside ``days``: the published revision of each execution target, and
#:   the causal record of an armed dispatch. Both exist because a restart must not
#:   lose them. A revision that reset to one on every reboot would tell Stage B
#:   that every target was brand new, and a causal record that did not survive a
#:   reboot would make an owned dispatch indistinguishable from a stranger's --
#:   which is the one situation where the safe action is to touch nothing.
#:   Absent on every earlier document, and read as "nothing was running" rather
#:   than as a claim.
#:
#:   v1.0.0-beta.20 keys that causal record on the Stage-B **run** rather than on
#:   the Stage-A publication, and the minor version deliberately does not move:
#:   beta.19 defined the record but never wrote one, so no stored document in
#:   existence contains the older shape. There is nothing to migrate, and bumping
#:   the version would claim a compatibility boundary that was never crossed.
#:
#:   v1.0.0-beta.24 adds the admitted window and the battery target to that
#:   record, because a restart that finds a dispatch still running has to be able
#:   to reconstruct the run it belongs to rather than mint a competing one. This
#:   time the minor version *does* move: beta.24 is the first release that writes
#:   records at all, but a document written by a beta.24 build and read by a later
#:   one must be distinguishable from one that predates the fields. A record
#:   without them is read as insufficient evidence, which is a defined state
#:   rather than an error -- see the restart rule in ``coordinator``.
STORAGE_MINOR_VERSION: Final = 6
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
#: * **1.3** -- v1.0.0-beta.12. Partitions gained ``prs``, the price issuances,
#:   and index rows gained ``prfp``, their fingerprints. A document without
#:   either reads unchanged.
#: * **1.4** -- v1.0.0-beta.13. Partitions gained ``rsv``, the reserve
#:   requirements, and index rows gained ``rsvfp``, their fingerprints. A
#:   document without either reads unchanged, and an installation without
#:   battery planning writes neither.
#: * **1.5** -- v1.0.0-beta.14. Partitions gained ``eco``, the economic plans,
#:   and index rows gained ``ecofp``, their fingerprints. Keyed on the plan's
#:   *inputs* rather than on the plan, because the plan itself moves every
#:   quarter-hour. A document without either reads unchanged, and an installation
#:   without prices writes neither.
#:
#: * **1.6** -- v1.0.0-beta.16. Economic snapshots gained
#:   ``tpc``/``tpi`` (what the terminal condition cost, so its effect can be
#:   judged from live evidence rather than argued) and ``dc`` (how many battery
#:   direction changes the plan actually made, which the run count over-states).
#:   Additive: a document without them reads unchanged.
#:
#: * **1.7** -- v1.0.0-beta.17. The two terminal-condition scalars were renamed
#:   to say what they are: ``tpc``/``tpi`` became ``tplc``/``tpli`` (a
#:   *whole-horizon plan* difference, not realised money), and ``tfrc`` records
#:   whether the bound changed the first run -- the only part of it a reader can
#:   act on. The reader accepts the old keys, so a beta.16 document loads with
#:   every figure intact.
#:
#:   Also gained ``br``/``mrc``/``mrd``: which rule chose the state-space lattice
#:   and the peak power it can represent in each direction. beta.17 selects the
#:   bucket per installation rather than fixing it, so an economic figure recorded
#:   before the upgrade and one recorded after can legitimately differ by up to
#:   one bucket -- and these three make that explicable from the document rather
#:   than by recomputing a lattice whose inputs may since have changed. Documents
#:   written earlier simply lack them; they are **not** back-filled, because
#:   making old and new figures look continuous would be a fabrication.
#:
#: One honest note for anyone diffing stored documents: photovoltaic snapshots
#: (``pvs``/``pvo``) arrived in v1.0.0-beta.9 **without** a minor bump, so a
#: document stamped 1.2 may or may not carry them. The reader tolerates both,
#: which is why that omission is recorded here rather than papered over by
#: restamping documents that were written correctly.
FORECAST_STORAGE_MINOR_VERSION: Final = 7

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

#: Which battery actions this release may physically execute.
#:
#: **The release barrier, and it is a set rather than a flag.** Until beta.24 it
#: was a boolean, and tracing what flipping it would actually permit is what
#: forced this shape: the command source falls back to the Phase-3 reserve guard
#: whenever Stage B has no charge intent, ``authorize`` never looked at the
#: direction, and ``write_refusal`` only checks a command against *its own*
#: family. So a single ``True`` would have authorised reserve-guard **discharges**
#: on the first refresh with no charge to make -- which is not what "enable
#: execution" was ever meant to mean.
#:
#: A set says the true thing instead. beta.24 permits exactly one action, and
#: everything else -- discharge, export, curtailment, the reserve guard -- is
#: refused by the same mechanism that permits the charge, rather than by an
#: upstream accident that a later edit could undo.
#:
#: ``frozenset()`` reproduces every release up to beta.23 exactly, so "executes
#: nothing" remains representable, and the barrier cannot be half-opened.
CONTROL_EXECUTABLE_ACTIONS: Final = frozenset({ACTION_CHARGE})

#: Whether real execution is permitted to leave this release at all.
#:
#: **Derived, so it can never disagree with the set above.** Every reader of this
#: constant asks the same question -- "does this release send anything?" -- and
#: keeps working unchanged. Which action it may send is a separate question with a
#: separate answer, and conflating them is what made the old boolean dangerous.
CONTROL_EXECUTION_AVAILABLE: Final = bool(CONTROL_EXECUTABLE_ACTIONS)

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
#: A command was authorized and sent. Added in beta.19 so the vocabulary is in
#: place before anything can reach this state -- ``CONTROL_EXECUTION_AVAILABLE``
#: is still false, so in this release it is unreachable by construction, exactly
#: as ``started`` is on the Activity surface.
CONTROL_STATE_EXECUTED: Final = "executed"
#: A command was sent and could not be confirmed, or the device refused it.
#:
#: **beta.31, and it names a runtime meaning the sensor previously hid.** The
#: staged-write path already marks the report with an execution error and returns
#: -- but the state it left behind was whatever eligibility had computed before
#: the write was attempted, so a failed command published as ``eligible`` or
#: ``inhibited``. A reader watching the entity could not tell a refresh that sent
#: nothing from one whose command failed, which is the single most important
#: distinction the entity can carry.
CONTROL_STATE_ERROR: Final = "error"
CONTROL_STATE_OFF: Final = "off"
CONTROL_STATE_INHIBITED: Final = "inhibited"
CONTROL_STATE_ELIGIBLE: Final = "eligible"
CONTROL_STATE_IDLE: Final = "idle"

CONTROL_STATE_OPTIONS: Final = (
    CONTROL_STATE_OFF,
    CONTROL_STATE_INHIBITED,
    CONTROL_STATE_ELIGIBLE,
    CONTROL_STATE_IDLE,
    # Declared, and unreachable in this release. Declaring it now means beta.20
    # changes a barrier rather than an entity's enumeration, and a dashboard built
    # against beta.19 does not acquire a new state it has never seen.
    CONTROL_STATE_EXECUTED,
    # beta.31. **Additive**: every value an automation could already be matching
    # on still exists and still means what it meant, so nothing built against
    # beta.30 breaks. What changes is that a failed write stops being reported as
    # though it were an eligibility outcome.
    CONTROL_STATE_ERROR,
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
#: The action is outside :data:`CONTROL_EXECUTABLE_ACTIONS`.
#:
#: Not a hazard and not a mode problem: the command is well-formed and may be
#: perfectly safe, and this release simply does not execute that direction. Named
#: for what a reader needs to know rather than for the constant that decided it.
REFUSE_LIVE_ACTION_NOT_PERMITTED: Final = "live_direction_not_permitted"
#: A reset was asked for a dispatch Alpha EMS cannot prove it owns.
#:
#: The whole entitlement of the stop path, and the reason it can afford to ignore
#: the mode, the opt-in and the safety verdict: it is gated on the strongest fact in
#: the system instead of on the weakest three.
REFUSE_RESET_NOT_OWNED: Final = "reset_not_owned"
#: A reset was asked with no stop condition behind it.
#:
#: A reset is a response to something. Without a reason it is a write looking for a
#: justification, which is the shape of every accident this project is built to
#: avoid.
REFUSE_RESET_WITHOUT_REASON: Final = "reset_without_reason"
#: The owned run's action could not be established from the persisted record.
#:
#: Fails closed: no reset is planned and the device dead-man ends the dispatch. A
#: missing action is never defaulted to a charge -- guessing what to stop is how a
#: stop becomes a start in the other direction.
REFUSE_RESET_ACTION_UNKNOWN: Final = "reset_action_unknown"
#: A marker release was asked while a dispatch is still running behind it.
#:
#: Releasing it there would assert an ownership conclusion nobody has, and leave a
#: dispatch nothing can later prove or stop.
REFUSE_MARKER_STILL_DISPATCHING: Final = "marker_still_dispatching"

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

# Phase 7. One, and only one. The counterfactuals beside it -- the
# same-interval-only requirement, the PV-blind requirement, the peak and the two
# dependency figures -- are diagnostics, because a user acts on the requirement
# and reads the rest only when asking why it moved. Its state is an **energy**:
# the state of charge it implies is derived and travels as an attribute, matching
# the rest of the project, where energy is the conserved quantity and a
# percentage is a reading of it.
SENSOR_DYNAMIC_RESERVE: Final = "dynamic_battery_reserve"

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

#: How the selection reaching this refresh was arrived at.
#:
#: ``auto_initial`` is the refresh that resolved it from discovery and wrote it
#: down. ``stored`` is every refresh after that, which read it from the entry.
#:
#: Deliberately *not* "user", which was the first attempt. Once the resolved
#: default has been persisted it is indistinguishable from a set the user chose
#: by hand, because both are simply a list in the options -- so calling it a user
#: decision would have labelled the first snapshot of every installation as a
#: choice nobody made. Telling the two apart would need a second stored field,
#: which is more configuration than the distinction is worth. This records what is
#: actually known.
PV_SELECTION_ORIGIN_AUTO: Final = "auto_initial"
PV_SELECTION_ORIGIN_STORED: Final = "stored"

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
#: The stored entry id names no config entry that exists. Provable, unlike the
#: entry *state* check this replaces.
#:
#: beta.9 asked whether the Solcast config entry was in state ``LOADED`` and
#: refused to read the source when it was not. That produced a live false
#: negative on every Home Assistant restart: Solcast registers its actions at
#: component level, so both appear registered while its config entry is still
#: setting up -- and Alpha EMS takes its first refresh during its own setup,
#: which can win that race. The result was a capability snapshot reporting both
#: actions present and the entry "not loaded", held until the next quarter-hour
#: boundary.
#:
#: The state was never needed. Calling a registered action is safe by
#: definition, and a failure is already caught and reported as
#: ``PV_UNAVAILABLE_SERVICE_FAILED``. So capability is now established from facts
#: that can be demonstrated: an entry is selected, that entry exists, and the
#: actions are registered.
PV_UNAVAILABLE_ENTRY_NOT_FOUND: Final = "solcast_entry_not_found"
PV_UNAVAILABLE_SERVICE_MISSING: Final = "solcast_query_service_missing"
PV_UNAVAILABLE_DIAGNOSTIC_MISSING: Final = "solcast_diagnostic_service_missing"
PV_UNAVAILABLE_SERVICE_FAILED: Final = "solcast_query_failed"
PV_UNAVAILABLE_NO_SITES_DISCOVERED: Final = "no_solcast_sites_discovered"
PV_UNAVAILABLE_EMPTY_SELECTION: Final = "no_solcast_site_selected"
PV_UNAVAILABLE_NO_ROWS: Final = "solcast_returned_no_rows"
PV_UNAVAILABLE_UNUSABLE_ROWS: Final = "solcast_rows_unusable"
PV_UNAVAILABLE_PERIOD_REFUSED: Final = "source_period_not_a_multiple_of_fifteen"

PV_UNAVAILABLE_REASONS: Final = (
    PV_UNAVAILABLE_NOT_CONFIGURED,
    PV_UNAVAILABLE_NO_SOLCAST_ENTRY,
    PV_UNAVAILABLE_ENTRY_NOT_FOUND,
    PV_UNAVAILABLE_SERVICE_MISSING,
    PV_UNAVAILABLE_DIAGNOSTIC_MISSING,
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

#: Why surplus production is or is not modelled as entering the battery.
#:
#: The approved Phase-5 design treated autonomous absorption as unconditional
#: physics. The vendor control surface contradicts that in its own design notes:
#: with Excess Export on, PV below the inverter's AC limit goes to house load and
#: feed-in and the battery is charged with zero. So it is predicated on observable
#: state, and the reason is recorded rather than assumed.
PV_ABSORPTION_SELF_CONSUMPTION: Final = "self_consumption"
PV_ABSORPTION_NO_SUPPRESSING_FEATURE: Final = "no_suppressing_feature_present"
PV_ABSORPTION_EXCESS_EXPORT: Final = "excess_export_active"
PV_ABSORPTION_PEAK_SHAVING: Final = "peak_shaving_active"
PV_ABSORPTION_DISPATCH_ACTIVE: Final = "dispatch_active"
PV_ABSORPTION_STATE_UNREADABLE: Final = "device_state_unreadable"

PV_ABSORPTION_REASONS: Final = (
    PV_ABSORPTION_SELF_CONSUMPTION,
    PV_ABSORPTION_NO_SUPPRESSING_FEATURE,
    PV_ABSORPTION_EXCESS_EXPORT,
    PV_ABSORPTION_PEAK_SHAVING,
    PV_ABSORPTION_DISPATCH_ACTIVE,
    PV_ABSORPTION_STATE_UNREADABLE,
)

#: The vendor helper that carries the inverter's configured AC limit, read for
#: one purpose only: telling a clipped day from an over-forecast one. On a big day
#: the forecast exceeds the actual by design, because an inverter cannot pass more
#: than its limit however bright it is, and Phase 9 must not learn a correction
#: for that. Absent means the detection is suppressed rather than a ceiling
#: guessed -- a guessed limit would produce a flag that looked like evidence.
SELECT_INVERTER_AC_LIMIT: Final = "input_select.alphaess_helper_inverter_ac_limit"

#: Which shape an action response arrived in.
#:
#: Both Solcast actions wrap their result: the forecast query returns a list under
#: ``data`` and the diagnostic returns a mapping under it. beta.10 unwrapped the
#: first and read the second at the top level, so every field came back absent and
#: the PV layer reported no discovered sites on an account that had two. Recording
#: the shape means a future change of convention shows up as a named fact rather
#: than as everything being empty at once.
RESPONSE_SHAPE_NESTED: Final = "nested_under_data"
RESPONSE_SHAPE_FLAT: Final = "flat"
RESPONSE_SHAPE_UNUSABLE: Final = "unusable"

# --- Phase 6: quarter-hour electricity prices ---------------------------------
#
# Read from a Frank Quarter Prices integration the user has already installed and
# selected. Alpha EMS never talks to any network service and never asks Frank to
# fetch: it reads published entity state. Frank owns fetching, retry, caching,
# tomorrow publication and the midnight rollover.
#
# Phase 6 *knows* prices. It decides nothing because of them. No price value
# reaches the decision layer -- a structural guard asserts no identifier in
# ``simulation`` or ``policy`` contains a price term, and it passes unchanged.

#: The Frank domain, and the entity keys Alpha EMS resolves.
#:
#: Frank builds every unique id as ``f"{entry_id}_{key}"`` and documents that
#: pattern as a deliberate stable contract. Resolving through the entity registry
#: by unique id therefore isolates the *selected* entry by construction -- two
#: Frank entries can never be combined -- and survives a user renaming the
#: entity, which hard-coding ``sensor.frank_prices_today`` would not.
FRANK_KEY_PRICES_TODAY: Final = "prices_today"
FRANK_KEY_PRICES_TOMORROW: Final = "prices_tomorrow"
FRANK_KEY_TOMORROW_AVAILABLE: Final = "tomorrow_prices_available"
FRANK_KEY_CURRENT_PRICE: Final = "current_price"
FRANK_KEY_CURRENT_RETURN_PRICE: Final = "current_return_price"

#: The two Frank options the export reconstruction depends on. Read from the
#: *Frank* entry, never duplicated as Alpha EMS settings: the user configured
#: them once and the return-price sensor they can see is derived from them.
FRANK_OPTION_FEED_IN_ADJUSTMENT: Final = "feed_in_adjustment"
FRANK_OPTION_APPLY_FEED_IN_VAT: Final = "apply_feed_in_vat"
FRANK_DEFAULT_FEED_IN_ADJUSTMENT: Final = 0.0
FRANK_DEFAULT_APPLY_FEED_IN_VAT: Final = False
#: Frank's VAT rate, and the precision it rounds its computed feed-in price to.
#: Mirrored rather than chosen, because the point is to agree with the sensor the
#: user is looking at.
FRANK_VAT_RATE: Final = 0.21
FRANK_PRICE_PRECISION: Final = 6

#: Market timezone per country. Frank publishes a *market* day -- midnight to
#: midnight in the market's own zone -- and deliberately refuses to use Home
#: Assistant's. Recorded as provenance only: it never decides availability, and
#: Alpha EMS does not reimplement Frank's publication scheduler.
FRANK_MARKET_TIMEZONES: Final[dict[str, str]] = {
    "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels",
}

#: Every mutating or fetch-triggering Frank action, named once so a structural
#: test can prove none appears anywhere in this package. Alpha EMS adds no
#: service caller at all in Phase 6, so this is belt-and-braces over a property
#: that is already structural.
FRANK_FORBIDDEN_SERVICES: Final = (
    "update_prices",
    "force_update",
    "refresh",
    "fetch_prices",
    "clear_cache",
    "set_options",
)

#: Why no usable price series could be produced.
#:
#: ``PRICE_TOMORROW_NOT_PUBLISHED`` is **normal operation**, not a failure. Frank
#: does not request tomorrow before noon market time and publishes it around
#: 13:00-14:00, so between market midnight and publication the tomorrow entity is
#: legitimately unavailable. Treating that as a source fault would mark a healthy
#: installation degraded for half of every day.
PRICE_UNAVAILABLE_NOT_CONFIGURED: Final = "frank_entry_not_selected"
PRICE_UNAVAILABLE_ENTRY_NOT_FOUND: Final = "frank_entry_not_found"
PRICE_UNAVAILABLE_ENTITY_MISSING: Final = "frank_prices_entity_missing"
PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE: Final = "frank_prices_unavailable"
PRICE_UNAVAILABLE_ATTRIBUTE_UNUSABLE: Final = "frank_prices_attribute_unusable"
PRICE_UNAVAILABLE_EMPTY: Final = "frank_prices_empty"
PRICE_TOMORROW_NOT_PUBLISHED: Final = "frank_tomorrow_not_published"
PRICE_UNAVAILABLE_UNUSABLE_ROWS: Final = "frank_prices_rows_unusable"
PRICE_UNAVAILABLE_OPTIONS_UNREADABLE: Final = "frank_options_unreadable"

PRICE_UNAVAILABLE_REASONS: Final = (
    PRICE_UNAVAILABLE_NOT_CONFIGURED,
    PRICE_UNAVAILABLE_ENTRY_NOT_FOUND,
    PRICE_UNAVAILABLE_ENTITY_MISSING,
    PRICE_UNAVAILABLE_SOURCE_UNAVAILABLE,
    PRICE_UNAVAILABLE_ATTRIBUTE_UNUSABLE,
    PRICE_UNAVAILABLE_EMPTY,
    PRICE_TOMORROW_NOT_PUBLISHED,
    PRICE_UNAVAILABLE_UNUSABLE_ROWS,
    PRICE_UNAVAILABLE_OPTIONS_UNREADABLE,
)

#: How the export price for an interval was arrived at. Frank's own vocabulary,
#: mirrored verbatim, because the export price is a *reconstruction* of a value
#: Frank's upstream does not publish -- and a configuration-derived estimate must
#: never be mistaken for a measured price.
PRICE_EXPORT_BASIS_API_FIELD: Final = "api_feed_in_price"
PRICE_EXPORT_BASIS_ADJUSTMENT: Final = "market_price_plus_adjustment"
PRICE_EXPORT_BASIS_ADJUSTMENT_VAT: Final = "market_price_plus_adjustment_including_vat"
PRICE_EXPORT_BASIS_UNKNOWN: Final = "unavailable"

#: Optional explicit feed-in field. Absent from every block the live capture
#: observed, and absent from Frank's pinned key set -- kept because Frank honours
#: it if its upstream ever publishes one, and dropping the branch would silently
#: prefer a reconstruction over a real figure.
FRANK_FIELD_FEED_IN_PRICE: Final = "feed_in_price"

#: Day-level flags on stored price evidence.
#:
#: ``vat_ratio_unexpected`` is the observation that replaced an assumption. The
#: 21 % relation between ``market_price_tax`` and ``market_price`` held on every
#: captured block, but it is VAT legislation rather than arithmetic -- so it is
#: checked and flagged, never used to derive a stored value away.
PRICE_FLAG_VAT_RATIO_UNEXPECTED: Final = "vat_ratio_unexpected"
PRICE_FLAG_COMPONENTS_VARIED: Final = "day_components_varied"
PRICE_FLAG_RESOLUTION_DISAGREES: Final = "reported_resolution_disagrees"
PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED: Final = "import_cross_check_failed"
PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED: Final = "export_cross_check_failed"
PRICE_FLAG_SOURCE_CHANGED: Final = "price_source_changed"

PRICE_DAY_FLAGS: Final = (
    PRICE_FLAG_VAT_RATIO_UNEXPECTED,
    PRICE_FLAG_COMPONENTS_VARIED,
    PRICE_FLAG_RESOLUTION_DISAGREES,
    PRICE_FLAG_IMPORT_CROSS_CHECK_FAILED,
    PRICE_FLAG_EXPORT_CROSS_CHECK_FAILED,
    PRICE_FLAG_SOURCE_CHANGED,
)

#: Tolerance for the two live cross-checks, in EUR/kWh. One tenth of the source's
#: own least significant digit: the captured fields carry five decimal places, so
#: anything at or below this is rounding and anything above it is a real
#: disagreement worth reporting.
PRICE_CROSS_CHECK_TOLERANCE_EUR_KWH: Final = 1e-6

#: Tolerance for the VAT-ratio observation. The source rounds the tax to five
#: decimals, so the comparison is made at that scale rather than exactly.
PRICE_VAT_RATIO_TOLERANCE_EUR_KWH: Final = 5e-6

#: Outcome of comparing the normalised series against the two figures the source
#: publishes for the current interval. Recorded as evidence of contract drift; it
#: never overrides the series and never reaches a decision.
PRICE_CROSS_CHECK_AGREES: Final = "agrees"
PRICE_CROSS_CHECK_DISAGREES: Final = "disagrees"
PRICE_CROSS_CHECK_NOT_COMPARABLE: Final = "not_comparable"

#: Bumped when the price mapping rule itself changes, so evidence recorded under
#: an older rule is identifiable rather than pooled.
PRICE_MAPPING_VERSION: Final = 1

#: The source period must be a whole number of planning intervals. The live
#: source publishes fifteen-minute blocks and Frank supports an hourly fallback;
#: both are whole multiples, and anything else is refused rather than rounded.
PRICE_SOURCE_PERIOD_STEP_MINUTES: Final = QUARTER_MINUTES

# --- Phase 7: dynamic battery reserve -----------------------------------------

#: Bumped when the reserve recursion itself changes, so a requirement recorded
#: under an older rule is identifiable rather than pooled with a newer one -- the
#: same role ``FORECAST_MODEL_VERSION`` and ``PRICE_MAPPING_VERSION`` play.
RESERVE_MODEL_VERSION: Final = 1

#: Hex characters kept from a reserve digest. Sixteen, as everywhere else in the
#: evidence layer: enough that a collision is not a practical concern, short
#: enough to read in a diagnostics download.
RESERVE_FINGERPRINT_CHARS: Final = 16

#: Whether the last drawdown window ended inside the forecast horizon.
#:
#: ``CLOSED`` means the requirement is a complete answer. ``TRUNCATED`` means the
#: deficit was still climbing at the last interval anybody forecast, so demand
#: continues past the horizon and the requirement is a **lower bound**. The
#: distinction is not cosmetic: a backward recursion from a cut-off horizon
#: always understates, and publishing that without saying so would be the same
#: mistake the PV-blind disclaimer exists to prevent.
RESERVE_HORIZON_CLOSED: Final = "closed"
RESERVE_HORIZON_TRUNCATED: Final = "truncated"

#: Why the published requirement is a lower bound, or absent when it is not.
#:
#: Two independent causes, and a consumer needs to know which. A truncated
#: horizon understates because unforecast demand follows. A headroom-limited
#: projection understates because surplus was credited that the pack could not
#: have retained. Both are detected and reported; neither is ever corrected, and
#: neither ever selects a different reserve model.
RESERVE_BOUND_TRUNCATED: Final = "truncated"
#: Why a planned grid charge exists. **A money-spending controller must be able
#: to answer "why now, why this much, why not wait", and until beta.31 it could
#: not**: ``safety_buy`` was a label applied after the fact by diffing two solves,
#: so the payload could say a purchase was reserve-attributable without anything
#: in the code having computed how much was actually unavoidable.
#:
#: These are ordered by how little choice there was. The first two are compelled
#: by physics; the rest are economic decisions that had to clear a gate.
BUY_REASON_REACHABILITY: Final = "reachability_bridge"
BUY_REASON_UNCERTAINTY: Final = "uncertainty_margin"
BUY_REASON_ARBITRAGE: Final = "economic_arbitrage"
BUY_REASON_FUTURE_SELF_USE: Final = "strategic_future_self_use"
BUY_REASON_MIXED: Final = "mixed"
BUY_REASON_UNKNOWN: Final = "unknown"

#: How long the reachability reserve must survive with **no credit at all**, in
#: intervals. Four quarters, one hour.
#:
#: **The question a standard deviation cannot answer.** Reachability credits
#: replenishment that requires *this controller to act successfully* -- to hold
#: ownership, win the write, and have a grid on the other end. Forecast error says
#: nothing about any of that. So one component of the margin is not statistical at
#: all: it is the demand of the next hour, credited with nothing, so that a Home
#: Assistant restart, a refused ownership check, a deaf actuator or a brief outage
#: is survivable from the floor without help.
#:
#: One hour is four replan cycles at the quarter-hour cadence and sixty ticks at
#: the physical one. Long enough that no single failure spans it; short enough
#: that it costs a fraction of a kWh rather than a reserve.
UNCERTAINTY_BLIND_INTERVALS: Final = 4

#: How many mean absolute errors of margin to carry, per root-interval.
#:
#: One. The margin grows as ``MAE * sqrt(n)`` because independent per-interval
#: errors partially cancel rather than accumulating linearly, and ``n`` is the
#: distance to the replenishment actually depended on -- not the horizon length,
#: which would make the margin a property of how far the forecast happens to
#: reach.
UNCERTAINTY_MAE_FACTOR: Final = 1.0

#: The hard ceiling on the whole margin, as a fraction of usable capacity.
#:
#: **Five per cent, and the cap is the point.** The reference installation runs a
#: 27 % weighted forecast error, and no honest reading of that figure turns into
#: "keep the pack full" -- but an uncapped statistical margin eventually would,
#: and it would be the autonomy reserve returning under a new name. On 21.6 kWh
#: this is 1.08 kWh, about five points of state of charge.
UNCERTAINTY_CAP_FRACTION: Final = 0.05

#: Which half of the margin is in force. Published because they answer different
#: questions and a reader must not have to guess which one produced the number.
UNCERTAINTY_BINDING_BLIND: Final = "blind_window"
UNCERTAINTY_BINDING_STATISTICAL: Final = "forecast_error"
UNCERTAINTY_BINDING_CAP: Final = "capped"

#: Which requirement a reserve projection is. Published beside the number so a
#: reader with only the JSON can tell them apart -- for six releases the autonomy
#: figure and a safety floor wore the same field name.
RESERVE_SEMANTICS_AUTONOMY: Final = "autonomy_no_future_grid"
RESERVE_SEMANTICS_REACHABILITY: Final = "reachability_priced_grid"

#: What limited a grid replenishment credit in the reachability recursion.
#:
#: ``beyond_window`` is the important one, and it is deliberately **not** named
#: after the reason the window ends. The reserve layer must not know that the
#: boundary is where published prices stop -- it is handed a count of actionable
#: intervals and asks no further questions, which is what keeps the safety bound
#: free of economics. ``test_no_economic_term_is_named_in_the_reserve`` enforces
#: that, and caught an earlier draft of this very constant.
RESERVE_GRID_CREDIT_NONE: Final = "none"
RESERVE_GRID_CREDIT_BEYOND_WINDOW: Final = "beyond_window"

RESERVE_BOUND_HEADROOM: Final = "headroom_limited"
RESERVE_BOUND_TRUNCATED_HEADROOM: Final = "truncated_headroom_limited"

RESERVE_BOUND_REASONS: Final = (
    RESERVE_BOUND_TRUNCATED,
    RESERVE_BOUND_HEADROOM,
    RESERVE_BOUND_TRUNCATED_HEADROOM,
)

#: Why no requirement could be calculated. A bounded vocabulary, like every other
#: ``*_UNAVAILABLE_*`` key space in this project.
#:
#: ``HORIZON_INCOMPLETE`` is the one worth reading twice: it means an interval
#: inside the horizon carried no load forecast, so the recursion could not reach
#: the present. Unknown is never bridged and never read as zero, so the honest
#: answer is that there is no requirement rather than a smaller one.
RESERVE_UNAVAILABLE_FORECAST: Final = "reserve_forecast_unavailable"
RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE: Final = "reserve_horizon_incomplete"
RESERVE_UNAVAILABLE_LIMITS: Final = "reserve_limits_unavailable"

RESERVE_UNAVAILABLE_REASONS: Final = (
    RESERVE_UNAVAILABLE_FORECAST,
    RESERVE_UNAVAILABLE_HORIZON_INCOMPLETE,
    RESERVE_UNAVAILABLE_LIMITS,
)

#: The single replenishment assumption the reserve rests on, named so it can be
#: recorded on every snapshot and compared across releases.
#:
#: It supersedes an earlier "same-interval netting only" rule, which was withdrawn
#: because without cross-interval credit the requirement degenerates to the whole
#: remaining net demand of the forecast -- a figure that grows when the forecast
#: horizon lengthens, and therefore a property of the forecast rather than of the
#: battery. The superseded definition is still computed every refresh as a
#: counterfactual, so the cost of the relaxation is measured rather than argued.
RESERVE_REPLENISHMENT_ASSUMPTION: Final = (
    "forecast_surplus_credited_within_charge_power_and_headroom"
)

# --- Phase 8: economic optimisation -------------------------------------------

#: Bumped when the optimiser itself changes, so a plan recorded under an older
#: rule is identifiable rather than pooled with a newer one -- the same role
#: ``RESERVE_MODEL_VERSION`` and ``PRICE_MAPPING_VERSION`` play.
ECONOMIC_MODEL_VERSION: Final = 1

#: Hex characters kept from an economic digest, as everywhere else in the
#: evidence layer.
ECONOMIC_FINGERPRINT_CHARS: Final = 16

#: Resolution of the optimiser's stored-energy grid, in DC kWh.
#:
#: The grid is an interpolation-free lattice: every candidate transition lands
#: exactly on a bucket, so a run of charges cannot accumulate a rounding. Two
#: things are measured against it and both are stated rather than inferred: the
#: reserve requirement is quantised **up** to a bucket, so at most one bucket too
#: much reserve is protected; and a measured state of charge is snapped **down**,
#: so the plan never assumes more stored energy than the pack holds.
#:
#: It is also the reason the lexicographic objective is sound. With the
#: requirement on a bucket boundary and every state on one, a violation is an
#: exact multiple of this value -- so a shortfall of a thousandth of a
#: kilowatt-hour is not ignored, it is unrepresentable, and reserve protection
#: cannot cost real money to defend an imaginary quantity.
ECONOMIC_BUCKET_KWH: Final = 0.25

#: Decimal places for reported money and power.
ECONOMIC_EUR_PRECISION: Final = 4
ECONOMIC_POWER_PRECISION: Final = 3

#: Planned runs described individually in diagnostics, soonest first.
#:
#: Eight rather than sixteen: a run is a ten-field mapping, and eight of those is
#: already a substantial payload against a ceiling written for flat lists. The
#: complete count sits beside it, because a truncated list that reads as complete
#: is worse than a count.
MAX_ECONOMIC_RUNS_REPORTED: Final = 8

#: How many per-quarter allocation rows the diagnostics may publish in total,
#: across all reported runs.
#:
#: The full trajectory is 192 rows and publishing it was rightly refused. But
#: without *any* per-quarter view there is no way to tell a broad reported window
#: from energy genuinely spread across every quarter of it -- the two look
#: identical from a window and a total, and that ambiguity cost a whole
#: investigation. A campaign averaging 3.50 kW turned out to be two quarters of
#: buying at 10 kW inside eleven quarters of free production absorption.
#:
#: Forty-eight is twelve hours of quarters: longer than any real campaign, a
#: quarter of the full trajectory, and enough that truncation is the exception. It
#: is a budget shared in run order rather than a per-run cap, so the first runs --
#: the ones a reader is looking at -- are always complete, and any shortfall is
#: reported rather than silently trimmed.
MAX_ECONOMIC_RUN_INTERVALS_REPORTED: Final = 48

#: What the optimiser wants to do. ``export`` and ``curtail_pv`` are economic
#: identities read off the grid residual rather than separate commands: a
#: discharge whose surplus reaches the meter is an export, and declined
#: production is a curtailment. Neither has an actuator in this release.
#: Take no economic battery action. Also the value published when production
#: naturally enters the battery: absorbing your own surplus is ambient physical
#: behaviour, not a decision, so it is reported as ``hold`` rather than as a
#: charge. See ``ECONOMIC_ACTION_CHARGE``.
ECONOMIC_ACTION_HOLD: Final = "hold"
#: **Buy** energy from the grid to put in the battery.
#:
#: This action, and ``CONF_ALLOW_GRID_CHARGING`` beside it, refer specifically to
#: discretionary economic *grid purchase*. Neither is about energy moving into the
#: pack:
#:
#:     physical battery charging from ambient production
#:         is not
#:     Alpha EMS economically choosing to buy from the grid
#:
#: Absorbed production therefore creates no action run, is charged no
#: ``CONF_MINIMUM_TRADE_GAIN_EUR``, and needs no opt-in. It remains part of the
#: physical trajectory and can still create value later through the energy it
#: stored -- it simply is not a trade anybody chose.
ECONOMIC_ACTION_CHARGE: Final = "charge"
ECONOMIC_ACTION_DISCHARGE: Final = "discharge"
ECONOMIC_ACTION_EXPORT: Final = "export"
ECONOMIC_ACTION_CURTAIL: Final = "curtail_pv"
ECONOMIC_ACTION_SAFETY_BUY: Final = "safety_buy"

ECONOMIC_ACTION_OPTIONS: Final = (
    ECONOMIC_ACTION_HOLD,
    ECONOMIC_ACTION_CHARGE,
    ECONOMIC_ACTION_DISCHARGE,
    ECONOMIC_ACTION_EXPORT,
    ECONOMIC_ACTION_CURTAIL,
    ECONOMIC_ACTION_SAFETY_BUY,
)

#: Why the optimiser wants what it wants. A bounded vocabulary, like every other
#: reason space in this project.
ECONOMIC_REASON_CHEAP_WINDOW: Final = "cheap_window"
ECONOMIC_REASON_EXPENSIVE_WINDOW: Final = "expensive_window"
ECONOMIC_REASON_SAFETY_BUY: Final = "safety_buy"
ECONOMIC_REASON_MAKE_HEADROOM: Final = "make_headroom"
ECONOMIC_REASON_NEGATIVE_EXPORT: Final = "negative_export"
ECONOMIC_REASON_RESERVE_RECOVERY: Final = "reserve_recovery"
ECONOMIC_REASON_NO_ACTION: Final = "no_profitable_action"

#: Why nothing could be planned.
ECONOMIC_UNAVAILABLE_LIMITS: Final = "economic_limits_unavailable"
ECONOMIC_UNAVAILABLE_NO_SOC: Final = "economic_soc_unavailable"
ECONOMIC_UNAVAILABLE_NO_PRICES: Final = "economic_prices_unavailable"
ECONOMIC_UNAVAILABLE_NO_RESERVE: Final = "economic_reserve_unavailable"
ECONOMIC_UNAVAILABLE_HORIZON_EMPTY: Final = "economic_horizon_empty"
#: The terminal condition could not be met from anywhere. Structurally
#: unreachable now that the bound is clamped to the bucketed hold trajectory, and
#: kept as a named guard rather than deleted: an earlier version reported this
#: case as ``economic_horizon_empty``, which was a lie about a horizon that was
#: four intervals long, and a wrong reason is worse than an unused one.
ECONOMIC_UNAVAILABLE_TERMINAL_UNREACHABLE: Final = "economic_terminal_unreachable"

ECONOMIC_UNAVAILABLE_REASONS: Final = (
    ECONOMIC_UNAVAILABLE_LIMITS,
    ECONOMIC_UNAVAILABLE_NO_SOC,
    ECONOMIC_UNAVAILABLE_NO_PRICES,
    ECONOMIC_UNAVAILABLE_NO_RESERVE,
    ECONOMIC_UNAVAILABLE_HORIZON_EMPTY,
    ECONOMIC_UNAVAILABLE_TERMINAL_UNREACHABLE,
)

#: Why the action the optimiser wants cannot be carried out.
#:
#: Precedence-ordered, most fundamental first. In this release the global barrier
#: always applies, so every action reports ``execution_unavailable`` -- which is
#: not low information, it is the single most important fact about the release,
#: present on every reading rather than in prose.
ECONOMIC_BLOCKED_EXECUTION_UNAVAILABLE: Final = "execution_unavailable"
ECONOMIC_BLOCKED_NOT_ENABLED: Final = "execution_not_enabled"
ECONOMIC_BLOCKED_MODE_NOT_ACTIVE: Final = "mode_not_active"
ECONOMIC_BLOCKED_NO_PRIMITIVE_EXPORT: Final = "no_primitive_export"
ECONOMIC_BLOCKED_NO_PRIMITIVE_CURTAIL: Final = "no_primitive_curtail"
#: The recommendation is a direction this release does not execute.
#:
#: Distinct from ``no_primitive_*``, which means no actuator exists at all. A
#: discharge to serve the house has a perfectly good actuator; this release simply
#: does not use it, and telling a reader "no primitive" would send them looking for
#: missing hardware.
#:
#: **Renamed in beta.27.1**, because the value said ``live_charge_only`` on a
#: release that also exports -- so a reader watching an export blocked here could
#: not tell a defect from the documented design. The constant keeps its name for
#: compatibility with anything importing it; only the published text changed.
ECONOMIC_BLOCKED_LIVE_CHARGE_ONLY: Final = "live_direction_not_executable"

#: Why the capability plan differs from the desired one. Diagnostics only: the
#: entity shows the two actions and lets them speak for themselves.
ECONOMIC_GAP_NO_PRIMITIVE: Final = "no_primitive"
ECONOMIC_GAP_FORECAST_INFEASIBLE: Final = "forecast_infeasible"
ECONOMIC_GAP_NONE: Final = "none"

#: The one economic setting.
#:
#: Charged once per discretionary battery-action *run* inside the objective, which
#: is what makes it a single mechanism rather than a threshold bolted on
#: afterwards. It suppresses the micro-cycle a per-kWh cost cannot -- a tenth of a
#: kilowatt-hour at a wide margin still earns only a few cents -- and it is
#: emphatically **not** a degradation model.
#:
#: Charged only against **discretionary economic action runs**. Ambient production
#: absorption is not one, so it never pays the fee -- charging it would have the
#: optimizer decline free energy to save money nobody pays.
#:
#: Reserve-protection charging can still happen below it, with no exemption rule,
#: because reserve feasibility has lexicographic priority in the objective.
CONF_MINIMUM_TRADE_GAIN_EUR: Final = "minimum_trade_gain_eur"
DEFAULT_MINIMUM_TRADE_GAIN_EUR: Final = 0.10
MIN_MINIMUM_TRADE_GAIN_EUR: Final = 0.0
MAX_MINIMUM_TRADE_GAIN_EUR: Final = 5.0

#: The minimum economic advantage a **marginal grid-caused** kilowatt-hour of
#: charging must earn, in euros per kWh. Distinct from
#: ``CONF_MINIMUM_TRADE_GAIN_EUR`` and deliberately not a replacement for it:
#:
#: * ``minimum_trade_gain_eur`` is a **fixed** amount a discretionary run must
#:   earn before it is worth starting at all. It gates *thin* trades.
#: * ``grid_charge_margin_eur_per_kwh`` is an **additional per-kWh** requirement
#:   on the energy a charge actually causes to be imported. It gates *large* ones.
#:
#: The second exists because the first does not scale. Measured on the released
#: beta.17 optimizer, a 14.2 kWh round trip was accepted while earning
#: 0.0371 EUR per grid-caused kWh -- the fixed gain was cleared once and the
#: volume behind it was unconstrained.
#:
#: **It is not a degradation model.** It is charged on marginal grid import
#: caused by charging, so four things are outside it by construction rather than
#: by exemption: ambient production absorption (causes no extra import), the
#: sun's share of a mixed quarter (only the grid share is the basis),
#: load-serving discharge and export (not charging at all), and reserve
#: feasibility -- the objective compares ``(violation, cost)`` lexicographically,
#: so no cost can outrank keeping the house supplied.
#:
#: Default **0.0**, which is exactly the behaviour of every release before
#: beta.18. Upgrading changes nothing until this is deliberately set.
CONF_GRID_CHARGE_MARGIN_EUR_PER_KWH: Final = "grid_charge_margin_eur_per_kwh"
DEFAULT_GRID_CHARGE_MARGIN_EUR_PER_KWH: Final = 0.0
MIN_GRID_CHARGE_MARGIN_EUR_PER_KWH: Final = 0.0

#: What one kWh through the battery costs in wear, in euros per kWh of **AC
#: throughput in both directions**: ``charge_ac + discharge_ac``.
#:
#: **The third economic term, and the only one on the discharge side.** The two
#: above gate *buying* -- one per run, one per grid-caused kWh -- so nothing at
#: all priced the discharge and export half. Freeing the pack from the autonomy
#: reserve gives the optimiser far more room to move energy, and room without a
#: price on movement is how a newly-freed optimiser finds churn.
#:
#: **The default is 0.0, and that is a derivation rather than a shrug.** The
#: obvious figure -- system cost over rated cycles -- is 11000 / (10000 x 21.6) =
#: 0.051 EUR per discharged kWh, or 0.025 across both directions. Both are wrong
#: as *marginal* costs, for two reasons. Most of that capital is the inverter and
#: the installation, which are sunk however the pack is cycled. And decisively:
#: ten thousand cycles at roughly one a day is twenty-seven years, so **calendar
#: life ends this battery long before cycle count does** -- a cycle not used is
#: not a cycle kept, and the marginal cost of one more ordinary cycle is about
#: nothing. It becomes real only past ~2 cycles/day, where the two limits
#: converge; that shape is convex, and a convex term would need throughput-so-far
#: as a fourth solver dimension. So this is offered linear, defaulted off, and
#: documented -- not guessed at.
#:
#: **Three disjoint bases, and they must stay disjoint.** ``minimum_trade_gain``
#: is per run; ``grid_charge_margin`` is per grid-caused charge kWh; this is per
#: kWh of total throughput. Setting two of them to "depreciation" double-charges
#: the buy side, which is why none of the three is named after it.
CONF_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: Final = "battery_throughput_cost_eur_per_kwh"
DEFAULT_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: Final = 0.0
MIN_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: Final = 0.0
MAX_BATTERY_THROUGHPUT_COST_EUR_PER_KWH: Final = 1.0

#: A ceiling, in kWh, on how much grid energy one Live charge run may buy.
#:
#: **A commissioning tightener, and zero disables it.** The Stage-A figure
#: ``expected_grid_to_battery_kwh`` is always the hard ceiling when published;
#: this only ever tightens it further, and only when set above zero.
#:
#: Written down because the obvious formulation is wrong and was caught in review:
#: ``min(stage_a_ceiling, configured)`` with a default of ``0.0`` yields a cap of
#: zero and forbids all charging. Absent means unconstrained, never zero -- the
#: same rule the headroom constraint and the charge cutoff both obey.
CONF_GRID_CHARGE_BUDGET_KWH: Final = "grid_charge_budget_kwh"
DEFAULT_GRID_CHARGE_BUDGET_KWH: Final = 0.0
MIN_GRID_CHARGE_BUDGET_KWH: Final = 0.0
MAX_GRID_CHARGE_BUDGET_KWH: Final = 50.0
MAX_GRID_CHARGE_MARGIN_EUR_PER_KWH: Final = 2.0

#: Explicit opt-ins for the two behaviours a user would be surprised by. Both
#: change the *published plan*, so they are meaningful in shadow and belong in
#: the form. ``CONF_CONTROL_EXECUTION_ENABLED`` is offered alongside them from
#: beta.20: it is one of two independent consents rather than a switch with
#: nothing behind it, and the form says plainly that the release also holds the
#: final step closed in code.
#:
#: ``CONF_ALLOW_GRID_CHARGING`` permits **buying**, and nothing else. Storing
#: production the house cannot use is permitted unconditionally, because it draws
#: nothing from the meter. See ``ECONOMIC_ACTION_CHARGE``.
CONF_ALLOW_GRID_CHARGING: Final = "allow_grid_charging"
CONF_ALLOW_BATTERY_EXPORT: Final = "allow_battery_export"
DEFAULT_ALLOW_GRID_CHARGING: Final = False
DEFAULT_ALLOW_BATTERY_EXPORT: Final = False

#: Entity key. One, and only one: the counterfactual plans, the per-run detail,
#: the solver figures and the provenance are all diagnostics.
SENSOR_ECONOMIC_ACTION: Final = "economic_action"

#: The **target** state-space bucket, and the fallback when no better lattice
#: qualifies. Until beta.17 this was the bucket, full stop; it is now the figure
#: :func:`economic.select_bucket_kwh` aims at while it looks for a lattice that
#: can express the configured peak power exactly. It remains the deadband for
#: reported energy (:data:`ECONOMIC_DEADBAND_ENERGY_KWH`), because that is a
#: statement about what a reader should notice rather than about the lattice.
#:
#: The band is a **hard** constraint on the chosen bucket, not a preference.
#: Left unbounded, the search will happily buy exact peak power with a lattice of
#: ten states for a twenty-two kilowatt-hour pack: the power becomes precise and
#: the state of charge becomes useless.
ECONOMIC_BUCKET_BAND_KWH: Final = (0.15, 0.40)

#: How much the state count may grow to buy representable power, as a fraction.
#: A tenth: enough to accept a slightly finer lattice that recovers real power,
#: never enough to pay for complexity that recovers none.
ECONOMIC_BUCKET_STATE_BUDGET: Final = 0.10

#: The largest divisor the bucket search will consider. Bounded so the search is
#: a fixed, small cost at setup rather than something that scales with the pack.
ECONOMIC_BUCKET_MAX_DIVISOR: Final = 80

#: Which rule produced the lattice actually in use. Published because two
#: installations can legitimately end up on different lattices -- an installation
#: where no candidate clears the no-regression test keeps the beta.16 bucket --
#: and a diagnostics reader cannot interpret the figures without knowing which.
ECONOMIC_BUCKET_RULE_CONSTANT: Final = "constant_bucket"
ECONOMIC_BUCKET_RULE_ALIGNED: Final = "aligned_to_peak_power"

#: Whether tomorrow's prices are in the horizon at all. Derived from what the
#: source has actually published, never from a clock: there is no publication
#: time anywhere in this integration, and adding one would make a data question
#: into a scheduling assumption.
ECONOMIC_TOMORROW_PRESENT: Final = "present"
ECONOMIC_TOMORROW_ABSENT: Final = "absent"

#: How far the remaining-production cross-check may differ from the source's own
#: aggregate before it means something. The looser of a tenth and half a
#: kilowatt-hour: a proportional test alone would cry wolf at dusk, when the
#: remaining sum is a few hundred watt-hours and a rounding difference is a large
#: fraction of it.
PV_REMAINING_CROSS_CHECK_FRACTION: Final = 0.10
PV_REMAINING_CROSS_CHECK_FLOOR_KWH: Final = 0.5

#: How many consecutive refreshes a configured flexible-load entity may be absent
#: before it is worth a warning. Home Assistant sets integrations up in an
#: arbitrary order, so an entity this integration depends on is routinely missing
#: for the first refresh or two after a restart -- and a warning then describes
#: the startup sequence rather than the configuration. Learning is paused from
#: the very first absence regardless: the grace period governs the *log*, never
#: the safety rule, and a missing reading is never read as zero.
EV_ABSENCE_GRACE_REFRESHES: Final = 3

#: What a future Stage B would have to physically do. **Not** the economic action
#: label: ``discharge`` and ``export`` are both the battery delivering energy, but
#: one is measured at the battery and the other at the meter, and on the live
#: installation those differ by the entire house load -- 2.2 kW of battery
#: delivered 1.3 kW of export against 0.9 kW of load. A contract that blurred them
#: would command 1.3 kW and deliver 0.4.
#:
#: ``curtail_pv`` is deliberately absent. No actuator can decline production in
#: this release, so offering it as an intent would advertise a capability that does
#: not exist; a curtailment plan reports ``hold`` and says what it wanted in the
#: economic reason instead.
EXECUTION_INTENT_GRID_CHARGE: Final = "grid_charge"
EXECUTION_INTENT_SERVE_LOAD: Final = "serve_load"
EXECUTION_INTENT_NET_EXPORT: Final = "net_export"
EXECUTION_INTENT_HOLD: Final = "hold"

#: Which battery action each Stage-B intent becomes, where one exists.
#:
#: **Total and explicit, because a reset reads it to decide what to stop.** The
#: mapping was implicit in ``control_intent_for`` -- it returns ``ACTION_CHARGE`` for
#: a grid charge and ``None`` for everything else -- which is fine for building a
#: command and useless for the stop path, where there is no command to read.
#:
#: An intent absent from this map has no action, and the caller must fail closed
#: rather than guess. ``serve_load`` and ``net_export`` are deliberately absent:
#: the first keeps the Phase-3 reserve-guard behaviour and the second has no
#: primitive at all, so neither is something Alpha EMS can own or stop.
#: What a *stop* is stopping, per intent. Read at arm time and persisted, so the
#: stop path reads a record of what was armed rather than reconstructing it.
#:
#: **This maps an intent to a battery direction, and nothing else.** It must never
#: be used to choose an actuator *surface*: doing so is what would have routed
#: ``net_export`` onto the Force Discharging family, because that family is where
#: ``ACTION_DISCHARGE`` used to lead. The surface is chosen from
#: :data:`CONTROL_LIVE_DISPATCH_INTENTS`, which is keyed on the intent.
#: Actions an **intent** unlocks beyond the unconditional
#: :data:`CONTROL_EXECUTABLE_ACTIONS`.
#:
#: **Keyed on the intent, and deliberately not merged into that set.** Adding
#: ``ACTION_DISCHARGE`` there would authorise *every* discharge, the Phase-3
#: reserve guard's included -- and the reserve guard discharges into the house,
#: where energy reaching the meter is an accident. What beta.27 authorises is an
#: **admitted economic export**, a different thing that happens to share a battery
#: direction.
#:
#: So the unconditional set stays charge-only and this is consulted second. An
#: intent with no entry here unlocks nothing, which is how ``serve_load`` and every
#: unverified direction stay refused without being enumerated anywhere.
CONTROL_EXECUTABLE_ACTIONS_BY_INTENT: Final = {
    EXECUTION_INTENT_NET_EXPORT: frozenset({ACTION_DISCHARGE}),
}

EXECUTION_INTENT_ACTIONS: Final = {
    EXECUTION_INTENT_GRID_CHARGE: ACTION_CHARGE,
    EXECUTION_INTENT_NET_EXPORT: ACTION_DISCHARGE,
}

EXECUTION_INTENTS: Final = (
    EXECUTION_INTENT_GRID_CHARGE,
    EXECUTION_INTENT_SERVE_LOAD,
    EXECUTION_INTENT_NET_EXPORT,
    EXECUTION_INTENT_HOLD,
)

#: How long a published execution target may be trusted, in minutes. Two planning
#: intervals: long enough to survive one missed refresh, short enough that a
#: stalled integration cannot leave Stage B acting on a stale intention.
#:
#: **Anchored to the issue instant since beta.19, not to the window.** beta.18
#: anchored it to ``window_start``, which made it useless as the thing it is named
#: for: a run eighteen hours out carried a freshness deadline eighteen and a half
#: hours out, so a target could be stale by any ordinary meaning of the word and
#: still be inside it. Command freshness asks "how old is this instruction?" and
#: the answer cannot depend on when the instruction was for.
#:
#: The planning window stays entirely separate: ``window_start`` and
#: ``window_end`` say *when the energy is wanted*, and ``stale_after`` says *how
#: long this statement of intent may be believed*. Conflating them is what went
#: wrong, so they are now two independent facts about every target.
EXECUTION_TARGET_STALE_MINUTES: Final = 2 * QUARTER_MINUTES

#: Where Stage B stands with respect to one execution target.
#:
#: ``idle`` -- nothing actionable. ``armed`` -- a target is current and every gate
#: passed, so a command exists. ``running`` -- an owned dispatch is under way.
#: ``stopping`` -- a reset has been decided and is being carried out.
#: ``inhibited`` -- something refused, and the reason says which.
#: ``unproven`` -- a dispatch is active and ownership cannot be established, which
#: is the one state where the safe action is to do nothing at all.
EXECUTION_STATE_IDLE: Final = "idle"
EXECUTION_STATE_ARMED: Final = "armed"
EXECUTION_STATE_RUNNING: Final = "running"
EXECUTION_STATE_STOPPING: Final = "stopping"
EXECUTION_STATE_INHIBITED: Final = "inhibited"
EXECUTION_STATE_UNPROVEN: Final = "unproven"
#: A target is current and everything has been computed, but its window has not
#: opened yet.
#:
#: Distinct from ``armed`` because on this hardware **arming is delivering**:
#: measured on the real installation, turning the activation helper on starts
#: moving energy immediately. So "we know what to send" and "it is time to send
#: it" are different facts, and beta.19 conflated them -- it reached ``armed``
#: with a live power request fifteen minutes before the window opened, which
#: would have begun charging early the moment the barrier moved.
EXECUTION_STATE_PREPARED: Final = "prepared"

EXECUTION_STATES: Final = (
    EXECUTION_STATE_PREPARED,
    EXECUTION_STATE_IDLE,
    EXECUTION_STATE_ARMED,
    EXECUTION_STATE_RUNNING,
    EXECUTION_STATE_STOPPING,
    EXECUTION_STATE_INHIBITED,
    EXECUTION_STATE_UNPROVEN,
)

#: Whether a running dispatch belongs to Alpha EMS, and how confidently.
#:
#: Three values and not two, because "not ours" and "cannot tell" call for
#: different behaviour: a foreign dispatch is someone else's and must be left
#: alone, while an unproven one might be ours and must *still* be left alone --
#: but the second is a fault to report rather than a normal condition.
OWNERSHIP_NONE: Final = "none"

#: How ownership was established, so a reader is not left to infer it.
#:
#: ``exact`` is the steady state: the dispatch the inverter reports is the one the
#: persisted record says was armed. ``settling`` covers the refresh after Alpha EMS
#: itself armed or re-armed, where the recorded start is absent or has moved
#: because we moved it -- bounded by ``OWNERSHIP_CLAIM_WINDOW_SECONDS``, and the
#: narrower of the two by design. Published side by side because "owned" alone
#: would hide which evidence carried the decision.
OWNERSHIP_PROVENANCE_EXACT: Final = "exact"
OWNERSHIP_PROVENANCE_SETTLING: Final = "settling"
#: Causation shown by the device reflecting **the parameters this claim wrote**.
#:
#: **The provenance beta.30 rests ownership on**, and the reason Live execution
#: works at all. The two labels above both need the vendor's dispatch-start
#: register, whose semantics have never been measured on the hardware -- and on the
#: real installation neither could ever be satisfied, so ownership was permanently
#: ``unproven``, no correction ever landed, and the dead-man had to stop every run.
#:
#: This label needs no vendor register. It is granted when the marker is on, a claim
#: written *before* the writes names this run and this quarter, and the device
#: reflects the mode, sign, power, cutoff and duration that claim recorded. The
#: register may still *upgrade* the label to ``exact`` or ``settling``; it can no
#: longer withhold ownership.
OWNERSHIP_PROVENANCE_PARAMETERS: Final = "parameters"

#: The named factors an ownership verdict is composed from, so a refusal can say
#: **which one** failed rather than only that the verdict was negative. Reading
#: ``ownership_not_owned`` on sixteen consecutive ticks cost a day of hardware
#: debugging that one of these names would have ended.
OWNERSHIP_FACTOR_MARKER: Final = "marker"
OWNERSHIP_FACTOR_CLAIM: Final = "claim"
OWNERSHIP_FACTOR_RUN: Final = "claim_names_run"
OWNERSHIP_FACTOR_PLAN: Final = "claim_names_plan"
OWNERSHIP_FACTOR_MODE: Final = "readback_mode"
OWNERSHIP_FACTOR_SIGN: Final = "readback_sign"
OWNERSHIP_FACTOR_POWER: Final = "readback_power"
OWNERSHIP_FACTOR_CUTOFF: Final = "readback_cutoff"
OWNERSHIP_FACTOR_DURATION: Final = "readback_duration"
OWNERSHIP_FACTOR_DISPATCH: Final = "dispatch_active"
OWNERSHIP_FACTOR_NONE: Final = "none"

OWNERSHIP_OWNED: Final = "owned"
OWNERSHIP_FOREIGN: Final = "foreign"
OWNERSHIP_UNPROVEN: Final = "unproven"

OWNERSHIP_STATES: Final = (
    OWNERSHIP_NONE,
    OWNERSHIP_OWNED,
    OWNERSHIP_FOREIGN,
    OWNERSHIP_UNPROVEN,
)

#: Why the write boundary refused a command outright.
#:
#: These are not safety inhibits and not economic refusals: they mean the command
#: *itself* was malformed, and the only correct response to a malformed command is
#: to send none of it. Every one describes a mistake a future edit could make, so
#: each is checked against the real entity list rather than trusted.
CONTROL_REFUSE_DIRECTION_MISMATCH: Final = "direction_mismatch"
CONTROL_REFUSE_FOREIGN_FAMILY: Final = "foreign_family_entity"
CONTROL_REFUSE_RAW_DISPATCH_WRITE: Final = "raw_dispatch_write"
CONTROL_REFUSE_NEGATIVE_MAGNITUDE: Final = "negative_helper_magnitude"
CONTROL_REFUSE_SERVICE_NOT_PERMITTED: Final = "service_not_permitted"
#: A step outside the entity set this release may write.
#:
#: The last interlock, checked against the step list itself at the send site. It
#: names no action and reads no intent -- a subset test on entity ids, which is
#: why it catches a helper-family write, a write to an unknown entity and any
#: entity outside the permitted set with one comparison, and cannot be fooled by a
#: mislabelled command. Direction on the Dispatch surface is a *value*, so it is
#: :data:`CONTROL_REFUSE_DISPATCH_SIGN` that catches a wrong-way dispatch.
#:
#: **Renamed in beta.27.1**: the value said ``live_charge_only``, which was the
#: wrong thing to tell a reader on a release that executes two directions. The
#: constant keeps its name; only the published text changed.
CONTROL_REFUSE_ACTION_NOT_EXECUTABLE: Final = "entity_not_executable"

#: The owner marker's state, as five distinct facts rather than a boolean.
#:
#: **"Off" and "absent" are different, and beta.24 could not tell them apart.**
#: A missing helper means ownership is *structurally impossible* -- the arm writes
#: to nothing, the write reports success, and the causal record can never match --
#: while a marker that is merely off is the ordinary resting state. One of those
#: must refuse to execute and the other must not, so they cannot share a name.
MARKER_ABSENT: Final = "absent"
MARKER_UNAVAILABLE: Final = "unavailable"
MARKER_OFF: Final = "off"
MARKER_ON: Final = "on"
#: Written, and the readback did not agree. Distinct from ``off`` because the
#: write was attempted: it says the control surface did not do what it was asked.
MARKER_UNVERIFIED: Final = "unverified"

MARKER_STATES: Final = (
    MARKER_ABSENT,
    MARKER_UNAVAILABLE,
    MARKER_OFF,
    MARKER_ON,
    MARKER_UNVERIFIED,
)

#: The marker states in which no dispatch may be armed. ``off`` is absent from
#: this set on purpose: it is the state every arm *starts* from.
MARKER_STATES_UNUSABLE: Final = (MARKER_ABSENT, MARKER_UNAVAILABLE)

# --- beta.25: the physical controller ----------------------------------------

#: The smallest change in commanded power worth a service call, in kW.
#:
#: Two device steps. Quantise **first**, then compare: -2.00 and -2.04 land on the
#: same 0.1 kW step and writing the second buys nothing, while -2.0 to -2.3 is a
#: real correction. Well below any economically meaningful excursion, so a large
#: error is never hidden by it -- and ``dispatch_limited_by`` names a clamp when a
#: clamp, rather than the deadband, is what held the setpoint still.
DISPATCH_POWER_DEADBAND_KW: Final = 0.2

#: The device power resolution, in kW. Every commanded figure is a multiple of it.
DISPATCH_POWER_STEP_KW: Final = 0.1

#: The smallest non-zero energy one quarter can physically deliver.
#:
#: ``0.1 kW`` for a quarter of an hour. **A published objective below this is not
#: executable**, and beta.29 published many: an export plan with meter targets of
#: ``0.01`` and ``0.02`` kWh, which the actuator can only answer with ``0.025`` --
#: a 150 % overshoot, or nothing at all. Neither is what Stage A asked for.
#:
#: Derived rather than written down, so it cannot drift from the step it comes from.
MIN_EXECUTABLE_QUARTER_KWH: Final = DISPATCH_POWER_STEP_KW * 0.25

#: Why a published row cannot be executed. Reported, never silent: the economics
#: stay visible so a reader can see what was planned and why it was not armed.
QUARTER_NOT_EXECUTABLE_SUB_RESOLUTION: Final = "below_actuator_resolution"
QUARTER_NOT_EXECUTABLE_NO_OBJECTIVE: Final = "no_objective"

#: How stale an opening row may be and still be admitted, in seconds.
#:
#: **The fix for the skipped-quarter defect's second half.** A refresh lands a few
#: seconds *after* the boundary it is meant to open, so a rule of "strictly in the
#: future" can never admit the row that has just opened. This tolerates the jitter
#: and nothing more: a row that opened two minutes ago carries unmeasured delivery
#: and is refused.
PLAN_ADMISSION_LOOKBACK_SECONDS: Final = 120.0
#: How far a readback figure may sit from the claim and still be the same command.
#:
#: One helper step for power, because that is the resolution the value was written
#: at. Exact for the cutoff and the duration: both are written as whole numbers and
#: read back as the same whole numbers, so a difference is a different command.
OWNERSHIP_POWER_TOLERANCE_KW: Final = DISPATCH_POWER_STEP_KW + 1e-9

#: How fresh a measurement must be to actuate on, in seconds.
#:
#: **Deliberately not** :data:`BALANCE_MAX_SOURCE_AGE_SECONDS`, which is 300 and was
#: calibrated for *diagnostics* -- comparing accumulated energy. Reused for
#: actuation it would accept a five-minute-old photovoltaic reading as the basis for
#: a live setpoint.
#:
#: Ninety seconds is one and a half physical ticks, and it is derived from measured
#: behaviour rather than chosen: the installation reports source ages of five and
#: twelve seconds and a worst skew of nineteen against an allowance of ninety. So
#: this sits roughly seven times above the observed publish age and five times above
#: the worst observed skew -- it cannot fire on ordinary jitter -- while being three
#: times tighter than the diagnostics bound.
CONTROL_MAX_SOURCE_AGE_SECONDS: Final = 90.0

#: How many consecutive unusable physical ticks are tolerated before a provably
#: owned run is stopped.
#:
#: **Counted in physical ticks, never in economic refreshes.** Two refreshes is
#: about thirty minutes -- longer than the twenty-minute device dead-man it is
#: supposed to sit inside, which would mean the device ended the run before the
#: controller decided to. Three ticks is 180 seconds: fifteen percent of the
#: dead-man and twenty percent of one economic quarter, and the smallest count that
#: tolerates one dropped update plus a retry without ending a healthy run.
CONTROL_COHERENCE_GRACE_TICKS: Final = 3

#: Why the applied setpoint is not the calculated one. Typed, never a free string.
DISPATCH_LIMIT_NONE: Final = "none"
DISPATCH_LIMIT_INVERTER_POWER: Final = "inverter_power"
DISPATCH_LIMIT_MIN_SOC: Final = "min_soc"
DISPATCH_LIMIT_DYNAMIC_RESERVE: Final = "dynamic_reserve"
DISPATCH_LIMIT_REMAINING_GRID_ENERGY: Final = "remaining_grid_energy"
DISPATCH_LIMIT_HEADROOM: Final = "headroom"
DISPATCH_LIMIT_EXPORT_SAFETY: Final = "export_safety"
DISPATCH_LIMIT_GRID_LIMIT: Final = "grid_limit"
DISPATCH_LIMIT_QUANTISATION: Final = "quantisation"
DISPATCH_LIMIT_DIRECTION_GATE: Final = "direction_gate"
DISPATCH_LIMIT_DEADBAND: Final = "deadband"
DISPATCH_LIMIT_STALE_TARGET: Final = "stale_target"
DISPATCH_LIMIT_OWNERSHIP: Final = "ownership"
DISPATCH_LIMIT_SENSOR_COHERENCE: Final = "sensor_coherence"

#: The clamp order, and the order is contractual.
#:
#: Clamp four is the **grid**-energy cap, not the battery remainder, and that
#: correction is the whole reason this tuple is written down. ``battery_target_kwh``
#: is ``expected_pv_to_battery_kwh + expected_grid_to_battery_kwh`` -- a forecast
#: *composite* -- so clamping a grid-power controller with it stops absorption once
#: production runs ahead of forecast and leaks free photovoltaic energy to the grid
#: at an export price the optimizer had already judged worse than storing it. The
#: economic authorisation is the grid share; the pack is bounded by clamps five and
#: three.
DISPATCH_CLAMP_ORDER: Final = (
    DISPATCH_LIMIT_INVERTER_POWER,
    DISPATCH_LIMIT_MIN_SOC,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_REMAINING_GRID_ENERGY,
    DISPATCH_LIMIT_HEADROOM,
    DISPATCH_LIMIT_EXPORT_SAFETY,
    DISPATCH_LIMIT_GRID_LIMIT,
    DISPATCH_LIMIT_QUANTISATION,
)

#: Control-grade coherence, as a state rather than a boolean.
COHERENCE_OK: Final = "ok"
COHERENCE_HOLDING: Final = "holding"
COHERENCE_EXPIRED: Final = "expired"

#: What the controller did about it.
COHERENCE_ACTION_NONE: Final = "none"
COHERENCE_ACTION_HOLD: Final = "hold_last_setpoint"
COHERENCE_ACTION_STOP: Final = "stop_owned_run"
COHERENCE_ACTION_REFUSE_REARM: Final = "refuse_deadman_rearm"

#: Why a physical tick did nothing.
TICK_SKIPPED_LOCK_HELD: Final = "lock_held"
TICK_SKIPPED_NOT_LIVE: Final = "not_live"
TICK_SKIPPED_NO_RUN: Final = "no_owned_run"
TICK_SKIPPED_STALE_TARGET: Final = "stale_target"
TICK_SKIPPED_INCOHERENT: Final = "sensor_coherence"
TICK_SKIPPED_DEADBAND: Final = "within_deadband"
TICK_APPLIED: Final = "power_written"

#: Ownership, as four states. ``degraded`` is new in beta.25 and is **never** a
#: synonym for owned: it means causation is still provable while the marker is not,
#: which authorises exactly one write and nothing else.
OWNERSHIP_DEGRADED: Final = "degraded"

REFUSE_EMERGENCY_NOT_AUTHORIZED: Final = "emergency_self_stop_not_authorized"
REFUSE_EMERGENCY_NOT_THE_STOP: Final = "emergency_self_stop_permits_only_dispatch_off"
REFUSE_EMERGENCY_ATTEMPTS_SPENT: Final = "emergency_self_stop_attempts_spent"

#: The one operation the emergency authority grants, named so a test can assert
#: that it is the only one.
EMERGENCY_STOP_OPERATION: Final = "dispatch_enable_off"

#: How many times the narrowly authorised emergency stop is retried, one attempt
#: per physical tick, before the device dead-man is left to finish the job.
EMERGENCY_STOP_MAX_ATTEMPTS: Final = 3

# --- beta.27: the per-quarter execution envelope -----------------------------

#: How long one physical control interval is *assumed* to last when capping the
#: energy a single write may deliver, in seconds.
#:
#: **Deliberately longer than the sixty-second cadence it guards.** The tick is
#: "approximately" every sixty seconds, a readback lands after the write rather
#: than with it, and a tick can be skipped for lock contention -- so a cap built on
#: an assumed exact sixty seconds would be optimistic in exactly the situations
#: that matter.
#:
#: The direction of error is chosen: this can leave a target a few watt-hours short
#: near the end of a quarter, and that is preferred to an overshoot. Spending
#: energy Stage A did not authorise is a real cost; finishing marginally short is
#: recorded and forgotten.
CONTROL_TICK_ENERGY_HORIZON_SECONDS: Final = 90.0

#: How close to an admitted quarter's objective counts as having reached it.
#:
#: **Deliberately not** :data:`execution.TARGET_TOLERANCE_KWH`, which is ``0.25``
#: kWh. That figure is calibrated against a whole run's target and would call a
#: half-kilowatt-hour quarter finished when half of it had been delivered. A
#: quarter needs a quarter-scale figure.
#:
#: Ten watt-hours is below what one control tick can command at the 0.1 kW
#: quantisation step -- 0.1 kW for :data:`CONTROL_TICK_ENERGY_HORIZON_SECONDS`
#: delivers 2.5 Wh -- so the residue this forgives is smaller than the smallest
#: correction that could chase it, and no quarter can sit forever a few watt-hours
#: from done.
QUARTER_TARGET_TOLERANCE_KWH: Final = 0.01

#: Why a Live ``net_export`` was not authorised. **A separate vocabulary from the
#: ``INHIBIT_*`` reasons on purpose**: those describe the reserve-guard discharge
#: gate, which beta.27 does not touch, and reusing them would make a reader think
#: that gate had been widened. It has not.
EXPORT_REFUSE_NOT_EXPORT_INTENT: Final = "not_an_export_intent"
EXPORT_REFUSE_NO_QUARTER: Final = "no_admitted_quarter"
EXPORT_REFUSE_QUARTER_NOT_OPEN: Final = "quarter_not_open"
EXPORT_REFUSE_NOT_OWNED: Final = "ownership_not_provable"
EXPORT_REFUSE_RECORD_MISMATCH: Final = "causal_record_mismatch"
EXPORT_REFUSE_DISPATCH_FOREIGN: Final = "dispatch_not_ours"
EXPORT_REFUSE_INCOHERENT: Final = "sensor_incoherence"
EXPORT_REFUSE_CONFLICTING_FEATURE: Final = "conflicting_feature_active"
EXPORT_REFUSE_MISSING_ENTITY: Final = "missing_control_entity"
EXPORT_REFUSE_NO_FAILSAFE: Final = "no_failsafe_automation"
EXPORT_REFUSE_SOC_UNUSABLE: Final = "soc_unusable"
EXPORT_REFUSE_RESERVE_FLOOR: Final = "reserve_floor"
EXPORT_REFUSE_MIN_SOC: Final = "configured_min_soc"
EXPORT_REFUSE_NO_BATTERY_ALLOWANCE: Final = "no_battery_discharge_authorised"
EXPORT_REFUSE_NO_EXPORT_TARGET: Final = "no_meter_export_target"
EXPORT_REFUSE_INVERTER_LIMIT: Final = "inverter_discharge_limit"
EXPORT_REFUSE_SITE_EXPORT_LIMIT: Final = "site_export_limit"
EXPORT_REFUSE_TICK_HORIZON: Final = "tick_energy_horizon"
EXPORT_REFUSE_SIGN: Final = "dispatch_sign"
EXPORT_REFUSE_BELOW_DEVICE_MINIMUM: Final = "power_below_device_minimum"
EXPORT_REFUSE_ABOVE_DEVICE_MAXIMUM: Final = "power_above_device_maximum"
EXPORT_AUTHORISED: Final = "authorised"

#: Every condition a Live export must satisfy, in the order they are checked.
#: Published so the checklist is auditable from the diagnostics rather than only
#: from the source.
EXPORT_AUTHORISATION_ORDER: Final = (
    EXPORT_REFUSE_NOT_EXPORT_INTENT,
    EXPORT_REFUSE_NO_QUARTER,
    EXPORT_REFUSE_QUARTER_NOT_OPEN,
    EXPORT_REFUSE_MISSING_ENTITY,
    EXPORT_REFUSE_NO_FAILSAFE,
    EXPORT_REFUSE_CONFLICTING_FEATURE,
    EXPORT_REFUSE_DISPATCH_FOREIGN,
    EXPORT_REFUSE_NOT_OWNED,
    EXPORT_REFUSE_RECORD_MISMATCH,
    EXPORT_REFUSE_INCOHERENT,
    EXPORT_REFUSE_SOC_UNUSABLE,
    EXPORT_REFUSE_MIN_SOC,
    EXPORT_REFUSE_RESERVE_FLOOR,
    EXPORT_REFUSE_NO_BATTERY_ALLOWANCE,
    EXPORT_REFUSE_NO_EXPORT_TARGET,
    EXPORT_REFUSE_INVERTER_LIMIT,
    EXPORT_REFUSE_SITE_EXPORT_LIMIT,
    EXPORT_REFUSE_TICK_HORIZON,
    EXPORT_REFUSE_BELOW_DEVICE_MINIMUM,
    EXPORT_REFUSE_ABOVE_DEVICE_MAXIMUM,
    EXPORT_REFUSE_SIGN,
)

#: Why an admitted quarter stopped.
QUARTER_END_TARGET_REACHED: Final = "quarter_target_reached"
QUARTER_END_EXPIRED: Final = "quarter_expired"
QUARTER_END_SAFETY: Final = "quarter_safety_stop"

#: What bound the delivered energy short of the plan, when something did.
SHORTFALL_INVERTER_LIMIT: Final = "inverter_limit"
SHORTFALL_GRID_LIMIT: Final = "grid_limit"
SHORTFALL_RESERVE_LIMIT: Final = "reserve_limit"
SHORTFALL_HEADROOM_LIMIT: Final = "headroom_limit"
SHORTFALL_OWNERSHIP_LOSS: Final = "ownership_loss"
SHORTFALL_SENSOR_INCOHERENCE: Final = "sensor_incoherence"
SHORTFALL_TARGET_REACHED: Final = "target_reached"
SHORTFALL_QUARTER_EXPIRED: Final = "quarter_expired"
SHORTFALL_WRITE_FAILURE: Final = "hardware_write_failure"
SHORTFALL_DEADMAN_FAILURE: Final = "deadman_failure"
SHORTFALL_DIRECTION_GATE: Final = "direction_gate"
SHORTFALL_TICK_HORIZON: Final = "tick_energy_horizon"
SHORTFALL_NONE: Final = "none"

#: Stop reasons beta.27 adds. The quarter is the entitlement; the dead-man lease is
#: not, and these exist so a stop can say which of the two ended the run.
EXECUTION_STOP_QUARTER_TARGET_REACHED: Final = "quarter_target_reached"
EXECUTION_STOP_QUARTER_EXPIRED: Final = "quarter_expired"
EXECUTION_STOP_QUARTER_PROGRESS_UNKNOWN: Final = "quarter_progress_unknown"

#: The clamp that bound an export setpoint, in the documented order.
DISPATCH_LIMIT_MAX_DISCHARGE: Final = "inverter_discharge"
DISPATCH_LIMIT_REMAINING_DISCHARGE: Final = "remaining_discharge_energy"
DISPATCH_LIMIT_REMAINING_EXPORT: Final = "remaining_export_energy"
DISPATCH_LIMIT_TICK_HORIZON: Final = "tick_energy_horizon"

#: The deterministic export clamp order, written once so the code and the
#: documentation cannot drift. Battery-side and meter-side entries are converted
#: through the canonical identity, never compared directly.
DISPATCH_EXPORT_CLAMP_ORDER: Final = (
    DISPATCH_LIMIT_MAX_DISCHARGE,
    DISPATCH_LIMIT_MIN_SOC,
    DISPATCH_LIMIT_DYNAMIC_RESERVE,
    DISPATCH_LIMIT_REMAINING_DISCHARGE,
    DISPATCH_LIMIT_REMAINING_EXPORT,
    DISPATCH_LIMIT_TICK_HORIZON,
    DISPATCH_LIMIT_GRID_LIMIT,
    DISPATCH_LIMIT_EXPORT_SAFETY,
    DISPATCH_LIMIT_QUANTISATION,
    DISPATCH_LIMIT_DIRECTION_GATE,
)

#: Why a physical tick did what it did. ``no_owned_run`` is gone: it covered three
#: distinct conditions, so a reader could not tell "no authority" from "authority
#: but nothing armed".
TICK_SKIPPED_NO_QUARTER: Final = "no_admitted_quarter"
#: The open row's objective is below what one quarter can physically deliver.
#: Stage A marks such a row non-executable; this is Stage B's own backstop, because
#: a caller that armed one anyway would overshoot it by construction.
TICK_SKIPPED_SUB_RESOLUTION: Final = "objective_below_actuator_resolution"
TICK_SKIPPED_DISPATCH_INACTIVE: Final = "dispatch_not_active"
TICK_SKIPPED_OWNERSHIP: Final = "ownership_not_owned"
TICK_STOPPED_TARGET_REACHED: Final = "stopped_target_reached"
TICK_STOPPED_QUARTER_EXPIRED: Final = "stopped_quarter_expired"
TICK_ERROR: Final = "controller_error"

#: Which cadence produced a diagnostics record. Published so a sixty-second tick
#: reason can never again sit beside quarter-refresh figures as though the two
#: described one event.
CADENCE_PHYSICAL_TICK: Final = "physical_tick"
CADENCE_QUARTER_REFRESH: Final = "quarter_refresh"
CADENCE_OFF_REFRESH: Final = "off_refresh"

#: The bounded history of completed execution quarters.
#: How many completed quarters to publish and retain.
#:
#: **96, one civil day, and the number is the whole point.** At 12 this ring held
#: three hours, so by the time a diagnostic was downloaded every night charge
#: campaign had already been evicted -- and with the per-interval prices absent
#: from the payload as well, no purchase this integration had ever made could be
#: costed afterwards. An optimiser that spends real money and cannot show its
#: receipts is not auditable, whatever its diagnostics say.
MAX_COMPLETED_QUARTERS_REPORTED: Final = 96

#: How many dispatch-start probe samples to keep.
#:
#: A twenty-minute run is ~20 sixty-second ticks plus two refreshes, so this holds
#: one whole run including the arm and the samples after the stop.
MAX_DISPATCH_START_SAMPLES_REPORTED: Final = 32

#: How many **active-run** dispatch-start samples to keep, separately.
#:
#: **The ring above cannot answer the question it was built for.** It is ordered
#: by time and evicted by time, so a run that ends is followed by hours of idle
#: ``raw=0`` samples that push the only informative entries out. Measured: the
#: beta.30 probe captured a real Live charge at 03:15 and by 12:00 every one of
#: its thirty-two entries read ``0`` with ``phase: before_start``.
#:
#: This ring is appended to **only while a dispatch is running**, so an idle
#: sample can never displace an active one and the evidence survives until the
#: next run overwrites it. Twenty-four entries covers a dead-man lease at the
#: sixty-second tick, which is longer than any single run can last.
MAX_DISPATCH_START_ACTIVE_SAMPLES: Final = 24

#: How many Stage-A decision records to retain for replay.
#:
#: **Bounded and append-only, deliberately not a database.** One record per
#: quarter-hour refresh, so 192 is two days -- long enough to replay a full
#: weekend against a changed architecture, short enough that the payload stays
#: readable and the store stays small. The point is reproducibility, not history:
#: anything older is better answered by the forecast and price evidence layers
#: that already keep a year.
MAX_DECISION_RECORDS_RETAINED: Final = 192

#: How many of those records to put in a diagnostics download.
#:
#: Far fewer than are retained, because a payload is read by a person and two
#: days of quarter-hour records is seven hundred lines of scalars. The replay
#: harness reads the store, not the payload.
MAX_DECISION_RECORDS_PUBLISHED: Final = 16

#: Why a Stage-A decision was recorded, so a reader can tell a routine refresh
#: from one that actually changed the plan.
DECISION_RECORD_REASON_REFRESH: Final = "refresh"
DECISION_RECORD_REASON_REVISION: Final = "revision"

#: The causal-claim schema this release writes and will accept.
#:
#: **A claim written before beta.30 carries no quarter and no claim id**, so it
#: cannot be checked against the row Stage B is executing. Such a claim is refused
#: rather than adopted: an upgrade must not take over a dispatch armed under rules
#: this release no longer applies. The dead-man finishes any run in flight, which is
#: exactly what it is for.
CLAIM_SCHEMA_VERSION: Final = 2

#: Where the execution lifecycle currently is. One field, one question, so a reader
#: never has to infer the state from four others that were computed at different
#: instants.
LIFECYCLE_IDLE: Final = "idle"
LIFECYCLE_ADMITTED: Final = "admitted"
LIFECYCLE_STARTING: Final = "starting"
LIFECYCLE_EXECUTING: Final = "executing"
LIFECYCLE_UPDATING: Final = "updating"
LIFECYCLE_STOPPING: Final = "stopping"
LIFECYCLE_STOPPED: Final = "stopped"
LIFECYCLE_DEADMAN_EXPIRED: Final = "deadman_expired"
LIFECYCLE_CLEANUP_COMPLETE: Final = "cleanup_complete"
LIFECYCLE_FOREIGN: Final = "foreign"
LIFECYCLE_UNPROVEN: Final = "unproven"
LIFECYCLE_DEGRADED: Final = "degraded"

#: Where a probe sample sits in the physical lifecycle, derived from observable
#: transitions only. Diagnostics; nothing decides on it.
PROBE_PHASE_BEFORE_START: Final = "before_start"
PROBE_PHASE_AFTER_START: Final = "after_start"
PROBE_PHASE_STEADY: Final = "steady_active"
PROBE_PHASE_AFTER_REARM: Final = "after_rearm"
PROBE_PHASE_AFTER_STOP: Final = "after_stop"
PROBE_PHASE_IDLE: Final = "idle"

#: Which of the two authorisation caps produced the effective remainder.
CAP_FROZEN: Final = "frozen"
CAP_FORWARD: Final = "forward"
CAP_NONE: Final = "none"

#: Stop reasons added by beta.25.
EXECUTION_STOP_MARKER_LOST: Final = "ownership_marker_lost"
EXECUTION_STOP_COHERENCE_LOST: Final = "sensor_coherence_lost"
EXECUTION_STOP_CONFLICTING_FAMILY: Final = "conflicting_family_active"

#: Refusals added by beta.25.
CONTROL_REFUSE_DISPATCH_MODE: Final = "dispatch_mode_not_executable"
CONTROL_REFUSE_DISPATCH_SIGN: Final = "dispatch_sign_not_executable"
CONTROL_REFUSE_CONFLICTING_FAMILY: Final = "conflicting_family_active"

#: The Live execution envelope for the raw Dispatch surface, as a pair rather than
#: two loose facts. beta.25 may command mode 2 with a **negative** power and
#: nothing else; the sign is part of the barrier, not a downstream check.
CONTROL_EXECUTABLE_DISPATCH_MODES: Final = frozenset({2})

#: The permitted signed direction **per admitted intent**, and the gate is keyed on
#: the intent rather than on a single scalar for one reason: beta.27 executes two
#: directions, so "which sign may be sent" is only answerable once you know what is
#: being executed. A scalar could only ever describe one of them.
#:
#: An intent absent from this mapping is refused. That is what keeps
#: ``serve_load``, the negative-price modes and every unverified direction blocked
#: without needing a list of them.
CONTROL_EXECUTABLE_DISPATCH_SIGNS: Final = {
    EXECUTION_INTENT_GRID_CHARGE: -1,
    EXECUTION_INTENT_NET_EXPORT: +1,
}

#: The intents beta.27 may physically execute, and the set the *actuator surface*
#: is chosen from. Deliberately not derived from ``EXECUTION_INTENT_ACTIONS``: that
#: maps an intent to a battery *direction* for the stop path, and using a direction
#: to choose a surface is precisely the defect that would have routed export onto
#: the Force Discharging family.
CONTROL_LIVE_DISPATCH_INTENTS: Final = frozenset(
    {EXECUTION_INTENT_GRID_CHARGE, EXECUTION_INTENT_NET_EXPORT}
)

#: The bounded ring of recent physical decisions, so a download taken later can
#: reconstruct what happened earlier in the quarter -- diagnostics are rarely
#: captured at the moment production moved.
MAX_PHYSICAL_DECISIONS_REPORTED: Final = 16

#: What a staged sequence checks between its two stages.
#:
#: **Both verify a write, not a device.** ``marker_on`` reads back the helper the
#: claim just wrote; ``no_family_active`` reads back the activation booleans the
#: deactivation just cleared. Both are local ``input_boolean`` entities and settle
#: within the service call, which is what makes a same-refresh check meaningful.
#:
#: Deliberately **not** ``sensor.alphaess_dispatch_start``. That register is the
#: device's own readback and legitimately lags a poll behind, so gating the
#: cleanup on it would withhold the resting values every single time and release
#: the marker never -- a stop that can never finish is worse than the fault this
#: staging exists to fix. The register remains what the dead-man and the
#: ownership rule read; it is not what tells us our own write landed.
EXECUTION_VERIFY_MARKER_ON: Final = "marker_on"
EXECUTION_VERIFY_NO_FAMILY_ACTIVE: Final = "no_family_active"
#: The Dispatch enable helper reads off. Checked instead of the device register
#: for the same reason as above: the register lags a poll.
EXECUTION_VERIFY_DISPATCH_INACTIVE: Final = "dispatch_inactive"
#: A commanded Dispatch power reads back readable and signed as sent. The exact
#: float is deliberately not compared -- the helper quantises, and demanding
#: equality would fail on the device's own rounding rather than on a real fault.
EXECUTION_VERIFY_DISPATCH_SETPOINT: Final = "dispatch_setpoint"

#: Stage one of the arm was sent and the marker did not read back on, so stage two
#: -- which carries the activation -- was never sent and nothing was armed.
CONTROL_REFUSE_MARKER_NOT_VERIFIED: Final = "marker_not_verified"
#: Stage one of the stop was sent and the dispatch did not read back inactive, so
#: the cleanup was withheld and the ownership evidence was kept for a retry.
CONTROL_REFUSE_STOP_NOT_VERIFIED: Final = "stop_not_verified"

CONTROL_WRITE_REFUSALS: Final = (
    CONTROL_REFUSE_DIRECTION_MISMATCH,
    CONTROL_REFUSE_FOREIGN_FAMILY,
    CONTROL_REFUSE_RAW_DISPATCH_WRITE,
    CONTROL_REFUSE_NEGATIVE_MAGNITUDE,
    CONTROL_REFUSE_SERVICE_NOT_PERMITTED,
)

#: The owner marker: a helper Alpha EMS turns on as the first step of arming and
#: off as the last step of resetting.
#:
#: Deliberately **not** an AlphaESS helper. It is outside the vendor package
#: because its whole purpose is to record something the vendor surface cannot: who
#: armed the dispatch. Every AlphaESS arming path is driven by helper *values*, so
#: a dashboard-armed dispatch and a service-armed one leave byte-identical state --
#: which is why parameter matching is not merely weak evidence here but actively
#: misleading, the person watching Shadow being exactly the person who would set
#: those same figures by hand.
#:
#: It costs no new permitted service: ``turn_on`` and ``turn_off`` are already in
#: the closed set of three.
BOOLEAN_EXECUTION_OWNER: Final = "input_boolean.alpha_ems_dispatch_owner"

#: Why Stage B reduced the power the rolling controller asked for, or stopped.
#:
#: Every one of these is a *physical* or *published-constraint* reason. None of
#: them is a price, a value or a preference, and that is the whole point: Stage B
#: reduces for reasons it can measure or was told, never for reasons it decided.
EXECUTION_REDUCTION_NONE: Final = "none"
EXECUTION_REDUCTION_PV_AHEAD: Final = "pv_ahead_of_forecast"
EXECUTION_REDUCTION_HEADROOM: Final = "headroom_constraint"
EXECUTION_REDUCTION_RESERVE: Final = "reserve_limit"
EXECUTION_REDUCTION_SAFETY: Final = "safety_limited"
EXECUTION_REDUCTION_TARGET_MET: Final = "target_reached"

#: The Stage-A grid ceiling, or the commissioning tightener below it, has been
#: reached. Distinct from ``headroom_constraint``: that one is about how much
#: room is left in the pack, this one about how much energy this plan approved
#: buying.
EXECUTION_REDUCTION_BUDGET: Final = "grid_energy_ceiling"

EXECUTION_REDUCTION_REASONS: Final = (
    EXECUTION_REDUCTION_BUDGET,
    EXECUTION_REDUCTION_NONE,
    EXECUTION_REDUCTION_PV_AHEAD,
    EXECUTION_REDUCTION_HEADROOM,
    EXECUTION_REDUCTION_RESERVE,
    EXECUTION_REDUCTION_SAFETY,
    EXECUTION_REDUCTION_TARGET_MET,
)

#: Why an owned run stopped. Published so a stop is never silent, and so the
#: difference between "finished" and "gave up" is a recorded fact.
EXECUTION_STOP_TARGET_REACHED: Final = "target_reached"
EXECUTION_STOP_WINDOW_ENDED: Final = "window_ended"
EXECUTION_STOP_STAGE_A_HOLD: Final = "stage_a_hold"
EXECUTION_STOP_PLAN_REPLACED: Final = "plan_replaced"
EXECUTION_STOP_SAFETY: Final = "safety"
EXECUTION_STOP_RESERVE_LIMIT: Final = "reserve_limit"
EXECUTION_STOP_STALE_PLAN: Final = "stale_plan"
EXECUTION_STOP_SWITCHED_TO_SHADOW: Final = "user_switched_to_shadow"
EXECUTION_STOP_SWITCHED_OFF: Final = "user_switched_off"
EXECUTION_STOP_OWNERSHIP_CONFLICT: Final = "ownership_conflict"
EXECUTION_STOP_EXECUTION_ERROR: Final = "execution_error"
#: The approved grid energy for this run has all been bought.
EXECUTION_STOP_GRID_CEILING: Final = "grid_energy_ceiling"
#: No valid charge ceiling could be established, so the charge was refused
#: rather than given a substituted bound. See ``CONTROL_CUTOFF_MIN_PERCENT``.
EXECUTION_STOP_NO_CHARGE_CEILING: Final = "no_charge_ceiling"
#: A sustaining re-arm did not demonstrably advance the device dead-man.
#:
#: The controller refreshes every fifteen minutes against a twenty-minute
#: dead-man, so a run continues only because each refresh re-arms it. Whether
#: re-activating an already-active dispatch actually refreshes that timer is a
#: property of the control surface rather than of this integration, so it is
#: **measured** every refresh instead of assumed. When the timer does not move,
#: the honest conclusion is that the run is about to end whatever the controller
#: believes, so it is ended deliberately and said out loud.
EXECUTION_STOP_TIMER_NOT_REFRESHED: Final = "deadman_not_refreshed"
#: The Stage-A headroom cap reduced the request to nothing.
#:
#: Distinguished from ``target_reached`` since beta.24, because they are different
#: outcomes and a reader needs to tell them apart: one bought what the plan asked
#: for, the other stopped short because the pack ran out of room. Both stop, both
#: reset, and neither is a fault -- but "complete" and "stopped for headroom" are
#: not the same sentence.
EXECUTION_STOP_HEADROOM_REACHED: Final = "headroom_reached"

EXECUTION_STOP_REASONS: Final = (
    EXECUTION_STOP_TIMER_NOT_REFRESHED,
    EXECUTION_STOP_HEADROOM_REACHED,
    EXECUTION_STOP_GRID_CEILING,
    EXECUTION_STOP_NO_CHARGE_CEILING,
    EXECUTION_STOP_TARGET_REACHED,
    EXECUTION_STOP_WINDOW_ENDED,
    EXECUTION_STOP_STAGE_A_HOLD,
    EXECUTION_STOP_PLAN_REPLACED,
    EXECUTION_STOP_SAFETY,
    EXECUTION_STOP_RESERVE_LIMIT,
    EXECUTION_STOP_STALE_PLAN,
    EXECUTION_STOP_SWITCHED_TO_SHADOW,
    EXECUTION_STOP_SWITCHED_OFF,
    EXECUTION_STOP_OWNERSHIP_CONFLICT,
    EXECUTION_STOP_EXECUTION_ERROR,
)

#: How the realised battery figure was obtained, so a reader can weigh it.
#:
#: ``accumulated`` integrates measured battery power and is the better figure
#: within a quarter; ``state_of_charge_delta`` differences the persisted level and
#: is the only one that survives a restart. They are published together rather
#: than reconciled, because where they disagree the disagreement is the
#: information.
EXECUTION_BASIS_ACCUMULATED: Final = "accumulated"
EXECUTION_BASIS_SOC_DELTA: Final = "state_of_charge_delta"
EXECUTION_BASIS_BOTH: Final = "accumulated_and_state_of_charge"
EXECUTION_BASIS_UNAVAILABLE: Final = "unavailable"

#: Whether the realised figure is good enough to act on.
#:
#: ``partial`` is the honest answer for the first quarter after a restart: the
#: coverage threshold measures against the *whole* quarter, so a quarter that
#: began before the integration did can never reach it. Reporting that as
#: ``measured`` would dress a gap as a reading.
EXECUTION_QUALITY_MEASURED: Final = "measured"
EXECUTION_QUALITY_PARTIAL: Final = "partial"
EXECUTION_QUALITY_RECONSTRUCTED: Final = "reconstructed"
EXECUTION_QUALITY_UNAVAILABLE: Final = "unavailable"

#: What the battery actually did in a run, as the objective saw it.
#:
#: Distinct from the *action label* on purpose. One physical discharge carries
#: both ``discharge`` and ``export`` as house load rises and falls beneath it, so
#: the label changes while the direction does not -- which is why a reported run
#: count is an upper bound on the number of switches and never the switches
#: themselves. The switching fee is charged against the direction.
ECONOMIC_DIRECTION_IDLE: Final = "idle"
ECONOMIC_DIRECTION_CHARGE: Final = "charge"
ECONOMIC_DIRECTION_DISCHARGE: Final = "discharge"

ECONOMIC_DIRECTIONS: Final = (
    ECONOMIC_DIRECTION_IDLE,
    ECONOMIC_DIRECTION_CHARGE,
    ECONOMIC_DIRECTION_DISCHARGE,
)

#: Where a charge run's energy came from, derived from the exact marginal grid
#: import rather than guessed. The boundary is one state-space bucket: below that
#: the grid contribution is unrepresentable, so calling it anything but
#: production would be over-claiming.
#:
#: This exists because "charged 4.48 kWh" read as "bought 4.48 kWh", and on the
#: live installation only 1.55 kWh of that was even site import -- the rest was
#: the sun.
ECONOMIC_CHARGE_SOURCE_NONE: Final = "not_charging"
ECONOMIC_CHARGE_SOURCE_PRODUCTION: Final = "production"
ECONOMIC_CHARGE_SOURCE_MIXED: Final = "mixed"
ECONOMIC_CHARGE_SOURCE_GRID: Final = "grid"

ECONOMIC_CHARGE_SOURCES: Final = (
    ECONOMIC_CHARGE_SOURCE_NONE,
    ECONOMIC_CHARGE_SOURCE_PRODUCTION,
    ECONOMIC_CHARGE_SOURCE_MIXED,
    ECONOMIC_CHARGE_SOURCE_GRID,
)

#: Activity event kinds. Observational only: nothing in this integration
#: subscribes to them, no planner or execution state is derived from them, and
#: losing the recorder changes no figure.
ECONOMIC_EVENT_PLANNED: Final = "planned"
ECONOMIC_EVENT_CHANGED: Final = "changed"
ECONOMIC_EVENT_STARTED: Final = "started"
ECONOMIC_EVENT_ENDED: Final = "ended"
ECONOMIC_EVENT_CANCELLED: Final = "cancelled"
ECONOMIC_EVENT_REFUSED: Final = "refused"
#: A dispatch ended, with the reason it ended. Added in beta.19: ``started`` had
#: no counterpart, so a stop could only ever be inferred from the absence of a
#: further line -- and an announcement left standing after its run finished is the
#: one way this surface can mislead. **Execution-class**, so it is refused exactly
#: as ``started`` is while nothing can be sent.
ECONOMIC_EVENT_STOPPED: Final = "stopped"

#: What shadow says instead. Advice-class, and separate kinds rather than the same
#: kinds worded differently.
#:
#: The distinction is load-bearing. ``logbook_payload`` refuses an execution kind
#: outright while the barrier stands, and that refusal is what guarantees this
#: surface cannot claim the battery moved. A shadow line describing a dispatch it
#: *would* have started is not a claim about the battery, but if it carried the
#: ``started`` kind it would be refused -- and if the refusal were relaxed to let
#: it through, the guarantee would be gone for the real case too. Two kinds keeps
#: both: shadow speaks, execution stays refused, and the classification does the
#: work rather than a caller remembering to word things carefully.
ECONOMIC_EVENT_WOULD_START: Final = "would_start"
ECONOMIC_EVENT_WOULD_STOP: Final = "would_stop"

#: A standing condition began or ended. Transitions only: repeating an inhibit
#: every refresh is the spam the whole surface is designed against. Advice-class,
#: because a refusal to act is a statement about the pipeline rather than about the
#: battery.
ECONOMIC_EVENT_INHIBITED: Final = "inhibited"
ECONOMIC_EVENT_AVAILABLE: Final = "available"

#: A plan lifecycle reached its terminal state. **beta.31**, and the reason the
#: two below exist rather than ``stopped`` doing all three jobs: a reader needs to
#: know which of *succeeded*, *was called off* and *failed* happened, and a single
#: kind carrying a reason string made every one of them look alike in a history
#: view. ``finished`` is a success and nothing else; ``error`` is a failure and
#: nothing else; ``cancelled`` covers every ending that is neither.
#:
#: Both are **execution-class**, because both assert something about the battery:
#: ``finished`` says energy moved, ``error`` says a command failed. Neither is
#: reachable in Shadow, where a lifecycle can only be planned and then cancelled.
ECONOMIC_EVENT_FINISHED: Final = "finished"
ECONOMIC_EVENT_ERROR: Final = "error"

ECONOMIC_EVENT_KINDS: Final = (
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_CHANGED,
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_ERROR,
    ECONOMIC_EVENT_STOPPED,
    ECONOMIC_EVENT_WOULD_START,
    ECONOMIC_EVENT_WOULD_STOP,
    ECONOMIC_EVENT_INHIBITED,
    ECONOMIC_EVENT_AVAILABLE,
    ECONOMIC_EVENT_ENDED,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_REFUSED,
)

#: The kinds that describe *advice*: it appeared, it changed materially, it was
#: withdrawn before it began, its window elapsed, or it asked for something no
#: actuator can perform. Every one of them is a statement about what the
#: optimizer wants, and none is a statement about the battery.
#:
#: ``cancelled`` moved here in beta.16. beta.14 classified it as execution, on the
#: reading that cancelling is something you do to a command in flight. But
#: withdrawing *advice* that has not started is plainly an advice event, and the
#: one-message-per-run design needs to say so: a run announced and then dropped
#: before its window opened must be retracted, or the announcement is left
#: standing as a lie. ``started`` remains the sole execution kind.
ECONOMIC_ADVICE_EVENT_KINDS: Final = (
    ECONOMIC_EVENT_PLANNED,
    ECONOMIC_EVENT_CHANGED,
    ECONOMIC_EVENT_CANCELLED,
    ECONOMIC_EVENT_ENDED,
    ECONOMIC_EVENT_REFUSED,
    # beta.19. Shadow's own lifecycle, and the two pipeline transitions. None of
    # them says the battery did anything.
    ECONOMIC_EVENT_WOULD_START,
    ECONOMIC_EVENT_WOULD_STOP,
    ECONOMIC_EVENT_INHIBITED,
    ECONOMIC_EVENT_AVAILABLE,
)

#: The kinds that describe *execution*: a command went out, and later stopped.
#: Both unreachable while ``CONTROL_EXECUTION_AVAILABLE`` is false, and the emitter
#: refuses them rather than trusting that no caller will ask -- an Activity line
#: reading "started" while the integration sends nothing would be a lie about the
#: battery, which is the one thing this surface must never say.
#:
#: beta.19 added ``stopped`` here rather than making ``started`` do both jobs, and
#: added ``would_start``/``would_stop`` to the *advice* set for shadow. So the
#: refusal below is unchanged in strength while shadow gained a voice: a shadow run
#: cannot accidentally be filed as execution, because it does not carry an
#: execution kind.
ECONOMIC_EXECUTION_EVENT_KINDS: Final = (
    ECONOMIC_EVENT_STARTED,
    ECONOMIC_EVENT_STOPPED,
    # beta.31. A success asserts that energy moved and an error asserts that a
    # command failed, so both belong on the side of the barrier that is refused
    # while nothing can be sent.
    ECONOMIC_EVENT_FINISHED,
    ECONOMIC_EVENT_ERROR,
)

#: What kind of plan a lifecycle is, in the terms a person reads.
#:
#: **beta.31.** Activity used to render the optimizer's own action label, which is
#: a Stage-A word: ``safety_buy``, ``charge``, ``export``. A reader does not want
#: to know which branch of the solver produced a run; they want to know whether
#: their battery is about to buy something it had no choice about, buy something
#: because it was cheap, or sell.
#:
#: Six rather than the four a buy/sell split would give, because two directions
#: this release cannot execute still get planned and still deserve an honest name.
#: The three buy categories come from the purchase attribution -- the same
#: compelled/discretionary split :func:`economic.classify_purchase` publishes --
#: so the category a user reads and the attribution a diagnostic reader audits
#: cannot disagree.
ACTIVITY_CATEGORY_SAFETY_BUY: Final = "safety_buy"
ACTIVITY_CATEGORY_ECONOMIC_BUY: Final = "economic_buy"
ACTIVITY_CATEGORY_MIXED_BUY: Final = "mixed_buy"
ACTIVITY_CATEGORY_ECONOMIC_SELL: Final = "economic_sell"
ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE: Final = "economic_discharge"
ACTIVITY_CATEGORY_CURTAILMENT: Final = "curtailment"

ACTIVITY_CATEGORIES: Final = (
    ACTIVITY_CATEGORY_SAFETY_BUY,
    ACTIVITY_CATEGORY_ECONOMIC_BUY,
    ACTIVITY_CATEGORY_MIXED_BUY,
    ACTIVITY_CATEGORY_ECONOMIC_SELL,
    ACTIVITY_CATEGORY_ECONOMIC_DISCHARGE,
    ACTIVITY_CATEGORY_CURTAILMENT,
)

#: How far an announced run must move before it is worth a second Activity entry.
#:
#: **Deadbands, not buckets.** Until beta.16 the fingerprint bucketed each figure
#: and hashed it, so a value drifting across a bucket boundary fired while a
#: larger drift inside one did not. A deadband measured against the *announced*
#: value cannot flap at a boundary, which is the property that matters.
#:
#: Every threshold is an existing constant rather than a chosen percentage:
#:
#: * energy -- one state-space bucket. A smaller change is not representable in
#:   the optimizer's own state space, so it cannot be a different decision.
#: * power -- the smallest power the device will accept. A smaller change is not
#:   a commandable difference.
#: * time -- one planning interval. A smaller shift cannot change which quarter
#:   the run begins in.
ECONOMIC_DEADBAND_ENERGY_KWH: Final = ECONOMIC_BUCKET_KWH
ECONOMIC_DEADBAND_POWER_KW: Final = CONTROL_MIN_POWER_KW
ECONOMIC_DEADBAND_MINUTES: Final = QUARTER_MINUTES

#: How close a run's start must be before it is announced, in minutes.
#:
#: One planning interval. The plan is rebuilt every quarter, so this is the last
#: refresh before the run begins -- the first moment an announcement is about
#: something imminent rather than about a moving forecast. Announcing earlier is
#: what produced a fresh entry every fifteen minutes for a run eighteen hours out.
ECONOMIC_ANNOUNCE_LEAD_MINUTES: Final = QUARTER_MINUTES

#: How many announced runs to remember. The plan publishes at most this many, so
#: remembering more could never be consulted.
MAX_ECONOMIC_RUNS_TRACKED: Final = MAX_ECONOMIC_RUNS_REPORTED
