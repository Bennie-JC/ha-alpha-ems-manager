🇳🇱 [Nederlands](../nl/entities.md) | 🇬🇧 **English**

# Sensors and entities

**In one sentence:** Alpha EMS creates **17 sensors and one select** — 18 in total.

## Why this matters

All the detail lives in *attributes*, not in separate entities. Ninety-six quarter-hour
sensors would be technically easy and practically awful. So whatever you want on a
dashboard is almost always an attribute.

---

## Three things to know first

**1. Entity IDs follow your instance name.** The name you gave at setup is the device
name, and Home Assistant derives the IDs from it. With the default `Alpha EMS` you get
`sensor.alpha_ems_economic_action`. If you named it `Home`, it is
`sensor.home_economic_action`. Every ID on this page assumes the default name.

**2. Enum sensors show raw values.** You will see `safety_buy`, `mixed_buy`, `eligible`
and `not_executed` — not a polished description. That is deliberate: the names are not
translated, because Home Assistant derives the entity ID from the translated name and you
would otherwise get different IDs in every language. If you want friendly labels on a
dashboard, add them in the card.

**3. Everything is a primary entity.** There are no diagnostic entities. And with no data
they read `unknown`, not `unavailable` — you only see `unavailable` when the integration
itself is not loaded.

---

## Overview

| Entity | Unit | Kind |
|---|---|---|
| [Expected House Load Today](#expected-house-load-today) | kWh | forecast |
| [Expected House Load Tomorrow](#expected-house-load-tomorrow) | kWh | forecast |
| [Learning Confidence](#learning-confidence) | % | measured |
| [Learning Days](#learning-days) | — | measured |
| [Forecast Error Yesterday](#forecast-error-yesterday) | kWh | measured |
| [Forecast Error 7 Days](#forecast-error-7-days) | % | measured |
| [Battery Recommendation](#battery-recommendation) | — | advisory |
| [Planned Battery Power](#planned-battery-power) | kW | advisory |
| [Usable Battery Energy](#usable-battery-energy) | kWh | measured |
| [Dynamic Battery Reserve](#dynamic-battery-reserve) | kWh | advisory |
| [Economic Action](#economic-action) | — | execution |
| [Next Planned Action](#next-planned-action) | — | plan |
| [Control State](#control-state) | — | control |
| [Economic Value](#economic-value) | EUR | mixed |
| [Battery Return](#battery-return) | % | measured |
| [Current Campaign](#current-campaign) | — | execution |
| [Last Campaign Result](#last-campaign-result) | — | measured |
| [Control Mode](#control-mode) | — | **control (writable)** |

---

## Learning and forecasting

### Expected House Load Today

`sensor.alpha_ems_expected_house_load_today` — **kWh**

Your predicted house consumption for the whole day. This is *baseline* demand: if you
configured an EV sensor, car charging is already subtracted.

**Key attributes:** `actual_so_far_kwh` (measured today so far),
`forecast_remaining_kwh`, `measured_so_far_kwh` (raw, before subtraction),
`flexible_load_so_far_kwh` (EV energy removed), `model_days`, `confidence_percent`,
`adaptation_applied` and `adaptation_ratio` (whether and how much today's measurement
rescaled the forecast), `day_type`, `intervals_today`.

**When `unknown`?** When there are too few learned days. No forecast is more honest than
an invented number.

**No long-term statistics.** A prediction should not sit beside measured consumption on
the Energy dashboard.

### Expected House Load Tomorrow

`sensor.alpha_ems_expected_house_load_tomorrow` — **kWh**

The same, for tomorrow.

**Attributes:** `forecast_total_kwh`, `model_days`, `day_type`, `day_type_pooled`
(whether weekdays and weekends are still pooled for lack of data), `windows_used_days`,
`intervals_tomorrow`, `confidence_percent`.

### Learning Confidence

`sensor.alpha_ems_learning_confidence` — **%**

How mature and trustworthy the learning model is, 0 to 100.

It is *maturity* (how many days) multiplied by *quality* (how complete and stable).
Because it is a product, ninety days of gappy data can never score highly.

**Attributes:** `learned_days`, `maturity`, `coverage`, `recency`, `stability`,
`balance`, `quality`, `measured_coverage`.

**What is normal?** After 30 days maturity is around 63 % — it rises slowly by design.

### Learning Days

`sensor.alpha_ems_learning_days` — count

Calendar days with enough usable data. A day counts at 80 % coverage or better, and only
when complete; today therefore never counts.

**Attributes:** `retained_days`, `retained_intervals`, `history_start`, `history_end`,
`rejected_quarters`, `flexible_load_configured`, `intervals_without_flexible_data`,
`last_rejected_reason`.

### Forecast Error Yesterday

`sensor.alpha_ems_forecast_error_yesterday` — **kWh**

The difference between forecast and measurement. **Positive means the forecast was higher
than reality.**

**Attributes:** `absolute_error_kwh`, `error_percent`, `predicted_kwh`, `actual_kwh`,
`mae_kwh_per_interval`, `intervals_compared`, `intervals_in_day`, `horizon_days`,
`comparison_basis`.

**When `unknown`?** When the day could not be compared. Zero is the value of a perfect
forecast, not of a missing one.

### Forecast Error 7 Days

`sensor.alpha_ems_forecast_error_7_days` — **%**

The rolling error over seven days: summed absolute error divided by summed actual
consumption. "8 %" means the model was off by eight per cent of the energy it predicted.

**This is deliberately not an accuracy score.** There is no `100 − error` anywhere,
because that number goes negative on a bad week and invites comparison with unrelated
systems.

**Attributes:** `window_days` (7), `days_compared`, `intervals_compared`,
`mae_kwh_per_interval`, `bias_kwh_per_interval`, `predicted_kwh`, `actual_kwh`.

---

## The battery

### Battery Recommendation

`sensor.alpha_ems_battery_recommendation` — `hold` · `charge` · `discharge`

What the planning says the battery should do. **Advisory.**

**Attributes:** `reason`, `planned_power_kw`, `usable_energy_kwh`, `battery_soc_percent`,
`configured_min_soc_percent`, `effective_min_soc_percent`, `constraints`, `pv_aware`
(is a solar forecast behind it?), `basis`.

**When `unknown`?** When a hardware fact is missing — usually the capacity.

### Planned Battery Power

`sensor.alpha_ems_planned_battery_power` — **kW**

The quarter-average power implied by that recommendation. **Positive is into the
battery.**

⚠️ This is the plan's own convention and is unrelated to your configured *battery power
sign convention*. It is also an *average*, not a setpoint.

**Attributes:** `requested_mode`, `requested_power_kw`, `allowed_energy_kwh`,
`limiting_constraints`, `policy`, `policy_version`, `sign_convention`, `basis`.

### Usable Battery Energy

`sensor.alpha_ems_usable_battery_energy` — **kWh**

How much energy is available above your floor.

**Attributes:** `battery_soc_percent`, `capacity_kwh`, `stored_energy_kwh`,
`configured_min_soc_percent`, `effective_min_soc_percent`, `reserve_source`,
`coverage_hours`, `basis`.

⚠️ **This is an upper bound.** It applies a single efficiency figure and does not model
the inverter's own standby draw, so it is slightly optimistic.

### Dynamic Battery Reserve

`sensor.alpha_ems_dynamic_battery_reserve` — **kWh**

How much energy the battery should be holding right now.

**Note: this is an energy in kWh, not a percentage.** The corresponding percentage is in
`required_reserve_soc_percent`.

**Attributes:** `required_reserve_soc_percent`, `configured_min_soc_percent`,
`reserve_shortfall_kwh`, `reserve_reachable`, `replenishment_dependency_kwh`,
`lower_bound_reason`, `intervals_evaluated`, `basis`.

**`replenishment_dependency_kwh` is the attribute to know:** it says how much of the
reduction rests on sun that has not arrived yet. When it is large, the figure beside it
is optimistic.

**This is not a maximum.** The requirement falls while replenishment is close and rises
once it has passed. Around midday it can be as low as your configured floor — correctly,
because the afternoon sun refills the pack well before the evening peak.

**When `unknown`?** Without a forecast, or without a usable battery configuration. Never
a fabricated zero.

---

## Plan and execution

### Economic Action

`sensor.alpha_ems_economic_action` — `charge` · `export` · `safety_buy` · `mixed_buy` ·
`idle` (with `hold`, `discharge`, `curtail_pv` also declared)

What is economically happening **right now**.

**Attributes:** `owned` (is this dispatch ours?), `mode` (`off`/`shadow`/`active`),
`purpose`, `campaign_id`, `run_id`, `planned_kwh`, `realised_kwh`, `power_kw`,
`started_at`, `capability_action`, `execution_blocked_reason`.

**`execution_blocked_reason` is the attribute you will need most.** It says why nothing is
sent: `execution_not_enabled`, `mode_not_active`, `battery_export_not_permitted`,
`no_primitive_curtail`, `action_not_executable`, or `none` when nothing is in the way.

`idle` and `unknown` differ: `idle` means "nothing is happening", `unknown` means
"nothing could be worked out".

**This sensor also feeds the logbook.** See [Trading Log](../TRADING_LOG.md).

### Next Planned Action

`sensor.alpha_ems_next_planned_action` — same values

What is planned next, with full timestamps. This sensor exists precisely because
*Economic Action* only describes the present.

**About the next campaign:** `starts_at`, `ends_at`, `planned_kwh`, `power_kw`,
`purpose`, `campaign_id`, `run_id`, `price_eur_kwh`, `expected_value_eur`, `reason`.

**About the measurement boundary:** `objective_boundary` — `battery` for a purchase,
`meter` for a sale — and `objective_boundary_rule`, which explains that every published
objective is AC, so no efficiency factor should be applied to it.

**The two charge windows** — see
[example 10](how-it-works.md#10-wide-charge-span-narrow-buy-blocks):

| Attribute | Meaning |
|---|---|
| `next_charge_projection_at` / `next_charge_projection_end_at` | When the battery is charging |
| `next_grid_purchase_at` / `next_grid_purchase_end_at` | The next block where energy is really bought from the grid |
| `charge_window_rule` | One sentence explaining the difference |

**State-of-charge projection:** `battery_before_next_charge_soc_percent` and
`battery_before_next_charge_dc_kwh` (what is in the pack just before the next charge),
plus the same two for `..._export_...`. Also `minutes_until_reserve_floor`,
`reserve_floor_reached_at`, `projection_basis` and `projection_unavailable_reason`.

**`upcoming` — the campaigns ahead,** at most eight, each with:

| Key | Meaning |
|---|---|
| `objective_kwh` | The campaign's objective |
| `objective_boundary` | `battery` or `meter` |
| `grid_purchase_kwh` | How much of it is really bought |
| `production_charge_kwh` | How much arrives free |
| `grid_purchase_blocks` | The number of separate buying stretches |
| `charge_source` | `production`, `grid` or `mixed` |
| `will_execute` / `skip_reason` | Whether it is executable, and if not why |
| `expected_value_eur` | What it is expected to earn |

⚠️ **`grid_purchase_kwh` and its three neighbours live here, on `upcoming`, not on the
campaign sensors below.** Those publish a different vocabulary. On an export campaign
these four are `null` — not zero, because a sale buys nothing.

When nothing is planned the attribute set is smaller: `starts_at`, `ends_at`,
`planned_kwh` and `objective_boundary` are `null`, and `charge_window_rule`, `upcoming`
and the campaign fields are absent.

### Control State

`sensor.alpha_ems_control_state` — `off` · `inhibited` · `eligible` · `idle` ·
`executing` · `error`

What the control pipeline made of the recommendation.

**Attributes:** `inhibit_reason`, `authorization_refusal`, `action`, `device_power_kw`,
`commands_planned`, `capability_ready`, `dispatch_active`, `basis`.

**This is a different question from *Economic Action*, and they may disagree.** *Economic
Action* asks "what is economically best?", *Control State* asks "would a command be
allowed at all right now?". So `export` beside `inhibited` is not a contradiction.

### Current Campaign

`sensor.alpha_ems_current_campaign` — `planned` · `created` · `started` · `idle`

The action running now.

**Attributes:** `campaign_id`, `campaign_instance_id`, `purpose`, `classification`,
`classification_at_creation` (frozen at creation), `planned_kwh`, `realised_kwh`,
`window_start`, `window_end`, `first_executable_at`, `started_at`, `revision`,
`objective_boundary`, plus the breakdown: `safety_buy_kwh`, `coverage_buy_kwh`,
`economic_buy_kwh` for a charge campaign, or `grid_export_kwh` for a sale.

There is deliberately no `stopped` state; it would not be observable.

### Last Campaign Result

`sensor.alpha_ems_last_campaign_result` — `success` · `partial` · `canceled` · `failed` ·
`not_executed` · `superseded`

How the previous campaign ended.

**Attributes:** `available`, `planned_kwh`, `realised_kwh`, `shortfall_kwh`, `result`,
`completion_reason`, `objective_measurable`, `success_tolerance_kwh`,
`final_classification`, the timestamps, and the same breakdown as above.

**A delivered amount is not by itself a success.** Only the frozen objective, within
`success_tolerance_kwh`, counts as met. `not_executed` means created and never started;
`superseded` means started and replaced.

⚠️ **This sensor does not survive a restart.** Afterwards it reads `unknown` with
`no_campaign_closed_yet` until the next campaign ends. Do not read that as a campaign
that disappeared.

---

## Money

### Economic Value

`sensor.alpha_ems_economic_value` — **EUR**

The expected cash advantage of the selected plan over doing nothing, across the currently
known horizon.

`0.00` and `unknown` differ: `0.00` is a valid answer.

**The four terms that add up to today** — see [Economics](economics.md):
`realised_today_eur`, `in_progress_interval_eur`, `remaining_expected_today_eur`,
`forecast_revaluation_eur`, together `total_economic_value_today_eur`.

**The energy-value split:** `realised_self_consumption_value_eur`,
`realised_export_value_eur`, `realised_load_shifting_value_eur`,
`realised_energy_value_eur`.

**Also:** `decision_advantage_eur` (the same number as the state — a from-now comparison
that must **not** be added to the four terms), `accounting_basis`,
`accounting_unavailable_reason`, `stored_energy_marginal_value_eur_kwh`,
`current_import_price_eur_kwh`, `current_export_price_eur_kwh`, `horizon_from`,
`horizon_to`, `tomorrow_prices_known`.

**`figure_basis` is the most useful attribute on this sensor.** It maps every euro figure
to `measured`, `attributed`, `estimated` or `unclassified` — and therefore tells you
which numbers may legitimately be added together.

**When `unknown`?** `plan_unavailable`, `horizon_empty`, `no_actionable_intervals` or
`reserve_violation_outranks_money`.

### Battery Return

`sensor.alpha_ems_battery_return` — **%**

What percentage of your net investment has been recovered by realised benefit. Measured
cash only; no forecast reaches it.

**Investment:** `gross_investment_eur`, `subsidy_eur`, `other_one_time_credit_eur`,
`net_investment_eur`.

**Result:** `cumulative_realised_benefit_eur`, `remaining_to_recover_eur`,
`recovered_percent`, `average_realised_benefit_per_day_eur`, `sample_days`,
`trailing_30d_eur`, `trailing_90d_eur`.

**Payback:** `estimated_payback_date`, `estimated_payback_years`,
`payback_unavailable_reason`.

**Provenance:** `investment_date`, `accounting_start_date`, `sealed_through`,
`unsealed_past_days`, `unsealed_by_reason`, `lifetime_history_complete` and more — these
say which days were counted, which were not, and why.

**When `unknown`?** `no_investment_configured` (you entered no amount),
`no_finalised_days` (no day has closed yet) or `no_finalised_days_in_accounting_period`.

---

## Control

### Control Mode

`select.alpha_ems_control_mode` — **the only writable entity**

| Shown | Stored | Meaning |
|---|---|---|
| Off | `off` | Watches, sends nothing |
| Shadow | `shadow` | Decides fully, sends nothing |
| Live | `active` | Permitted actions may be sent |

The stored value `active` and the label "Live" differ on purpose: `active` is what every
stored document already says, "Live" is the clearer word.

Changing the mode refreshes the plan immediately, without the usual debounce.

See [Control and safety](control-and-safety.md).

---

## What about the logbook?

**"Trading Log" is not an entity.** There is no `sensor.alpha_ems_trading_log`. It is a
Home Assistant logbook view, fed by events *Economic Action* fires at each transition in
a campaign's lifecycle.

[docs/TRADING_LOG.md](../TRADING_LOG.md) contains a worked example with YAML you can
copy.

---

## Further reading

[Economics](economics.md) · [How Alpha EMS works](how-it-works.md) ·
[Diagnostics](diagnostics.md)
