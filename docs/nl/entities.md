🇳🇱 **Nederlands** | 🇬🇧 [English](../en/entities.md)

# Sensoren en entiteiten

**In één zin:** Alpha EMS maakt **17 sensoren en één keuze-entiteit** aan — samen 18.

## Waarom dit belangrijk is

Alle detailinformatie zit in *attributen*, niet in losse entiteiten. Zesennegentig
kwartiersensoren zouden technisch makkelijk zijn en in de praktijk onbruikbaar. Wat je
op een dashboard wilt tonen, staat dus bijna altijd in een attribuut.

---

## Drie dingen om vooraf te weten

**1. De entiteit-ID's volgen je instantienaam.** De naam die je bij het installeren
opgaf, is de apparaatnaam, en Home Assistant leidt daar de ID's uit af. Met de standaard
`Alpha EMS` krijg je `sensor.alpha_ems_economic_action`. Noemde je het `Thuis`, dan is het
`sensor.thuis_economic_action`. Alle ID's op deze pagina gaan uit van de standaardnaam.

**2. Keuzesensoren tonen ruwe waarden.** Je ziet `safety_buy`, `mixed_buy`, `eligible` en
`not_executed` — niet een nette Nederlandse of Engelse omschrijving. Dat is expres: de
namen worden niet vertaald, omdat Home Assistant de entiteit-ID uit de vertaalde naam
afleidt en je anders bij elke taalwissel andere ID's zou krijgen. Wil je nette labels op
je dashboard, dan maak je die zelf in de kaart.

**3. Alles is een gewone entiteit.** Er zijn geen diagnostische entiteiten. En bij gebrek
aan gegevens worden ze `unknown`, niet `unavailable` — `unavailable` zie je alleen als de
integratie zelf niet geladen is.

---

## Overzicht

| Entiteit | Eenheid | Soort |
|---|---|---|
| [Expected House Load Today](#expected-house-load-today) | kWh | voorspelling |
| [Expected House Load Tomorrow](#expected-house-load-tomorrow) | kWh | voorspelling |
| [Learning Confidence](#learning-confidence) | % | meting |
| [Learning Days](#learning-days) | — | meting |
| [Forecast Error Yesterday](#forecast-error-yesterday) | kWh | meting |
| [Forecast Error 7 Days](#forecast-error-7-days) | % | meting |
| [Battery Recommendation](#battery-recommendation) | — | advies |
| [Planned Battery Power](#planned-battery-power) | kW | advies |
| [Usable Battery Energy](#usable-battery-energy) | kWh | meting |
| [Dynamic Battery Reserve](#dynamic-battery-reserve) | kWh | advies |
| [Economic Action](#economic-action) | — | uitvoering |
| [Next Planned Action](#next-planned-action) | — | planning |
| [Control State](#control-state) | — | bediening |
| [Economic Value](#economic-value) | EUR | gemengd |
| [Battery Return](#battery-return) | % | meting |
| [Current Campaign](#current-campaign) | — | uitvoering |
| [Last Campaign Result](#last-campaign-result) | — | meting |
| [Control Mode](#control-mode) | — | **bediening (schrijfbaar)** |

---

## Leren en voorspellen

### Expected House Load Today

`sensor.alpha_ems_expected_house_load_today` — **kWh**

Je voorspelde huisverbruik voor de hele dag. Dit is het *baseline*-verbruik: heb je een
EV-sensor ingesteld, dan is het laden van je auto er al afgetrokken.

**Belangrijkste attributen:** `actual_so_far_kwh` (al gemeten vandaag),
`forecast_remaining_kwh` (nog verwacht), `measured_so_far_kwh` (ruw, vóór aftrek),
`flexible_load_so_far_kwh` (afgetrokken EV-energie), `model_days`,
`confidence_percent`, `adaptation_applied` en `adaptation_ratio` (of en hoeveel de
voorspelling is bijgesteld op wat er vandaag echt gebeurde), `day_type`,
`intervals_today`.

**Wanneer `unknown`?** Als er nog te weinig geleerde dagen zijn. Geen voorspelling is
eerlijker dan een verzonnen getal.

**Geen langetermijnstatistiek.** Een voorspelling hoort niet naast je gemeten verbruik op
het energiedashboard te staan.

### Expected House Load Tomorrow

`sensor.alpha_ems_expected_house_load_tomorrow` — **kWh**

Hetzelfde, voor morgen.

**Attributen:** `forecast_total_kwh`, `model_days`, `day_type`, `day_type_pooled` (of
werkdagen en weekenddagen nog samengenomen worden bij gebrek aan gegevens),
`windows_used_days`, `intervals_tomorrow`, `confidence_percent`.

### Learning Confidence

`sensor.alpha_ems_learning_confidence` — **%**

Hoe volwassen en betrouwbaar het leermodel is, van 0 tot 100.

Het is het product van *rijpheid* (hoeveel dagen) en *kwaliteit* (hoe compleet en
stabiel). Omdat het een vermenigvuldiging is, kunnen negentig dagen met veel gaten nooit
hoog scoren.

**Attributen:** `learned_days`, `maturity`, `coverage`, `recency`, `stability`,
`balance`, `quality`, `measured_coverage`.

**Wat is normaal?** Na 30 dagen zit de rijpheid rond 63 % — dat loopt met opzet langzaam
op.

### Learning Days

`sensor.alpha_ems_learning_days` — aantal

Het aantal kalenderdagen met genoeg bruikbare gegevens. Een dag telt mee bij minstens
80 % dekking, en alleen als hij compleet is; vandaag telt dus nooit mee.

**Attributen:** `retained_days`, `retained_intervals`, `history_start`, `history_end`,
`rejected_quarters`, `flexible_load_configured`, `intervals_without_flexible_data`,
`last_rejected_reason`.

### Forecast Error Yesterday

`sensor.alpha_ems_forecast_error_yesterday` — **kWh**

Het verschil tussen wat er voorspeld was en wat er gemeten is. **Positief betekent: er is
meer voorspeld dan verbruikt.**

**Attributen:** `absolute_error_kwh`, `error_percent`, `predicted_kwh`, `actual_kwh`,
`mae_kwh_per_interval`, `intervals_compared`, `intervals_in_day`, `horizon_days`,
`comparison_basis`.

**Wanneer `unknown`?** Als de dag niet vergeleken kon worden. Nul is de waarde van een
perfecte voorspelling, niet van een ontbrekende.

### Forecast Error 7 Days

`sensor.alpha_ems_forecast_error_7_days` — **%**

De rollende fout over zeven dagen: de opgetelde absolute fout gedeeld door het opgetelde
werkelijke verbruik. "8 %" betekent dat het model er acht procent naast zat op de energie
die het voorspelde.

**Dit is bewust géén nauwkeurigheidscijfer.** Er is nergens een `100 − fout`, omdat dat
getal bij een slechte week negatief wordt en uitnodigt tot vergelijken met systemen
waarmee het niets te maken heeft.

**Attributen:** `window_days` (7), `days_compared`, `intervals_compared`,
`mae_kwh_per_interval`, `bias_kwh_per_interval`, `predicted_kwh`, `actual_kwh`.

---

## De batterij

### Battery Recommendation

`sensor.alpha_ems_battery_recommendation` — `hold` · `charge` · `discharge`

Wat de batterij volgens de planning zou moeten doen. **Advies.**

**Attributen:** `reason`, `planned_power_kw`, `usable_energy_kwh`, `battery_soc_percent`,
`configured_min_soc_percent`, `effective_min_soc_percent`, `constraints`, `pv_aware`
(staat er zonvoorspelling achter?), `basis`.

**Wanneer `unknown`?** Als er een hardwaregegeven ontbreekt — meestal de capaciteit.

### Planned Battery Power

`sensor.alpha_ems_planned_battery_power` — **kW**

Het gemiddelde vermogen over het kwartier dat bij dat advies hoort. **Positief is de
batterij in.**

⚠️ Dit is de eigen conventie van het plan en staat los van je ingestelde
*battery power sign convention*. Het is bovendien een *gemiddelde*, geen setpoint.

**Attributen:** `requested_mode`, `requested_power_kw`, `allowed_energy_kwh`,
`limiting_constraints`, `policy`, `policy_version`, `sign_convention`, `basis`.

### Usable Battery Energy

`sensor.alpha_ems_usable_battery_energy` — **kWh**

Hoeveel energie er boven je ondergrens beschikbaar is.

**Attributen:** `battery_soc_percent`, `capacity_kwh`, `stored_energy_kwh`,
`configured_min_soc_percent`, `effective_min_soc_percent`, `reserve_source`,
`coverage_hours`, `basis`.

⚠️ **Dit is een bovengrens.** Er wordt één rendementsgetal gebruikt en het eigen
verbruik van de omvormer zit er niet in, dus het cijfer is iets optimistisch.

### Dynamic Battery Reserve

`sensor.alpha_ems_dynamic_battery_reserve` — **kWh**

Hoeveel energie de batterij op dit moment zou moeten aanhouden.

**Let op: dit is een energie in kWh, niet een percentage.** Het bijbehorende percentage
staat in `required_reserve_soc_percent`.

**Attributen:** `required_reserve_soc_percent`, `configured_min_soc_percent`,
`reserve_shortfall_kwh`, `reserve_reachable`, `replenishment_dependency_kwh`,
`lower_bound_reason`, `intervals_evaluated`, `basis`.

**`replenishment_dependency_kwh` is het attribuut om te kennen:** het zegt hoeveel van de
verlaging berust op zon die nog moet komen. Is dat getal groot, dan is de reserve ernaast
optimistisch.

**Dit is geen maximum.** De eis daalt als bijvullen dichtbij is en stijgt zodra dat
voorbij is. Rond het middaguur kan hij zo laag zijn als je ingestelde ondergrens — terecht,
want de middagzon vult de accu ruim voor de avondpiek.

**Wanneer `unknown`?** Zonder voorspelling of zonder bruikbare batterijconfiguratie. Nooit
een verzonnen nul.

---

## Plan en uitvoering

### Economic Action

`sensor.alpha_ems_economic_action` — `charge` · `export` · `safety_buy` · `mixed_buy` ·
`idle` (en `hold`, `discharge`, `curtail_pv` als mogelijke waarden)

Wat er **op dit moment** economisch gebeurt.

**Attributen:** `owned` (is deze opdracht van ons?), `mode` (`off`/`shadow`/`active`),
`purpose`, `campaign_id`, `run_id`, `planned_kwh`, `realised_kwh`, `power_kw`,
`started_at`, `capability_action`, `execution_blocked_reason`.

**`execution_blocked_reason` is het attribuut dat je het vaakst nodig hebt.** Het zegt
waarom er niets verstuurd wordt: `execution_not_enabled`, `mode_not_active`,
`battery_export_not_permitted`, `no_primitive_curtail`, `action_not_executable`, of
`none` als er niets in de weg staat.

`idle` en `unknown` zijn verschillend: `idle` betekent "er gebeurt niets", `unknown`
betekent "er kon niets worden uitgerekend".

**Deze sensor voedt ook het logboek.** Zie [Trading Log](../TRADING_LOG.md).

### Next Planned Action

`sensor.alpha_ems_next_planned_action` — zelfde waarden

Wat er hierna gepland staat, mét volledige tijdstippen. Deze sensor bestaat juist omdat
*Economic Action* alleen over het heden gaat.

**Over de eerstvolgende campagne:** `starts_at`, `ends_at`, `planned_kwh`, `power_kw`,
`purpose`, `campaign_id`, `run_id`, `price_eur_kwh`, `expected_value_eur`, `reason`.

**Over de grens waarop gemeten wordt:** `objective_boundary` — `battery` bij kopen,
`meter` bij verkopen — en `objective_boundary_rule`, dat uitlegt dat élk gepubliceerd
doel AC is en je er dus geen rendementsfactor overheen moet halen.

**De twee laadvensters** — zie [voorbeeld 10](how-it-works.md#10-brede-laadperiode-smalle-inkoopblokken):

| Attribuut | Betekenis |
|---|---|
| `next_charge_projection_at` / `next_charge_projection_end_at` | Wanneer de batterij laadt |
| `next_grid_purchase_at` / `next_grid_purchase_end_at` | Het eerstvolgende blok waarin écht van het net gekocht wordt |
| `charge_window_rule` | Eén zin die het verschil uitlegt |

**Projectie van de laadtoestand:** `battery_before_next_charge_soc_percent` en
`battery_before_next_charge_dc_kwh` (wat er in de accu zit vlak vóór de volgende
laadsessie), plus dezelfde twee voor `..._export_...`. Verder
`minutes_until_reserve_floor`, `reserve_floor_reached_at`, `projection_basis` en
`projection_unavailable_reason`.

**`upcoming` — de komende campagnes,** maximaal acht, elk met:

| Sleutel | Betekenis |
|---|---|
| `objective_kwh` | Het doel van de campagne |
| `objective_boundary` | `battery` of `meter` |
| `grid_purchase_kwh` | Hoeveel daarvan echt gekocht wordt |
| `production_charge_kwh` | Hoeveel er gratis binnenkomt |
| `grid_purchase_blocks` | Het aantal losse inkoopblokken |
| `charge_source` | `production`, `grid` of `mixed` |
| `will_execute` / `skip_reason` | Of het uitvoerbaar is, en zo niet waarom |
| `expected_value_eur` | Wat het naar verwachting oplevert |

⚠️ **`grid_purchase_kwh` en de drie buren staan hier, op `upcoming`, en niet op de
campagnesensoren hieronder.** Die publiceren een andere woordenschat. Bij een
verkoopcampagne zijn deze vier `null` — niet nul, want een verkoop koopt niets.

Staat er niets gepland, dan is de attributenset kleiner: `starts_at`, `ends_at`,
`planned_kwh` en `objective_boundary` zijn dan `null`, en `charge_window_rule`,
`upcoming` en de campagnevelden ontbreken.

### Control State

`sensor.alpha_ems_control_state` — `off` · `inhibited` · `eligible` · `idle` ·
`executing` · `error`

Wat de bedieningsketen van het advies vond.

**Attributen:** `inhibit_reason`, `authorization_refusal`, `action`, `device_power_kw`,
`commands_planned`, `capability_ready`, `dispatch_active`, `basis`.

**Dit is een andere vraag dan *Economic Action*, en ze mogen verschillen.** *Economic
Action* vraagt "wat is economisch het beste?", *Control State* vraagt "zou er nu
überhaupt een commando mogen?". `export` naast `inhibited` is dus geen tegenspraak.

### Current Campaign

`sensor.alpha_ems_current_campaign` — `planned` · `created` · `started` · `idle`

De actie die nu loopt.

**Attributen:** `campaign_id`, `campaign_instance_id`, `purpose`, `classification`,
`classification_at_creation` (vastgezet bij aanmaak), `planned_kwh`, `realised_kwh`,
`window_start`, `window_end`, `first_executable_at`, `started_at`, `revision`,
`objective_boundary`, plus de opsplitsing: `safety_buy_kwh`, `coverage_buy_kwh`,
`economic_buy_kwh` bij een laadcampagne, of `grid_export_kwh` bij een verkoop.

Er is bewust geen `stopped`-toestand; die zou niet waarneembaar zijn.

### Last Campaign Result

`sensor.alpha_ems_last_campaign_result` — `success` · `partial` · `canceled` · `failed` ·
`not_executed` · `superseded`

Hoe de vorige campagne is afgelopen.

**Attributen:** `available`, `planned_kwh`, `realised_kwh`, `shortfall_kwh`, `result`,
`completion_reason`, `objective_measurable`, `success_tolerance_kwh`,
`final_classification`, de tijdstippen, en dezelfde opsplitsing als hierboven.

**Een geleverde hoeveelheid betekent op zichzelf geen succes.** Alleen het bevroren doel,
binnen `success_tolerance_kwh`, telt als geslaagd. `not_executed` betekent: aangemaakt en
nooit gestart. `superseded`: gestart en vervangen.

⚠️ **Deze sensor overleeft een herstart niet.** Na een herstart staat hij op `unknown`
met `no_campaign_closed_yet`, tot de volgende campagne afloopt. Lees dat niet als een
campagne die is verdwenen.

---

## Geld

### Economic Value

`sensor.alpha_ems_economic_value` — **EUR**

De verwachte cash-winst van het gekozen plan ten opzichte van niets doen, over de nu
bekende horizon.

`0.00` en `unknown` zijn verschillend: `0.00` is een geldige uitkomst.

**De vier termen die optellen tot vandaag** — zie [Economie](economics.md):
`realised_today_eur`, `in_progress_interval_eur`, `remaining_expected_today_eur`,
`forecast_revaluation_eur`, samen `total_economic_value_today_eur`.

**De energiewaarde-opsplitsing:** `realised_self_consumption_value_eur`,
`realised_export_value_eur`, `realised_load_shifting_value_eur`,
`realised_energy_value_eur`.

**Verder:** `decision_advantage_eur` (hetzelfde getal als de toestand — een vergelijking
vanaf nu, die je **niet** bij de vier termen mag optellen), `accounting_basis`,
`accounting_unavailable_reason`, `stored_energy_marginal_value_eur_kwh`,
`current_import_price_eur_kwh`, `current_export_price_eur_kwh`, `horizon_from`,
`horizon_to`, `tomorrow_prices_known`.

**`figure_basis` is het nuttigste attribuut op deze sensor.** Het is een lijst die van elk
bedrag zegt of het `measured`, `attributed`, `estimated` of `unclassified` is — en dus
welke getallen je bij elkaar mág optellen.

**Wanneer `unknown`?** `plan_unavailable`, `horizon_empty`, `no_actionable_intervals` of
`reserve_violation_outranks_money`.

### Battery Return

`sensor.alpha_ems_battery_return` — **%**

Hoeveel procent van je netto-investering is terugverdiend met werkelijk gerealiseerd
voordeel. Alleen gemeten geld; geen voorspelling.

**Investering:** `gross_investment_eur`, `subsidy_eur`, `other_one_time_credit_eur`,
`net_investment_eur`.

**Resultaat:** `cumulative_realised_benefit_eur`, `remaining_to_recover_eur`,
`recovered_percent`, `average_realised_benefit_per_day_eur`, `sample_days`,
`trailing_30d_eur`, `trailing_90d_eur`.

**Terugverdientijd:** `estimated_payback_date`, `estimated_payback_years`,
`payback_unavailable_reason`.

**Herkomst:** `investment_date`, `accounting_start_date`, `sealed_through`,
`unsealed_past_days`, `unsealed_by_reason`, `lifetime_history_complete` en meer — die
vertellen welke dagen zijn meegeteld en welke nog niet, en waarom.

**Wanneer `unknown`?** `no_investment_configured` (je hebt geen bedrag ingevuld),
`no_finalised_days` (er is nog geen dag afgesloten) of
`no_finalised_days_in_accounting_period`.

---

## Bediening

### Control Mode

`select.alpha_ems_control_mode` — **de enige schrijfbare entiteit**

| Getoond | Opgeslagen | Betekenis |
|---|---|---|
| Off | `off` | Kijkt mee, stuurt niets |
| Shadow | `shadow` | Beslist volledig, verstuurt niets |
| Live | `active` | Toegestane acties mogen worden verstuurd |

De opgeslagen waarde `active` en het label "Live" verschillen met opzet: `active` is wat
elk opgeslagen document al zegt, "Live" is het duidelijkere woord.

Wisselen van stand vernieuwt het plan direct, zonder vertraging.

Zie [Veiligheid en bediening](control-and-safety.md).

---

## En het logboek?

**"Trading Log" is geen entiteit.** Er is geen `sensor.alpha_ems_trading_log`. Het is een
weergave in het Home Assistant-logboek, gevoed door gebeurtenissen die *Economic Action*
afvuurt bij elke overgang in de levensloop van een campagne.

[docs/TRADING_LOG.md](../TRADING_LOG.md) bevat een uitgewerkt voorbeeld met YAML dat je
kunt overnemen.

---

## Verder lezen

[Economie](economics.md) · [Hoe werkt Alpha EMS?](how-it-works.md) ·
[Diagnostiek](diagnostics.md)
