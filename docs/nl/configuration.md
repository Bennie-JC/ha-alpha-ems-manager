🇳🇱 **Nederlands** | 🇬🇧 [English](../en/configuration.md)

# Configuratie

**In één zin:** elk veld dat je in Alpha EMS Manager ziet, uitgelegd in de volgorde
waarin Home Assistant het je voorschotelt.

## Hoe je deze pagina gebruikt

Zet dit venster naast Home Assistant en werk van boven naar beneden mee. Eerst de
installatiestappen, daarna de vier optiepagina's — in precies dezelfde volgorde als het
scherm.

Achteraf iets wijzigen kan altijd via **Instellingen → Apparaten en diensten → Alpha EMS
Manager → Configureren**. De integratie herstart dan even; je geleerde geschiedenis
blijft behouden.

---

## De vier meetpunten

Voordat je sensoren gaat kiezen: Alpha EMS vraagt om vier **verschillende** elektrische
meetpunten. Ze meten niet hetzelfde en je mag ze niet verwisselen.

```
        Net  ↔  Huis  ↔  Batterij
                  ↑
                 Zon
```

| Meetpunt | Wat het meet |
|---|---|
| **Net** | Wat er over je netaansluiting gaat, in of uit |
| **Huis** | Wat je woning verbruikt, ongeacht waar het vandaan komt |
| **Batterij** | Wat er in of uit de accu gaat |
| **Zon** | Wat je panelen opwekken |

Op een zonnige middag kan je huis 2 kW verbruiken terwijl de meter 0 kW aanwijst. Kies
je daar de netsensor als huisverbruik, dan leert Alpha EMS dat je huis niets gebruikt.

⚠️ **Let op:** de meest gemaakte fout is de netsensor of de PV-sensor kiezen als
huisverbruik. Zie [Problemen oplossen](troubleshooting.md).

---

# Deel 1 — De installatiestappen

## Stap 1: Alpha EMS Manager

### Instance name

`name`
**Wat is dit?** De naam van deze installatie.

**Wat kies ik?** `Alpha EMS` als je maar één batterijsysteem hebt.

**Verplicht:** ja. **Standaard:** `Alpha EMS`.

⚠️ **Let op:** deze naam bepaalt de entiteit-ID's. Bij `Alpha EMS` krijg je
`sensor.alpha_ems_economic_action`; noem je het `Thuis`, dan wordt het
`sensor.thuis_economic_action`. Kies hem dus meteen goed — achteraf hernoemen betekent
al je dashboards aanpassen.

### House load power

`house_load_entity`
**Wat is dit?** De sensor die vertelt hoeveel je woning op dit moment verbruikt.

**Wat kies ik?** Op AlphaESS heet deze meestal *Current House Load*. Níet je netsensor.

**Eenheid:** W, kW of MW. **Verplicht:** ja.

**Waarvoor?** Dit is de meting waar het hele leermodel op draait.

**Leeg laten?** Kan niet — het formulier weigert, en zonder deze sensor kan de
integratie niet starten.

### Daily house load (validation, optional)

`daily_house_load_entity`
**Wat is dit?** Een dagteller van je huisverbruik in kWh.

**Wat kies ik?** Heb je zo'n sensor, kies hem; anders laat je het leeg.

**Eenheid:** Wh, kWh of MWh. **Verplicht:** nee.

**Waarvoor?** Puur als controle aan het eind van de dag: klopt wat Alpha EMS heeft
opgeteld met wat de teller zegt?

**Leeg laten?** Prima. Die controle wordt dan niet uitgevoerd, verder verandert er niets.

### EV charger power (optional)

`ev_power_entity`
**Wat is dit?** Het vermogen van je laadpaal.

**Wat kies ik?** De vermogenssensor van je laadpaal, of leeg als je er geen hebt.

**Eenheid:** W, kW of MW. **Verplicht:** nee.

**Waarvoor?** Om het laden van je auto van je huisverbruik af te trekken, zodat een
laadsessie niet als normaal verbruik wordt geleerd.

**Leeg laten?** Dan is het geleerde verbruik gelijk aan het gemeten verbruik.

**Fout ingesteld?** Deze sensor mag niet dezelfde zijn als *House load power*; dat wordt
geweigerd.

⚠️ **Let op:** kies een sensor die `0` publiceert als er niet geladen wordt. Een lader
die `unavailable` meldt bij stilstand maakt bijna elk kwartier onbruikbaar om te leren.

### This system has solar panels

`has_pv`
**Wat is dit?** Of er zonnepanelen bij dit systeem horen.

**Verplicht:** ja. **Standaard: aan.**

**Waarvoor?** Staat dit aan, dan komt er een extra stap waarin je je PV-sensor kiest.

**Uit zetten?** Doe dat als je geen panelen hebt; de zonnestap wordt dan overgeslagen.

### Use a PV forecast source

`use_pv_forecast`
**Wat is dit?** Of Alpha EMS *verwachte* zonneproductie mag meenemen.

**Verplicht:** ja. **Standaard: uit.**

**Waarvoor?** Aan betekent: vraag Solcast wat er vandaag en morgen aan zon komt, en
gebruik dat in het plan.

**Aanzetten zonder Solcast?** Dat geeft direct een foutmelding bij het veld. Configureer
eerst [Solcast](solar.md).

**Normale keuze:** aan, als je panelen hebt en Solcast draait.

---

## Stap 2: Battery

### Battery state of charge

`battery_soc_entity`
**Wat is dit?** De laadtoestand van je batterij, in procenten.

**Wat kies ik?** De SoC-sensor van je AlphaESS-systeem.

**Eenheid:** exact `%`. **Verplicht:** ja.

**Waarvoor?** Alles: hoeveel er in zit, hoeveel ruimte er is, of de reserve gehaald wordt.

### Battery power

`battery_power_entity`
**Wat is dit?** Het vermogen dat op dit moment in of uit de batterij gaat.

**Eenheid:** W, kW of MW. **Verplicht:** ja.

**Waarvoor?** Om te meten wat er werkelijk geladen of ontladen is.

### Battery power sign convention

`battery_power_sign`
**Wat is dit?** Of een *negatief* getal laden betekent, of juist ontladen.

**Opties:** *Negative means charging (AlphaESS default)* · *Positive means charging*.

**Standaard:** negatief is laden — dat is wat AlphaESS doet.

Zie [De twee sign conventions](#de-twee-sign-conventions) hieronder voor een test.

### Usable battery capacity (DC)

`battery_capacity_kwh` — **Wat vul ik in?** De **bruikbare** capaciteit van je batterij in kWh, volgens de
specificatie van je fabrikant. Voorbeeld: een pakket met 21,6 kWh bruikbaar → `21.6`.

**Eenheid:** kWh. **Bereik:** 0,1 – 200,0, stap 0,1. **Verplicht:** ja (bij installatie).
**Standaard:** geen — er valt niets te raden.

**Fout ingesteld?**
- **Te hoog:** Alpha EMS denkt dat er meer energie beschikbaar is dan er werkelijk in
  zit, en plant te krap.
- **Te laag:** Alpha EMS wordt onnodig voorzichtig en laat kansen liggen.

⚠️ **Let op:** vul de *bruikbare* capaciteit in, niet de bruto capaciteit van het
typeplaatje.

### Minimum state of charge

`battery_min_soc_percent` — **Wat is dit?** De ondergrens die je zelf aanhoudt: hier gaat Alpha EMS nooit onder.

**Eenheid:** %. **Bereik:** 0,0 – 100,0, stap 1,0. **Standaard:** `20`.
**Verplicht:** ja (maar altijd ingevuld).

**Normale keuze:** de standaard `20`, of wat je omvormer zelf al aanhoudt.

**`0` invullen** mag en betekent: Alpha EMS houdt zelf geen reserve aan, alleen de
ondergrens van je omvormer geldt nog.

**Fout ingesteld?** Een waarde van 100 of hoger wordt geweigerd.

⚠️ **Belangrijk:** dit is de **harde vloer**, niet alles wat de batterij aanhoudt. De
[dynamische reserve](how-it-works.md#de-reserve) kan tijdelijk méér vragen dan deze
ondergrens, omdat het vooruitkijkt naar wat je vanavond nog nodig hebt. Dit veld op 15 %
zetten betekent dus niet dat de batterij altijd tot 15 % leeggaat.

### Maximum charge power (AC)

`battery_max_charge_kw` — **Wat vul ik in?** Het maximale laadvermogen van je omvormer, in kW, volgens de
specificatie.

**Eenheid:** kW. **Bereik:** 0,1 – 50,0, stap 0,1. **Verplicht:** ja (bij installatie).

**Fout ingesteld?** Te hoog en Alpha EMS plant laadsessies die je omvormer niet aankan —
die vallen dan korter uit dan bedoeld. Te laag en er wordt onnodig langzaam geladen.

### Maximum discharge power (AC)

`battery_max_discharge_kw`
Hetzelfde, voor ontladen. Zelfde eenheid, bereik en gevolgen.

### Round-trip efficiency

`battery_round_trip_efficiency_percent` — **Wat is dit?** Hoeveel procent van de energie die je erin stopt, er weer uit komt.

**Eenheid:** %. **Bereik:** 50,0 – 100,0, stap 1,0. **Standaard:** `90`.

**Normale keuze:** de standaard `90`, tenzij je de werkelijke waarde van je systeem kent.

**Waarvoor?** Om te bepalen of energie bewaren meer waard is dan verkopen: een kWh die
je opslaat, komt er niet volledig weer uit.

⚠️ **Let op:** vul `90` in, niet `0.90`. De ondergrens van 50 vangt die vergissing op.

---

## Stap 3: Solar production

*Je ziet deze stap alleen als je bij* This system has solar panels *ja hebt gezegd.*

### PV production power

`pv_power_entity`
**Wat is dit?** De sensor die meet hoeveel je panelen nu opwekken.

**Eenheid:** W, kW of MW. **Verplicht:** ja, in deze stap.

**Waarvoor?** Om te zien hoeveel zon er over is naast je huisverbruik, en of die in de
batterij past in plaats van naar het net.

---

## Stap 4: Grid meter

### Grid power

`grid_power_entity`
**Wat is dit?** De sensor die vertelt hoeveel stroom je woning op dit moment van het net
haalt of teruglevert.

**Wat kies ik?** De vermogenssensor van je P1/slimme meter — bijvoorbeeld HomeWizard P1,
DSMR of SlimmeLezer.

**Eenheid:** W, kW of MW. **Verplicht:** ja.

**Waarvoor?** Dit is het instrument dat *bepaalt* wat teruglevering is. Alpha EMS
gebruikt het om te voorkomen dat een ontlading ongewenst het net op gaat, en om te meten
wat er echt is verkocht.

### Grid power sign convention

`grid_power_sign`
**Wat is dit?** Of een *positief* getal betekent dat je van het net afneemt.

**Opties:** *Positive means importing from the grid* · *Negative means importing*.

**Standaard:** positief is afnemen — de gebruikelijke Nederlandse P1-conventie.

### Cumulative grid export counter (optional)

`grid_export_energy_entity`
**Wat is dit?** Een oplopende teller van wat je in totaal hebt teruggeleverd, in kWh.

**Eenheid:** Wh, kWh of MWh. **Verplicht:** nee.

**Waarvoor?** Een extra controle van de teruglever-boekhouding tegen je meterstand.

**Leeg laten?** Prima; die controle vervalt, verder verandert er niets.

---

## Stap 5: Prices and forecast

### Frank Quarter Prices instance

`frank_entry_id`
**Wat kies ik?** Je geconfigureerde Frank Quarter Prices-instantie.

**Verplicht:** ja.

**Waarom een instantie en geen entiteit?** Zo blijft de koppeling werken als je later een
entiteit hernoemt.

**Ontbreekt Frank?** Dan kun je Alpha EMS niet toevoegen. Installeer eerst
[Frank Quarter Prices](requirements.md#3-frank-quarter-prices).

### Solcast PV Forecast instance

`solcast_entry_id`
*Je ziet dit veld alleen als je* Use a PV forecast source *hebt aangezet.*

**Wat kies ik?** Je geconfigureerde Solcast-instantie. **Verplicht:** ja, als de
voorspelling aan staat.

---

# Deel 2 — De optiepagina's

**Instellingen → Apparaten en diensten → Alpha EMS Manager → Configureren** geeft een
menu met vier pagina's.

## Bronnen (Sources)

Dezelfde sensoren als bij de installatie, plus drie velden die alleen hier staan. De
volgorde op het scherm is: house load, daily house load, battery SoC, battery power,
battery sign, has PV, PV power, grid power, grid sign, Frank, PV-voorspelling, Solcast,
EV-lader, Solcast-locaties, teruglever-teller.

Alles wat hierboven al is uitgelegd, geldt onveranderd. Drie aanvullingen:

### PV production power (op deze pagina)

Hier is het veld technisch optioneel, maar als *This system has solar panels* aan staat
en je het leegmaakt, krijg je een foutmelding. Wil je de PV-sensor echt weg, zet dan
eerst de schakelaar uit.

### Solcast sites that belong to this system

`selected_solcast_site_ids` — *Je ziet dit veld alleen als:* de PV-voorspelling aan staat, er een Solcast-instantie is
gekozen, Solcast bereikbaar is, én de locaties konden worden uitgelezen.

**Wat kies ik?** De daken die bij *dit* huis horen. Een Solcast-account kan ook daken van
een ander adres bevatten.

**Standaard:** alle gevonden locaties.

Een locatie die Solcast niet meer aanbiedt blijft in de lijst staan, gemarkeerd met
*(no longer offered by Solcast)*, zodat hij niet stilletjes verdwijnt.

### Velden leegmaken

Op deze pagina kun je *daily house load*, *EV charger power*, *cumulative grid export
counter*, *PV production power* en *Solcast instance* echt leegmaken; de eerdere waarde
wordt dan gewist.

De **instantienaam** kun je hier niet wijzigen. Dat doe je door de integratie zelf te
hernoemen in Home Assistant — en onthoud dat dat je entiteit-ID's niet meeverandert.

---

## Accuplanning (Battery planning)

Dezelfde vijf velden als installatiestap 2: capaciteit, minimum SoC, maximaal laden,
maximaal ontladen, rendement.

**Eén verschil, en het is belangrijk.** Capaciteit, maximaal laden en maximaal ontladen
zijn hier **optioneel**. Maak je er één leeg, dan gaat Alpha EMS niet gokken: de
accuplanning wordt *unavailable* met een reden erbij, en de entiteiten die daarvan
afhangen worden `unknown`. Leren en voorspellen gaan gewoon door.

Minimum SoC en rendement kun je niet leegmaken; die hebben altijd een waarde.

---

## Regeling (Control)

Drie instellingen.

### Export safety margin

`control_export_margin_percent` — **Wat is dit?** Een veiligheidsmarge op hoeveel je aansluiting nog kan opnemen voordat
er ongewenst wordt teruggeleverd.

**Eenheid:** %. **Bereik:** 0 – 50, stap 1. **Standaard:** `10`.

**Waarvoor?** Je huisverbruik kan veranderen tussen het moment van meten en het moment
van sturen. Deze marge vangt dat op.

**Wanneer aanpassen?** *Geavanceerd.* Hoger maakt Alpha EMS voorzichtiger en de
commando's kleiner.

### Grid energy budget per charge run

`grid_charge_budget_kwh` — **Wat is dit?** Een bovengrens op hoeveel kWh er per laadsessie van het net gekocht mag
worden.

**Eenheid:** kWh. **Bereik:** 0,0 – 50,0, stap 0,1. **Standaard:** `0.0`.

⚠️ **`0` betekent: geen limiet — niet: niet kopen.** Wil je niet van het net kopen, zet
dan *Allow buying from the grid* uit op de Economie-pagina.

**Wanneer aanpassen?** *Alleen aanpassen als* je bewust een plafond per sessie wilt.

### Allow Alpha EMS to send commands

**Wat is dit?** De hoofdschakelaar voor het versturen van commando's
(`control_execution_enabled`).

**Standaard: uit.**

| | |
|---|---|
| **UIT** | Alpha EMS rekent alles uit en stuurt niets — ook niet in *Live*. |
| **AAN** | Commando's mogen verstuurd worden, **mits** Control Mode op *Live* staat. |

Dit is één van de twee toestemmingen. De andere is de entiteit **Control Mode**, en de
één impliceert de ander niet. Zie [Veiligheid en bediening](control-and-safety.md).

⚠️ **Let op:** zet dit pas aan nadat je in *Shadow* hebt gezien wat Alpha EMS zou doen.

---

## Economie (Economics)

Negen instellingen. **Vijf beïnvloeden het plan, vier alleen de rapportage.**

| Instelling | Sleutel | Beïnvloedt |
|---|---|---|
| Minimum gain per trade | `minimum_trade_gain_eur` | **het plan** |
| Extra margin per grid-charged kWh | `grid_charge_margin_eur_per_kwh` | **het plan** |
| Battery wear cost per kWh | `battery_throughput_cost_eur_per_kwh` | **het plan** |
| Allow buying from the grid | `allow_grid_charging` | **het plan** |
| Allow selling from the battery | `allow_battery_export` | **het plan** |
| Battery investment (gross) | `battery_investment_eur` | alleen rapportage |
| Subsidy received | `battery_subsidy_eur` | alleen rapportage |
| Other one-time credit | `other_one_time_credit_eur` | alleen rapportage |
| Purchase date | `battery_investment_date` | alleen rapportage |

Dat onderscheid is belangrijk: je investeringsbedrag invullen verandert **niets** aan
wanneer je batterij handelt. Het bepaalt alleen wat de sensor *Battery Return* laat zien.

### Minimum gain per trade

**Wat is dit?** Het bedrag dat één handeling minimaal moet opleveren voordat hij de
moeite waard is.

**Eenheid:** EUR. **Bereik:** 0,00 – 5,00, stap 0,01. **Standaard:** `0.10`.

**Waarvoor?** Om te voorkomen dat je plan volloopt met handelingen van twee cent. Het
wordt één keer per aaneengesloten actie gerekend, niet per kWh — en alleen voor
handelingen die Alpha EMS zelf kiest, nooit voor opgevangen zon.

**Normale keuze:** de standaard.

**`0` invullen** betekent: neem elke handeling die iets oplevert.

### Extra margin per grid-charged kWh

**Wat is dit?** Een extra eis **per kWh** bovenop het vaste minimum, voor energie die je
echt van het net koopt.

**Eenheid:** EUR/kWh. **Bereik:** 0,00 – 2,00, stap 0,01. **Standaard:** `0.0` (uit).

**Waarom naast de vorige?** Een vast bedrag schaalt niet mee. Zodra één handeling die
drempel haalt, is het volume erachter onbegrensd. Met deze marge moet elke gekochte kWh
zichzelf terugverdienen.

**Wanneer aanpassen?** *Alleen aanpassen als* je vindt dat er te veel volume wordt
verhandeld voor te weinig marge.

Je eigen zon, het zonnedeel van een gemengd kwartier, ontladen naar je huis en kopen voor
de veiligheid vallen hier allemaal **buiten**.

### Battery wear cost per kWh

**Wat is dit?** Wat een verplaatste kWh je aan batterijslijtage kost.

**Eenheid:** EUR/kWh. **Bereik:** 0,000 – 1,000, stap 0,001. **Standaard:** `0.0`.

**Normale keuze:** de standaard `0`, tenzij je een onderbouwd cijfer hebt.

### Allow buying from the grid

**Standaard: uit.**

| | |
|---|---|
| **UIT** | Alpha EMS koopt geen stroom van het net om op te slaan. Je eigen zon opvangen mag nog steeds. |
| **AAN** | Inkopen mag onderdeel worden van het plan. |

⚠️ **Je eigen zon in de batterij is géén "kopen van het net".** Daar is deze schakelaar
niet voor nodig.

### Allow selling from the battery

**Standaard: uit.**

| | |
|---|---|
| **UIT** | Alpha EMS mag een verkoopkans wél berekenen en tonen, maar voert hem niet uit. |
| **AAN** | Verkopen mag onderdeel worden van het plan, en mag worden uitgevoerd zodra Control Mode op *Live* staat en het versturen van commando's aan staat. |

### Battery investment (gross)

**Wat vul ik in?** Wat je batterij heeft gekost, inclusief btw en installatie.

**Eenheid:** EUR. **Bereik:** 0 – 1.000.000, stap 1. **Verplicht:** nee.

**Waarvoor?** Alleen voor de sensor *Battery Return*, die bijhoudt hoeveel je hebt
terugverdiend.

**Leeg laten?** Dan is *Battery Return* niet beschikbaar, met de reden
`no_investment_configured`. Dat is iets anders dan een investering van nul.

### Subsidy received · Other one-time credit

Bedragen die van je bruto-investering af gaan. Zelfde eenheid en bereik, ook alleen voor
de rapportage.

### Purchase date

**Wat is dit?** De aankoopdatum, om de terugverdienperiode vanaf het juiste moment te
rekenen.

**Leeg laten?** Dan wordt de periode niet vermeld; de rest van de sensor werkt gewoon.

⚠️ **Bekend gedrag:** deze vier optionele geldvelden kunnen op dit moment niet meer
worden *leeggemaakt* via de interface. Vul je er één in en maak je hem later leeg, dan
blijft de oude waarde bewaard. Een andere waarde invullen werkt wel gewoon.

---

## De twee sign conventions

Dit is de instelling waar het vaakst iets misgaat, en de gevolgen zijn groot: met een
verkeerde conventie leest Alpha EMS laden als ontladen, of afnemen als terugleveren.

### De batterij testen

1. Kijk naar je batterij op een moment dat hij **duidelijk aan het laden is**.
2. Kijk naar de waarde van je *Battery power*-sensor.
3. Staat er een **min** voor (`-1200 W`)? → kies *Negative means charging*.
4. Staat er **geen** min voor (`1200 W`)? → kies *Positive means charging*.

Voor AlphaESS is de eerste bijna altijd de juiste; die is dan ook de standaard.

### Het net testen

1. Kijk op een moment dat je **zeker weet dat je afneemt** — 's avonds, geen zon, geen
   batterij die ontlaadt.
2. Kijk naar je *Grid power*-sensor.
3. `+1200 W` → kies *Positive means importing from the grid*.
4. `-1200 W` → kies *Negative means importing*.

### Hoe zie je dat het fout staat?

Download de diagnostiek en kijk naar de energiebalans. Bij een omgekeerd teken is het
verschil tien tot twintig keer groter dan de toegestane marge — dat valt meteen op. Zie
[Diagnostiek](diagnostics.md#is-de-energiebalans-gezond).

---

## Volgende stap

[Hoe werkt Alpha EMS?](how-it-works.md) · [Veiligheid en bediening](control-and-safety.md)
