🇳🇱 [Nederlands](../nl/diagnostics.md) | 🇬🇧 **English**

# Diagnostics

**In one sentence:** diagnostics answers "why did Alpha EMS do that?" — this page tells
you where to look for each question.

## Why this matters

Almost every decision Alpha EMS makes can be traced. The download contains the whole plan,
the reasons, the intermediate steps and the measured outcome. This is not a list of every
field — it is a list of questions.

## Getting the download

**Settings → Devices & Services → Alpha EMS Manager → Download diagnostics**

You get a JSON file. It contains no credentials — the integration holds none — and no full
history dump.

---

## Is Alpha EMS receiving all its sources?

**Look in:** `sources`

For each source you see whether it was found, its current value and the unit it publishes.
If something is missing, this is where you see which.

**Also look in:** the control-entity block. It lists which AlphaESS entities are missing or
unavailable — including `input_boolean.alpha_ems_dispatch_owner` and
`automation.alphaess_dispatch_reset_full`.

---

## Is the forecast healthy?

**Look in:** `learning` and `confidence`

- `measured_valid_intervals` rising → your house-load sensor is producing usable data
- `measured_coverage` high but `baseline_coverage` low → your EV sensor is the problem,
  not your house-load sensor
- `confidence` shows the components separately: coverage, recency, stability, balance

**Also look in:** `forecast_history` for how good the forecasts have been so far, broken
down by how far ahead they were made and by time of day.

---

## Why did Alpha EMS buy?

**Look in:** `economic_plan` → `campaigns[]`

Each campaign carries the split by reason:

| Field | Meaning |
|---|---|
| `safety_buy_ac_kwh` | Compelled to reach the reserve — no price threshold |
| `coverage_buy_ac_kwh` | Your house consumes it later anyway; cheaper now |
| `economic_buy_ac_kwh` | A trade Alpha EMS chose itself |

`coverage_saving_eur` and `coverage_baseline_charge_ac_kwh` show what ordinary economics
would have bought on its own, so you can check the split rather than take it on trust.

See [the three reasons](how-it-works.md#three-reasons-to-buy).

## Why did Alpha EMS *not* buy?

**Look in:** `sensor.alpha_ems_economic_action` → `execution_blocked_reason`

Usually one of these:

| Reason | What to do |
|---|---|
| `execution_not_enabled` | Switch on *Allow Alpha EMS to send commands* |
| `mode_not_active` | Set Control Mode to Live |
| `action_not_executable` | This action has no actuator (`serve_load`) |
| `no_primitive_curtail` | Curtailing solar does not exist here |
| `none` | Nothing is in the way |

If nothing is in the way and still nothing happens, look at the plan itself: the purchase
may not have cleared your thresholds. Then `minimum_trade_gain_eur` or
`grid_charge_margin_eur_per_kwh` is the explanation.

---

## Why did Alpha EMS *not* sell?

This is the most-asked question on a high evening price, and usually the answer is that it
was right. See [example 8](how-it-works.md#8-high-price-and-still-no-sale).

**Look in:** `execution_blocked_reason` for `battery_export_not_permitted` — then *Allow
selling from the battery* is off.

**Otherwise look in:** the plan. Selling is refused when the revenue does not beat what
that energy saves your house later, or when it would make the reserve unsafe. The latter
is absolute: no price buys a reserve violation.

**Also check:** `retention_gate`. If it reads `refused_export_superior`, then selling did
beat keeping.

---

## Did the campaign reach its target?

**Look in:** `sensor.alpha_ems_last_campaign_result`

| Field | Meaning |
|---|---|
| `planned_kwh` | The frozen objective |
| `realised_kwh` | What was actually delivered |
| `shortfall_kwh` | The difference |
| `result` | `success`, `partial`, `canceled`, `failed`, `not_executed`, `superseded` |
| `completion_reason` | Why it ended |

`campaign_objective_reached` means the target really was met; `window_ended` means time
simply ran out. Since beta.40 those two no longer contradict each other.

⚠️ A delivered amount is not by itself a success. Only the frozen objective, within
`success_tolerance_kwh`, counts.

---

## Did execution match the plan?

**Look in:** the `execution` block

It shows side by side what Stage A expected of production, house load and the grid against
what actually happened. Also how much energy reached the battery, what power the
controller asked for, and why it did not ask for more.

**On the executing quarter:**

| Field | Meaning |
|---|---|
| `battery_objective_realized_this_quarter_kwh` | What the plan asked for and got |
| `battery_absorbed_extra_this_quarter_kwh` | Free solar kept on top of it |
| `battery_realized_this_quarter_kwh` | The two added |
| `retention_authorised_this_quarter` | Whether keeping free solar was worth it |
| `absorption_gate` | Why, in one word |

**Is the `run_id` changing every quarter,** or the revision counting up on every refresh?
That is a bug worth reporting. Stage A's `plan_id` *should* change every fifteen minutes —
that is the rolling horizon.

---

## Whose dispatch was it?

**Look in:** `execution` → `ownership` → `state`

`owned` is ours, `foreign` is not ours and is left alone, `unproven` means the marker is on
but the record does not match. See
[Control and safety](control-and-safety.md#whose-dispatch-is-this).

**Why did the last dispatch stop?** `execution.carried.last_ended` keeps the last ended run
with its reason, target, delivered energy and what was left — so you can ask hours
afterwards.

⚠️ This is **session-local**. A restart clears it, deliberately: a retained claim about a
session that is no longer running would be a stale fact wearing the clothes of a current
one.

---

## Is the energy balance healthy?

**Look in:** `energy_balance`

The check tests whether this roughly holds:

```
solar + grid import + discharge  ≈  house load + charge + grid export
```

| Field | What to watch |
|---|---|
| `last_coherent_sample` | `residual_w` against `allowed_residual_w` |
| `tolerance_reason` | Why the allowance is that size |
| `failed_samples` vs `skipped_incoherent_samples` | Many skipped = a slow source, not a fault |
| `source_time_skew_seconds` | Near 90 seconds means one source polls far more slowly |

**An inverted sign is unmissable:** the residual is then ten to twenty times its allowance.
A moderate overshoot points more at different measurement boundaries in your installation
than at a mistake you made.

**This check can never reject a learned interval.** It is a quality signal feeding the
confidence score; it influences no battery decision at all.

---

## Why is Battery Return standing still?

**Look in:** the attributes of `sensor.alpha_ems_battery_return`

| Field | Meaning |
|---|---|
| `sealed_through` | Up to which day everything is closed |
| `unsealed_past_days` | How many past days are not yet closed |
| `unsealed_by_reason` | Why those days are still open |
| `terminally_unsealable_days` | Days that can never be closed |
| `seal_pass_blocked_reason` | Why the last attempt did nothing |

A day only counts once it is closed. If there are none yet, the sensor reads `unknown` with
`no_finalised_days` — that is not a fault, just patience.

---

## What is not in diagnostics

No credentials, no full learning history, and no price series. The `price` block carries
the counts, the coverage, the horizon and why the next day may be absent — but never the
series itself.

---

## Further reading

[Troubleshooting](troubleshooting.md) · [Sensors and entities](entities.md) ·
[How Alpha EMS works](how-it-works.md)
