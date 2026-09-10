🇳🇱 [Nederlands](../nl/solar.md) | 🇬🇧 **English**

# Solar

**In one sentence:** with a solar forecast, Alpha EMS knows free energy is coming and can
keep room for it.

## Why this matters

Without a forecast Alpha EMS is blind to the sun. It still stores incoming production, but
it cannot anticipate it. That costs money in exactly one way: it fills the pack cheaply in
the morning, and by midday there is no room left for free solar.

---

## Two different things

| | What it is | Required? |
|---|---|---|
| **PV power sensor** | Measures what your panels generate **now** | Yes, if you have panels |
| **Solcast** | Forecasts what they will generate **later** | No |

You always need the first if you have panels; you select it during setup. The second is
optional and adds the looking-ahead.

---

## Setting up Solcast

**Only Solcast is supported** — <https://github.com/BJReplay/ha-solcast-solar>. There are
no other forecast sources.

**Step 1 — at Solcast.** Create an account, request an API key and enter your rooftop
sites: orientation, tilt and capacity. Their documentation is the authority.

**Step 2 — install the Solcast integration** in Home Assistant and configure it with your
API key.

**Step 3 — in Alpha EMS.** Switch on *Use a PV forecast source* and select your Solcast
instance.

**Step 4 — choose your rooftops.** Under *Solcast sites that belong to this system*, tick
the sites belonging to *this* home.

⚠️ **This step is often skipped and it matters.** A Solcast account can hold rooftops for
a second property or for somebody else. Folding those in silently would feed your plan
wrong figures. On an upgrade every site found is selected for you once; a rooftop you add
to Solcast later is reported as available but **not** added on its own.

**Your API allowance is untouched.** Alpha EMS calls two read-only actions that serve
Solcast's own cache. Nothing is fetched from Solcast, so your allowance is not used. The
mutating actions — update, force-update, clear-data, dampening, hard-limit — appear
nowhere in the source.

---

## What Alpha EMS does with the forecast

**Expected production is netted against expected load** before the battery is asked for
anything. On a sunny afternoon the answer is then "hold" — not because a rule was added,
but because the existing rule is finally shown the right number.

**Keeping room free.** When Alpha EMS expects a lot of sun, it may decide not to fill the
pack from the grid in the morning. See
[example 6](how-it-works.md#6-headroom-for-future-solar).

**The reserve falls.** The dynamic reserve credits sun that has not arrived yet. On a
summer night with a sunny day ahead, the battery need not carry energy the sun will
supply.

⚠️ Watch `replenishment_dependency_kwh` on
`sensor.alpha_ems_dynamic_battery_reserve`. It says how much of the reduction rests on sun
that has not arrived. When it is large, the reserve beside it is optimistic.

---

## Absorbing free solar

When more sun arrives than is needed, Alpha EMS can put the surplus into the battery
instead of exporting it — if keeping it is worth more than the export price.

The decision is a comparison, not a rule:

```
efficiency × what a stored kWh is worth    >    the export price
```

Where selling genuinely pays more, Alpha EMS sells. There is no "never export" rule.

**This can never buy anything.** The extra charging is capped at the production actually
measured spare at that moment, so your meter can only move toward zero.

Absorption stops on its own terms: a full pack, the inverter's power limit, the sun going
away, or the quarter ending. What does not fit simply goes to the grid — on a good day
some export always remains.

---

## Why a charge window looks so wide

This is the most commonly reported confusion, and it comes from the absorption above.

```
The battery charges:        08:30 ────────────────────► 16:30
Real grid buying:                 11:45─12:15  12:45─13:00  14:00─14:30
```

A quarter that only takes in free solar **does not interrupt a charge run**. That is
deliberate: otherwise every cloud would cut the run in two and you would pay the switching
cost twice. The consequence is that one charge campaign on a sunny day can span almost the
whole solar period.

Use `next_grid_purchase_at` and `next_grid_purchase_end_at` when you want to know when
energy is really bought. See
[example 10](how-it-works.md#10-wide-charge-span-narrow-buy-blocks).

---

## Forecast is not measurement

Both sides are kept separately, and **nothing** is corrected. A disappointing day does not
change tomorrow's forecast.

That is deliberate: it keeps the recorded error a measurement *of* the model rather than a
product *of* it.

Three reasons forecast and measurement can differ structurally without anything being
wrong:

**Different measurement boundaries.** Your PV figure sums DC strings and an AC meter;
Solcast does not state which boundary it uses. A persistent difference is then a property
of the installation, not a forecast error.

**Inverter clipping.** If your array can out-produce your inverter's AC limit, the
forecast will exceed the measurement on the best days by design. Where the limit is
readable the day is flagged; where it is not, the check is switched off rather than
guessing a ceiling.

**Solcast's own settings.** If you enable auto-dampening or actuals blending at Solcast,
the series Alpha EMS reads has already been adjusted by something else. Both are recorded
in diagnostics.

---

## Without Solcast

Everything keeps working. Sun that arrives is stored and counted, self-consumption is
calculated normally, and your economics are correct.

What you lose is the looking ahead: no keeping room for the afternoon, and a reserve that
gives no credit for sun still to come. Without a forecast an interval is "PV-blind" and
the state-of-charge projection is reported as a lower bound.

⚠️ If **Excess Export** is enabled on your inverter, it deliberately sends surplus to the
grid instead of the battery. Alpha EMS sees this and reports its projection as a lower
bound rather than overstating your pack.

---

## Further reading

[Configuration](configuration.md) · [How Alpha EMS works](how-it-works.md) ·
[Troubleshooting](troubleshooting.md)
