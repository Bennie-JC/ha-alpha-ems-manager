🇳🇱 [Nederlands](../nl/configuration.md) | 🇬🇧 **English**

# Configuration

**In one sentence:** every field you can see in Alpha EMS Manager, explained in the order
Home Assistant presents it.

## How to use this page

Put this window beside Home Assistant and work top to bottom. The setup steps first,
then the four options pages — in exactly the order the screen shows them.

You can change anything later through **Settings → Devices & Services → Alpha EMS
Manager → Configure**. The integration reloads briefly; your learned history is kept.

---

## The four measurement points

Before choosing sensors: Alpha EMS asks for four **different** electrical measurement
points. They do not measure the same thing and must not be swapped.

```
        Grid  ↔  House  ↔  Battery
                   ↑
                 Solar
```

| Point | What it measures |
|---|---|
| **Grid** | What crosses your grid connection, in or out |
| **House** | What your home consumes, wherever it comes from |
| **Battery** | What goes into or out of the pack |
| **Solar** | What your panels generate |

On a sunny afternoon your house may consume 2 kW while the meter reads 0 kW. Choose the
grid sensor as house load there, and Alpha EMS learns that your house uses nothing.

⚠️ **Note:** the most common mistake is choosing the grid or PV sensor as house load.
See [Troubleshooting](troubleshooting.md).

---

# Part 1 — The setup steps

## Step 1: Alpha EMS Manager

### Instance name

`name`
**What is this?** The name of this installation.

**What do I choose?** `Alpha EMS` if you have one battery system.

**Required:** yes. **Default:** `Alpha EMS`.

⚠️ **Note:** this name determines your entity IDs. With `Alpha EMS` you get
`sensor.alpha_ems_economic_action`; name it `Home` and you get
`sensor.home_economic_action`. Choose it correctly now — renaming later means reworking
every dashboard.

### House load power

`house_load_entity`
**What is this?** The sensor telling you how much your home is consuming right now.

**What do I choose?** On AlphaESS this is usually *Current House Load*. Not your grid
sensor.

**Unit:** W, kW or MW. **Required:** yes.

**What for?** This is the measurement the whole learning model runs on.

**Leave it empty?** You cannot — the form refuses, and without it the integration cannot
start.

### Daily house load (validation, optional)

`daily_house_load_entity`
**What is this?** A daily total of house consumption in kWh.

**What do I choose?** If you have such a sensor, pick it; otherwise leave it empty.

**Unit:** Wh, kWh or MWh. **Required:** no.

**What for?** Purely an end-of-day cross-check: does what Alpha EMS added up match what
the counter says?

**Leave it empty?** Fine. That check is skipped; nothing else changes.

### EV charger power (optional)

`ev_power_entity`
**What is this?** The power of your charge point.

**What do I choose?** Your charge point's power sensor, or empty if you have none.

**Unit:** W, kW or MW. **Required:** no.

**What for?** To subtract car charging from your house load, so a charging session is
not learned as ordinary demand.

**Leave it empty?** Then learned load equals measured load.

**Set wrongly?** It may not be the same sensor as *House load power*; that is refused.

⚠️ **Note:** choose a sensor that publishes `0` when idle. A charger reporting
`unavailable` while doing nothing makes almost every quarter unusable for learning.

### This system has solar panels

`has_pv`
**What is this?** Whether this system has solar panels.

**Required:** yes. **Default: on.**

**What for?** When on, an extra step appears where you pick your PV sensor.

**Turn it off?** Do that if you have no panels; the solar step is then skipped.

### Use a PV forecast source

`use_pv_forecast`
**What is this?** Whether Alpha EMS may use *expected* solar production.

**Required:** yes. **Default: off.**

**What for?** On means: ask Solcast what sun is coming today and tomorrow, and use it in
the plan.

**Turn it on without Solcast?** You get an immediate error on the field. Configure
[Solcast](solar.md) first.

**Normal choice:** on, if you have panels and Solcast is running.

---

## Step 2: Battery

### Battery state of charge

`battery_soc_entity`
**What is this?** Your battery's state of charge, as a percentage.

**What do I choose?** The SoC sensor from your AlphaESS system.

**Unit:** exactly `%`. **Required:** yes.

**What for?** Everything: how much is stored, how much room there is, whether the
reserve is reachable.

### Battery power

`battery_power_entity`
**What is this?** The power currently going into or out of the battery.

**Unit:** W, kW or MW. **Required:** yes.

**What for?** To measure what was really charged or discharged.

### Battery power sign convention

`battery_power_sign`
**What is this?** Whether a *negative* number means charging, or discharging.

**Options:** *Negative means charging (AlphaESS default)* · *Positive means charging*.

**Default:** negative is charging — what AlphaESS does.

See [The two sign conventions](#the-two-sign-conventions) below for a test.

### Usable battery capacity (DC)

`battery_capacity_kwh` — **What do I enter?** The **usable** capacity of your battery in kWh, from your
manufacturer's specification. Example: a pack with 21.6 kWh usable → `21.6`.

**Unit:** kWh. **Range:** 0.1 – 200.0, step 0.1. **Required:** yes (at setup).
**Default:** none — there is nothing to guess from.

**Set wrongly?**
- **Too high:** Alpha EMS thinks more energy is available than really is, and plans too
  tightly.
- **Too low:** Alpha EMS becomes unnecessarily conservative and leaves value unused.

⚠️ **Note:** enter the *usable* capacity, not the gross figure on the nameplate.

### Minimum state of charge

`battery_min_soc_percent` — **What is this?** The floor you set yourself: Alpha EMS never goes below it.

**Unit:** %. **Range:** 0.0 – 100.0, step 1.0. **Default:** `20`.
**Required:** yes (but always filled).

**Normal choice:** the default `20`, or whatever your inverter already holds back.

**Entering `0`** is allowed and means Alpha EMS keeps no reserve of its own; only your
inverter's floor applies.

**Set wrongly?** A value of 100 or more is refused.

⚠️ **Important:** this is the **hard floor**, not everything the battery holds back. The
[dynamic reserve](how-it-works.md#the-reserve) may temporarily require *more* than this
floor, because it looks ahead at what you still need this evening. Setting this to 15 %
does not mean the battery always runs down to 15 %.

### Maximum charge power (AC)

`battery_max_charge_kw` — **What do I enter?** Your inverter's maximum charge power in kW, from the specification.

**Unit:** kW. **Range:** 0.1 – 50.0, step 0.1. **Required:** yes (at setup).

**Set wrongly?** Too high and Alpha EMS plans charges your inverter cannot deliver, so
they fall short. Too low and it charges more slowly than it needs to.

### Maximum discharge power (AC)

`battery_max_discharge_kw`
The same, for discharging. Same unit, range and consequences.

### Round-trip efficiency

`battery_round_trip_efficiency_percent` — **What is this?** How much of the energy you put in comes back out.

**Unit:** %. **Range:** 50.0 – 100.0, step 1.0. **Default:** `90`.

**Normal choice:** the default `90`, unless you know your system's real figure.

**What for?** To decide whether keeping energy is worth more than selling it: a stored
kWh does not come back whole.

⚠️ **Note:** enter `90`, not `0.90`. The floor of 50 catches that mistake.

---

## Step 3: Solar production

*You only see this step if you answered yes to* This system has solar panels.

### PV production power

`pv_power_entity`
**What is this?** The sensor measuring what your panels generate now.

**Unit:** W, kW or MW. **Required:** yes, in this step.

**What for?** To see how much sun is spare beside your house load, and whether it fits
in the battery instead of going to the grid.

---

## Step 4: Grid meter

### Grid power

`grid_power_entity`
**What is this?** The sensor telling you how much power your home is currently drawing
from, or feeding back to, the grid.

**What do I choose?** Your P1/smart meter's power sensor — for example HomeWizard P1,
DSMR or SlimmeLezer.

**Unit:** W, kW or MW. **Required:** yes.

**What for?** This is the instrument that *defines* export. Alpha EMS uses it to stop a
discharge unintentionally reaching the grid, and to measure what was really sold.

### Grid power sign convention

`grid_power_sign`
**What is this?** Whether a *positive* number means you are drawing from the grid.

**Options:** *Positive means importing from the grid* · *Negative means importing*.

**Default:** positive is importing — the usual Dutch P1 convention.

### Cumulative grid export counter (optional)

`grid_export_energy_entity`
**What is this?** A rising total of everything you have fed back, in kWh.

**Unit:** Wh, kWh or MWh. **Required:** no.

**What for?** An extra cross-check of the export accounting against your meter reading.

**Leave it empty?** Fine; that check is skipped and nothing else changes.

---

## Step 5: Prices and forecast

### Frank Quarter Prices instance

`frank_entry_id`
**What do I choose?** Your configured Frank Quarter Prices instance.

**Required:** yes.

**Why an instance and not an entity?** So the link keeps working if you rename an entity
later.

**No Frank?** You cannot add Alpha EMS. Install
[Frank Quarter Prices](requirements.md#3-frank-quarter-prices) first.

### Solcast PV Forecast instance

`solcast_entry_id`
*You only see this field if you enabled* Use a PV forecast source.

**What do I choose?** Your configured Solcast instance. **Required:** yes, when the
forecast is on.

---

# Part 2 — The options pages

**Settings → Devices & Services → Alpha EMS Manager → Configure** gives a menu of four
pages.

## Sources

The same sensors as at setup, plus three fields that only appear here. The on-screen
order is: house load, daily house load, battery SoC, battery power, battery sign, has
PV, PV power, grid power, grid sign, Frank, PV forecast, Solcast, EV charger, Solcast
sites, export counter.

Everything explained above applies unchanged. Three additions:

### PV production power (on this page)

Here the field is technically optional, but if *This system has solar panels* is on and
you clear it, you get an error. To remove the PV sensor for real, switch that toggle off
first.

### Solcast sites that belong to this system

`selected_solcast_site_ids` — *You only see this field if:* the PV forecast is on, a Solcast instance is selected,
Solcast is reachable, **and** its sites could actually be read.

**What do I choose?** The rooftops belonging to *this* home. A Solcast account may also
hold rooftops at another address.

**Default:** every site found.

A site Solcast no longer offers stays in the list, marked *(no longer offered by
Solcast)*, so it does not disappear silently.

### Clearing fields

On this page you really can clear *daily house load*, *EV charger power*, *cumulative
grid export counter*, *PV production power* and *Solcast instance*; the earlier value is
erased.

The **instance name** cannot be changed here. Rename the integration itself in Home
Assistant instead — and remember that does not rename your entity IDs.

---

## Battery planning

The same five fields as setup step 2: capacity, minimum SoC, max charge, max discharge,
efficiency.

**One difference, and it matters.** Capacity, max charge and max discharge are
**optional** here. Clear one and Alpha EMS does not guess: battery planning becomes
unavailable with a stated reason, and the entities depending on it read `unknown`.
Learning and forecasting carry on.

Minimum SoC and efficiency cannot be cleared; they always hold a value.

---

## Control

Three settings.

### Export safety margin

`control_export_margin_percent` — **What is this?** A safety margin on how much your connection can still absorb before
unintended export happens.

**Unit:** %. **Range:** 0 – 50, step 1. **Default:** `10`.

**What for?** Your house load can change between the moment of measuring and the moment
of commanding. This margin absorbs that.

**When to change it?** *Advanced.* Higher makes Alpha EMS more cautious and its commands
smaller.

### Grid energy budget per charge run

`grid_charge_budget_kwh` — **What is this?** An upper bound on how many kWh may be bought from the grid per charge
run.

**Unit:** kWh. **Range:** 0.0 – 50.0, step 0.1. **Default:** `0.0`.

⚠️ **`0` means no limit — not "do not buy".** If you do not want grid buying, switch
*Allow buying from the grid* off on the Economics page.

**When to change it?** *Only change this if* you deliberately want a per-run ceiling.

### Allow Alpha EMS to send commands

**What is this?** The master switch for sending commands
(`control_execution_enabled`).

**Default: off.**

| | |
|---|---|
| **OFF** | Alpha EMS calculates everything and sends nothing — not even in *Live*. |
| **ON** | Commands may be sent, **provided** Control Mode is set to *Live*. |

This is one of two consents. The other is the **Control Mode** entity, and neither
implies the other. See [Control and safety](control-and-safety.md).

⚠️ **Note:** only switch this on after watching what Alpha EMS would do in *Shadow*.

---

## Economics

Nine settings. **Five affect the plan, four affect only reporting.**

| Setting | Key | Affects |
|---|---|---|
| Minimum gain per trade | `minimum_trade_gain_eur` | **the plan** |
| Extra margin per grid-charged kWh | `grid_charge_margin_eur_per_kwh` | **the plan** |
| Battery wear cost per kWh | `battery_throughput_cost_eur_per_kwh` | **the plan** |
| Allow buying from the grid | `allow_grid_charging` | **the plan** |
| Allow selling from the battery | `allow_battery_export` | **the plan** |
| Battery investment (gross) | `battery_investment_eur` | reporting only |
| Subsidy received | `battery_subsidy_eur` | reporting only |
| Other one-time credit | `other_one_time_credit_eur` | reporting only |
| Purchase date | `battery_investment_date` | reporting only |

That distinction matters: entering your investment changes **nothing** about when your
battery trades. It only changes what the *Battery Return* sensor shows.

### Minimum gain per trade

**What is this?** The amount a single action must earn before it is worth planning.

**Unit:** EUR. **Range:** 0.00 – 5.00, step 0.01. **Default:** `0.10`.

**What for?** To stop your plan filling with two-cent trades. It is charged once per
continuous action, not per kWh — and only for actions Alpha EMS itself chooses, never
for absorbed sunshine.

**Normal choice:** the default.

**Entering `0`** means: take every action that earns anything.

### Extra margin per grid-charged kWh

**What is this?** An extra requirement **per kWh** on top of the fixed minimum, for
energy actually bought from the grid.

**Unit:** EUR/kWh. **Range:** 0.00 – 2.00, step 0.01. **Default:** `0.0` (off).

**Why alongside the previous one?** A fixed amount does not scale. Once one action
clears that threshold, the volume behind it is unbounded. With this margin, every bought
kWh has to earn its keep.

**When to change it?** *Only change this if* you think too much volume is being traded
for too little margin.

Your own solar, the solar share of a mixed quarter, discharging to your house and buying
for safety all fall **outside** it.

### Battery wear cost per kWh

**What is this?** What a moved kWh costs you in battery wear.

**Unit:** EUR/kWh. **Range:** 0.000 – 1.000, step 0.001. **Default:** `0.0`.

**Normal choice:** the default `0`, unless you have a figure you trust.

### Allow buying from the grid

**Default: off.**

| | |
|---|---|
| **OFF** | Alpha EMS buys no grid energy to store. Absorbing your own solar is still allowed. |
| **ON** | Buying may become part of the plan. |

⚠️ **Your own solar filling the battery is not "buying from the grid".** This switch is
not needed for that.

### Allow selling from the battery

**Default: off.**

| | |
|---|---|
| **OFF** | Alpha EMS may calculate and show a selling opportunity, but will not act on it. |
| **ON** | Selling may become part of the plan, and may be executed once Control Mode is *Live* and command sending is enabled. |

### Battery investment (gross)

**What do I enter?** What your battery cost, including tax and installation.

**Unit:** EUR. **Range:** 0 – 1,000,000, step 1. **Required:** no.

**What for?** Only for the *Battery Return* sensor, which tracks how much you have
recovered.

**Leave it empty?** Then *Battery Return* is unavailable with the reason
`no_investment_configured`. That is different from an investment of zero.

### Subsidy received · Other one-time credit

Amounts deducted from your gross investment. Same unit and range, also reporting only.

### Purchase date

**What is this?** The purchase date, so the payback period is counted from the right
moment.

**Leave it empty?** The period is simply not stated; the rest of the sensor works.

⚠️ **Known behaviour:** these four optional money fields currently cannot be *cleared*
through the interface. Fill one in and empty it later, and the old value is kept.
Entering a different value works normally.

---

## The two sign conventions

This is the setting most often got wrong, and the consequences are large: with the wrong
convention Alpha EMS reads charging as discharging, or importing as exporting.

### Testing the battery

1. Look at your battery at a moment when it is **clearly charging**.
2. Look at your *Battery power* sensor's value.
3. Is there a **minus** sign (`-1200 W`)? → choose *Negative means charging*.
4. Is there **no** minus sign (`1200 W`)? → choose *Positive means charging*.

For AlphaESS the first is almost always right, which is why it is the default.

### Testing the grid

1. Pick a moment when you are **certain you are importing** — evening, no sun, no
   battery discharging.
2. Look at your *Grid power* sensor.
3. `+1200 W` → choose *Positive means importing from the grid*.
4. `-1200 W` → choose *Negative means importing*.

### How do you spot a wrong setting?

Download diagnostics and look at the energy balance. With an inverted sign the residual
is ten to twenty times its allowance — impossible to miss. See
[Diagnostics](diagnostics.md#is-the-energy-balance-healthy).

---

## Next

[How Alpha EMS works](how-it-works.md) · [Control and safety](control-and-safety.md)
