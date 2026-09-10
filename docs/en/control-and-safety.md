🇳🇱 [Nederlands](../nl/control-and-safety.md) | 🇬🇧 **English**

# Control and safety

**In one sentence:** two switches must be on before anything reaches your battery, and
after that there are ten more checks.

## Why this matters

Alpha EMS can genuinely control your battery. This page describes exactly when it does
and when it does not — so you know what you are enabling, and why it sometimes does
nothing.

---

## The three modes

You choose the mode with the `select.alpha_ems_control_mode` entity.

| Shown | Stored | What it does |
|---|---|---|
| **Off** | `off` | Alpha EMS calculates and publishes, but sends nothing. If one of its own dispatches is still running it is stopped cleanly — after that, silence. |
| **Shadow** | `shadow` | The **whole** pipeline runs: the same translation, the same safety checks, the same command list as Live. Only the sending does not happen. |
| **Live** | `active` | Permitted actions may really be sent. |

**Shadow is where you start.** It answers exactly the question you want to ask: *would
this command have been safe, and what exactly would it have sent?* That is why
`sensor.alpha_ems_control_state` distinguishes `inhibited` (a safety check refused) from
`eligible` (nothing refused; only the mode or the switch held it back).

Changing mode refreshes the plan immediately, without the usual debounce.

If Alpha EMS does not recognise the stored mode — after a manual edit, say — it falls back
to `off`.

---

## Two consents, and neither implies the other

```
Control Mode = Live                       ✓
        and
Allow Alpha EMS to send commands = on     ✓
        ↓
    something may be sent
```

On a fresh installation **both are off**. And *Allow buying from the grid* and *Allow
selling from the battery* are separately off, so even with both consents on, a fresh
installation still does nothing on its own.

See [Configuration](configuration.md#control).

---

## What can and cannot be executed

| Action | Executable? |
|---|---|
| Charging from the grid (`grid_charge`) | **Yes** |
| Exporting to the grid (`net_export`) | **Yes** |
| Discharging to serve the house (`serve_load`) | **No** |
| Curtailing solar production (`curtail_pv`) | **No** |

**Why not `serve_load`?** Discharging into your house is ordinary inverter behaviour, and
your inverter does it better than Alpha EMS could: it tracks your load continuously, while
a fixed-power command cannot. No command from this integration is needed.

**Why not `curtail_pv`?** No actuator for it exists in this setup.

Both are refused, and not in one place: `serve_load` fails the direction check, again at
the sign check, and once more at the send site. `curtail_pv` is not even an executable
intent.

---

## The gate ladder

Before a command leaves, all of these must pass — in this order, and the first to refuse
is the reason you are shown.

**First the safety verdict.** Missing or unavailable control entities, a missing reset
automation, *Excess Export* or *Peak Shaving* switched on, a dispatch that is not ours, an
unconfigured battery, a stale plan, unusable or stale readings, a power below the device
minimum or above its maximum, and for a discharge also an unusable grid reading or
imminent export.

**Then the consents.**

| # | Check | Refuses with |
|---|---|---|
| 1 | Was the safety verdict good? | `unsafe` |
| 2 | Is Control Mode set to Live? | `mode_not_active` |
| 3 | Is command sending enabled? | `execution_not_enabled` |
| 4 | Is execution possible in this release? | `execution_unavailable` |
| 5 | Is there anything to send? | `no_commands` |
| 6 | Is this direction permitted? | `live_action_not_permitted` |
| 7 | Has the cooldown passed? | `cooldown` |

**And three more at the send site itself:** whether execution is available again, whether
every step falls inside the permitted entities, and whether the sign and mode match the
intent. Those last three are deliberate — a mistake in the first layer must not be enough
to send something to your battery.

---

## Whose dispatch is this?

This is the heart of the safety design, and it is a real problem: the AlphaESS package
records nowhere **who** started a dispatch. Whether you switch it on from your dashboard
or Alpha EMS does it, the values left behind are identical.

Hence two things together: the `input_boolean.alpha_ems_dispatch_owner` helper, turned on
first and off last, **and** a recorded account requiring the dispatch to have begun at the
moment Alpha EMS wrote it.

That gives six states:

| State | Meaning |
|---|---|
| `none` | No dispatch is running |
| `owned` | Marker on and the record matches — ours |
| `degraded` | Marker gone, but record and read-back still hold |
| `releasing` | Our own dispatch is draining |
| `unproven` | Marker on, but the record does not match |
| `foreign` | Marker off and not provable — **Alpha EMS keeps away** |

⚠️ **`degraded` is never a synonym for `owned`.** It authorises exactly one write, not
carrying on as if nothing happened.

**A dispatch Alpha EMS cannot prove is its own is never touched, stopped or
overwritten.** That is safe, but it also means a manually started charge simply keeps
running.

---

## The dead-man timer

An AlphaESS dispatch has a duration; when it expires your installation returns to normal
behaviour by itself. That is your fail-safe if Home Assistant or Alpha EMS stops running.

Alpha EMS sets that duration alternately to 20 and 25 minutes. That looks arbitrary and is
not: the AlphaESS package refreshes the duration on a *change* of the value. Writing 20
twice in a row changes nothing, and the dispatch would then expire silently mid-charge.
The intended dead-man stays about 20 minutes; the 25 is not a longer run.

The check that adjusts power every minute **deliberately never extends it**. Otherwise a
technical correction would extend a run the economics never extended.

---

## Why Alpha EMS lowers the power

A command passes through eight bounds in turn, and the bound that actually binds is the
reason reported.

For charging: the inverter's power → your configured floor → the dynamic reserve → the
grid import still authorised → the room in the pack → export safety → the grid limit →
rounding to a step your inverter accepts.

Exporting has a comparable sequence, which also includes the remaining discharge, the
remaining export target and the time left in the quarter.

Two things may only **raise** a command, never lower it: absorbing free solar, and
delivering a purchase that safety compelled. The first buys nothing; the second stays
bounded by what was already frozen for that quarter.

Rounding is always **downwards**. The final command is never larger than what was
requested.

---

## When a plan is withdrawn

A running action stops when the target is reached, when the pack is full or empty enough,
when the plan is withdrawn, when the window closes, when a safety condition appears, or
when you change the mode back.

That last one is a real abort: selecting **Off** or **Shadow** stops a charge Alpha EMS
started, not merely the starting of the next one.

---

## After a restart

If Home Assistant restarts while a dispatch was running, Alpha EMS adopts it and **stops
it** — it does not continue.

That sounds harsh, but the alternative is worse. Progress inside the open quarter is not
persisted, so after a restart Alpha EMS does not know how much has been delivered.
Carrying on from an unknown position is guessing. The cost of stopping is bounded: at most
the remainder of one quarter, and at the next quarter the plan simply makes a new one.

**Not every adoption is a restart.** If a live plan names exactly this dispatch, that is
an ordinary quarter-boundary hand-over and progress stays known.

**If the record cannot prove which dispatch is running, nothing happens.** No stop is
issued against a dispatch of unprovable provenance; Alpha EMS leaves it to the dead-man.

Reloading the integration behaves like a cold boot.

---

## Note: out-of-date text in the settings

The descriptions on the **Control** and **Economics** options pages still say this release
cannot send a command to your inverter. That text predates Live execution and is no longer
correct.

In *Live*, with command sending enabled, charging and exporting do reach your battery.
What this page describes comes from the current source and is authoritative. The interface
strings will be corrected in a later release.

---

## Further reading

[Configuration](configuration.md) · [Sensors and entities](entities.md) ·
[Diagnostics](diagnostics.md) · [Troubleshooting](troubleshooting.md)
