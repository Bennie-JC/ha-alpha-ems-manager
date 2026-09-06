# Trading Log

A worked Home Assistant example that renders one coherent campaign story in Dutch.

**This file is documentation, not runtime.** The integration ships no YAML and depends
on none: it publishes the lifecycle, and Home Assistant renders it. Copy what you want
into your own `configuration.yaml`.

## Why the event, and not the sensors

`sensor.alpha_ems_manager_current_campaign` and `..._last_campaign_result` are
**projections**. They are recomputed on every refresh, they re-fire a trigger whenever
any attribute moves even if the state does not, and `current_campaign` does not return
early when idle -- it emits its whole attribute set with every value `None` and
`classification: "unknown"`. A template triggered on their state changes therefore
duplicates lines on ordinary refreshes and can read a well-formed campaign made
entirely of nulls.

`last_campaign_result` also **does not survive a restart**: it is held in memory only,
so after a reload it reads unavailable until the next terminal. Its disappearance must
never be read as a campaign ending.

`alpha_ems_campaign` is the transition record instead. Each kind fires **once**, behind
guards the integration already maintains -- `created` only where the instance id is
minted, `started` behind a mark on the persisted lifecycle record, `removed` behind a
**persisted** `closed_lifecycle` latch. So exactly-once needs no bookkeeping in Jinja,
attribute refreshes produce nothing, and a restart replays neither `created` nor
`started`. Only genuinely unfinished terminals are published after a restart, once.

## The kinds, and which to render

| kind | carries | render |
|---|---|---|
| `planned` | announcement; `campaign_instance_id` is **always** `null` | not in this log |
| `created` | the instance id, `planned_kwh` (creation snapshot, immutable), window | both creation lines |
| `started` | `frozen_target_kwh`, `classification_at_start` (**beta.49**), `started_at` | the start line |
| `stopped` | internal bookkeeping | nothing |
| `removed` | `result`, `realised_kwh`, `shortfall_kwh`, `completion_reason` | result + terminal |
| `plan_closed` | an announcement that never ran | optional |

**Both creation lines come from `created`, and that is forced rather than chosen.** At
`planned` time no instance exists, so no announcement event can carry the id -- and the
required order puts the id line first.

## Which figure goes on which line

Three different quantities, and mixing them is the classic error:

- **plan-created** uses `planned_kwh` from `created`. It is the creation snapshot,
  written once and never rewritten, so it is identical on every later kind.
- **started** uses `frozen_target_kwh` -- the execution target frozen at first physical
  activation. It may legitimately exceed `planned_kwh`, because the campaign target
  grows monotonically.
- **result and terminal** use `realised_kwh` over `realised_kwh + shortfall_kwh`. When
  `shortfall_kwh` is `null` the denominator is unknown, and the line drops it rather
  than inventing one.

## Classification

From `classification_at_start`, frozen when execution began so a line already written
cannot change meaning later. Values: `economic_buy`, `coverage_buy`, `safety_buy`,
`mixed_buy`, `economic_export`, `serve_load`, `unknown`. **`unknown` and `null` render
no type segment at all** -- never the word "unknown".

## Outcome mapping

| `result` | action line | terminal line | reason shown |
|---|---|---|---|
| `success` | `... afgerond —> R / T` | `Campagne geëindigd —> R / T` | no |
| `partial` | `... gedeeltelijk afgerond —> R / T` | `Campagne geëindigd —> R / T` | no |
| `superseded` | `... gedeeltelijk afgerond —> R / T` | `Campagne geëindigd —> R / T` | no |
| `canceled` | — | `Campagne geannuleerd —> R / T` | no |
| `failed` | — | `Campagne mislukt —> R / T —> <reason>` | **only here** |
| `not_executed` | — | `Campagne niet uitgevoerd` | no |

`failed` and `not_executed` are terminal on their own: no `Campagne geëindigd` follows
either. And `superseded` is **not** a failure -- the integration only renames a
`partial` to `superseded` when a plan replaced it, so a campaign that met its tolerance
stays `success` and one that did partial work reads as partial work.

## The automation

Two ordered lines from one event need two writes, so this uses `logbook.log` -- a
native service, not a logging backend. It also sidesteps a real trap: two campaigns
producing byte-identical text would be a single state change, and a template sensor
would silently log only one of them.

```yaml
automation:
  - alias: Alpha EMS trading log
    mode: queued
    max: 25
    trigger:
      - platform: event
        event_type: alpha_ems_campaign
    action:
      - variables:
          e: "{{ trigger.event.data }}"
          sell: "{{ e.purpose in ['sell', 'net_export', 'export'] }}"
          verb: "{{ 'Verkopen' if sell else 'Kopen' }}"
          plan_noun: "{{ 'Verkoopplan' if sell else 'Koopplan' }}"
          types:
            economic_buy: economische koop
            coverage_buy: dekkingskoop
            safety_buy: veiligheidskoop
            mixed_buy: gemengde koop
            economic_export: economische verkoop
            serve_load: eigen verbruik
          kind: "{{ e.kind }}"
          r: "{{ e.realised_kwh if e.realised_kwh is number else none }}"
          s: "{{ e.shortfall_kwh if e.shortfall_kwh is number else none }}"
          t: "{{ (r + s) if (r is number and s is number) else none }}"
          pair: >-
            {{ '%.2f kWh / %.2f kWh'|format(r, t) if t is number
               else ('%.2f kWh'|format(r) if r is number else '') }}
          window: >-
            {% if e.window_start and e.window_end %}
            {{ e.window_start | as_datetime | as_local | strftime('%H:%M') }} –
            {{ e.window_end | as_datetime | as_local | strftime('%H:%M') }}
            {% endif %}
      - choose:
          # --- creation: the id line, then the plan line, in that order ---
          - conditions: "{{ kind == 'created' }}"
            sequence:
              - service: logbook.log
                data:
                  name: Alpha EMS
                  message: "Campagne gestart ID: {{ e.campaign_instance_id }}"
              - service: logbook.log
                data:
                  name: Alpha EMS
                  message: >-
                    {{ plan_noun }} aangemaakt —>
                    {{ '%.2f kWh'|format(e.planned_kwh) }}
                    {{ ('—> ' ~ window.strip()) if window.strip() else '' }}

          # --- first physical execution, once per campaign ---
          - conditions: "{{ kind == 'started' }}"
            sequence:
              - service: logbook.log
                data:
                  name: Alpha EMS
                  message: >-
                    {{ verb }} gestart —>
                    {{ '%.2f kWh'|format(e.frozen_target_kwh)
                       if e.frozen_target_kwh is number else '' }}
                    {{ ('—> ' ~ types[e.classification_at_start])
                       if e.classification_at_start in types else '' }}

          # --- terminal ---
          - conditions: "{{ kind == 'removed' }}"
            sequence:
              - choose:
                  - conditions: "{{ e.result in ['success'] }}"
                    sequence:
                      - service: logbook.log
                        data:
                          name: Alpha EMS
                          message: "{{ verb }} afgerond —> {{ pair }}"
                  - conditions: "{{ e.result in ['partial', 'superseded'] }}"
                    sequence:
                      - service: logbook.log
                        data:
                          name: Alpha EMS
                          message: >-
                            {{ verb }} gedeeltelijk afgerond —> {{ pair }}
              - choose:
                  - conditions: "{{ e.result == 'failed' }}"
                    sequence:
                      - service: logbook.log
                        data:
                          name: Alpha EMS
                          message: >-
                            Campagne mislukt —> {{ pair }} —> {{ e.completion_reason }}
                  - conditions: "{{ e.result == 'not_executed' }}"
                    sequence:
                      - service: logbook.log
                        data: {name: Alpha EMS, message: Campagne niet uitgevoerd}
                  - conditions: "{{ e.result == 'canceled' }}"
                    sequence:
                      - service: logbook.log
                        data:
                          name: Alpha EMS
                          message: "Campagne geannuleerd —> {{ pair }}"
                default:
                  - service: logbook.log
                    data:
                      name: Alpha EMS
                      message: "Campagne geëindigd —> {{ pair }}"
```

The arrow is the literal three characters `—>`; the window dash is an en dash. Both
are plain text and need no special rendering.

## What to expect

```
Campagne gestart ID: 1b637f043a8e3a65
Verkoopplan aangemaakt —> 4.97 kWh —> 19:30 – 00:00
Verkopen gestart —> 4.52 kWh —> economische verkoop
Verkopen gedeeltelijk afgerond —> 3.55 kWh / 4.52 kWh
Campagne geëindigd —> 3.55 kWh / 4.52 kWh
```

`4.97` and `4.52` differ legitimately: the first is what the campaign was created to
do, the second what it froze as its target when execution actually started.
