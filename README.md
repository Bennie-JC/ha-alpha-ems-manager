# Alpha EMS Manager

[![CI](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Bennie-JC/ha-alpha-ems-manager?include_prereleases&sort=semver)](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Home Assistant custom integration that learns how much electricity your
household **actually** uses, and forecasts what it will use today and tomorrow.

This is **Phase 4**. It observes, learns, predicts, keeps a record of how wrong
its predictions turned out to be, works out what the battery *should* do, and now
builds the complete path from that decision to an inverter command — and stops one
step short of walking it. **Nothing in this integration sends a command to your
battery.** The final step is unreachable, not merely switched off.

---

## Project status

> **Current release: `1.0.0-beta.9` — a public beta.**
>
> The integration is feature-complete for Phase 5 and covered by 1844 automated
> tests, but the learning and forecast model has **not** yet been validated
> across enough real-world complete days to be called stable. Treat it as
> something to run and observe, not yet as something to depend on.
>
> It is safe in one important respect: it never writes to your inverter, never
> issues a charge or discharge command, and cannot change how your system
> behaves. The worst case is an inaccurate forecast.
>
> **This integration is not in the HACS default repository.** Install it as a
> HACS *custom repository* — see [Installation](#installation). A submission for
> default inclusion is intended once a stable release exists.

What still needs real-world observation before `1.0.0` stable:

- Several complete days learned end-to-end, with forecast accuracy compared
  against measured consumption.
- A daylight-saving transition observed live (the logic is covered by tests, but
  has not run through a real one).
- The battery recommendation watched in shadow mode across enough real days to
  trust it before Phase 4 is allowed to act on it.
- One unexplained energy-balance residual on the maintainer's system resolved or
  understood — see [Known limitations](#known-limitations).

---

## Installation

### HACS (recommended)

Alpha EMS Manager is not yet in the HACS default list, so it has to be added as a
custom repository first.

1. In Home Assistant, go to **HACS**.
2. Open the **⋮** menu (top right) → **Custom repositories**.
3. Add the repository:
   - **Repository:** `https://github.com/Bennie-JC/ha-alpha-ems-manager`
   - **Type:** `Integration`
4. Click **Add**, then search HACS for **Alpha EMS Manager** and install it.
   - This is a pre-release, so enable **Show beta versions** in the download
     dialog if `1.0.0-beta.9` is not offered.
5. **Restart Home Assistant.**
6. Continue with [Configuration](#configuration).

### Manual

1. Download the source for the release you want from the
   [Releases](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases) page.
2. Copy the `custom_components/alpha_ems_manager/` directory into your Home
   Assistant `config/custom_components/` directory, so that you end up with
   `config/custom_components/alpha_ems_manager/manifest.json`.
3. **Restart Home Assistant.**
4. Continue with [Configuration](#configuration).

### Upgrading

Upgrade through HACS (or replace the directory for a manual install) and restart.
Learned history is preserved: it lives in `.storage`, keyed per config entry, and
is not touched by an upgrade.

The one exception is **upgrading from 0.1.0**, which is not possible in place —
see the note under [Configuration](#configuration).

---

## What it does today

- Connects energy sources you already have in Home Assistant — AlphaESS, a P1
  meter, Frank Quarter Prices, Solcast — without duplicating any of them.
- Measures real household consumption every 15 minutes using time-weighted
  integration of a house-load power sensor.
- Optionally separates **flexible load** (EV charging) from the household
  baseline, so a charging session does not get learned as ordinary demand.
- Stores up to 365 days of quarter-hour learning history, daylight-saving safe.
- Builds a baseline household-load forecast for today and tomorrow from five
  overlapping look-back windows, with separate weekday and weekend behaviour.
- Adapts today's remaining forecast to what has actually been measured so far.
- Reports how mature and trustworthy the learned model currently is.
- **Records every forecast it issues, and matches it against what the house
  really did.** Each prediction is kept exactly as it stood when it was made,
  together with the model state behind it; once the day is over, the measured
  baseline is matched to it quarter by quarter and the error becomes visible.

### Forecast-error history (new in Phase 2)

The point is not the two new sensors. It is that the evidence behind them is
kept, so that a later phase can eventually work out *why* a forecast was wrong
rather than only that it was.

- A prediction is recorded when it **changes**, not on every refresh. Under the
  current model that means two records per day for each target: one made the day
  before, one made on the day itself.
- Each record keeps the per-quarter prediction, which intervals were modelled
  and which were extrapolated from a neighbour, the number of learned days
  behind it, the weekday/weekend decision, the confidence at the time, and the
  version of the model that produced it.
- When the day is over, the measured **baseline** — the same quantity the model
  predicts — is matched to it interval by interval. A quarter that was never
  measured stays missing; it is never counted as zero consumption.
- Raw quarter-level evidence is kept for **365 days**, matching the learning
  history it can be correlated with. Small daily summaries are kept far longer.
- A day whose two sides are not comparable is **kept but never scored** — the
  clearest case being a flexible-load source selected or removed part-way through
  a day, which makes the morning's baseline and the afternoon's two different
  quantities. A day that merely has *gaps* is still scored, on the intervals it
  has. Diagnostics reports every excluded day together with the facts that
  excluded it.
- `Forecast Error Yesterday` appears as soon as one prior day has been scored.
  `Forecast Error 7 Days` waits for about two full days of compared intervals
  before publishing a rate, and reports its sample size honestly while it waits.

None of this changes the forecast. Nothing in the learning path reads it.

### Battery planning (new in Phase 3)

Phase 3 works out what the battery *should* do and simulates what would happen.
**It never sends a command to your battery, and nothing executes its plan.** It
is published so you can watch it for a few weeks before any later phase is
allowed to act on it.

Three new entities:

| Entity | What it is |
|---|---|
| **Battery Recommendation** | `hold`, `charge` or `discharge`, with the reason in its attributes. `unknown` when a battery setting is missing. |
| **Planned Battery Power** | The power the recommendation implies, in kW, positive for charging. An *average* over the quarter-hour, not an inverter setpoint. |
| **Usable Battery Energy** | How much energy is actually available above your minimum state of charge, in kWh. |

To use it, enter your battery's own figures under **Options → Battery
planning**: usable capacity, minimum state of charge, maximum charge and
discharge power, and round-trip efficiency. The capacity and the two power limits
have **no default** — nothing can work them out from a percentage sensor, and
guessing would produce a plan your inverter could not carry out. Leave them empty
and the three entities read `unknown` and say which figure is missing; learning
and forecasting carry on exactly as before.

**Minimum state of charge is a hard floor.** No recommendation and no simulation
ever goes below it. `0 %` is allowed and means the integration keeps no reserve of
its own, in which case only your inverter's own floor applies.

Everything else — the simulated trajectory, where the floor would bite, the
comparison against leaving the battery alone, the projected state of charge — is
in diagnostics rather than in an entity.

### Control (new in Phase 4)

Phase 4 builds everything needed to turn that recommendation into a real AlphaESS
command: the translation, every safety check, the exact helper values, in the
exact order. **Then it stops.** This release cannot send the command, and not
because a switch is off — the last step is unreachable by a constant in the source.

Two new entities:

| Entity | What it is |
|---|---|
| **Control Mode** | `off`, `shadow` or `active`. Starts at `off`. |
| **Control State** | `inhibited`, `eligible`, `idle` or `off` — what the pipeline decided, with the reason in its attributes. |

**Shadow is the one to use.** It runs the *real* pipeline — the same translation,
the same safety checks, the same command list `active` would use — and writes
nothing. Its diagnostics answer a specific question: *would this command have
been safe, and what exactly would it have sent?* That is why `Control State`
distinguishes `inhibited` (a safety check refused) from `eligible` (nothing
refused, and only the release barrier stopped it).

Set it to `shadow` and watch it for a few weeks. Diagnostics shows which parts of
your AlphaESS control surface were found, what your inverter is currently doing,
the intent, the quantised command, and the ordered list of helper writes.

Two settings, under **Options → Control**: the command duration (a safety timeout
rather than a delivery time — if Alpha EMS stopped running, this is how long
before your inverter returned to normal by itself) and an export safety margin.

**What Alpha EMS will never do**, in this release or a later one:

- write any setting your inverter keeps in flash memory — no schedules, no
  cutoff schedules, no feed-in limit, no grid-safety settings;
- switch off **Excess Export** or **Peak Shaving**. If either is on, Alpha EMS
  stands down and says so, rather than taking the battery from a feature you
  chose;
- touch a dispatch it did not start.

Alpha EMS uses the AlphaESS package's own helpers and its own tested write
sequence. It never writes a Modbus register directly.

### Solar forecast (new in Phase 5)

Alpha EMS was completely blind to the sun until now. It read your PV sensor for
the energy-balance check and nothing else, which had a visible consequence: on a
sunny afternoon it would recommend discharging the battery to cover a load the
panels were already covering.

Phase 5 fixes that by reading a forecast from the **Solcast PV Forecast**
integration you already have, and netting expected production against predicted
load *before* the battery is asked for anything. When the sun covers the house,
the recommendation is to hold — not because a new rule was added, but because the
existing one is now shown the right number.

Nothing is polled. Alpha EMS calls two read-only Solcast actions, both of which
serve that integration's own cache and **consume none of your API allowance**.
The mutating ones — update, force-update, clear-data, set-options, dampening,
hard-limit — appear nowhere in the source, and a test proves it.

**One new setting: which sites are yours.** A Solcast account can hold rooftop
sites that have nothing to do with this system — a second property, someone
else's array — and folding those into your plan would be silently wrong. So
**Options → Sources** now lists your Solcast sites by name and asks you to tick
the ones that feed this AlphaESS system. On an upgrade every site found is
selected for you and written down once; a site you add to Solcast later is
reported as available but is *not* added to your plan without you saying so.

You are never asked which site connects to which inverter, or which is AC- or
DC-coupled. That is not something most people can answer reliably, and a guessed
answer recorded as fact would be worse than the honest "unknown" that is stored
instead.

**Measured production is now recorded too**, per quarter-hour, so the forecast can
be checked against what the panels actually did. Both sides are kept raw:
nothing adjusts the forecast in response to being wrong, and a bad day cannot
change the next one. Every interval is labelled with why it could or could not be
compared — no forecast, no reading, night, one site quiet, not yet elapsed — which
is what a later phase would need in order to learn from it honestly.

**The projected state of charge is now realistic**, where your inverter's own
state says it is storing surplus. If **Excess Export** is on it is not, because
that feature deliberately sends production to the grid instead — so the projection
says so and reports itself as a lower bound rather than quietly overstating your
battery.

**And the export safety check is now measured rather than reconstructed.** Beta.8
compared a proposed discharge against your house load alone, which was
under-protective whenever the panels were already covering that load: on this
installation, real samples with 3.1 kW of PV against 2.0 kW of load passed a check
that should have refused, because the site was already exporting a kilowatt before
the battery was asked to add to it. The absorbing capacity is now taken from the
meter — `import − export + battery discharge` — which is the instrument that
defines export. It needs no PV term at all, so no daylight rule and no assumption
about how your arrays are wired, and it accounts for loads your house-load sensor
cannot see.

## What it does **not** do yet

- ❌ No automatic battery control. It never writes to your inverter.
- ❌ No charge or discharge decisions, no reserve calculation.
- ❌ No energy arbitrage or price-based trading.
- ❌ No EV charge scheduling. Phase 1 only *separates* EV consumption from the
  baseline; it never starts, stops or plans charging.
- ❌ No Solcast-driven *optimisation*. The forecast reduces what the battery is
  asked to supply and makes the projection realistic, but nothing schedules,
  trades or charges on the strength of it. It also never changes the learned
  household load, which is defined to be independent of production.
- ❌ No self-correction. Phase 2 *records* forecast error; it does not feed it
  back into the model. Nothing adjusts itself in response to being wrong.
- ❌ **No battery control, still.** Phase 4 builds the entire control path --
  translation, safety checks, the exact command -- and cannot execute it. No
  service call reaches your inverter, and that is enforced by a build-time
  constant rather than by a setting.
- ❌ No price-aware or economic battery planning, and no automatic charging. No
  cheap-hour buying, no reserve sized for tomorrow's weather, no overnight
  carry-over, no arbitrage. Expected production is an input to the plan, never a
  reason to buy or sell.
- ❌ No self-learning PV correction. Forecast and actual are both recorded raw;
  nothing is adjusted in response to error.
- ❌ No stopping or continuing a dispatch. Nothing in the AlphaESS control
  surface records *who* started one, so Alpha EMS cannot prove a running dispatch
  is its own -- and it will never modify or cancel one it cannot prove it
  created. See [Known limitations](#known-limitations).
- ❌ No API calls of its own — see [No external polling](#no-external-polling).

---

## Requirements

| Integration | Required? | Why |
|---|---|---|
| A **house-load power** sensor | **Yes** | The measurement source. On AlphaESS this is *Current House Load*. |
| A **battery SOC** and **battery power** sensor | **Yes** | The state of charge drives the battery planning and is recorded per quarter-hour; the power is used for the energy-balance check. |
| A **grid power** sensor | **Yes** | Any meter integration: HomeWizard P1, DSMR, SlimmeLezer, … |
| [Frank Quarter Prices](https://github.com/Bennie-JC/ha-frank-quarter-prices) | **Yes** | Set up before adding Alpha EMS Manager. |
| An **EV charger power** sensor | Optional | Enables flexible-load separation. Any W/kW sensor; no brand assumed. |
| A **PV production power** sensor | Only if you have solar | |
| [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) | Only if you enable the PV forecast | |

Alpha EMS Manager is a **data-fusion layer**. It reads entities that other
integrations publish. It never reimplements their communication, never calls
their APIs, and never republishes their entities.

**Minimum Home Assistant version: 2025.1.0.** The integration uses
`entry.runtime_data`, generic `ConfigEntry` typing and coordinator
`config_entry` support, none of which exist in older cores.

---

## Three kinds of load

The distinction that everything else rests on.

| Term | Meaning |
|---|---|
| **Measured house load** | Everything behind your house-load meter, EV charging included. Ground truth; always recorded and never overwritten. |
| **Baseline house load** | `max(measured − flexible, 0)`. The demand Alpha EMS does **not** expect to be able to schedule. This is what the forecast learns. |
| **Flexible load** | Separately measured consumption a future optimiser may move in time. Phase 1 supports EV charging only. |

With no EV sensor configured, **baseline equals measured** and the model behaves
exactly as if the feature did not exist.

### Why the split matters

A battery optimiser fed EV charging as ordinary household demand makes worse
decisions in a specific way: it reserves battery energy to cover a load that is
itself schedulable. It would hold charge for a car that it should instead be
*co-scheduling* with cheap prices and surplus solar. That is a category error,
not a rounding error, and it gets worse as more flexible load is added later.

The two sensors named *Expected House Load Today* / *Tomorrow* therefore forecast
**baseline** demand whenever a flexible-load sensor is configured. Both the
measured and the flexible totals are visible as attributes and in diagnostics, so
the three quantities can always be reconciled.

---

## Why house load is not grid power

Consider a sunny afternoon:

```
PV production      5.0 kW
House load         2.0 kW
Battery charging   3.0 kW
Grid exchange      ~0 kW
```

A model that learned from the **grid meter** would record **0 kW** — the house
appears to consume nothing. A model that learned from **PV** would record 5 kW.
Both are wrong. The household consumed **2.0 kW**.

The same applies at night:

```
PV production      0 kW
House load         1.5 kW
Battery discharge  1.2 kW
Grid import        0.3 kW
```

The learning target is **1.5 kW**, not the 0.3 kW that crossed the meter.

**PV, battery and grid are kept entirely separate from load learning.** They are
recorded only for the optional energy-balance quality check and can never change
a learned interval.

---

## Sign conventions

Nothing about sign is assumed. Both are configurable, with defaults that match
the hardware this was developed against.

| Source | Default | Note |
|---|---|---|
| Battery power | **Negative means charging** | AlphaESS reports e.g. `-664 W` while charging. |
| Grid power | **Positive means importing** | The usual Dutch P1 convention. |

Internally exactly one convention exists:

```
house_load_w        >= 0      ev_w            >= 0
pv_w                >= 0
battery_charge_w    >= 0      battery_discharge_w >= 0
grid_import_w       >= 0      grid_export_w       >= 0
```

If you pick the wrong convention, the energy-balance check in diagnostics shows a
large residual. That is what it is there for — see
[Energy-balance checking](#energy-balance-checking).

EV charging power has **no** sign option, deliberately. It is consumption, so a
value below a narrow noise band is treated as an *invalid sample* rather than
being reinterpreted — reading a negative as zero would leave a charging session
inside the baseline, and subtracting it would inflate the baseline.

---

## How measurement works

A single reading taken at `xx:15` says nothing about the preceding fifteen
minutes, so each power signal is **integrated over time**.

- The most recent reading is held constant until the next one arrives
  (left-handed integration — correct for a sensor that only publishes on change).
- A 60-second safety sampler keeps the accumulators moving when a source is quiet.
- A gap longer than **5 minutes** contributes **no energy and no coverage**. A
  dead sensor can never manufacture consumption.
- An interval is accepted when **≥ 80 %** of it was covered by valid samples.
  Coverage is measured against the full interval, so one joined halfway
  through — which is what happens after every restart — can never qualify.
- `unknown`, `unavailable`, `NaN` and non-numeric states become **missing data**,
  never zero.

House load and EV are integrated **independently**, so the two sensors may update
at completely different rates. Nothing is approximated as `power × 0.25`; a
charging session that starts at minute 8 contributes seven minutes of energy, not
fifteen.

### Missing flexible-load data

If an EV sensor is configured but unreadable for an interval:

- the **measured** value is still stored — ground truth is never discarded;
- the **baseline** for that interval is marked invalid rather than assuming no
  charging;
- a day needs ≥ 80 % *baseline* coverage to count as learned, so a day whose
  charger was down keeps its measured history but does not become a learned day.

An idle charger reporting a numeric **0** is perfectly valid data and costs
nothing. Only *unreadable* is treated as missing.

### Daylight saving

Integration happens in absolute UTC; local wall-clock time is used only to label
an interval with its behavioural slot. A civil day is stored with its **real**
length — 92 intervals on a spring-forward day, 96 normally, 100 on a fall-back
day — keyed by chronological position rather than by wall clock. The repeated
02:00–02:59 hour is therefore retained **twice** and both occurrences feed the
statistics; the skipped spring-forward hour is simply absent, never zero.
Forecasts are generated for the target day's real length too.

---

## The forecast model

A transparent statistical model. No machine learning, no cloud service.

Five overlapping look-back windows are blended per behavioural slot:

| Window | Weight | Role |
|---|---|---|
| 7 days | 0.35 | Recent behaviour |
| 30 days | 0.27 | Current habits |
| 90 days | 0.20 | Seasonal drift |
| 180 days | 0.12 | Half-year shape |
| 365 days | 0.06 | Annual reference |

The overlap is the point: a day inside the 7-day window is also inside all four
longer windows, so recent observations carry more total influence than their
nominal weight suggests.

Windows without at least two observations **drop out and the remaining weights
renormalise**. A three-day-old installation still forecasts.

**Weekday vs weekend** are modelled separately once there is enough of each;
below that the model pools all days rather than overfitting to one Saturday.

**Today's forecast adapts** to what has been measured: if the house has run at
twice the modelled rate all morning the remainder is raised, but only halfway
(damping 0.5) and only within a 0.6–1.6 clamp. Adaptation is suppressed for the
first two hours of the day.

---

## Learning confidence

```
confidence = 100 × maturity × quality
```

`maturity = 1 − exp(−learned_days / 30)`. `quality` is a weighted mean of
baseline **coverage** (0.35), **recency** (0.20), **stability** (0.25) and
**energy balance** (0.20); a component with no data drops out and the rest
renormalise. Because the two are multiplied, **90 days of gappy data cannot score
highly** — day count alone never buys confidence.

A day counts as **learned** when at least **80 %** of the intervals that actually
occurred carry a valid *baseline*. Only completed days count.

### How long until it is useful?

| Elapsed | What to expect |
|---|---|
| **Day 1** | Only the intervals seen so far exist. `Learning days` is still 0 and confidence is near zero. Both forecasts may read `unknown`. Normal. |
| **2 days** | A forecast appears. Treat it as a rough order of magnitude. |
| **~7 days** | An initial usable short-term pattern; weekday and weekend are still pooled. |
| **~30 days** | Meaningful recent behaviour; the weekday/weekend split is active and confidence is moderate. |
| **~90 days** | Stronger day-type and early seasonal context. |
| **~180 days** | Broader seasonal context. |
| **365 days** | Full retained annual context; the 365-day window contributes throughout. |

These describe how much *history* the model has, not a promised accuracy. No
accuracy percentage is claimed, because none has been measured yet.

---

## Entities

Exactly six. Everything else lives in diagnostics.

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.alpha_ems_expected_house_load_today` | kWh | Predicted **baseline** consumption today |
| `sensor.alpha_ems_expected_house_load_tomorrow` | kWh | Predicted **baseline** consumption tomorrow |
| `sensor.alpha_ems_learning_confidence` | % | How mature and trustworthy the model is |
| `sensor.alpha_ems_learning_days` | — | Calendar days with sufficient valid baseline data |
| `sensor.alpha_ems_forecast_error_yesterday` | kWh | Yesterday's forecast minus what was measured |
| `sensor.alpha_ems_forecast_error_7_days` | % | Rolling forecast error over the last 7 days |

Neither forecast sensor declares a `state_class`. They carry `device_class:
energy` so the UI formats them properly, but a *prediction* must not become a
long-term statistic or appear on the Energy dashboard next to measured
consumption.

The two error sensors are the other way round. They measure something that has
already happened, so they *do* carry a state class — but no `device_class`,
because a signed difference is not consumption and must not be offered to the
Energy dashboard.

**Reading the error sensors.** `forecast_error_yesterday` is signed: **positive
means the forecast was higher than reality**. `forecast_error_7_days` is
`sum(|error|) / sum(actual)` over the window — so "8 %" means the model was off
by eight per cent of the energy it was predicting. It is deliberately *not* an
accuracy score: there is no `100 − error` figure anywhere, because that number
goes negative on a bad week and invites comparison with unrelated systems.

Both read `unknown` until there is something honest to report, and stay
`unknown` rather than showing `0` — zero is the value of a *perfect* forecast,
not of a missing one. Expect the first value the morning after your first
complete day, and the rolling figure after roughly two.

Attributes are small scalars only. **No per-interval profile is ever exposed** —
the recorder writes every attribute on every state change.

---

## Storage

- Home Assistant's `Store`, keyed per config entry so two instances never share
  state.
- One entry per **real** quarter-hour interval, chronologically indexed from
  local midnight. Rejected intervals are `null`, so a gap stays a gap.
- Measured and flexible energy are both persisted; baseline is always derived,
  which keeps the relationship auditable and reversible.
- Retention: **365 days**, oldest pruned automatically.
- Writes are debounced (60 s) and flushed on unload and on Home Assistant stop.
- **Schema version 2.** A version 1 document (the fixed 96-slot development
  format) is discarded on load with a warning rather than misread; it cannot
  represent a fall-back day. No other Home Assistant storage is touched.

### Forecast history

Kept separately from the learning history, on its own schema version, so that a
problem with one cannot damage the other.

- A small always-loaded index, plus one document per calendar month of predicted
  days. Home Assistant rewrites a whole document on every save, so a single
  year-long file would be a megabyte through the disk on each write — and one
  corrupt byte would cost the lot.
- Roughly **3.5 kB per day**, so about **1.3 MB** at the 365-day steady state.
- Raw per-quarter evidence is pruned at 365 days; the reduced daily summaries
  behind the rolling figure are kept for years at about 200 bytes a day.
- If a document cannot be read, nothing is written for the rest of the session
  and the file is left exactly as found. If the *learning* history cannot be
  read, no day is matched at all, rather than permanently recording that every
  measurement was missing.

---

## No external polling

Alpha EMS Manager makes **no outbound network requests**: an empty `requirements`
list, no HTTP client imported anywhere, and no polling interval on its
coordinator. Frank and Solcast availability is determined by inspecting the
config-entry registry, not by contacting them. This is enforced by tests.

---

## Configuration

Add via **Settings → Devices & Services → Add Integration → Alpha EMS Manager**.
Frank Quarter Prices must already be configured.

Five short steps: identity and sources, battery, solar (skipped without PV),
grid meter, and the price/forecast integrations. Every selection is validated
against the live state machine — **units** are checked rather than entity names.

Frank and Solcast are selected as **config entries**, so renaming an entity later
cannot break anything. AlphaESS, the grid meter and the EV charger are selected
as **entities**, because the AlphaESS Modbus package is a YAML template package
with no config entry to reference.

All sources can be changed later through the options flow. Learned history is
preserved across a reload.

> **Upgrading from 0.1.0.** The two configuration models share no keys, so an
> old entry **cannot** be migrated and will refuse to load with a clear error.
> Remove the integration and add it again. The old `.storage` file
> (`alpha_ems_manager_learning`) is left untouched and can be deleted by hand.

---

## Energy-balance checking

After sign normalisation the flows should roughly satisfy:

```
PV + grid import + battery discharge  ≈  house load + battery charge + grid export
```

This is a **data-quality signal only**. It never rejects a learning interval; it
feeds the confidence score and diagnostics.

**Tolerance** is an allowance in watts, built from the three physical reasons the
identity cannot close exactly:

```
allowed = 40 W                    fixed:  inverter auxiliary draw + rounding
        + 5 %  of DC-side power   PV and battery cross a conversion stage
        + 3 %  of AC-side power   grid and house load are separate instruments

a sample passes when |residual| <= allowed
```

Why not a flat percentage: on a hybrid inverter, PV and battery power are
measured on the **DC** side while house load and grid are **AC** quantities. The
residual therefore scales with how much energy is being *converted*, not with
total power. A flat percentage is wrong in both directions — too strict for a
battery delivering 240 W AC from 300 W DC (a healthy 20 % "error" at low load,
where inverter efficiency genuinely falls away), and far too loose at high power,
where 15 % of 10 kW would hide a mis-selected entity.

A sign inversion or a wrong entity lands ten to twenty times over this allowance,
so it is still caught immediately and unambiguously.

**Your sources do not share a clock.** A house-load template can publish a fresh
reading the instant a kettle switches on, while the battery and grid meters still
describe the previous few seconds. Two mechanisms stop that from being reported
as a configuration error:

- **Coherence gating** — if the participating sources' most recent reports are
  more than 90 s apart, or the oldest is over 5 minutes old, the sample is
  *skipped*. Not a pass, not a failure, no warning. This catches a source that
  has genuinely stopped.
- **Sustained-failure debounce** — a warning needs **3 consecutive coherent
  failures**. Sampling is once a minute, so that means roughly three minutes of
  continuous imbalance. A load step resolves in seconds; a wrong sign convention
  fails every single sample.

**Pass rate** is `passed / eligible`, where eligible excludes skipped samples.
Counting them as failures would blame your configuration for an integration's
polling schedule; counting them as passes would hide a dead source.

Warnings are additionally rate-limited to one per hour per cause.

---

## Diagnostics

**Settings → Devices & Services → Alpha EMS Manager → Download diagnostics.**

Includes configured sources and availability, normalised readings, sign
conventions, measured **and** baseline coverage, flexible-load status, learned
and retained interval counts, the full confidence derivation, forecast totals,
the last energy-balance residual, storage schema version, and Frank/Solcast
availability. No credentials — the integration holds none — and no history dump.

The `forecast_history` block carries everything the two error sensors do not:
how many predictions are stored and for which days, how many are still waiting
to be matched, forecast error broken down by look-ahead, by time of day and by
whether an interval was modelled or extrapolated, the model version and
parameter fingerprint behind each record, and the health of every storage
partition.

---

## Troubleshooting

Enable debug logging first:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.alpha_ems_manager: debug
```

**Expected load stays `unknown` or 0**
Two full days of history are needed before any forecast appears. Check
diagnostics → `learning.measured_valid_intervals` is increasing. If it is 0,
the house-load source is not producing usable values — check
`sources.house_load.state` and `.unit`.

**Learning days does not increase**
A day needs ≥ 80 % *baseline* coverage and must be complete; today never counts.
Compare `learning.measured_coverage` with `learning.baseline_coverage` in
diagnostics: if measured is high and baseline is low, the flexible-load sensor is
the problem, not the house-load sensor.

**Learning confidence stays low**
Confidence is `maturity × quality`, and maturity saturates slowly by design — 30
days is only ~63 %. If it is lower than the day count suggests, check the
`confidence` block: `coverage`, `recency`, `stability` and `balance` are all
reported separately.

**EV configured but baseline learning incomplete**
Look at `flexible_load.intervals_without_valid_data`. A charger that reports
`unavailable` when idle (rather than `0`) will invalidate the baseline for every
idle interval. Either pick a sensor that reports a numeric zero, or leave the EV
field empty.

**Energy-balance warnings**
`Sustained energy-balance mismatch over N consecutive checks` means the identity
failed three coherent samples in a row — roughly three minutes. A *single* odd
instant never warns. The message comes in two forms, and they mean different
things:

- *"…usually means one term of the identity is wrong"* — the residual is many
  times its physical allowance. Check the selected entities and the two sign
  conventions; something is genuinely misconfigured.
- *"…consistent with the sources being measured at different electrical
  boundaries"* — the residual is only moderately over the allowance. This is far
  more likely to be your inverter's DC/AC measurement boundaries or conversion
  losses than a mistake you made. Learning is unaffected.

In diagnostics, `energy_balance.last_coherent_sample` now reports `mode`,
`flows_w`, `residual_w`, `allowed_residual_w` and `tolerance_reason` — enough to
attribute a residual to an operating mode without reproducing it. Also compare
`failed_samples` against `skipped_incoherent_samples`: a high skipped count with
`source_time_skew_seconds` near the 90 s limit means one source is simply polling
much more slowly than the others, which is a timing observation, not a fault.

**Source entity unavailable**
Gaps are recorded as missing coverage, never as zero. Short outages are absorbed;
longer ones reduce the day's completeness. Warnings are rate-limited to one per
hour per cause, so a long outage will not flood the log.

**Legacy config entry detected**
`Alpha EMS Manager config entry … uses the version 1 source model` means an entry
from 0.1.0 is present. Remove the integration and add it again.

---

## Known limitations

Beyond the Phase-1 scope listed at the top, these are the current honest caveats:

- **Beta status.** Real-world learning and forecast validation is ongoing. The
  model has not been observed across enough complete days, nor through a live
  daylight-saving transition, to justify a stable release.
- **Phase 4 builds and validates the control path but cannot execute it.** This
  is deliberate, and it is not only caution. Two things are unresolved. First,
  nothing in the AlphaESS control surface records who armed a dispatch, so Alpha
  EMS cannot prove a running one is its own — which blocks both stopping a
  command and continuing one past a single fifteen-minute interval. Matching
  power, cutoff or duration is *not* proof: the person most likely to have set
  exactly those figures by hand is you, watching the shadow recommendation.
  Second, today's recommendation is the discharge that covers your predicted
  load, which your inverter already does by itself — and does better, because it
  tracks load continuously while a fixed-power command cannot. **Do not read
  future control as proven.** It has not been physically tested, and enabling it
  will be a separate, explicitly authorised step.
- **The energy-balance warning is not used to block control.** On this
  installation the house-load figure is derived from the inverter's own grid
  register while the balance check reads a separate meter, so the residual is
  really the difference between two meters — the battery term cancels out and the
  state of charge never enters it. It can therefore grow large without anything
  being wrong with the readings a battery command depends on, and two real
  samples (+1394 W and −10149 W) did exactly that. The warning is unchanged and
  still fires; no tolerance was widened. It simply is not evidence about the
  right thing, so control does not consult it. More detail is now recorded in
  diagnostics — the sign of each failure, and how the overshoot scales with power
  — which is what could eventually tell a measurement boundary from a real fault.
- **Forecasts start out unavailable, and that is correct.** Around two full days
  of valid history are needed before any forecast appears. The model does not
  fabricate a value to avoid an empty state — an honest `unknown` is more useful
  than a confident guess. Learning confidence then rises slowly by design.
- **An EV sensor that reports `unavailable` while idle will stall learning.**
  A missing flexible-load reading invalidates that interval's baseline rather
  than being assumed to be zero, so an idle-unavailable charger invalidates most
  of the day. Prefer a sensor that reports a numeric `0`, or leave the EV field
  empty.
- **A recurring energy-balance residual at low power.** On the maintainer's own
  system a sustained, coherent residual — roughly 154 W on 740 W of supply, and
  again 155 W on 661 W — is reported from time to time and then clears on its own
  (the recorded pass rate is around 99 %). The grid figure comes from a separate
  P1 meter while house load, PV and battery all come from the inverter, so a
  roughly constant offset between two instruments on different electrical
  boundaries is the leading explanation: negligible on a busy afternoon, a large
  fraction of a quiet one. The residual's sign is the direction unmeasured
  conversion loss would take, not the direction a sign error would.

  No threshold has been widened to silence it: doing so would blind the check at
  every power level to explain one regime, and hiding a wiring or sign error is
  worse than the warning. It **cannot** affect learning — the check is a quality
  signal and can never reject an interval — and it cannot affect forecast-error
  scoring either. It does slightly depress the reported confidence score.
- **The simulation can see your solar now, but only as well as Solcast can.**
  Where a forecast covers an interval, production is netted against predicted load
  and surplus is modelled as stored — so the projection is realistic rather than
  the "what if there were no sun" answer beta.8 gave. Three caveats remain, and
  diagnostics states which applies. Without a forecast an interval is still
  PV-blind. With **Excess Export** on, your inverter deliberately sends surplus to
  the grid instead of the battery, so the projection is reported as a *lower*
  bound. And forecast and measured production sit on different electrical
  boundaries — your PV figure sums DC strings and an AC meter, and Solcast does
  not state its own — so a persistent difference between them is a property of the
  installation rather than forecast error. That is recorded, never corrected.
  The projected state of charge is still not published as an entity.
- **`Usable Battery Energy` is an upper bound.** It applies a single round-trip
  efficiency figure, which flatters a real inverter at low power, and it does not
  model the inverter's own standby draw. Both make the number slightly optimistic.
  Neither is guessed at, because doing so would mean inventing figures for your
  hardware.
- **Which Solcast site is on which inverter is unknown.** Solcast divides a roof
  by orientation and AlphaESS divides it by electrical coupling, so having the
  same number of each proves nothing. You declare which sites are *yours*, and
  that is all that is asked; the correspondence is recorded as unknown rather than
  guessed. It is not needed for planning, and it would matter only to a later
  phase trying to attribute a difference between forecast and actual.
- **Clipping looks like forecast error, and is flagged rather than corrected.** If
  your array can out-produce your inverter's AC limit, the forecast will exceed
  the measured figure on the best days by design. Where the limit is readable, the
  day is flagged; where it is not, the check is switched off rather than guessing a
  ceiling.
- **Solcast's own settings can change what "raw" means.** If you enable its
  auto-dampening or actuals blending, the series Alpha EMS reads has already been
  adjusted by something else. Both are recorded in diagnostics, and both were off
  on the installation this was built against.
- **No in-place upgrade from 0.1.0.** The configuration models share no keys.
- **Single house-load source.** Multi-phase or multi-meter summing is not
  supported; provide one already-summed power sensor.

---

## Development

```bash
pip install -r requirements-test.txt
python -m pytest          # pytest.ini supplies everything
ruff check .
ruff format --check .
```

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the developer reference for the
learning, persistence and energy-balance layers. Read it before changing any of
them — it records decisions that a reasonable-looking change would silently undo,
particularly around daylight saving and interval identity.

CI runs the same lint and test commands, plus Home Assistant's `hassfest`
validator and HACS validation, on every push and pull request.

> **Windows note.** Home Assistant imports the POSIX-only `fcntl` and `resource`
> modules at startup, and `pytest-homeassistant-custom-component` pulls those in
> while its plugin loads — before any `conftest.py` runs. `tests/win_compat.py`
> installs inert stubs and is loaded via `-p tests.win_compat` in `pytest.ini`.
> It does nothing on Linux or macOS.

---

## License

See [LICENSE](LICENSE).
