🇳🇱 **Nederlands** | 🇬🇧 [English](../en/troubleshooting.md)

# Problemen oplossen

**In één zin:** de meeste meldingen blijken gedrag dat klopt — dit zijn de gevallen die je
het vaakst tegenkomt, en hoe je ze uit elkaar houdt.

## Eerst dit

Zet debug-logging aan als je iets uitzoekt:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.alpha_ems_manager: debug
```

En download de [diagnostiek](diagnostics.md) — bijna elke vraag hieronder is daar te
beantwoorden.

---

## Installeren en instellen

### Ik kan Alpha EMS niet toevoegen

**"Frank Quarter Prices is not set up yet"** — Frank is verplicht en moet vóór Alpha EMS
geconfigureerd zijn. Zie [Vereisten](requirements.md#3-frank-quarter-prices).

**Een foutmelding bij de Solcast-schakelaar** — je hebt *Use a PV forecast source*
aangezet zonder geconfigureerde Solcast-integratie. Zet hem uit, of installeer eerst
[Solcast](solar.md).

**"config entry … uses the version 1 source model"** — dit is een entry van 0.1.0. Die kan
niet worden omgezet: verwijder de integratie en voeg hem opnieuw toe. Zie
[Installatie](installation.md#upgraden-vanaf-010).

### Een sensor wordt geweigerd

De eenheid moet kloppen: vermogen in W, kW of MW; energie in Wh, kWh of MWh; de
laadtoestand exact in `%`. Wordt je EV-sensor geweigerd, controleer dan of het niet
dezelfde entiteit is als je huisverbruikssensor.

---

## Leren en voorspellen

### Verwacht verbruik blijft `unknown`

Er zijn ongeveer twee volledige dagen geschiedenis nodig. Kijk in de diagnostiek of
`learning.measured_valid_intervals` oploopt. Staat die op 0, dan levert je
huisverbruikssensor geen bruikbare waarden — controleer `sources.house_load.state` en
`.unit`.

**Dit is geen storing.** Er wordt bewust geen verzonnen getal gepubliceerd om een leeg
vakje te vullen.

### Learning Days loopt niet op

Een dag telt mee bij minstens 80 % dekking en moet compleet zijn; vandaag telt dus nooit.

Vergelijk `learning.measured_coverage` met `learning.baseline_coverage`. Is de eerste hoog
en de tweede laag, dan zit het probleem bij je EV-sensor, niet bij je huisverbruikssensor.

### Learning Confidence blijft laag

De betrouwbaarheid is rijpheid × kwaliteit, en rijpheid loopt met opzet langzaam op — na
30 dagen ongeveer 63 %. Is hij lager dan het aantal dagen doet vermoeden, kijk dan in het
`confidence`-blok welk onderdeel achterblijft.

### Mijn EV-lader verpest het leren

Meldt je lader `unavailable` in plaats van `0` als hij stilstaat, dan wordt het
huisverbruik voor elk stilstaand kwartier ongeldig — en dat is het grootste deel van de
dag. Kijk naar `flexible_load.intervals_without_valid_data`.

Kies een sensor die een numerieke nul publiceert, of laat het veld leeg.

---

## Prijzen

### De prijzen van morgen ontbreken

Frank vraagt de prijzen van de volgende dag niet op vóór het middaguur en publiceert ze
rond 13:00–14:00. Tot dat moment is "vandaag compleet, morgen afwezig" normaal.

Alpha EMS trekt geen conclusie uit de klok: is de dag er eerder, dan wordt hij gebruikt.

### De dekking is lager dan 1.0

Frank publiceert een *marktdag* van middernacht tot middernacht in de tijdzone van de
markt, terwijl Alpha EMS een burgerlijke dag in jouw tijdzone plant. Draai je Home
Assistant buiten `Europe/Amsterdam` of `Europe/Brussels`, dan lopen die niet gelijk.

Het tekort wordt gemeld in plaats van weggerekend.

---

## Plannen en beslissingen

### Er is een plan, maar er gebeurt niets

Loop deze af, in deze volgorde:

1. Staat **Control Mode** op **Live**? Zo niet: `mode_not_active`.
2. Staat *Allow Alpha EMS to send commands* aan? Zo niet: `execution_not_enabled`.
3. Gaat het om kopen? Staat *Allow buying from the grid* aan?
4. Gaat het om verkopen? Staat *Allow selling from the battery* aan? Zo niet:
   `battery_export_not_permitted`.
5. Bestaat `input_boolean.alpha_ems_dispatch_owner`?
6. Bestaat `automation.alphaess_dispatch_reset_full` **en staat hij aan**?

Het attribuut `execution_blocked_reason` op `sensor.alpha_ems_economic_action` zegt welke
het is.

### Alpha EMS kocht op een duur moment

Kijk of het een `safety_buy` was. Dan is dit geen fout: zonder die aankoop haalt de
batterij de reserve niet, en veiligheid gaat boven prijs. Er gelden geen prijsdrempels
voor.

Zie [voorbeeld 3](how-it-works.md#3-safety-buy).

### Alpha EMS verkocht niet bij een hoge prijs

Vaak precies goed. Verkopen tegen 0,29 om diezelfde energie later terug te kopen tegen
0,30 is verlies. En zou de reserve er onveilig van worden, dan gaat het sowieso niet door.

Controleer wel of *Allow selling from the battery* aan staat.

Zie [voorbeeld 8](how-it-works.md#8-hoge-prijs-en-toch-niet-verkopen).

### De laadperiode is veel breder dan ik verwacht

Dat klopt bijna altijd. Een kwartier waarin alleen gratis zon binnenkomt onderbreekt een
laadsessie niet, dus op een zonnige dag beslaat één campagne bijna de hele zonperiode.

Kijk naar `next_grid_purchase_at` en `next_grid_purchase_end_at` voor het moment waarop er
echt gekocht wordt, en naar `grid_purchase_kwh` tegenover `production_charge_kwh` voor de
verhouding.

Zie [voorbeeld 10](how-it-works.md#10-brede-laadperiode-smalle-inkoopblokken).

### Het plan verandert steeds

Toekomstige kwartieren mogen veranderen zodra de zon, je verbruik of de prijzen
veranderen. Een kwartier dat al begonnen is, verandert niet.

Verandert het `run_id` elk kwartier, of loopt de revisie bij elke verversing op, dan is dat
wél een fout die het melden waard is.

### De campagne haalde zijn doel niet

`partial` met `window_ended` betekent dat de tijd op was. Mogelijke oorzaken: de zon viel
tegen waardoor er meer van het net had gemoeten, de accu zat vol, of een grens knelde.

Kijk in het `execution`-blok naar welke grens bond. Wat er in een afgesloten kwartier is
gemist, wordt **niet** later ingehaald — elk kwartier heeft zijn eigen bevroren doel.

---

## Batterij en aansturing

### Alpha EMS raakt een lopende laadsessie niet aan

Die is waarschijnlijk niet van hemzelf. Kijk naar `execution.ownership.state`: bij
`foreign` kan Alpha EMS niet bewijzen dat hij hem gestart is, en dan blijft hij eraf. Dat
is met opzet.

Meest voorkomende oorzaak: de helper `input_boolean.alpha_ems_dispatch_owner` bestaat niet.

### De accuplanning is `unknown`

Een hardwaregegeven ontbreekt. Meestal is de capaciteit of een vermogensgrens leeggemaakt
op de pagina *Accuplanning*. De reden staat erbij: `REASON_MISSING_CAPACITY` of
`REASON_MISSING_POWER_LIMITS`.

Er wordt bewust niet gegokt. Vul de waarden opnieuw in.

### Last Campaign Result staat op `unknown` na een herstart

Dat hoort zo. Die sensor wordt niet opgeslagen en staat na een herstart op `unknown` met
`no_campaign_closed_yet`, tot de volgende campagne afloopt. Lees het niet als een campagne
die is verdwenen.

### Na een herstart is de laadsessie gestopt

Ook dat hoort zo. De voortgang binnen het lopende kwartier wordt niet opgeslagen, dus
doorgaan zou gokken zijn. Bij het volgende kwartier maakt het plan een nieuwe sessie.

---

## Metingen

### Waarschuwingen over de energiebalans

*"Sustained energy-balance mismatch over N consecutive checks"* betekent drie samenhangende
mislukte metingen achter elkaar, dus ongeveer drie minuten. Eén vreemd moment waarschuwt
nooit.

Er zijn twee varianten en ze betekenen iets anders:

| Melding | Betekenis |
|---|---|
| *"…usually means one term of the identity is wrong"* | Het verschil is vele malen de marge. Controleer je sensoren en je twee sign conventions — er is echt iets verkeerd ingesteld. |
| *"…consistent with the sources being measured at different electrical boundaries"* | Matig over de marge. Waarschijnlijk de DC/AC-grenzen van je omvormer, niet jouw fout. |

**Dit beïnvloedt het leren niet** en blokkeert geen enkele beslissing over je batterij.
Het telt alleen mee in de betrouwbaarheidsscore.

### Een blijvend klein verschil bij laag vermogen

Op de referentie-installatie komt een aanhoudend verschil voor van rond de 154 W op 740 W,
dat vanzelf weer verdwijnt; de slagingskans blijft rond 99 %.

De verklaring is bijna zeker dat het netcijfer van een aparte P1-meter komt terwijl
huisverbruik, zon en batterij allemaal van de omvormer komen. Een min of meer constante
afwijking tussen twee instrumenten op verschillende elektrische grenzen is
verwaarloosbaar op een drukke middag en een flink deel van een rustige.

**Er is geen drempel opgerekt om dit stil te krijgen.** Dat zou de controle op elk
vermogensniveau blind maken om één situatie te verklaren, en een bedradings- of tekenfout
verbergen is erger dan de waarschuwing.

### Een bron is even weg

Gaten worden geregistreerd als ontbrekende dekking, nooit als nul. Korte onderbrekingen
worden opgevangen; langere verlagen de compleetheid van de dag. Waarschuwingen zijn
beperkt tot één per uur per oorzaak, dus een lange storing overspoelt je log niet.

---

## De zon

### Voorspeld en gemeten verschillen structureel

Drie normale oorzaken: verschillende meetgrenzen tussen je PV-cijfer en dat van Solcast,
begrenzing door je omvormer op de beste dagen, en Solcast-instellingen als auto-dampening
of het bijmengen van werkelijke productie.

Er wordt **niets** gecorrigeerd. Dat is met opzet. Zie [Zonnepanelen](solar.md).

### De zonvoorspelling telt niet mee

Controleer of *Use a PV forecast source* aan staat, of er een Solcast-instantie is
gekozen, én of je onder *Solcast sites that belong to this system* daadwerkelijk daken
hebt aangevinkt.

---

## Nog steeds vast?

Meld het met een [diagnostiekdownload](diagnostics.md) erbij op de
[issue tracker](https://github.com/Bennie-JC/ha-alpha-ems-manager/issues). Daar staan geen
inloggegevens in.

---

## Verder lezen

[Diagnostiek](diagnostics.md) · [Configuratie](configuration.md) ·
[Hoe werkt Alpha EMS?](how-it-works.md)
