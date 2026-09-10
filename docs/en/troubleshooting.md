🇳🇱 [Nederlands](../nl/troubleshooting.md) | 🇬🇧 **English**

# Troubleshooting

**In one sentence:** most reports turn out to be correct behaviour — these are the cases
you will meet most often, and how to tell them apart.

## First of all

Enable debug logging when investigating:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.alpha_ems_manager: debug
```

And download [diagnostics](diagnostics.md) — nearly every question below is answered
there.

---

## Installing and setting up

### I cannot add Alpha EMS

**"Frank Quarter Prices is not set up yet"** — Frank is mandatory and must be configured
before Alpha EMS. See [Requirements](requirements.md#3-frank-quarter-prices).

**An error on the Solcast toggle** — you enabled *Use a PV forecast source* without a
configured Solcast integration. Switch it off, or install [Solcast](solar.md) first.

**"config entry … uses the version 1 source model"** — this is a 0.1.0 entry. It cannot be
migrated: remove the integration and add it again. See
[Installation](installation.md#upgrading-from-010).

### A sensor is refused

The unit must match: power in W, kW or MW; energy in Wh, kWh or MWh; state of charge
exactly `%`. If your EV sensor is refused, check it is not the same entity as your
house-load sensor.

---

## Learning and forecasting

### Expected load stays `unknown`

About two full days of history are needed. Check whether
`learning.measured_valid_intervals` is rising in diagnostics. If it reads 0, your
house-load sensor is not producing usable values — check `sources.house_load.state` and
`.unit`.

**This is not a fault.** No invented number is published to fill an empty box.

### Learning Days does not increase

A day needs at least 80 % coverage and must be complete; today therefore never counts.

Compare `learning.measured_coverage` with `learning.baseline_coverage`. If the first is
high and the second low, the problem is your EV sensor, not your house-load sensor.

### Learning Confidence stays low

Confidence is maturity × quality, and maturity saturates slowly by design — about 63 %
after 30 days. If it is lower than the day count suggests, look in the `confidence` block
to see which component is lagging.

### My EV charger is ruining the learning

If your charger reports `unavailable` instead of `0` when idle, house load is invalidated
for every idle quarter — which is most of the day. Look at
`flexible_load.intervals_without_valid_data`.

Choose a sensor that publishes a numeric zero, or leave the field empty.

---

## Prices

### Tomorrow's prices are missing

Frank does not request the next day's prices before noon and publishes them around
13:00–14:00. Until then, "today complete, tomorrow absent" is normal.

Alpha EMS draws no conclusion from the clock: if the day is already there, it is used.

### Coverage is below 1.0

Frank publishes a *market day* from midnight to midnight in the market's own timezone,
while Alpha EMS plans a civil day in yours. If you run Home Assistant outside
`Europe/Amsterdam` or `Europe/Brussels`, those are different spans.

The shortfall is reported rather than extrapolated away.

---

## Plans and decisions

### There is a plan, but nothing happens

Work through these in order:

1. Is **Control Mode** set to **Live**? If not: `mode_not_active`.
2. Is *Allow Alpha EMS to send commands* on? If not: `execution_not_enabled`.
3. Is this a purchase? Is *Allow buying from the grid* on?
4. Is this a sale? Is *Allow selling from the battery* on? If not:
   `battery_export_not_permitted`.
5. Does `input_boolean.alpha_ems_dispatch_owner` exist?
6. Does `automation.alphaess_dispatch_reset_full` exist **and is it switched on**?

The `execution_blocked_reason` attribute on `sensor.alpha_ems_economic_action` says which
one it is.

### Alpha EMS bought at an expensive moment

Check whether it was a `safety_buy`. If so this is not a fault: without that purchase the
battery cannot reach its reserve, and safety outranks price. No price threshold applies.

See [example 3](how-it-works.md#3-safety-buy).

### Alpha EMS did not sell at a high price

Often exactly right. Selling at 0.29 to buy the same energy back at 0.30 is a loss. And if
selling would make the reserve unsafe, it does not happen at all.

Do check that *Allow selling from the battery* is on.

See [example 8](how-it-works.md#8-high-price-and-still-no-sale).

### The charge window is far wider than I expected

Almost always correct. A quarter that only takes in free solar does not interrupt a charge
run, so on a sunny day one campaign spans almost the whole solar period.

Look at `next_grid_purchase_at` and `next_grid_purchase_end_at` for when buying really
happens, and at `grid_purchase_kwh` against `production_charge_kwh` for the ratio.

See [example 10](how-it-works.md#10-wide-charge-span-narrow-buy-blocks).

### The plan keeps changing

Future quarters may change as the sun, your load or the prices change. A quarter that has
already started does not.

If the `run_id` changes every quarter, or the revision counts up on every refresh, that
*is* a bug worth reporting.

### The campaign missed its target

`partial` with `window_ended` means time ran out. Possible causes: the sun disappointed so
more should have come from the grid, the pack filled up, or a bound was binding.

Look in the `execution` block for which bound bound. Energy missed in a closed quarter is
**not** made up later — each quarter has its own frozen target.

---

## Battery and control

### Alpha EMS will not touch a running charge

It is probably not its own. Look at `execution.ownership.state`: at `foreign` Alpha EMS
cannot prove it started the dispatch, so it keeps away. That is deliberate.

Most common cause: the `input_boolean.alpha_ems_dispatch_owner` helper does not exist.

### Battery planning reads `unknown`

A hardware fact is missing. Usually the capacity or a power limit was cleared on the
*Battery planning* page. The reason is stated: `REASON_MISSING_CAPACITY` or
`REASON_MISSING_POWER_LIMITS`.

Nothing is guessed. Enter the values again.

### Last Campaign Result is `unknown` after a restart

That is expected. The sensor is not persisted and reads `unknown` with
`no_campaign_closed_yet` after a restart, until the next campaign ends. Do not read it as a
campaign that disappeared.

### The charge stopped after a restart

Also expected. Progress inside the open quarter is not persisted, so continuing would be
guessing. At the next quarter the plan makes a new run.

---

## Measurement

### Energy-balance warnings

*"Sustained energy-balance mismatch over N consecutive checks"* means three coherent
failed samples in a row, so roughly three minutes. A single odd instant never warns.

There are two variants and they mean different things:

| Message | Meaning |
|---|---|
| *"…usually means one term of the identity is wrong"* | The residual is many times its allowance. Check your sensors and your two sign conventions — something really is misconfigured. |
| *"…consistent with the sources being measured at different electrical boundaries"* | Moderately over the allowance. Most likely your inverter's DC/AC boundaries, not a mistake you made. |

**This does not affect learning** and blocks no battery decision. It only feeds the
confidence score.

### A persistent small residual at low power

On the reference installation a sustained residual of around 154 W on 740 W appears from
time to time and then clears on its own; the pass rate stays around 99 %.

The explanation is almost certainly that the grid figure comes from a separate P1 meter
while house load, solar and battery all come from the inverter. A roughly constant offset
between two instruments on different electrical boundaries is negligible on a busy
afternoon and a large fraction of a quiet one.

**No threshold has been widened to silence it.** That would blind the check at every power
level to explain one regime, and hiding a wiring or sign error is worse than the warning.

### My numbers do not match the Alpha app

Mostly expected. The two apps split the same day along different lines, and only one row
is measured on the same basis in both.

- **Sold to grid (kWh)** — this one *should* agree, with
  `realised_metered_export_kwh`. Both are meter-side totals. If it does not reconcile,
  that is worth reporting.
- **Sold to grid (€)** — same energy, but the two apps can treat the export price
  differently, so the euros may not match.
- **Self-consumption** — Alpha EMS publishes no self-consumption **volume**, so there is
  nothing to compare against Alpha's kWh figure, and the euro difference cannot currently
  be traced to a single cause.
- **Load shifting** and **the total** — different decomposition axis and different
  baseline. Not expected to match.

⚠️ Do not compare Alpha's *Sold to grid* against `realised_export_value_eur`. That is the
no-battery counterfactual — what your array *without* a battery would have sold — and it
matches no row in the Alpha app.

The full mapping, with a worked example, is in
[Economics](economics.md#comparing-with-the-alpha-app).

### A source drops out

Gaps are recorded as missing coverage, never as zero. Short outages are absorbed; longer
ones reduce the day's completeness. Warnings are rate-limited to one per hour per cause, so
a long outage will not flood your log.

---

## Solar

### Forecast and measurement differ structurally

Three normal causes: different measurement boundaries between your PV figure and Solcast's,
inverter clipping on the best days, and Solcast settings such as auto-dampening or actuals
blending.

**Nothing is corrected.** That is deliberate. See [Solar](solar.md).

### The solar forecast is not being used

Check that *Use a PV forecast source* is on, that a Solcast instance is selected, **and**
that you actually ticked rooftops under *Solcast sites that belong to this system*.

---

## Still stuck?

Report it with a [diagnostics download](diagnostics.md) attached on the
[issue tracker](https://github.com/Bennie-JC/ha-alpha-ems-manager/issues). It contains no
credentials.

---

## Further reading

[Diagnostics](diagnostics.md) · [Configuration](configuration.md) ·
[How Alpha EMS works](how-it-works.md)
