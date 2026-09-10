🇳🇱 [Nederlands](../nl/how-it-works.md) | 🇬🇧 **English**

# How Alpha EMS works

**In one sentence:** every quarter of an hour, Alpha EMS builds the cheapest plan that is
still safe, and executes the part of it that is permitted.

## Why this matters

Once you understand how Alpha EMS thinks, you recognise its decisions — including the
ones that look odd at first. Nearly every "this must be a bug" report turns out to be one
of the situations below.

---

## The quarter-hour loop

Every fifteen minutes Alpha EMS looks at:

- your **expected house load**, from the learning model
- the battery's **state of charge**
- the **electricity prices** per quarter, from Frank
- the **expected solar production**, from Solcast
- the **free room** in the pack
- the **reserve** the battery must hold

From that comes one plan for the whole known horizon — today, and once prices are
published, tomorrow too. The plan says, per quarter, what should happen.

Separately, a short check runs every minute and adjusts power *within* the quarter you
are in. It recalculates nothing: it reads the target already frozen for that quarter and
moves toward it.

## Safety before money

This is the most important rule, and it is absolute. Alpha EMS first compares whether a
plan is feasible for the reserve, and only then what it costs. There is no price that
buys a reserve violation, and no mode in which this works differently.

That is why Alpha EMS can buy at an expensive moment. See [example 3](#3-safety-buy).

## The reserve

The **dynamic reserve** is the energy the battery should hold to keep supplying your home
until the next moment it can be refilled.

It rises and falls through the day. On a sunny midday it may be low: the sun will refill
the pack shortly. In the early evening, with the peak still ahead, it is high.

Your configured *minimum state of charge* is the **hard floor** — Alpha EMS never goes
below it. The dynamic reserve may temporarily require **more** than that floor.

## Three reasons to buy

Every bought kWh gets exactly one reason. This is not a label applied afterwards; it
decides which rules apply to it.

| Reason | What it means | Price thresholds? |
|---|---|---|
| **Safety** | Without this purchase the battery cannot reach its reserve | No — safety outranks price |
| **Coverage** | Your house will consume this energy later anyway; it is cheaper now | No — it is not a trade |
| **Economic** | A trade Alpha EMS chooses because it earns a profit | Yes, your thresholds apply |

## Why plans change

The plan for the future may shift on every refresh, and that is correct: the sun
disappoints, your load runs differently, or tomorrow's prices are published.

**A quarter that has already started is frozen.** It is not rewritten under the
controller. Energy missed in that quarter is not made up later either.

---

# Worked examples

*Every amount, time and percentage below is an example to show the idea. They are not
thresholds Alpha EMS literally applies, and not a promise about what your installation
will earn.*

## 1. Economic Buy

```
Battery:    35 %
13:00:      € 0.20/kWh
20:00:      € 0.55/kWh
Room:       plenty
Reserve:    safe
```

**Decision:** Alpha EMS buys energy into the battery at 13:00.

**What you see:** `economic_buy`.

> This is not an emergency purchase. Alpha EMS chooses to buy cheap energy now because
> it will be worth more later.

Your thresholds do apply here: both *Minimum gain per trade* and *Extra margin per
grid-charged kWh* must be cleared, or the purchase does not happen.

## 2. Coverage Buy

```
Now:                    € 0.24/kWh
Later, unavoidable:     € 0.42/kWh
```

Your house will draw that energy from the grid later regardless — the battery will be
empty and there is no sun left.

**Decision:** Alpha EMS buys it now.

**What you see:** `coverage_buy`.

> Your house needs that electricity later anyway. Alpha EMS buys it earlier because it
> is cheaper now.

**The difference from an economic buy.** An economic buy is a *trade*: it must earn a
profit and clear your thresholds. A coverage buy is **not a trade** — it is the same
unavoidable purchase, at a cheaper moment. Going from 0.42 to 0.24 is not profit; it is
the same shopping at a cheaper shop. Demanding a profit threshold would refuse it for
the wrong reason.

Alpha EMS only buys this way for energy your house is forecast to actually consume, and
that energy cannot be sold.

## 3. Safety Buy

```
Battery:    forecast to run too low tonight
Reserve:    about to become unreachable
This hour:  NOT the cheapest of the day
```

**Decision:** Alpha EMS buys anyway, now.

**What you see:** `safety_buy`.

> Alpha EMS is not buying here because this is the cheapest quarter, but because
> otherwise the battery cannot hold enough reserve later.

**This is the example to remember.** If you see a purchase at an expensive moment, check
whether it is a safety buy before reporting it as a fault. No price threshold applies to
this purchase — a reserve that cannot be met is not an amount you can weigh money
against.

## 4. Mixed Buy

One campaign can have more than one reason:

```
2 kWh   compelled by the reserve
4 kWh   additionally worth buying economically
------
6 kWh   total, in one charge run
```

**What you see:** `mixed_buy`.

**Do not read this as "the safety buy grew".** They are two separate components that
happen to fall in the same window. Only physical reachability can compel a purchase; an
economically attractive purchase never becomes compelled, however attractive. And the
other way round: those 4 kWh still have to clear your thresholds, while the 2 kWh do not.

## 5. Absorbing free solar

```
Solar:      4.0 kW
House:      1.0 kW
Spare:      3.0 kW
Battery:    has room
```

**Decision:** Alpha EMS can put those 3 kW into the battery instead of exporting them —
when keeping is worth more than the export price.

**This is not buying from the grid.** Nothing is purchased and no permission is needed.
It happens even with *Allow buying from the grid* switched off.

Where selling genuinely pays more, Alpha EMS sells. There is no "never export" rule.

**Why this matters:** a quarter that only takes in free solar does not interrupt a charge
run. So a charge campaign can span hours while real buying happens in only a few
quarters. See [example 10](#10-wide-charge-span-narrow-buy-blocks).

## 6. Headroom for future solar

```
Battery:    80 %
Morning:    cheap grid power available
Midday:     strong solar forecast
```

**Decision:** Alpha EMS does *not* fill the pack from the grid.

> Cheap electricity is not always the smartest electricity when free sun is coming.

If it filled up now, there would be no room for the sun later and that production would
go to the grid at a low export rate. This morning's cheap kWh would displace this
afternoon's free one.

This only works with Solcast configured — without a forecast Alpha EMS does not know sun
is coming. See [Solar](solar.md).

## 7. Net Export

```
Battery:    95 %
House:      low demand
Reserve:    safe
Evening:    high export price
```

**Decision:** Alpha EMS sells battery energy to the grid.

**What you see:** `export` on *Economic Action*.

**One thing to note:** what leaves the battery and what crosses the meter are not the
same number. Your house takes its share first. If 1.0 kW leaves the battery and your
house uses 0.3 kW, then 0.7 kW reaches the meter. Both figures are published, each at its
own boundary. See [Economics](economics.md).

## 8. High price, and still no sale

```
Evening:    attractive export price
But:        your house needs that energy later
And:        buying it back costs more
```

**Decision:** Alpha EMS does **not** sell.

Selling at 0.29 to buy the same energy back at 0.30 is a loss, whatever the headline
price says. The same applies if selling would make the reserve unsafe: then it does not
happen at all, at any price.

**This is the clearest example of why this is an EMS and not a "sell when the price is
high" rule.** If you see a high export price pass without a sale, that is often exactly
right.

## 9. Replanning

```
12:00   Plan says: buy at 13:00
12:30   The sun disappoints, your load runs differently,
        or tomorrow's prices are published
12:30   Plan now says something else
```

Future quarters may change. That is not instability — it is new information.

**A quarter that has already started does not change.** It is fixed until it ends. What
was not bought in that quarter is not made up later either: each quarter has its own
frozen target.

After a Home Assistant restart a running dispatch is **stopped**, not continued. Alpha
EMS no longer knows how much was delivered inside the open quarter, and carrying on from
an unknown position would be guessing. At the next quarter the plan simply starts again.

## 10. Wide charge span, narrow buy blocks

This is the most important of the ten, because the sensors look confusing without it.

```
The battery charges:        08:30 ────────────────────► 16:30

Real grid buying:                 11:45─12:15
                                        12:45─13:00
                                              14:00─14:30
```

The battery may be charging between 08:30 and 16:30. That does **not** mean Alpha EMS
buys from the grid for all those hours. The rest of that time, free solar is going in.

**Two questions, two answers:**

| Attribute | What it says |
|---|---|
| `next_charge_projection_at` / `next_charge_projection_end_at` | When the battery is expected to be charging |
| `next_grid_purchase_at` / `next_grid_purchase_end_at` | The next *contiguous* block where energy is really bought from the grid |

The buy block ends at the first quarter that buys nothing. A later block simply becomes
the next block once the first has passed.

**Why is the charge span so wide?** Because absorbing free solar (example 5) does not
interrupt a charge run — otherwise every cloud would cut the run in two and you would pay
the switching cost twice. On a sunny day the charge span therefore runs almost level with
the solar hours.

**On the campaign itself** you also get how much was bought and how much arrived free:
`grid_purchase_kwh` and `production_charge_kwh`, plus `grid_purchase_blocks` with the
number of separate buying stretches. See [Sensors and entities](entities.md).

---

## Further reading

[Economics](economics.md) · [Solar](solar.md) ·
[Control and safety](control-and-safety.md) · [Diagnostics](diagnostics.md)
