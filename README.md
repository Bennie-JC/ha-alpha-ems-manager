# Alpha EMS Manager

[![CI](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Bennie-JC/ha-alpha-ems-manager?include_prereleases&sort=semver)](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Home Assistant custom integration that learns how much electricity your
household **actually** uses, and forecasts what it will use today and tomorrow.

It observes, learns, predicts, keeps a record of how wrong its predictions turned
out to be, works out what the battery *should* do, reads what the sun and the
market are expected to offer, and now works out how much energy the battery ought
to be holding — then builds the complete path from that decision to an inverter
command and stops one step short of walking it. **Nothing in this integration
sends a command to your battery.** The final step is unreachable, not merely
switched off.

---

## Project status

> **Current release: `1.0.0-beta.39` — a public beta.**
>
> Stage A is feature-complete. Stage B — the physical execution controller — is
> wired end to end and, from beta.24, **can charge your battery**. From beta.27 it
> executes each 15-minute plan interval as an explicit energy target, and can also
> **export to the grid** when the plan says so. Covered by 4320 automated tests.
>
> **Two actions are executable: buying energy into the battery, and selling it back
> out.** Serving the house from the battery, and curtailing production, are
> calculated, published and explained, and are refused
> at three independent boundaries before anything reaches the inverter. That is a
> property of the source, not a setting you could clear by accident.
>
> **Charging does nothing until you ask for it twice.** The Control Mode select
> must be set to *Live*, and command sending must be enabled in the options. A fresh
> installation is `off`, an upgrade changes neither, and until both are set the
> integration behaves exactly as it did before: it writes nothing at all.
>
> The learning and forecast model has still **not** been validated across enough
> real-world complete days to be called stable. Treat it as something to run and
> observe, not yet as something to depend on.
>
> In *off* and *shadow* it never writes to your inverter and cannot change how your
> system behaves; the worst case is an inaccurate forecast. In *Live* it can buy
> energy on a schedule it believes is cheap, and — from `beta.29` — it can **sell
> energy back to the grid** when the plan says that is worth more. It can stop doing
> either at any time: by reaching the target, by running out of room, by the plan
> being withdrawn, by a safety condition, or because you switched the mode back.
>
> It can never discharge to serve your house, and never curtails production.
>
> **Export has not yet been validated on real hardware.** Charging was validated on
> the live installation in `beta.26`; exporting has been proven only in tests. If
> you enable *Live* on this release, watch your grid meter during the first planned
> export quarter.
>
> **`beta.34` is the release that read its own diagnostics.** `beta.33` ran a full
> supervised *Live* day on the reference installation, and two diagnostics captures
> from it are the evidence for everything in this release. The campaign machinery
> worked on real hardware. Eleven defects surfaced across four layers, none of them
> visible from the test suite — because in every case a test existed that pinned the
> behaviour under a condition production cannot produce.
>
> **The export protection was using a day index where an offset was expected.** The
> live installation published a survival requirement of **23.09 kWh on a 21.6 kWh
> battery** and treated it as a hard test, which forbade every battery-caused export
> at every interval: not a protection, a prohibition. The window is now converted at
> the boundary, the floor is clamped to what the pack can actually hold, and where a
> requirement genuinely cannot be met the decision falls back to the price
> comparison rather than vetoing outright. With that fixed, a two-day horizon selects
> **two complete buy-sell cycles**, and seeing tomorrow's prices no longer changes
> what the plan does today.
>
> **A dispatch was armed that ownership could never prove was ours.** For twenty
> minutes on 2026-08-29 a real charge ran that the controller correctly refused to
> touch, and only the inverter's own dead-man ended it. The ownership claim now
> follows whichever authority produced the command, and an arm with no authority at
> all is refused before anything is written.
>
> **A successful charge was recorded as a failure.** 1.063 kWh delivered against
> 1.11 planned appeared in the logbook as *Failed — Measurement Unavailable*. Three
> separate faults produced that sentence and all three are fixed: the objective is
> captured before it can be lost, "no target was published" is no longer filed as
> "the measurement cannot be trusted", and the completion tolerance is no longer the
> smallest command the hardware can issue.
>
> **Two entities were describing the wrong tense.** *Economic Action* announced
> tomorrow evening's sale as though it were happening now, with a dateless clock
> window as the only clue. It now reports the present interval only, and there is a
> **new `Next Planned Action` sensor** carrying the plan with full timestamps.
> *Control State* read *Executing* while the dispatch was off and the only thing
> written was a stale ownership marker; it now says `executing` only while a command
> that moves the battery is on the wire.
>
> **`beta.33` connects the campaign layer `beta.32` shipped unwired.** Every
> published execution target carried `campaign_id: null`, so no campaign ever
> opened: the realised accumulator never advanced, the objective was never frozen
> and no campaign terminal was ever filed. It was found in a live diagnostics
> download rather than by a test, because the campaign tests all constructed their
> own identities by hand. A multi-segment sale — an export, a quarter where the
> house eats everything the pack gives it, then the rest of the export — is now one
> campaign with one lifecycle rather than three unrelated runs.
>
> **`beta.33` also closes an audit of all thirty settings you can change.** A
> minimum state of charge at or above 50 % produced no economic plan at all, silently
> — that is fixed. *Command duration* has been **removed**: it never reached the Live
> Dispatch duration register, which is driven by an internal safety dead-man that
> alternates on its own and is not a preference. A stored value stays where it is,
> does nothing, and needs no migration. The battery wear cost per kWh has been
> implemented since `beta.18` and reachable only by hand-editing storage; it now has
> a field, still defaulting to zero. And several published fields that had quietly
> become untrue — a `serve_load` row claiming it was armable, a blocked reason that
> always read `execution_unavailable`, and the package docstring's claim that no
> command reaches the battery — now report what is actually the case.
>
> **`beta.32` fixed what sits around the optimiser.** `beta.31` shipped the right
> economics; the layers that describe, group and protect the plan had not caught up.
> Three things a user will notice. A sale is now refused when its price does not beat
> what that energy is worth to the house before the next cheap window — selling at
> 0.29 to buy the same energy back at 0.30 is a loss whatever the headline price
> says, and on the measured scenario that turns 0.833 kWh of compelled purchase into
> zero while still spending the pack down to 25.6 %. One decision now reads as one
> story: fifteen log lines for three decisions become three, and a finished export
> reports what it delivered instead of ending in silence. And an export run can no
> longer report success before delivering anything — it compared a *battery ceiling*
> against a completion tolerance, so a run whose ceiling was 0.25 kWh stopped on its
> first evaluation and called it done.
>
> **`beta.31` changed how the battery decides to spend money.** Until then the
> planner held whatever inventory would have covered the whole forecast *with no
> further grid purchase ever* — a figure that reached 73 % SoC against a 20 % floor
> and immobilised 97 % of the usable pack. It credited future sun and refused to
> credit the future grid, so it paid real money to hold energy it did not need.
> `beta.31` made the hard constraint physical reachability, priced the inventory
> left at the end of the priced horizon instead of bounding it, and put every
> discretionary purchase through the economic gates. The pack is working inventory:
> buy low, displace expensive import, let the charge fall, refill at the next
> attractive feasible window. `beta.32` keeps every one of those properties and adds
> the one thing that architecture was missing — a *price* standing between a
> discretionary export and the 20 % floor, where before there was only a constant
> 0.42 kWh margin.
>
> **`beta.30` was the release whose controller could finally hold the wheel** —
> ownership proven from evidence Alpha EMS writes itself, and the boundary fault
> that skipped every second quarter of a multi-quarter run. All of that Stage-B
> behaviour is carried into `beta.32` unchanged. `execution.py` gains exactly one
> fix — the export completion test above — and `safety.py`, `dispatch.py`,
> `control.py`, `quarter.py` and both AlphaESS modules are untouched.
> **If you are running anything earlier, upgrade.**
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
     dialog if `1.0.0-beta.39` is not offered.
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
| **Control Mode** | **Off**, **Shadow** or **Live**. Starts at Off. (The stored value behind "Live" is still `active`, so nothing that already reads it breaks.) |
| **Control State** | `inhibited`, `eligible`, `idle` or `off` — what the pipeline decided, with the reason in its attributes. |

**Shadow is the one to start with.** It runs the *real* pipeline — the same
translation, the same safety checks, the same command list Live would use — and
writes nothing. Its diagnostics answer a specific question: *would this command have
been safe, and what exactly would it have sent?* That is why `Control State`
distinguishes `inhibited` (a safety check refused) from `eligible` (nothing refused,
and only the mode or the opt-in stopped it).

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

### Prices, and why they change nothing yet

Alpha EMS reads quarter-hour electricity prices from your
[Frank Quarter Prices](https://github.com/Bennie-JC/ha-frank-quarter-prices)
integration and normalises them onto the same interval identity as load and
generation. **No price value reaches a battery decision.** That is not a policy
statement to be trusted — the price layer is not importable from the modules that
decide anything, no identifier in those modules is an economic term, and obtaining
prices calls no service at all. All three are asserted by tests that fail if the
structure changes.

If your battery recommendation moves after upgrading to beta.12, that is a bug.

**Three prices, and only one of them is a measurement of the same kind.** The
source publishes a wholesale price and an all-in purchase price for each interval.
It publishes **no export price at all** — the upstream endpoint has no such field —
so the export figure is *reconstructed* from the wholesale price plus the
adjustment you configured in Frank, and every interval carries a label saying
which rule produced it. A configuration-derived estimate must never be readable as
a published price.

That asymmetry matters more than it looks. Sourcing markup plus energy tax is a
fixed **0.129 EUR/kWh floor** on the import side and absent from the export side,
so on a negative wholesale interval **importing still costs money while exporting
earns a negative amount**. Import and export are not two signs of one number, and
a later phase cannot answer "what does buying cost" and "what does exporting
earn" from a single field.

**The two feed-in settings are read from Frank, not duplicated here.** You
configured them once, the return-price sensor on your dashboard is derived from
them, and a second copy in Alpha EMS would drift away from the figure you can see.
There is nothing new to configure.

**The next day being absent before about 13:00 is normal.** Frank does not request
tomorrow's prices before noon market time and publishes them around 13:00 to
14:00. Until then a healthy installation shows today complete and tomorrow absent,
and Alpha EMS reports that with its own reason rather than as a fault. It draws no
conclusion from the clock in either direction: publication can be late, and if
Frank already has the day before 13:00 it is consumed. The midnight rollover is
Frank's to perform — Alpha EMS copies nothing, retains nothing stale and
synthesises no day-after-next.

**Unknown is never zero.** An interval with no price is absent from the series. An
interval priced at zero is a known zero. Beyond the horizon there are no intervals
at all — not placeholders, not zeroes — because a later phase that confused the two
would plan free electricity across a hole in the data.

Coverage below 1.0 is normal for some installations rather than a fault: Frank
publishes a *market* day, midnight to midnight in the market's own timezone, while
Alpha EMS plans a Home Assistant civil day. If you run Home Assistant outside
`Europe/Amsterdam` or `Europe/Brussels` those are different spans, so part of your
local day is priced by a market day that may not exist yet. Mapping is by instant
for exactly this reason, and the shortfall is reported rather than extrapolated
away.

The `price` block in a diagnostics download carries the counts, coverage, the
horizon, both cross-checks against Frank's own current-interval figures, and the
reason the next day is absent. It never carries the series itself.

### Battery reserve (new in Phase 7)

`sensor.alpha_ems_dynamic_battery_reserve` answers one physical question, in kWh:

> How much stored energy must be present so the battery never runs short of the
> net demand it can physically serve, over the forecast horizon?

**It is calculated, published, and obeyed by nothing.** Your configured minimum
state of charge is still the one hard floor, and the planner still discharges to
it. If this sensor reads higher than the floor, that is a *report* — Alpha EMS
does not hold energy back to satisfy it, does not buy energy to reach it, and
issues no command either way. Deciding what to do about a shortfall is Phase 8.

**Sunshine lowers it; a dark forecast raises it.** There is no season, no month
check and no summer or winter mode — the behaviour falls out of load and
production alone. On a summer night with a sunny day forecast, the battery does
not need to carry energy the sun is expected to supply before the evening needs
it. In midwinter with little production forecast, there is nothing to credit and
the figure rises.

Three things are worth understanding before you rely on it.

**It is not a maximum.** The requirement is low while replenishment is imminent
and high once it has passed, so it rises and falls through the day rather than
decaying. At midday on a sunny day it can read as low as your configured floor —
correctly, because this afternoon's production is expected to refill the pack long
before tonight draws on it. The largest requirement anywhere in the horizon is
reported separately in diagnostics as `peak_required_reserve_kwh`, and it is
deliberately *not* what the sensor shows: holding the peak now would reserve
energy the sun is about to supply.

**It assumes forecast surplus reaches the battery.** That assumption is what makes
the figure useful rather than degenerate, and it is a forecast rather than an
observation. The `replenishment_dependency_kwh` attribute says how much of the
reduction rests on it: when it is large, the figure beside it is optimistic. If
you run with Excess Export or Peak Shaving permanently enabled, surplus never
reaches your battery at all — read `required_same_interval_only_kwh` in
diagnostics instead, which is the same calculation with that assumption removed.

**It carries no margin for being wrong.** It is a point estimate over the load and
production forecasts, with no allowance for forecast error. Where the measured load
bias is negative — the model under-predicting — the requirement may be low. Both
figures are in the `reserve` diagnostics block. Learning from measured error is
Phase 9.

`lower_bound_reason` says when the figure understates: `truncated` means demand
continues past the last interval anyone forecast, and `headroom_limited` means some
requirement in the horizon exceeds the whole pack. Detected and reported, never
silently corrected.

### Economic plan (new in Phase 8)

`sensor.alpha_ems_economic_action` answers a different question from every sensor
before it:

> Given the prices, the load and the production that are actually known, and
> subject to the reserve, what is the cheapest thing to do with the battery?

Its state is what Alpha EMS **wants** to do — `hold`, `charge`, `discharge`,
`export`, `curtail_pv` or `safety_buy`. Two attributes sit beside it and both
matter more than the state:

- `capability_action` is what actuators that actually exist could produce. Export
  and photovoltaic curtailment have **no** primitive in this release, so a desired
  `export` will regularly show a capability of `discharge` or `hold`.
- `execution_blocked_reason` is why nothing is sent, deepest reason first. It
  reads `execution_not_enabled` or `mode_not_active` until you turn both switches
  on, and `live_charge_only` for an action this release does not execute -- which is
  a different fact from `no_primitive_export`, and reported differently.

**It is a different question from `Control State`, and they will disagree.**
`Economic Action` answers "what is the cheapest thing to do with the battery?";
`Control State` answers "would the control pipeline presently allow a command at
all?". So `export` beside `inhibited` is not a contradiction — it is the optimizer
wanting something and the safety pipeline reporting that nothing is sendable, most
often `power_below_device_minimum`, which simply means the intent is smaller than
the 0.2 kW step the inverter accepts. Nothing is sent either way.

**It is calculated, published, and executed by nothing.** No service call reaches
your inverter. There is no cheap-hour buying, no selling, no curtailment — the
plan is a recommendation you can watch, compare against your bill, and disagree
with.

Three things are worth understanding before you read it.

**The optimum is not shaped by what can be built yet.** Two plans are computed
independently: one over every action the physics allows, and one over the actions
with a primitive. `economic_value_forgone_eur` in diagnostics is the euro
difference — that is, what building the missing actuator would be worth. Letting
the absence of a primitive distort the optimum would have made that number
impossible to state.

**Safety always wins, at any price.** The objective compares reserve feasibility
*before* cost, so a shortfall can never unlock a profitable export. There is no
fallback and no mode switch: when a shortfall cannot be avoided at all, the first
comparison simply ties and the plan minimises cost while holding the shortfall at
its unavoidable minimum. A `safety_buy` is a charge the reserve is responsible
for, identified by re-solving with the reserve relaxed rather than by guessing
from the price.

**Two behaviours are off until you turn them on.** Charging from the grid and
selling from the battery are both opt-ins on the new **Economics** options page,
and both default to off. They change what the plan says, not what happens — a
battery that may only store its own sunshine has far less to decide, which is why
the sensor often reads `hold` out of the box.

**Your solar filling the battery is not "charging from the grid".** This is the
distinction the sensor is built around:

> your battery physically charging from surplus production
> **is not**
> Alpha EMS choosing to buy energy from the grid

`charge` as a state, and the *Allow charging from the grid* opt-in, both mean the
second thing only. When your panels fill the battery while Alpha EMS is not
economically doing anything, the sensor reads **`hold`** — that absorption creates
no action, costs no minimum gain, needs no opt-in, and is still part of the
trajectory the plan is computed over. It can absolutely create value later,
through the energy it put in the pack; it is just not a decision anybody made.

So a sunny afternoon with both opt-ins off will show `hold` while the battery
fills. That is correct, not a fault.

There are two economic knobs on that page and they are **not** the same thing.

`minimum_trade_gain_eur` is a **fixed** amount a single charge or discharge must
earn before it is worth planning at all — charged once per stretch of one action
rather than per kilowatt-hour, and **only** for actions Alpha EMS actually chose,
never for absorbed sunshine. It stops a plan full of two-cent trades. It is not a
wear model; it is your answer to "how small is too small".

`grid_charge_margin_eur_per_kwh`, new in beta.18 and **off by default**, is an
**additional per-kilowatt-hour** requirement on energy a charge actually causes to
be bought from the grid. It exists because a fixed amount does not scale: once a
trade clears it, the volume behind it is unconstrained, so a fourteen-kilowatt-hour
round trip can be planned while earning under four cents a kilowatt-hour. Set this
and every bought kilowatt-hour has to earn its keep. Your own solar entering the
battery, the solar share of a mixed quarter, discharging to supply the house, and
charging to protect the reserve are all outside it — the last of those because
keeping the house supplied outranks every economic figure, at any margin.

Since beta.16 a sunny quarter in the middle of a charging campaign no longer ends
that campaign. Absorbed sunshine still creates no action of its own, but it is now
*transparent* to a charge that is already under way, so one afternoon of buying
interrupted by cloud gaps pays the gain once rather than once per gap. A quarter
where the battery genuinely does nothing still ends the run, exactly as before.

Every planned run appears in the `economic_plan` block of a diagnostics download
with all five energy boundaries stated separately — the two battery-side AC
figures, the two grid-side ones, and the curtailment — because every euro in the
payload is priced on grid energy and a reader has to be able to check that.

**A note on upgrading to beta.24: it can charge your battery now, if you ask
it to twice.**

This was the first release in which Alpha EMS could send a command to your
inverter, and it could do exactly one thing: **buy energy into the battery** when
the plan said the price was worth it. Discharging, exporting and curtailing were all
calculated and explained, and refused before anything reached the hardware.

> **Superseded by `beta.27`**, which also executes authorised **net export**. The
> paragraphs below describe `beta.24` as it shipped and are kept for anyone
> upgrading across several releases; see [Project status](#project-status) for what
> the current release does.

**Upgrading changes nothing on its own.** Two switches have to be on: the Control
Mode select has to say *Live*, and command sending has to be enabled in the options.
A fresh installation is `off`, and upgrading from beta.23 leaves both exactly as you
had them. Until you set both, this release behaves like every one before it and
writes nothing.

If you do turn it on, it can also stop -- which is worth saying because for most of
this release's development it could not. A charge stops when the target is reached,
when the battery runs out of room, when the plan is withdrawn, when the window
closes, when a safety condition appears, or when you switch Control Mode back to
*Shadow* or *Off*. That last one is the abort: selecting it stops a charge Alpha EMS
started, rather than merely declining to start another.

The Activity feed stays short. One line when a charge is planned, one when it
actually starts, one when it ends -- not one every quarter of an hour.

**A note on upgrading to beta.23: when a charge plan stops, the log now says
why.**

Nothing about *when* a plan stops has changed. What changed is that the reason is
reported.

A real charge run for 8.06 kWh ended an hour and a half before its window closed
and the log said only "Shadow run finished: plan ended." The decision was right --
the battery had filled from the sun, there was no longer room for the rest of the
purchase, and the plan was withdrawn -- but nothing in the download said so, and an
`export` recommendation appearing at the same moment made it look as though one plan
had cancelled the other. It had not; both were consequences of a full battery.

Three things caused that silence, and all three are fixed: the reason was being
discarded unless Alpha EMS owned the dispatch, which in shadow it never does; the
reason survived only the single refresh it happened on, so a download taken later
carried nothing; and the log had one generic phrase for every possible ending.

Lifecycle lines are now short and specific -- `Charge plan ended - 1.76 / 8.06 kWh,
no command sent` -- and a download keeps the last ended run under
`execution.carried.last_ended`, so you can ask why hours afterwards. That record is
**per session**: restarting Home Assistant clears it, deliberately, rather than
leaving an old claim standing as though it were still being observed.

Nothing else moved. Live execution is still disabled.

**A note on upgrading to beta.22: three diagnostics figures were wrong, and
one of them was costing you charge.**

A real diagnostics download caught four things. The one that changed behaviour: on
a charge run where the plan sets a stored-energy ceiling, the controller was
subtracting the expected production twice and finishing well short of what the
plan approved — on a worked example, 12.85 kWh where the plan asked for 18. That is
fixed. It could only ever charge *less* than intended, so nothing was ever at risk;
it just quietly declined most of a sunny day's approved charging.

The other three are diagnostics. `projected_end_energy_kwh` was counting expected
production twice and once published 31.9 kWh for a 22 kWh battery. The per-quarter
reserve requirement was lining up against the wrong quarter. And the two `revision`
numbers in the same payload are both correct but were not labelled — the top-level
one is the plan revision frozen when the run was accepted, and `carried.run.revision`
is the live count since; there is now a rule field beside them saying so.

Nothing else moved. Live execution is still disabled.

**A note on upgrading to beta.21: a setting that did nothing now does
something.**

If you ever set **grid charge margin** above zero, it was being ignored — the
value never reached the planner. It works now, so a non-zero margin will start
refusing thin grid purchases it previously allowed. If you left it at zero, which
is the default, nothing about your plan changes.

A diagnostics download now also breaks each planned run down by quarter-hour. That
is worth knowing about if a campaign has ever looked wider than it should: a
thirteen-quarter charge window is often two quarters of buying inside a long band
of storing free production, and the `absorbing` flag on each quarter says which is
which. Activity now names the peak power beside the campaign average when the two
differ, because the average alone read as though the battery were being run gently
when it was not.

**A note on upgrading to beta.20: the command exists now, and is still not
sent.**

beta.19 built the controller and connected it to nothing — its power reached no
actuator, and the command was still being built from the reserve-guard plan, which
never charges. beta.20 connects it. A `grid_charge` target now becomes a real
six-step AlphaESS command with a positive charge power and an upper state-of-charge
cutoff, and that command is then refused, whole, at the barrier.

The difference matters when you read the diagnostics. Before, there was no command
to look at. Now there is one, and `applied_kw` is still `0.0`.

**What to watch for in shadow.** The `execution` block publishes the Stage-A
publication and the carried run side by side. Stage A's `plan_id` changes every
fifteen minutes — that is the rolling horizon, not a new plan — while the carried
`run_id` should stay the same for the whole of a charge window. If you see the
`run_id` changing every quarter, or the revision counting upward every refresh,
that is a bug worth reporting.

One behaviour is deliberate and looks odd at first: while a charge is fifteen
minutes away the controller reports `prepared` and the reserve guard may still be
discharging to cover the house. At the window boundary Stage B takes over. That
direction reversal is existing reserve-guard behaviour, and whether it should
change is a question for real data rather than for a guess.

**A note on upgrading to beta.19: the controller exists now, and still sends
nothing.**

beta.19 adds Stage B — the part that works out *how* to physically achieve what the
economic plan asked for. It measures how much energy has actually reached the
battery, works out the power needed to finish inside the window, respects the reserve
and every hardware limit, and then stops: the last step is unreachable by a constant
in the source, exactly as it has been since Phase 4.

There is one thing to do if you want to be ready for the release that *can* act.
**Create a helper called `input_boolean.alpha_ems_dispatch_owner`** (Settings →
Devices & Services → Helpers → Toggle). Alpha EMS switches it on as the first step of
starting a dispatch and off as the last step of stopping one, and it is how Alpha EMS
tells its own dispatch from one you started by hand — which the AlphaESS helpers
cannot do, because arming from a dashboard and arming from an automation leave
identical values behind. Without it, a running dispatch is treated as yours and left
alone. That is the safe answer, and it is also the answer that means Alpha EMS could
never stop its own run.

**What to look at while it is in shadow.** A diagnostics download now has an
`execution` section: what Stage A expected of production, house load and the grid
against what actually happened, how much has reached the battery, what power the
controller would have asked for, and why it is not asking for more. `applied_kw` is
always zero there, and `executed` is always false.

Since beta.23 that section also answers *why the last run stopped*, which it could
not before. `result.stop_reason` is filled in whenever a run ends, and
`carried.last_ended` keeps the last one -- its reason, identity, intent, window,
target, what reached the battery and what was left -- so a download taken hours
later still answers the question. `carried.ended_reason` beside it is truthful for
the single refresh the run ended on, which is why the record exists.

`last_ended` is **session-local and not written to disk**. A restart forgets it, on
purpose: it records what this session watched happen, and a retained claim that
outlived its session would be a stale fact wearing the clothes of a current one.

**A note on upgrading to beta.18: the battery will sell in the evening now.**

Until beta.18 the plan was not allowed to end a horizon holding less than it would
have held by doing nothing. That sounds like a sensible rule against emptying the
battery, and it was not one: with no sun left to come, "doing nothing" is a flat
line, so the rule really said *never end lower than you are now* — and the plan
therefore refused to sell into the evening peak, because selling ends lower. It got
worse as the day went on, because the bar was re-read from the current charge at
every refresh, so every charge raised it.

That rule is gone. What protects your battery is the **dynamic battery reserve**,
which is the thing actually designed for the job: it is checked at every quarter of
the horizon rather than only at the end, and it is calculated from load and
production forecasts that reach *further ahead than prices are published* — so on a
summer evening it is still asking for around 15 kWh, and in winter around 19 kWh,
at the point where the prices stop. Energy above that figure is yours to trade, and
now it is traded.

Expect to see evening discharges into expensive quarters that beta.17 would have
declined. Your reserve setting is unchanged and is enforced exactly as before; if
you want the battery to hold more back, raise the reserve rather than looking for
the old rule.

**A note on upgrading to beta.17.** The optimizer now picks the size of its
internal energy step to match your inverter's power, so the whole of it is
usable. One consequence is visible: economic figures either side of the upgrade
are rounded on slightly different steps, and can differ by up to one step —
around 0.26 kWh on a 22 kWh, 10 kW installation — for no reason other than that.
Older stored records are left exactly as they were rather than rewritten to match,
and every diagnostics download records the step it used, so a figure can always be
read in the terms it was computed under.

**A charge run's energy is not the same thing as energy bought.** "Charged
4.48 kWh" can mean 1.5 kWh from the grid and the rest from the roof, and the
`grid_import_kwh` beside it is *site* import including house load — a third
quantity again. Since beta.16 each run also states what it caused on its own:
`marginal_grid_import_kwh`, `marginal_grid_export_kwh`, and a `charge_source` of
`production`, `mixed` or `grid`. These are differences against leaving the battery
alone through the same quarters, not shares apportioned by a rule of thumb.

Beside them, `marginal_cost_eur` is what the run cost compared with doing nothing,
and **a negative figure means it saved money.** The older per-run figure was a
cash flow, so every charge run read negative simply because buying costs money,
and a discharge that exactly covered the house read `0.00` while wiping out the
whole import bill. The cash flow is still there as `net_cash_flow_eur`; it is just
no longer the only number.

**A figure at the battery is not a figure at the meter.** An export run moves
more energy out of the battery than it sends to the grid, because the house takes
the difference — so since beta.17 an Activity line names both boundaries instead
of putting one of each in the same sentence. "0.95 kW average (0.95 kWh from the
battery), of which 0.27 kWh reaches the grid" is the same run beta.16 described as
"0.95 kW, 0.27 kWh", which was true and read as arithmetic.

**What a future controller would be told.** A diagnostics download now carries an
`execution_target` for each planned run: what to do, over which absolute window,
and **how much energy at which boundary** — the battery figure and the meter figure
in separate fields, because they are different numbers. Sending 1.3 kW to the
battery when 1.3 kW of export was wanted delivers about 0.4 kW, since the house
takes its share first. Nothing in this release consumes any of it, and no actuator
exists for it.

**What today actually cost** appears beside it, computed from measured flows at the
prices recorded for the same quarters — import cost, export revenue, net cash flow,
and what the battery saved by supplying the house. These are *realised* figures and
are never mixed with the forecast ones. What is deliberately absent is a profit
figure per trade: working out which stored kilowatt-hour came from which earlier
charge needs a convention a battery does not physically have, and a number that
depends on an arbitrary choice does not belong beside numbers that do not.

A run about to start files one line in the **logbook**, on the sensor's own
history — once, when it is within a quarter of an hour of beginning, and then
nothing while it runs. If it changes materially it says so once; if it is dropped
before its window opens, or its window passes, it says that once too. Earlier
releases re-announced whatever the plan currently said on every refresh, which
buried the log in near-identical lines for a run already under way.

Nothing reads those lines back. Since `beta.32` a line carries the `Advisory`
marker only when the action genuinely has no actuator — a Live sale is executed, and
marking it advisory was a false claim on a line a command was about to be sent for.

### What today earned, and what is still coming (new in beta.39)

Through `beta.38` the Economic Value sensor could tell you what the *plan* was worth
against doing nothing. It could not tell you what the day had actually done — and
the two figures that looked closest to an answer were the two it would have been
most wrong to use. `decision_advantage_eur` is a from-now comparison against a
counterfactual, not a realised quantity. `net_cash_flow_eur` is import less export,
so a **negative** value means money arrived. Neither is a profit, and adding one to
the other rests on neither.

Four attributes now answer it, on the same entity, and they add up:

```
realised_today_eur              what the closed part of today realised
+ in_progress_interval_eur      what the quarter in flight has realised so far
+ remaining_expected_today_eur  what the plan still expects before midnight
+ forecast_revaluation_eur      what the energy today opened with has been revalued by
= total_economic_value_today_eur
```

The total telescopes to one sentence: **today's cash, plus what the pack is worth
now, less what it was worth when the day opened.** It is an economic *position*, not
money in the bank — the remaining term is a forecast and two of the terms are
planner valuations — and `accounting_basis` says so on the entity.

Three properties are worth knowing:

- **One counterfactual throughout.** Every avoidance figure, realised and planned
  alike, is measured against a household with no battery at all. The planner's own
  `avoided_import_eur` uses a different and smaller baseline — leaving the battery
  alone *this* interval, which includes the inverter serving residual load by itself
  — and the two are never added.
- **The quarter in flight is a separate term** and joins realised history only when
  its measurement closes, so `realised_today_eur` can never go down.
- **Any missing addend takes the total with it.** `accounting_unavailable_reason`
  names which, and there is never a zero standing in for an unknown. The reasons you
  are most likely to see are `no_opening_valuation` (the first day after upgrading,
  until the next local midnight) and `horizon_short_of_midnight` (Home Assistant
  running outside the market's timezone with only one price day published, so part
  of the local civil day is unpriced — publish tomorrow's prices as well and it
  resolves).

The attribute names are English because Home Assistant does not translate attribute
*names* at all; the labels belong to your dashboard. A Dutch card:

```yaml
type: entities
title: Economische waarde
entities:
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: realised_today_eur
    name: Gerealiseerd vandaag
    suffix: " EUR"
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: in_progress_interval_eur
    name: Lopend kwartier
    suffix: " EUR"
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: remaining_expected_today_eur
    name: Nog verwacht
    suffix: " EUR"
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: forecast_revaluation_eur
    name: Herwaardering
    suffix: " EUR"
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: total_economic_value_today_eur
    name: Totaal
    suffix: " EUR"
  - entity: sensor.alpha_ems_economic_value
    type: attribute
    attribute: accounting_unavailable_reason
    name: Reden onbekend
```

Replace `sensor.alpha_ems_economic_value` with your own entity id if Home Assistant
gave it a different one — check **Developer tools → States** for the entity whose
`device_class` is `monetary`.

The full audit trail is in the nested `today_accounting` attribute: the day's
interval partition, both ends of the position, where the opening valuation came from
and when, and the reconciliation error. It is also in the diagnostics download.

### Safety, economics, or both (new in beta.39)

A charge campaign can have two reasons at once, and until `beta.39` it could only
report one of them. `Economic Action` and `Next Planned Action` now distinguish
three:

| State | What it means |
|---|---|
| `Safety Buy` | Physical reachability compelled the purchase, and nothing more was bought. |
| `Charge` | Nothing was compelled. The whole purchase is a trade the optimiser chose. |
| `Mixed Buy` | Reachability compelled part of it, **and** the optimiser independently found further charging worth doing in the same window. |

The live campaign that prompted this bought 8.06 kWh of which 0.83 was compelled
and 7.22 was chosen, and reported `Safety Buy` — so seven of its eight
kilowatt-hours looked like energy the battery had no choice about. The two figures
were always published and always correct; what was missing was a word that matched
them. They are in the diagnostics download as `safety_buy_kwh` and
`economic_buy_kwh` on the run's execution target, and `purchase.classification`
beside them, and none of the three changed.

**A Mixed Buy is not a Safety Buy that grew.** Only physical reachability can
compel a purchase — that has been true since `beta.14` and is unchanged. A run
with nothing compelled cannot acquire a compulsory component by being
economically attractive, however attractive.

### Export safety, and why a discharge gets smaller (changed in beta.15)

A forced discharge sets the **battery's** rate, so whatever the house cannot use
leaves through your meter — and the inverter's own feed-in limit does not apply to
a dispatch. Alpha EMS therefore measures how much your site can absorb, at the
meter:

> `capacity = grid import − grid export + battery discharge already flowing`

measured rather than reconstructed from house load and PV, because the meter is
the instrument that *defines* export. A configured margin (10 %, not adjustable)
comes off that capacity, because your load can change after the reading was taken.

**Until beta.15 a discharge larger than that was refused outright.** A
recommendation to discharge 1.1 kW into a house absorbing 0.99 kW produced
`inhibited` with `would_export`, and nothing happened — even though 0.8 kW would
have been perfectly safe. With modest household load that could persist for hours,
and it made "discharge to the house" close to unusable.

**Since beta.15 the command is made smaller instead.** The order is:

1. measure the absorbing capacity at the meter;
2. take the margin off the **capacity** (0.99 → 0.891 kW);
3. clamp the request to what remains;
4. round **down** to a step the inverter accepts (→ 0.8 kW);
5. recompute the energy the command will deliver.

The 0.9 kW step is rejected, because it would exceed the margined bound. Nothing
is ever rounded up, and the final command can never be larger than what was
requested.

**It is still refused when nothing useful survives.** `would_export` has not gone
away and means what it always meant. You will still see it when your site has no
spare absorption, when it is already exporting, when the safe power falls below
the smallest command the inverter accepts (0.2 kW), or when the grid meter is
missing or stale. Fail closed is unchanged.

**`eligible` now means "something safe remains"** rather than "the request was
safe as asked". A reduced command reads `eligible`, and the `export_check` block
of a diagnostics download says by how much: `requested_power_kw`,
`safe_capacity_kw`, `safety_limited` and `final_command_power_kw`.

**This text described beta.24 and was left standing for six releases.** The clamp
still governs every charge, every hold and every reserve-guard discharge, and
`INHIBIT_WOULD_EXPORT` still fires on them — there, energy reaching the meter is an
accident. But an **admitted `net_export` quarter** has taken a separate authorisation
path since `beta.27`, because for it the meter *is* the objective and the same
question has the opposite answer. `beta.32` finished the correction: `export` is in
`IMPLEMENTED_ACTIONS`, so the capability plan no longer reports value the plant can
actually capture, and a Live sale no longer carries an `Advisory` marker.

Serving the house from the battery (`serve_load`) and photovoltaic curtailment remain
non-executable, and the clamp is still what keeps an *unintended* export from
happening.

## What it does **not** do yet

- ✅ **Charging is carried out** since beta.24, and **selling to the grid** since
  beta.27 — both in *Live* mode with command sending enabled, and both subject to the
  calculated reserve. The two entries below this used to deny the second one; they
  described beta.24 and were left standing.
- ❌ **No load-serving discharge, and no curtailment.** Both are calculated,
  published and explained, and refused at three independent boundaries before
  anything reaches the inverter. `serve_load` is deliberately not an executable
  intent: a discharge into the house is ordinary inverter behaviour and needs no
  command from this integration.
- ⚠️ **Multi-quarter export campaigns have never run on hardware, and could not
  have.** The campaign layer shipped unwired in `beta.32` and is connected for the
  first time in `beta.33`, so every campaign lifecycle — the accumulator, the frozen
  objective, the single terminal — is executing for the first time on this release.
  Live `net_export` itself has been performed on this installation. If you enable
  *Live*, watch your grid meter during the first long planned export, and check that
  the campaign files exactly one terminal when it ends.
- ❌ No EV charge scheduling. Phase 1 only *separates* EV consumption from the
  baseline; it never starts, stops or plans charging.
- ❌ No Solcast-driven *optimisation*. The forecast reduces what the battery is
  asked to supply and makes the projection realistic, but nothing schedules,
  trades or charges on the strength of it. It also never changes the learned
  household load, which is defined to be independent of production.
- ❌ No self-correction. Phase 2 *records* forecast error; it does not feed it
  back into the model. Nothing adjusts itself in response to being wrong.
- ❌ **No battery control, still.** Phase 4 builds the entire control path --
  translation, safety checks, the export clamp, the exact command -- and cannot
  execute it. No service call reaches your inverter, and that is enforced by a
  build-time constant rather than by a setting. beta.15 makes `eligible` reachable
  far more often; it does not make it executable.
- ❌ No price-aware or economic battery planning, and no automatic charging. No
  cheap-hour buying, no overnight carry-over, no arbitrage. Expected production is
  an input to the plan, never a reason to buy or sell.
- ❌ **No enforcement of the calculated reserve.** Phase 7 works out how much
  energy the battery ought to be holding and publishes it; Phase 8 optimises
  *subject to* it. The planner still discharges to the *configured* minimum state
  of charge, which remains the one hard floor, so a requirement above it shapes
  the published plan and is obeyed by nothing.
- ❌ **No safety buy is ever made.** Phase 8 identifies one, labels it, and prices
  it. Buying energy needs a command, and there is none.
- ❌ **No economic *action* from prices, even though a plan is now computed from
  them.** Phase 8 ranks intervals, picks windows and expresses an objective — and
  publishes the answer. The reserve Phase 7 calculates is still a physical figure
  no price can move, the price layer is still unreachable from the reserve, and
  the optimizer still cannot reach a price source: prices arrive as a value, not
  as something it can query. See
  [Prices, and why they change nothing yet](#prices-and-why-they-change-nothing-yet).
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
| [Frank Quarter Prices](https://github.com/Bennie-JC/ha-frank-quarter-prices) | **Yes** | Set up before adding Alpha EMS Manager. From beta.12 its price series is read and normalised; it still drives no decision. |
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

Exactly thirteen — twelve sensors and one control. Everything else lives in
diagnostics: per-slot profiles, window means, the simulated trajectory, the
per-interval reserve curve, the economic counterfactuals, every planned run, the
constraint tallies and the whole evidence layer. Ninety-six quarter sensors would
be technically easy and practically awful.

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.alpha_ems_expected_house_load_today` | kWh | Predicted **baseline** consumption today |
| `sensor.alpha_ems_expected_house_load_tomorrow` | kWh | Predicted **baseline** consumption tomorrow |
| `sensor.alpha_ems_learning_confidence` | % | How mature and trustworthy the model is |
| `sensor.alpha_ems_learning_days` | — | Calendar days with sufficient valid baseline data |
| `sensor.alpha_ems_forecast_error_yesterday` | kWh | Yesterday's forecast minus what was measured |
| `sensor.alpha_ems_forecast_error_7_days` | % | Rolling forecast error over the last 7 days |
| `sensor.alpha_ems_battery_recommendation` | — | What the battery *should* do this interval. Advisory |
| `sensor.alpha_ems_planned_battery_power` | kW | The advised interval-average power. Positive is charging |
| `sensor.alpha_ems_usable_battery_energy` | kWh | Energy deliverable above the configured minimum |
| `sensor.alpha_ems_dynamic_battery_reserve` | kWh | Energy that *ought* to remain available. Obeyed by nothing |
| `sensor.alpha_ems_economic_action` | — | What it *wants* to do with the battery, and what could actually be done |
| `sensor.alpha_ems_control_state` | — | What the control pipeline made of the recommendation |
| `select.alpha_ems_control_mode` | — | `off`, `shadow` or `active`. Nothing executes in any of them |

The three battery sensors and the control pair describe a plan that is never
carried out, `Dynamic Battery Reserve` describes a requirement nothing enforces,
and `Economic Action` describes a trade nobody makes. They are published so all of
it can be watched for weeks before anything is allowed to act on any of it.

`Economic Action` carries `device_class: enum` and no state class, for the same
reason `Battery Recommendation` does: a long-term statistic over a category means
nothing.

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
- **Only charging executes, and only when you have asked for it twice.**
  Discharging, exporting and curtailment are built, validated and refused. The
  provenance problem that blocked all of it until beta.24 — nothing in the AlphaESS
  control surface records *who* armed a dispatch — is solved with a marker outside
  the vendor namespace plus a persisted causal record tied to the dispatch the
  device itself reports. Matching power, cutoff or duration is still *not* proof:
  the person most likely to have set exactly those figures by hand is you, watching
  the shadow recommendation. A dispatch Alpha EMS cannot prove it started is never
  touched, stopped or overwritten.
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
