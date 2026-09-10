🇳🇱 [Nederlands](../nl/requirements.md) | 🇬🇧 **English**

# Requirements

**In one sentence:** Alpha EMS Manager plans and controls, but measures nothing itself —
it needs other integrations to publish your battery, your meter, your prices and your
solar.

## Why this matters

Alpha EMS Manager makes no outbound connections. It has no API key, calls no server and
does not talk to your inverter. Everything it knows, it reads from entities other
integrations put into Home Assistant.

If one is missing, Alpha EMS is missing part of the picture — and in some cases you
cannot even add the integration.

---

## Overview

| Component | Needed? | What for |
|---|---|---|
| [AlphaESS package (Hillview Lodge)](#1-the-alphaess-package) | **Required** | Battery data and the control surface |
| [`input_boolean.alpha_ems_dispatch_owner`](#2-the-ownership-helper) | **Required** | Recognising its own dispatches |
| [Frank Quarter Prices](#3-frank-quarter-prices) | **Required** | Quarter-hour prices |
| [Smart meter / P1](#4-smart-meter--p1-metering) | **Required** | What really happens at the grid connection |
| [Home Assistant 2025.1.0+](#5-home-assistant) | **Required** | The APIs it uses |
| [Solcast PV Forecast](#6-solcast-optional) | Optional | Expected solar production |
| [PV power sensor](#7-pv-power-sensor) | Required with solar | Measured solar production |
| [EV charger sensor](#8-ev-charger-optional) | Optional | Keeping car charging out of the model |

---

## 1. The AlphaESS package

**Required** — <https://projects.hillviewlodge.ie/alphaess/>

Alpha EMS Manager does not communicate with your AlphaESS inverter itself. The Hillview
Lodge package is a YAML template package that provides the AlphaESS entities and the
control surface; Alpha EMS reads and writes those entities.

**What you do:** install and configure the package following *their* documentation, and
check the entities exist in Home Assistant before adding Alpha EMS. Their documentation
is the authority — this page deliberately does not copy their instructions.

**What Alpha EMS reads from it:** house load, battery state of charge and power, solar
production, and the whole dispatch surface below.

### The entities that must exist

Alpha EMS checks this list at startup. If one is missing, learning and forecasting carry
on as normal but nothing is executed — and diagnostics names the missing one.

**The ownership helper (you create this yourself, see below):**
`input_boolean.alpha_ems_dispatch_owner`

**Dispatch read-back sensors:**
`sensor.alphaess_dispatch_start` · `sensor.alphaess_dispatch_mode` ·
`sensor.alphaess_dispatch_active_power` · `sensor.alphaess_dispatch_soc` ·
`sensor.alphaess_dispatch_time`

**The reset automation:** `automation.alphaess_dispatch_reset_full` — must exist **and
be switched on**. This is the fail-safe that returns your installation to normal if
Alpha EMS ever stops running.

**The charge helpers:**
`input_boolean.alphaess_helper_force_charging` ·
`input_boolean.alphaess_helper_force_charging_hold` ·
`input_number.alphaess_helper_force_charging_power` ·
`input_number.alphaess_helper_force_charging_cutoff_soc` ·
`input_number.alphaess_helper_force_charging_duration` ·
`timer.alphaess_helper_force_charging_timer`

**The discharge helpers:**
`input_boolean.alphaess_helper_force_discharging` ·
`input_boolean.alphaess_helper_force_discharging_hold` ·
`input_number.alphaess_helper_force_discharging_power` ·
`input_number.alphaess_helper_force_discharging_cutoff_soc` ·
`input_number.alphaess_helper_force_discharging_duration` ·
`timer.alphaess_helper_force_discharging_timer`

**The Dispatch surface** — this is what Alpha EMS actually uses to control the battery:
`input_boolean.alphaess_helper_dispatch` ·
`input_select.alphaess_helper_dispatch_mode` ·
`input_number.alphaess_helper_dispatch_power` ·
`input_number.alphaess_helper_dispatch_cutoff_soc` ·
`input_number.alphaess_helper_dispatch_duration` ·
`input_boolean.alphaess_helper_dispatch_pv_switch` ·
`timer.alphaess_helper_dispatch_timer`

The force-charge and force-discharge helper families are **never written** by Alpha EMS
in normal operation. They must exist because Alpha EMS reads them to see whether you or
another automation is doing something.

---

## 2. The ownership helper

**Required, and you create it yourself.**

```
input_boolean.alpha_ems_dispatch_owner
```

*Settings → Devices & Services → Helpers → Toggle.* The name must match exactly.

**Why this is needed.** The AlphaESS package records nowhere *who* started a dispatch.
Whether you switch it on from your dashboard or Alpha EMS does it, the values left
behind are identical. So Alpha EMS turns this helper on as the **first** step of its own
dispatch, and off as the **last**.

**What happens without it.** Alpha EMS executes nothing. Worse, without the helper it
could start a dispatch it could then never recognise as its own, and therefore never
adjust or stop. That is why it is first in the list checked at startup.

⚠️ **Note:** Alpha EMS never touches a dispatch it cannot prove is its own. That is safe
behaviour, but it also means a manually started charge simply keeps running.

---

## 3. Frank Quarter Prices

**Required** — <https://github.com/Bennie-JC/ha-frank-quarter-prices>

Provides Frank Energie's dynamic quarter-hour prices: an import price and an export rate
per quarter, for today and (from about 13:00) tomorrow.

**What you do:** install and configure this **before** Alpha EMS Manager. Without a
configured Frank instance you cannot add Alpha EMS — the form stops with a message
saying Frank must be set up first.

**What Alpha EMS does with it:** everything involving money. When buying pays, when
holding is worth more than selling, and what a day earned.

**About tomorrow.** Frank publishes the next day's prices only around 13:00–14:00. Until
then it is normal for Alpha EMS to know only today, and to look ahead less far. That is
not a fault.

---

## 4. Smart meter / P1 metering

**Required.**

Any Home Assistant integration that publishes **grid power** will do. Examples:
HomeWizard P1, DSMR, SlimmeLezer — or another integration providing a correct power
sensor for your grid connection. No brand is required.

**What you do:** during setup you select which sensor this is. Alpha EMS does not talk
to the hardware; it only reads the entity.

**What Alpha EMS does with it:** the meter is the instrument that *defines* export. It
is used to check that a discharge does not unintentionally reach the grid, and to
measure what was really sold.

⚠️ **Note:** direction is critical. See
[sign conventions](configuration.md#the-two-sign-conventions).

---

## 5. Home Assistant

Minimum Home Assistant version: 2025.1.0

Alpha EMS uses `entry.runtime_data`, generic `ConfigEntry` typing and coordinator
`config_entry` support. None of the three exists in older releases, so an older core
would crash immediately. HACS blocks installation below it.

---

## 6. Solcast (optional)

**Optional** — <https://github.com/BJReplay/ha-solcast-solar>

This is the only supported solar-forecast source.

**When do you need it?** When you want *expected* solar production to influence the plan
— for example to keep room in the battery for the afternoon.

**What you do:** configure your API key and rooftop sites at Solcast first, following
their documentation. Then, in Alpha EMS, select which sites belong to *this* system. A
Solcast account can hold rooftops for a second property, and folding those in silently
would be wrong.

**Your API allowance.** Alpha EMS calls two read-only actions that serve Solcast's own
cache. No extra fetch is made, so your allowance is untouched.

**Without Solcast** everything works except looking ahead at solar. Production that
arrives is still stored and counted; it is simply not anticipated.

---

## 7. PV power sensor

**Required as soon as you tell the settings you have solar panels** — which is the
default.

This is the sensor measuring what your panels are generating *now*. Separate from
Solcast: Solcast forecasts, this sensor measures.

If you have no panels, switch *This system has solar panels* off and the whole step is
skipped.

---

## 8. EV charger (optional)

**Optional.**

A power sensor for your charge point keeps car charging out of the learned household
baseline. Without it, Alpha EMS learns a charging session as ordinary household demand
and starts reserving energy for a car that may not be there tomorrow.

**This is not a charge scheduler.** Alpha EMS never starts, stops or plans your charge
point. It only removes the charging power from the learning series.

⚠️ **Note:** choose a sensor that publishes `0` when idle. A charger reporting
`unavailable` while doing nothing invalidates most of the day for the learning model.

---

## Next

[Installation](installation.md) · [Configuration](configuration.md)
