🇳🇱 **Nederlands** | 🇬🇧 [English](../en/how-it-works.md)

# Hoe werkt Alpha EMS?

**In één zin:** elk kwartier maakt Alpha EMS het goedkoopste plan dat nog steeds veilig
is, en voert daar het deel van uit dat mag.

## Waarom dit belangrijk is

Als je begrijpt hoe Alpha EMS denkt, herken je zijn keuzes terug — ook de keuzes die op
het eerste gezicht vreemd lijken. Bijna elke "dit is vast een bug"-melding blijkt een van
de situaties hieronder.

---

## De kwartierlus

Elke vijftien minuten kijkt Alpha EMS naar:

- je **verwachte huisverbruik**, uit het leermodel
- de **laadtoestand** van je batterij
- de **stroomprijzen** per kwartier, van Frank
- de **verwachte zonneproductie**, van Solcast
- de **vrije ruimte** in de accu
- de **reserve** die de batterij moet aanhouden

Daaruit volgt één plan voor de hele bekende horizon — vandaag, en zodra de prijzen
gepubliceerd zijn ook morgen. Dat plan zegt per kwartier wat er zou moeten gebeuren.

Daarnaast draait er elke minuut een korte controle die het vermogen bijstuurt binnen het
kwartier waar je in zit. Die herrekent níets: hij leest het al vastgelegde doel en
beweegt ernaartoe.

## Veiligheid gaat vóór geld

Dit is de belangrijkste regel, en hij is absoluut. Alpha EMS vergelijkt eerst of een plan
haalbaar is voor de reserve, en pas daarna wat het kost. Er is geen prijs waarvoor de
reserve wordt opgegeven en geen stand waarin dat anders werkt.

Daarom kan Alpha EMS op een duur moment kopen. Zie [voorbeeld 3](#3-safety-buy).

## De reserve

De **dynamische reserve** is de hoeveelheid energie die de batterij zou moeten hebben om
je huis te kunnen blijven voeden tot het volgende moment waarop hij bijgevuld kan worden.

Die stijgt en daalt door de dag heen. Op een zonnige middag mag hij laag zijn: de zon
vult straks bij. In de vroege avond, met de piek nog voor de boeg, is hij hoog.

Je ingestelde *minimum state of charge* is de **harde vloer** — daar gaat Alpha EMS nooit
onder. De dynamische reserve kan tijdelijk **méér** vragen dan die vloer.

## Drie redenen om te kopen

Elke gekochte kWh krijgt precies één reden. Dat is geen etiket achteraf; het bepaalt
welke regels erop van toepassing zijn.

| Reden | Wat het betekent | Prijsdrempels? |
|---|---|---|
| **Safety** | Zonder deze aankoop haalt de batterij de reserve niet | Nee — veiligheid gaat voor |
| **Coverage** | Je huis verbruikt deze energie later toch; nu is hij goedkoper | Nee — het is geen handel |
| **Economic** | Een handel die Alpha EMS zelf kiest omdat hij winst oplevert | Ja, jouw drempels gelden |

## Waarom plannen veranderen

Het plan voor de toekomst kan bij elke verversing verschuiven, en dat hoort zo: de zon
valt tegen, je verbruik loopt anders, of de prijzen van morgen worden gepubliceerd.

**Een kwartier dat al begonnen is, ligt vast.** Dat wordt niet onder de regelaar
weggeschreven. Wat er in dat kwartier gemist is, wordt ook niet later ingehaald.

---

# Praktijkvoorbeelden

*Alle bedragen, tijden en percentages hieronder zijn voorbeelden om het idee te laten
zien. Het zijn geen drempels die Alpha EMS letterlijk hanteert en geen belofte over wat
jouw installatie oplevert.*

## 1. Economic Buy

```
Batterij:   35 %
13:00:      € 0,20/kWh
20:00:      € 0,55/kWh
Ruimte:     genoeg
Reserve:    veilig
```

**Beslissing:** Alpha EMS koopt om 13:00 energie in de batterij.

**Wat je ziet:** `economic_buy` — *economische koop*.

> Dit is geen noodkoop. Alpha EMS kiest zelf om nu goedkope stroom te kopen omdat die
> later meer waard is.

Hier gelden jouw drempels wél: *Minimum gain per trade* en *Extra margin per grid-charged
kWh* moeten allebei gehaald worden, anders gaat de aankoop niet door.

## 2. Coverage Buy

```
Nu:                       € 0,24/kWh
Later, onvermijdelijk:    € 0,42/kWh
```

Je huis gaat die energie later hoe dan ook van het net halen — de batterij is dan leeg
en er is geen zon meer.

**Beslissing:** Alpha EMS koopt hem nu.

**Wat je ziet:** `coverage_buy`.

> Je huis heeft die stroom later toch nodig. Alpha EMS koopt hem eerder omdat hij nu
> goedkoper is.

**Het verschil met een economische koop.** Een economische koop is een *handel*: hij moet
winst opleveren en jouw drempels halen. Een coverage buy is **geen handel** — het is
dezelfde onvermijdelijke aankoop, alleen op een goedkoper moment. Van 0,42 naar 0,24 is
geen winst, het is dezelfde boodschappen doen bij een goedkopere winkel. Daarom een
winstdrempel eisen zou hem om de verkeerde reden afwijzen.

Alpha EMS koopt op deze manier alleen energie die je huis volgens de voorspelling
werkelijk gaat verbruiken, en die energie kan niet worden verkocht.

## 3. Safety Buy

```
Batterij:   dreigt vanavond te leeg te raken
Reserve:    wordt straks niet meer haalbaar
Dit uur:    NIET het goedkoopste van de dag
```

**Beslissing:** Alpha EMS koopt tóch, nu.

**Wat je ziet:** `safety_buy`.

> Alpha EMS koopt hier niet omdat dit het goedkoopste kwartier is, maar omdat de
> batterij anders later niet genoeg reserve kan houden.

**Dit is het voorbeeld dat je moet onthouden.** Zie je een aankoop op een duur moment,
kijk dan eerst of het een safety buy is voordat je het als storing meldt. Voor deze
aankoop gelden geen prijsdrempels — een reserve die niet gehaald wordt is geen bedrag
waar je tegen kunt afwegen.

## 4. Mixed Buy

Eén campagne kan meer dan één reden hebben:

```
2 kWh   verplicht voor de reserve
4 kWh   daarnaast economisch de moeite waard
------
6 kWh   totaal, in één laadsessie
```

**Wat je ziet:** `mixed_buy`.

**Lees dit niet als "de safety buy werd groter".** Het zijn twee losse componenten die
toevallig in hetzelfde venster vallen. Alleen fysieke haalbaarheid kan een aankoop
verplicht maken; een economisch aantrekkelijke aankoop wordt dat nooit alsnog, hoe
aantrekkelijk ook. En andersom: die 4 kWh moet nog steeds jouw drempels halen, terwijl
de 2 kWh dat niet hoeft.

## 5. Gratis zon opvangen

```
Zon:        4,0 kW
Huis:       1,0 kW
Over:       3,0 kW
Batterij:   heeft ruimte
```

**Beslissing:** Alpha EMS kan die 3 kW in de batterij stoppen in plaats van hem terug te
leveren — als bewaren meer waard is dan de terugleverprijs.

**Dit is geen inkopen van het net.** Er wordt niets gekocht en er is geen toestemming
voor nodig. Ook met *Allow buying from the grid* uit gebeurt dit gewoon.

Waar verkopen echt meer oplevert, verkoopt Alpha EMS. Er is geen regel die zegt "nooit
terugleveren".

**Waarom dit ertoe doet:** een kwartier waarin alleen gratis zon binnenkomt, onderbreekt
een laadsessie niet. Daardoor kan een laadcampagne uren beslaan terwijl er maar in een
paar kwartieren echt gekocht wordt. Zie [voorbeeld 10](#10-brede-laadperiode-smalle-inkoopblokken).

## 6. Ruimte bewaren voor de zon

```
Batterij:   80 %
Ochtend:    goedkope stroom beschikbaar
Middag:     veel zon verwacht
```

**Beslissing:** Alpha EMS laadt níet helemaal vol vanaf het net.

> Goedkope stroom is niet altijd de slimste stroom als gratis zon eraan komt.

Zou hij nu volladen, dan is er straks geen ruimte voor de zon en moet die naar het net
tegen een lage terugleverprijs. De goedkope kWh van vanochtend verdringt dan een gratis
kWh van vanmiddag.

Dit werkt alleen als Solcast is ingesteld — zonder voorspelling weet Alpha EMS niet dat
er zon aankomt. Zie [Zonnepanelen](solar.md).

## 7. Terugleveren

```
Batterij:   95 %
Huis:       weinig verbruik
Reserve:    veilig
Avond:      hoge terugleverprijs
```

**Beslissing:** Alpha EMS verkoopt batterij-energie aan het net.

**Wat je ziet:** `export` op *Economic Action*.

**Let op één ding:** wat er uit de batterij gaat en wat er over de meter gaat, zijn niet
hetzelfde getal. Je huis pakt zijn deel er eerst af. Gaat er 1,0 kW uit de batterij en
verbruikt je huis 0,3 kW, dan komt er 0,7 kW bij de meter aan. Beide cijfers worden
gepubliceerd, elk aan zijn eigen kant. Zie [Economie](economics.md).

## 8. Hoge prijs, en toch niet verkopen

```
Avond:      aantrekkelijke terugleverprijs
Maar:       je huis heeft die energie straks zelf nodig
En:         terugkopen kost later méér
```

**Beslissing:** Alpha EMS verkoopt **niet**.

Verkopen tegen 0,29 om diezelfde energie later terug te kopen tegen 0,30 is verlies, wat
de kop van de prijs ook zegt. Hetzelfde geldt als verkopen de reserve onveilig zou maken:
dan gaat het sowieso niet door, ongeacht de prijs.

**Dit is het beste voorbeeld van waarom dit een EMS is en geen "verkoop bij hoge prijs"-
regel.** Zie je een hoge terugleverprijs voorbijgaan zonder verkoop, dan is dat vaak
precies goed.

## 9. Herplannen

```
12:00   Plan zegt: kopen om 13:00
12:30   De zon valt tegen, je verbruik loopt anders,
        of de prijzen van morgen zijn gepubliceerd
12:30   Plan zegt nu iets anders
```

Toekomstige kwartieren mogen veranderen. Dat is geen instabiliteit — het is nieuwe
informatie.

**Een kwartier dat al begonnen is, verandert niet.** Dat ligt vast tot het afloopt. Wat
er in dat kwartier niet is gekocht, wordt later ook niet ingehaald: elk kwartier heeft
zijn eigen, bevroren doel.

Na een herstart van Home Assistant wordt een lopende opdracht **gestopt**, niet
voortgezet. Alpha EMS weet dan namelijk niet meer hoeveel er binnen het lopende kwartier
al geleverd is, en doorgaan op een onbekende stand zou raden zijn. Bij het volgende
kwartier begint het plan gewoon opnieuw.

## 10. Brede laadperiode, smalle inkoopblokken

Dit is de belangrijkste van de tien, omdat de sensoren er anders verwarrend uitzien.

```
De batterij laadt:          08:30 ────────────────────► 16:30

Er wordt echt gekocht:            11:45─12:15
                                        12:45─13:00
                                              14:00─14:30
```

De batterij laadt misschien tussen 08:30 en 16:30. Dat betekent **niet** dat Alpha EMS al
die uren stroom van het net koopt. De rest van die tijd gaat er gratis zonne-energie in.

**Twee vragen, twee antwoorden:**

| Attribuut | Wat het zegt |
|---|---|
| `next_charge_projection_at` / `next_charge_projection_end_at` | Wanneer de batterij naar verwachting laadt |
| `next_grid_purchase_at` / `next_grid_purchase_end_at` | Het eerstvolgende aaneengesloten blok waarin er écht van het net gekocht wordt |

Het inkoopblok stopt bij het eerste kwartier dat niets koopt. Een later blok wordt
gewoon het volgende blok zodra het eerste voorbij is.

**Waarom is de laadperiode zo breed?** Omdat het opvangen van gratis zon (voorbeeld 5)
een laadsessie niet onderbreekt — anders zou elke wolk de sessie in tweeën knippen en
zou je twee keer de schakelkosten betalen. Op een zonnige dag loopt de laadperiode
daardoor bijna gelijk met de zonuren.

**Op de campagne zelf** staat ook hoeveel er van gekocht is en hoeveel gratis binnenkwam:
`grid_purchase_kwh` en `production_charge_kwh`, plus `grid_purchase_blocks` met het
aantal losse inkoopblokken. Zie [Sensoren en entiteiten](entities.md).

---

## Verder lezen

[Economie](economics.md) · [Zonnepanelen](solar.md) ·
[Veiligheid en bediening](control-and-safety.md) · [Diagnostiek](diagnostics.md)
