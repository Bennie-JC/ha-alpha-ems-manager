🇳🇱 [Nederlands](../nl/economics.md) | 🇬🇧 **English**

# Economics

**In one sentence:** what your battery earns you, split into parts that do not overlap,
so you are allowed to add them together.

## Why this matters

"What is that battery actually earning?" is a hard question, because several answers are
all true and mean different things. This page starts with the simple answer and only then
goes deeper.

---

## The simple story: energy value

There are three ways your energy is worth money:

| What happens | How it counts |
|---|---|
| Solar → house | **Self-consumption** |
| Solar → grid | **Export** |
| Battery moves energy from cheap to expensive, or avoids expensive import | **Load shifting** |

Together they form the **energy value**:

```
Self-consumption   € 0.60
Export             € 0.15
Load shifting      € 1.25
---------------------------
Energy value       € 2.00
```

**Why you may add these up:** every kilowatt-hour counts in exactly one row. Solar that
goes into your house is self-consumption and not export. Solar that goes to the grid is
export and not self-consumption. The battery moving energy through time is load shifting
and nothing else. They are deliberately defined so that no kWh is counted twice.

### Self-consumption

Solar feeding your house directly is electricity you did not have to buy. Its value is
what you would otherwise have paid — the import price at that moment.

### Export

Solar you do not use yourself, going to the grid. Its value is what you receive for it:
the export rate.

⚠️ Importing and exporting are **not two sides of one number**. The import side carries a
fixed markup of roughly € 0.129/kWh in sourcing margin and energy tax; the export side
does not. So on a negative market price, importing still costs money while exporting earns
a negative amount.

⚠️ **This row is not your meter.** It answers "what would this array have sold *without*
a battery". It has to: some of what your meter sent out came from the battery, and that
sale is already counted under load shifting. Putting it here as well would count one sale
twice. If you want the meter, see [Your actual export](#your-actual-export) below.

### Load shifting

This is the battery's own work. Energy that went into the pack cheaply in the afternoon
and avoids expensive grid import in the evening. Or energy bought when it was cheap
rather than at the expensive moment you needed it.

With a dynamic contract this is usually the largest part.

---

## What you see on the sensor

`sensor.alpha_ems_economic_value` carries these four attributes, and they add up:

```
realised_today_eur              what the closed part of today realised
+ in_progress_interval_eur      what the quarter in flight has realised so far
+ remaining_expected_today_eur  what the plan still expects before midnight
+ forecast_revaluation_eur      how much the energy the day opened with has been
                                revalued
= total_economic_value_today_eur
```

In one sentence: **today's cash, plus what the pack is worth now, less what it was worth
when the day opened.**

This is an economic **position**, not money in the bank. The third term is a forecast and
two terms are planner valuations. The sensor says so itself, in `accounting_basis`.

**Two properties to know:**

- The **same counterfactual** is used throughout: a household with no battery at all. Not
  "what if the battery did nothing this interval".
- The **quarter in flight is a separate term** and only joins history when its measurement
  closes. That is why `realised_today_eur` can never go down.

**If one term is missing, the total is missing.** A zero is never substituted for an
unknown. `accounting_unavailable_reason` says which term it was. The two you are most
likely to see:

- `no_opening_valuation` — the first day after installing, until the next midnight
- `horizon_short_of_midnight` — only one price day is published, so part of your day has
  no price. Resolves once tomorrow's prices arrive.

---

## Your actual export

Two export figures, answering two different questions. Both are correct, and mixing them
up is the most common way to read a wrong number off this sensor.

| Attribute | The question it answers |
|---|---|
| `realised_export_value_eur` | What an array like yours **without a battery** would have sold. Part of the energy-value sum above. |
| `realised_metered_export_kwh` | How much energy **actually left your meter** today. |
| `realised_metered_export_revenue_eur` | What that energy fetched. |

A tile reading *Sold to grid — 0.49 kWh · € 0.11* wants the second pair.

**What the metered pair includes:** everything that crossed the meter outward, whether it
came from your panels or from the battery. Your meter cannot tell the difference, and this
figure is your meter's.

⚠️ **The volume is measured; the price may be reconstructed.** The kilowatt-hours come
from your grid sensor and carry no efficiency factor at all. The rate they are valued at
is the export rate recorded for each quarter, which Alpha EMS reconstructs from the market
price and your configured feed-in adjustment. It is a good figure to steer by, not a
settled invoice — your supplier's annual statement is the authority on what you were paid.

**Do not add the metered pair to the energy-value sum.** It is deliberately outside it. The
three components already account for every kilowatt-hour exactly once, and the metered
revenue is the same electricity under a different convention.

A quarter that exported while no export rate was recorded is **skipped**, not valued at
zero. So the pair can be a little low on a day with a price gap, and is never inflated.

---

## Three numbers that look alike and are not

This is the most common misreading, so explicitly:

| Attribute | What it is | May you add it? |
|---|---|---|
| `realised_today_eur` | Cash actually realised in the closed part of today | Yes, with the other three terms |
| `total_economic_value_today_eur` | Today's whole position: the sum of the four terms | It *is* the sum |
| `decision_advantage_eur` | What the chosen plan does better than doing nothing, **from now on** | **No** |

`decision_advantage_eur` is the same value as the sensor's state. It is a forward-looking
comparison, not a realised quantity, and adding it to the four position terms produces a
number that means nothing.

Diagnostics also carries `net_cash_flow_eur`: import less export. There, a **negative**
value means money came in. That is not a profit and does not belong in a sum with the
rest.

### So which amounts may you add?

There is an attribute for that: **`figure_basis`**. It names the basis of every amount on
the sensor:

| Basis | Meaning |
|---|---|
| `measured` | Measured, from real readings and published prices |
| `attributed` | Assigned by a fixed rule |
| `estimated` | Estimated, for example a forecast |
| `unclassified` | Not yet classified |

If you are building a dashboard, look here: adding amounts with different bases gives a
number you cannot use.

---

## Why there is no profit-per-trade figure

You would expect "this charge earned € 0.43". Alpha EMS deliberately does not publish
that.

To calculate it you must know *which* stored kilowatt-hour you are selling now — this
morning's at 0.20, or yesterday's at 0.31. **A battery does not track that.** There is no
label on an electron. Every answer therefore rests on a convention you choose yourself
(first-in-first-out, average price, or something else), and different conventions give
different profits for exactly the same day.

A number that depends on an arbitrary choice does not belong beside numbers that do not.
What *is* published is the measured result over a period — and that does not depend on a
convention.

---

## Payback: Battery Return

`sensor.alpha_ems_battery_return` answers the other money question: **how much of my
battery has been recovered?**

```
net investment = gross investment − subsidy − other one-time credit
```

Against that stands the accumulated, actually realised benefit of every **closed** day.
The sensor shows what percentage has been recovered.

**Measured cash only.** No forecast, no planner valuation, no day in progress. A day
counts only once it is closed, and that happens once — afterwards it does not change.

**This is reporting, not steering.** Entering your investment changes nothing about when
your battery trades. See [Configuration](configuration.md#economics).

**When is it unavailable?**

| Reason | Meaning |
|---|---|
| `no_investment_configured` | You entered no amount. Different from an investment of zero. |
| `no_finalised_days` | No day has closed yet. |
| `no_finalised_days_in_accounting_period` | Closed days exist, but none inside the period since your purchase date. |

The attributes `unsealed_past_days`, `unsealed_by_reason` and `sealed_through` say which
days have not been counted and why — useful when the figure appears to be standing still.

---

## Two boundaries not to confuse

On a sale, more energy leaves the battery than arrives at the meter, because your house
takes its share first.

```
Out of the battery:  1.0 kW
House uses:          0.3 kW
At the meter:        0.7 kW
```

Alpha EMS publishes both, each at its own boundary, and says explicitly which is which:
`objective_boundary` is `battery` for a purchase and `meter` for a sale.

⚠️ **Every published objective is AC.** `objective_boundary` names the meter face it is
measured at, not whether it is AC or DC. So do **not** apply an efficiency factor to it.

---

## Further reading

[Sensors and entities](entities.md) · [How Alpha EMS works](how-it-works.md) ·
[Diagnostics](diagnostics.md) · [Trading Log](../TRADING_LOG.md)
