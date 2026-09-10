🇳🇱 **Nederlands** | 🇬🇧 [English](../en/diagnostics.md)

# Diagnostiek

**In één zin:** de diagnostiek beantwoordt "waarom deed Alpha EMS dit?" — deze pagina
vertelt waar je per vraag moet kijken.

## Waarom dit belangrijk is

Bijna elke beslissing van Alpha EMS is te herleiden. De download bevat het hele plan, de
redenen, de tussenstappen en de gemeten uitkomst. Dit is geen lijst van alle velden — het
is een lijst van vragen.

## De download maken

**Instellingen → Apparaten en diensten → Alpha EMS Manager → Diagnostiek downloaden**

Je krijgt een JSON-bestand. Er staan geen inloggegevens in — de integratie heeft die niet
— en geen volledige geschiedenis.

---

## Krijgt Alpha EMS alle bronnen binnen?

**Kijk in:** `sources`

Per bron staat er of hij gevonden is, wat zijn huidige waarde is en welke eenheid hij
publiceert. Ontbreekt er iets, dan zie je hier welke.

**Kijk ook in:** het blok met de bedieningsentiteiten. Daar staat welke van de
AlphaESS-entiteiten ontbreken of onbereikbaar zijn — inclusief
`input_boolean.alpha_ems_dispatch_owner` en `automation.alphaess_dispatch_reset_full`.

---

## Is de voorspelling gezond?

**Kijk in:** `learning` en `confidence`

- `measured_valid_intervals` loopt op → je huisverbruikssensor levert bruikbare gegevens
- `measured_coverage` hoog maar `baseline_coverage` laag → je EV-sensor is het probleem,
  niet je huisverbruikssensor
- `confidence` toont de losse onderdelen: dekking, actualiteit, stabiliteit, balans

**Kijk in:** `forecast_history` voor hoe goed de voorspellingen tot nu toe waren,
uitgesplitst naar hoe ver vooruit ze werden gemaakt en naar tijdstip van de dag.

---

## Waarom kocht Alpha EMS?

**Kijk in:** `economic_plan` → `campaigns[]`

Per campagne staat de opsplitsing naar reden:

| Veld | Betekenis |
|---|---|
| `safety_buy_ac_kwh` | Verplicht om de reserve te halen — geen prijsdrempel |
| `coverage_buy_ac_kwh` | Je huis verbruikt het later toch; nu goedkoper |
| `economic_buy_ac_kwh` | Een handel die Alpha EMS zelf koos |

`coverage_saving_eur` en `coverage_baseline_charge_ac_kwh` laten zien wat de gewone
economie op eigen kracht al gekocht zou hebben, zodat je de opsplitsing kunt narekenen in
plaats van hem te moeten geloven.

Zie [de drie redenen](how-it-works.md#drie-redenen-om-te-kopen).

## Waarom kocht Alpha EMS *niet*?

**Kijk in:** `sensor.alpha_ems_economic_action` → `execution_blocked_reason`

Meestal is het één van deze:

| Reden | Wat je doet |
|---|---|
| `execution_not_enabled` | Zet *Allow Alpha EMS to send commands* aan |
| `mode_not_active` | Zet Control Mode op Live |
| `action_not_executable` | Deze actie heeft geen aansturing (`serve_load`) |
| `no_primitive_curtail` | Terugregelen van zon bestaat hier niet |
| `none` | Er staat niets in de weg |

Staat er niets in de weg en gebeurt er tóch niets, kijk dan naar het plan zelf: mogelijk
haalde de aankoop je drempels niet. Dan is `minimum_trade_gain_eur` of
`grid_charge_margin_eur_per_kwh` de verklaring.

---

## Waarom verkocht Alpha EMS *niet*?

Dit is de meest gestelde vraag bij een hoge avondprijs, en meestal is het antwoord dat het
klopt. Zie [voorbeeld 8](how-it-works.md#8-hoge-prijs-en-toch-niet-verkopen).

**Kijk in:** `execution_blocked_reason` voor `battery_export_not_permitted` — dan staat
*Allow selling from the battery* uit.

**Kijk anders in:** het plan. Verkopen wordt geweigerd als de opbrengst niet opweegt tegen
wat die energie je huis later bespaart, of als de reserve erdoor onveilig zou worden. Dat
laatste is absoluut: geen prijs koopt een reserveovertreding af.

**Kijk ook naar:** `retention_gate`. Staat daar `refused_export_superior`, dan won
verkopen juist wél van bewaren.

---

## Heeft de campagne zijn doel gehaald?

**Kijk in:** `sensor.alpha_ems_last_campaign_result`

| Veld | Betekenis |
|---|---|
| `planned_kwh` | Het bevroren doel |
| `realised_kwh` | Wat er werkelijk is geleverd |
| `shortfall_kwh` | Het verschil |
| `result` | `success`, `partial`, `canceled`, `failed`, `not_executed`, `superseded` |
| `completion_reason` | Waarom hij eindigde |

`campaign_objective_reached` betekent dat het doel echt gehaald is; `window_ended`
betekent dat de tijd op was. Sinds beta.40 spreken die twee elkaar niet meer tegen.

⚠️ Een geleverde hoeveelheid is op zichzelf geen succes. Alleen het bevroren doel, binnen
`success_tolerance_kwh`, telt.

---

## Kwam de uitvoering overeen met het plan?

**Kijk in:** het `execution`-blok

Daar staat naast elkaar wat Stage A verwachtte van productie, huisverbruik en het net, en
wat er werkelijk gebeurde. Verder hoeveel energie de batterij heeft bereikt, welk vermogen
de regelaar vroeg, en waarom hij niet méér vroeg.

**Op het uitvoerende kwartier:**

| Veld | Betekenis |
|---|---|
| `battery_objective_realized_this_quarter_kwh` | Wat het plan vroeg en kreeg |
| `battery_absorbed_extra_this_quarter_kwh` | Gratis zon die er bovenop is bewaard |
| `battery_realized_this_quarter_kwh` | De twee bij elkaar |
| `retention_authorised_this_quarter` | Of gratis zon bewaren de moeite waard was |
| `absorption_gate` | Waarom, in één woord |

**Loopt het `run_id` elk kwartier op** of telt de revisie bij elke verversing? Dat is een
fout die het melden waard is. Het `plan_id` van Stage A hoort wél elke vijftien minuten te
veranderen — dat is de schuivende horizon.

---

## Van wie was de opdracht?

**Kijk in:** `execution` → `ownership` → `state`

`owned` is van ons, `foreign` is niet van ons en wordt met rust gelaten, `unproven`
betekent dat de markering aan staat maar het verslag niet klopt. Zie
[Veiligheid en bediening](control-and-safety.md#van-wie-is-deze-opdracht).

**Waarom stopte de vorige opdracht?** `execution.carried.last_ended` bewaart de laatste
afgelopen opdracht met reden, doel, geleverde energie en wat er overbleef — zodat je het
uren later nog kunt nakijken.

⚠️ Dit is **per sessie**. Een herstart wist het, met opzet: een bewaarde bewering over een
sessie die niet meer draait, zou een verouderd feit zijn dat zich voordoet als een actueel
feit.

---

## Is de energiebalans gezond?

**Kijk in:** `energy_balance`

De controle test of dit ongeveer klopt:

```
zon + netinkoop + ontlading  ≈  huisverbruik + lading + teruglevering
```

| Veld | Waar je op let |
|---|---|
| `last_coherent_sample` | `residual_w` tegenover `allowed_residual_w` |
| `tolerance_reason` | Waarom de toegestane marge zo groot is |
| `failed_samples` vs `skipped_incoherent_samples` | Veel overgeslagen = een trage bron, geen fout |
| `source_time_skew_seconds` | Zit dit tegen de 90 seconden aan, dan polt één bron veel langzamer |

**Een omgekeerd teken valt meteen op:** het verschil is dan tien tot twintig keer de
toegestane marge. Een matige overschrijding wijst eerder op verschillende meetgrenzen in
je installatie dan op een fout van jou.

**Deze controle kan nooit een geleerd interval afkeuren.** Het is een kwaliteitssignaal en
telt mee in de betrouwbaarheidsscore; het beïnvloedt geen enkele beslissing over je
batterij.

---

## Waarom staat Battery Return stil?

**Kijk in:** de attributen van `sensor.alpha_ems_battery_return`

| Veld | Betekenis |
|---|---|
| `sealed_through` | Tot welke dag alles is afgesloten |
| `unsealed_past_days` | Hoeveel voorbije dagen nog niet zijn afgesloten |
| `unsealed_by_reason` | Waarom die dagen nog openstaan |
| `terminally_unsealable_days` | Dagen die nooit meer afgesloten kunnen worden |
| `seal_pass_blocked_reason` | Waarom de laatste poging niets deed |

Een dag telt pas mee als hij is afgesloten. Zijn dat er nog geen, dan staat de sensor op
`unknown` met `no_finalised_days` — dat is geen storing, alleen geduld.

---

## Wat er níet in de diagnostiek staat

Geen inloggegevens, geen volledige leergeschiedenis, en geen prijsreeksen. Het `price`-blok
bevat de aantallen, de dekking, de horizon en waarom de volgende dag eventueel ontbreekt —
maar nooit de reeks zelf.

---

## Verder lezen

[Problemen oplossen](troubleshooting.md) · [Sensoren en entiteiten](entities.md) ·
[Hoe werkt Alpha EMS?](how-it-works.md)
