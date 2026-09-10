🇳🇱 **Nederlands** | 🇬🇧 [English](../en/requirements.md)

# Vereisten

**In één zin:** Alpha EMS Manager rekent en stuurt, maar meet zelf niets — het heeft
andere integraties nodig die je batterij, je meter, je prijzen en je zon publiceren.

## Waarom dit belangrijk is

Alpha EMS Manager legt geen enkele verbinding naar buiten. Het heeft geen API-sleutel,
belt geen server en praat niet met je omvormer. Alles wat het weet, leest het uit
entiteiten die andere integraties in Home Assistant zetten.

Ontbreekt er één, dan mist Alpha EMS een stuk van het beeld — en in sommige gevallen
kun je de integratie niet eens toevoegen.

---

## Overzicht

| Onderdeel | Nodig? | Waarvoor |
|---|---|---|
| [AlphaESS-pakket (Hillview Lodge)](#1-het-alphaess-pakket) | **Vereist** | Batterijgegevens en de aansturing |
| [`input_boolean.alpha_ems_dispatch_owner`](#2-de-eigenaar-helper) | **Vereist** | Herkennen van eigen opdrachten |
| [Frank Quarter Prices](#3-frank-quarter-prices) | **Vereist** | Kwartierprijzen |
| [Slimme meter / P1](#4-slimme-meter--p1-meting) | **Vereist** | Wat er echt op de netaansluiting gebeurt |
| [Home Assistant 2025.1.0+](#5-home-assistant) | **Vereist** | De gebruikte API's |
| [Solcast PV Forecast](#6-solcast-optioneel) | Optioneel | Verwachte zonneproductie |
| [PV-vermogenssensor](#7-pv-vermogenssensor) | Vereist mét zon | Gemeten zonneproductie |
| [EV-laderssensor](#8-ev-lader-optioneel) | Optioneel | Autoladen buiten het leermodel houden |

---

## 1. Het AlphaESS-pakket

**Vereist** — <https://projects.hillviewlodge.ie/alphaess/>

Alpha EMS Manager communiceert niet zelf met je AlphaESS-omvormer. Het
Hillview Lodge-pakket is een YAML-template-pakket dat de AlphaESS-entiteiten en de
bedieningsinterface levert; Alpha EMS leest en schrijft die entiteiten.

**Wat je doet:** installeer en configureer het pakket volgens *hun* documentatie, en
controleer dat de entiteiten in Home Assistant bestaan voordat je Alpha EMS toevoegt.
Die documentatie is leidend — hier staat bewust geen kopie van hun instructies.

**Wat Alpha EMS eruit leest:** het huisverbruik, de laadtoestand en het vermogen van de
batterij, de zonneproductie, en de hele opdracht-interface hieronder.

### De entiteiten die moeten bestaan

Alpha EMS controleert deze lijst bij het opstarten. Ontbreekt er één, dan blijft het
leren en voorspellen gewoon doorgaan, maar wordt er niets uitgevoerd — en de
diagnostiek vertelt welke ontbreekt.

**De eigenaar-helper (maak je zelf, zie hieronder):**
`input_boolean.alpha_ems_dispatch_owner`

**Uitleessensoren van de opdracht:**
`sensor.alphaess_dispatch_start` · `sensor.alphaess_dispatch_mode` ·
`sensor.alphaess_dispatch_active_power` · `sensor.alphaess_dispatch_soc` ·
`sensor.alphaess_dispatch_time`

**De reset-automatisering:** `automation.alphaess_dispatch_reset_full` — moet bestaan
**en aan staan**. Dit is de noodrem die je installatie terugzet als Alpha EMS zou
stoppen met draaien.

**De laad-helpers:**
`input_boolean.alphaess_helper_force_charging` ·
`input_boolean.alphaess_helper_force_charging_hold` ·
`input_number.alphaess_helper_force_charging_power` ·
`input_number.alphaess_helper_force_charging_cutoff_soc` ·
`input_number.alphaess_helper_force_charging_duration` ·
`timer.alphaess_helper_force_charging_timer`

**De ontlaad-helpers:**
`input_boolean.alphaess_helper_force_discharging` ·
`input_boolean.alphaess_helper_force_discharging_hold` ·
`input_number.alphaess_helper_force_discharging_power` ·
`input_number.alphaess_helper_force_discharging_cutoff_soc` ·
`input_number.alphaess_helper_force_discharging_duration` ·
`timer.alphaess_helper_force_discharging_timer`

**De Dispatch-interface** — dit is wat Alpha EMS daadwerkelijk gebruikt om te sturen:
`input_boolean.alphaess_helper_dispatch` ·
`input_select.alphaess_helper_dispatch_mode` ·
`input_number.alphaess_helper_dispatch_power` ·
`input_number.alphaess_helper_dispatch_cutoff_soc` ·
`input_number.alphaess_helper_dispatch_duration` ·
`input_boolean.alphaess_helper_dispatch_pv_switch` ·
`timer.alphaess_helper_dispatch_timer`

De laad- en ontlaad-helpers worden **niet** beschreven door Alpha EMS tijdens normaal
gebruik. Ze moeten bestaan omdat Alpha EMS ze leest om te zien of jij of een andere
automatisering iets aan het doen is.

---

## 2. De eigenaar-helper

**Vereist, en je maakt hem zelf aan.**

```
input_boolean.alpha_ems_dispatch_owner
```

*Instellingen → Apparaten en diensten → Helpers → Schakelaar.* De naam moet exact
kloppen.

**Waarom dit nodig is.** Het AlphaESS-pakket legt nergens vast wíé een opdracht heeft
gestart. Of jij hem vanaf je dashboard aanzet of Alpha EMS het doet, de achtergelaten
waarden zijn identiek. Alpha EMS zet daarom deze helper aan als **eerste** stap van een
eigen opdracht, en uit als **laatste** stap.

**Wat er gebeurt zonder deze helper.** Alpha EMS voert niets uit. Erger nog: zonder de
helper zou het een opdracht kunnen starten die het daarna nooit als de zijne kan
herkennen, en dus nooit kan bijsturen of stoppen. Daarom staat hij bovenaan de lijst
die bij het opstarten wordt gecontroleerd.

⚠️ **Let op:** Alpha EMS raakt een opdracht waarvan het niet kan bewijzen dat hij van
hemzelf is, nooit aan. Dat is veilig gedrag, maar het betekent ook dat een handmatig
gestarte laadsessie gewoon doorloopt.

---

## 3. Frank Quarter Prices

**Vereist** — <https://github.com/Bennie-JC/ha-frank-quarter-prices>

Levert de dynamische kwartierprijzen van Frank Energie: een inkoopprijs en een
teruglevertarief per kwartier, voor vandaag en (vanaf ongeveer 13:00) morgen.

**Wat je doet:** installeer en configureer deze integratie **vóór** Alpha EMS Manager.
Zonder een geconfigureerde Frank-instantie kun je Alpha EMS niet toevoegen — het
formulier stopt met de melding dat Frank eerst moet worden ingesteld.

**Wat Alpha EMS ermee doet:** alles wat met geld te maken heeft. Wanneer inkopen loont,
wanneer bewaren meer waard is dan verkopen, en wat een dag heeft opgeleverd.

**Over morgen.** Frank publiceert de prijzen voor de volgende dag pas rond 13:00–14:00.
Tot dat moment is het normaal dat Alpha EMS alleen vandaag kent en verder vooruitkijken
beperkt is. Dat is geen storing.

---

## 4. Slimme meter / P1-meting

**Vereist.**

Elke Home Assistant-integratie die het **netvermogen** publiceert voldoet. Voorbeelden:
HomeWizard P1, DSMR, SlimmeLezer — of een andere integratie die een correcte
vermogenssensor voor je netaansluiting levert. Er is geen merk verplicht.

**Wat je doet:** tijdens het instellen kies je zelf welke sensor dit is. Alpha EMS praat
niet met de hardware; het leest alleen de entiteit.

**Wat Alpha EMS ermee doet:** de meter is het instrument dat *bepaalt* wat teruglevering
is. Hij wordt gebruikt om te controleren of een ontlading niet ongewenst het net op gaat,
en om te meten wat er werkelijk is verkocht.

⚠️ **Let op:** de richting is cruciaal. Zie
[sign conventions](configuration.md#de-twee-sign-conventions).

---

## 5. Home Assistant

Minimum Home Assistant version: 2025.1.0

Alpha EMS gebruikt `entry.runtime_data`, generieke `ConfigEntry`-typering en
coordinator-`config_entry`-ondersteuning. Die bestaan geen van drieën in oudere
versies, dus een oudere kern zou meteen crashen. HACS blokkeert de installatie eronder.

---

## 6. Solcast (optioneel)

**Optioneel** — <https://github.com/BJReplay/ha-solcast-solar>

Dit is de enige ondersteunde bron voor zonvoorspelling.

**Wanneer heb je dit nodig?** Als je wilt dat *verwachte* zonneproductie meetelt in het
plan — bijvoorbeeld om ruimte in de batterij vrij te houden voor de middag.

**Wat je doet:** configureer eerst je API-sleutel en je daklocaties bij Solcast zelf,
volgens hun documentatie. Daarna kies je in Alpha EMS welke locaties bij *dit* systeem
horen. Een Solcast-account kan namelijk ook daken van een tweede woning bevatten, en
die zomaar meerekenen zou stil verkeerd zijn.

**Je API-limiet.** Alpha EMS roept twee alleen-lezen acties aan die de eigen cache van
Solcast bedienen. Er wordt geen enkele extra opvraag bij Solcast gedaan, dus je limiet
wordt niet aangesproken.

**Zonder Solcast** werkt alles behalve het vooruitkijken naar zon. Zonne-energie die
binnenkomt wordt nog steeds opgeslagen en meegeteld; er wordt alleen niet op
geanticipeerd.

---

## 7. PV-vermogenssensor

**Vereist zodra je in de instellingen aangeeft dat je zonnepanelen hebt** — en dat staat
standaard aan.

Dit is de sensor die meet hoeveel je panelen *nu* opwekken. Los van Solcast: Solcast
voorspelt, deze sensor meet.

Heb je geen zonnepanelen, zet dan *This system has solar panels* uit; de hele stap wordt
dan overgeslagen.

---

## 8. EV-lader (optioneel)

**Optioneel.**

Een vermogenssensor van je laadpaal houdt het laden van je auto buiten het geleerde
huisverbruik. Zonder deze sensor leert Alpha EMS een laadsessie als gewoon huisverbruik,
en gaat het reserveren voor een auto die er morgen misschien niet is.

**Dit is geen laadplanner.** Alpha EMS start, stopt of plant je laadpaal niet. Het haalt
het laadvermogen alleen uit de leerreeks.

⚠️ **Let op:** kies een sensor die `0` publiceert als er niet geladen wordt. Een lader
die `unavailable` meldt als hij niets doet, maakt het grootste deel van de dag
onbruikbaar voor het leermodel.

---

## Volgende stap

[Installatie](installation.md) · [Configuratie](configuration.md)
