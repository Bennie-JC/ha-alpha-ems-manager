🇳🇱 **Nederlands** | 🇬🇧 [English](../en/solar.md)

# Zonnepanelen

**In één zin:** met een zonvoorspelling weet Alpha EMS dat er gratis energie aankomt, en
kan het daar ruimte voor vrijhouden.

## Waarom dit belangrijk is

Zonder voorspelling is Alpha EMS blind voor de zon. Het slaat binnenkomende productie
gewoon op, maar het kan er niet op vooruitlopen. Dat kost geld op precies één manier: het
laadt 's ochtends goedkoop vol, en 's middags is er geen ruimte meer voor gratis zon.

---

## Twee verschillende dingen

| | Wat het is | Verplicht? |
|---|---|---|
| **PV-vermogenssensor** | Meet wat je panelen **nu** opwekken | Ja, als je zonnepanelen hebt |
| **Solcast** | Voorspelt wat ze **straks** gaan opwekken | Nee |

De eerste heb je altijd nodig als je panelen hebt; die kies je bij het instellen. De
tweede is optioneel en voegt het vooruitkijken toe.

---

## Solcast instellen

**Alleen Solcast wordt ondersteund** — <https://github.com/BJReplay/ha-solcast-solar>. Er
zijn geen andere voorspellingsbronnen.

**Stap 1 — bij Solcast zelf.** Maak een account, vraag een API-sleutel aan en voer je
daklocaties in: oriëntatie, hellingshoek en vermogen. Hun documentatie is leidend.

**Stap 2 — installeer de Solcast-integratie** in Home Assistant en configureer hem met je
API-sleutel.

**Stap 3 — in Alpha EMS.** Zet *Use a PV forecast source* aan en kies je
Solcast-instantie.

**Stap 4 — kies je daken.** Onder *Solcast sites that belong to this system* vink je aan
welke locaties bij dít huis horen.

⚠️ **Deze stap wordt vaak overgeslagen en is belangrijk.** Een Solcast-account kan ook
daken van een tweede woning of van iemand anders bevatten. Die stilzwijgend meerekenen
zou je plan verkeerd voeden. Bij een upgrade worden alle gevonden locaties één keer voor
je geselecteerd; een dak dat je later bij Solcast toevoegt, wordt gemeld maar **niet**
vanzelf toegevoegd.

**Je API-limiet blijft ongemoeid.** Alpha EMS roept twee alleen-lezen acties aan die de
eigen cache van Solcast bedienen. Er wordt niets bij Solcast opgehaald, dus je limiet
wordt niet aangesproken. De acties die wél iets veranderen — update, force-update,
clear-data, dampening, hard-limit — komen in de broncode niet voor.

---

## Wat Alpha EMS met de voorspelling doet

**De verwachte productie wordt van je verwachte verbruik afgetrokken** vóórdat er iets aan
de batterij wordt gevraagd. Op een zonnige middag is het antwoord dan "houden" — niet
omdat er een regel is bijgekomen, maar omdat de bestaande regel nu het juiste getal ziet.

**Ruimte vrijhouden.** Verwacht Alpha EMS veel zon, dan kan het besluiten de accu 's
ochtends niet vol te laden vanaf het net. Zie
[voorbeeld 6](how-it-works.md#6-ruimte-bewaren-voor-de-zon).

**De reserve daalt.** De dynamische reserve houdt rekening met zon die nog moet komen. Op
een zomernacht met een zonnige dag in het vooruitzicht hoeft de batterij niet de energie
mee te dragen die de zon straks levert.

⚠️ Kijk daarbij naar `replenishment_dependency_kwh` op
`sensor.alpha_ems_dynamic_battery_reserve`. Dat zegt hoeveel van de verlaging op nog niet
gearriveerde zon berust. Is dat getal groot, dan is de reserve ernaast optimistisch.

---

## Gratis zon opvangen

Komt er meer zon dan er nodig is, dan kan Alpha EMS het overschot in de batterij stoppen
in plaats van het terug te leveren — als bewaren meer waard is dan de terugleverprijs.

De afweging is een vergelijking, geen regel:

```
rendement × wat een opgeslagen kWh waard is    >    de terugleverprijs
```

Levert verkopen echt meer op, dan verkoopt Alpha EMS. Er is geen "nooit
terugleveren"-regel.

**Er kan hierdoor nooit iets gekocht worden.** Het extra laden is begrensd door de
productie die op dat moment gemeten écht over is, dus je meter kan alleen richting nul
bewegen.

Het opvangen stopt vanzelf: bij een volle accu, bij de vermogensgrens van de omvormer, als
de zon wegvalt, of aan het einde van het kwartier. Wat er niet in past, gaat gewoon het
net op — op een goede dag blijft er altijd wat over.

---

## Waarom een laadperiode zo breed lijkt

Dit is de meest gemelde verwarring, en het komt door het opvangen hierboven.

```
De batterij laadt:          08:30 ────────────────────► 16:30
Er wordt echt gekocht:            11:45─12:15  12:45─13:00  14:00─14:30
```

Een kwartier waarin alleen gratis zon binnenkomt, **onderbreekt een laadsessie niet**. Dat
is expres: anders zou elke wolk de sessie in tweeën knippen en betaalde je twee keer de
schakelkosten. Het gevolg is dat één laadcampagne op een zonnige dag bijna de hele
zonperiode kan beslaan.

Gebruik `next_grid_purchase_at` en `next_grid_purchase_end_at` als je wilt weten wanneer
er echt van het net gekocht wordt. Zie
[voorbeeld 10](how-it-works.md#10-brede-laadperiode-smalle-inkoopblokken).

---

## Voorspeld is niet gemeten

Beide kanten worden apart bewaard, en er wordt **niets** gecorrigeerd. Een tegenvallende
dag verandert de voorspelling van morgen niet.

Dat is met opzet: zo blijft de vastgelegde afwijking een meting van het model, en niet een
product ervan.

Drie redenen waarom voorspeld en gemeten structureel kunnen verschillen, zonder dat er
iets mis is:

**Verschillende meetgrenzen.** Jouw PV-cijfer telt DC-strings en een AC-meter bij elkaar op;
Solcast zegt niet welke grens zij hanteren. Een blijvend verschil is dan een eigenschap
van je installatie, geen voorspelfout.

**Begrenzing door de omvormer.** Kan je dak meer opwekken dan je omvormer kan doorlaten,
dan is de voorspelling op de beste dagen per definitie hoger dan de meting. Waar die grens
uitleesbaar is wordt de dag gemarkeerd; waar dat niet kan, wordt de controle uitgezet in
plaats van een plafond te raden.

**Solcast-instellingen.** Zet je bij Solcast zelf auto-dampening of het bijmengen van
werkelijke productie aan, dan is de reeks die Alpha EMS leest al door iets anders
bijgesteld. Beide worden in de diagnostiek vastgelegd.

---

## Zonder Solcast

Alles blijft werken. Zon die binnenkomt wordt opgeslagen en meegeteld, zelfverbruik wordt
gewoon berekend, en je economie klopt.

Wat je mist, is het vooruitkijken: geen ruimte vrijhouden voor de middag, en een reserve
die geen krediet geeft voor zon die nog moet komen. Zonder voorspelling is een interval
"PV-blind" en wordt de projectie van de laadtoestand als ondergrens gerapporteerd.

⚠️ Staat **Excess Export** aan op je omvormer, dan stuurt die het overschot bewust naar
het net in plaats van naar de batterij. Alpha EMS ziet dat en rapporteert zijn projectie
dan als een ondergrens in plaats van je accu te mooi voor te stellen.

---

## Verder lezen

[Configuratie](configuration.md) · [Hoe werkt Alpha EMS?](how-it-works.md) ·
[Problemen oplossen](troubleshooting.md)
