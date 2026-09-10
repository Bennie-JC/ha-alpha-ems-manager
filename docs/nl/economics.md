🇳🇱 **Nederlands** | 🇬🇧 [English](../en/economics.md)

# Economie

**In één zin:** wat je batterij je oplevert, opgesplitst in delen die elkaar niet
overlappen, zodat je ze bij elkaar mag optellen.

## Waarom dit belangrijk is

"Wat levert die batterij nou op?" is een lastige vraag, omdat er meerdere antwoorden zijn
die allemaal waar zijn en iets anders betekenen. Deze pagina begint bij het eenvoudige
antwoord en gaat pas daarna dieper.

---

## Het eenvoudige verhaal: energiewaarde

Er zijn drie manieren waarop je energie geld waard is:

| Wat er gebeurt | Hoe het telt |
|---|---|
| Zon → huis | **Zelfverbruik** |
| Zon → net | **Teruglevering** |
| Batterij verplaatst energie van goedkoop naar duur, of voorkomt dure inkoop | **Lastverschuiving** |

Bij elkaar vormen die de **energiewaarde**:

```
Zelfverbruik       € 0,60
Teruglevering      € 0,15
Lastverschuiving   € 1,25
---------------------------
Energiewaarde      € 2,00
```

**Waarom je deze mag optellen:** elke kilowattuur telt in precies één rij mee. Zon die je
huis in gaat is zelfverbruik en géén teruglevering. Zon die het net op gaat is
teruglevering en géén zelfverbruik. De batterij die energie in de tijd verschuift is
lastverschuiving en niets anders. Ze zijn met opzet zo gedefinieerd dat er geen kWh
dubbel geteld wordt.

### Zelfverbruik

Zon die rechtstreeks je huis voedt, hoef je niet in te kopen. Wat het waard is, is wat je
anders had moeten betalen — de inkoopprijs op dat moment.

### Teruglevering

Zon die je niet zelf gebruikt en die naar het net gaat. Wat het waard is, is wat je
ervoor krijgt: het terugleverbedrag.

⚠️ Inkopen en terugleveren zijn **geen twee kanten van hetzelfde getal**. Aan de
inkoopkant zit een vaste opslag van ongeveer € 0,129/kWh aan inkoopmarge en energiebelasting,
en aan de teruglever­kant niet. Bij een negatieve marktprijs kost afnemen dus nog steeds
geld, terwijl terugleveren een negatief bedrag oplevert.

⚠️ **Deze regel is niet je meter.** Hij beantwoordt de vraag "wat zou deze installatie
*zonder* accu hebben verkocht". Dat moet ook zo: een deel van wat je meter het net op
stuurde kwam uit de accu, en die verkoop staat al onder lastverschuiving. Hem hier ook
meetellen zou één verkoop dubbel tellen. Wil je de meter zelf, zie
[Wat er echt is teruggeleverd](#wat-er-echt-is-teruggeleverd) hieronder.

### Lastverschuiving

Dit is het werk van de batterij zelf. Energie die 's middags goedkoop de accu in ging en
's avonds dure netinkoop voorkomt. Of energie die is ingekocht toen het goedkoop was in
plaats van op het dure moment waarop je hem nodig had.

Dit is meestal het grootste deel bij een batterij met een dynamisch contract.

---

## Wat je op de sensor ziet

Op `sensor.alpha_ems_economic_value` staan deze vier attributen, en ze tellen op:

```
realised_today_eur              wat het afgesloten deel van vandaag heeft opgeleverd
+ in_progress_interval_eur      wat het lopende kwartier tot nu toe opleverde
+ remaining_expected_today_eur  wat het plan nog verwacht vóór middernacht
+ forecast_revaluation_eur      hoeveel de energie waarmee de dag begon in waarde
                                is veranderd
= total_economic_value_today_eur
```

In één zin: **de cash van vandaag, plus wat de accu nu waard is, min wat hij waard was
toen de dag begon.**

Dit is een economische **positie**, geen geld op je rekening. De derde term is een
voorspelling en twee termen zijn waarderingen van de planner. De sensor zegt dat zelf ook,
in `accounting_basis`.

**Twee eigenschappen die je moet kennen:**

- Er wordt overal **dezelfde vergelijking** gebruikt: een huishouden zónder batterij. Niet
  "wat als de batterij dit kwartier niets deed".
- Het **lopende kwartier is een aparte term** en schuift pas naar de geschiedenis als de
  meting is afgesloten. Daardoor kan `realised_today_eur` nooit omlaag.

**Ontbreekt er één term, dan ontbreekt het totaal.** Er wordt nooit een nul ingevuld voor
iets wat onbekend is. `accounting_unavailable_reason` zegt welke term het was. De twee die
je het vaakst ziet:

- `no_opening_valuation` — de eerste dag na installeren, tot de volgende middernacht
- `horizon_short_of_midnight` — er is maar één prijsdag gepubliceerd, dus een deel van je
  dag heeft geen prijs. Los op door te wachten tot de prijzen van morgen er zijn.

---

## Wat er echt is teruggeleverd

Twee teruglevercijfers, die twee verschillende vragen beantwoorden. Beide zijn juist, en ze
door elkaar halen is de meest gemaakte fout bij het lezen van deze sensor.

| Attribuut | De vraag die het beantwoordt |
|---|---|
| `realised_export_value_eur` | Wat een installatie als de jouwe **zonder accu** verkocht zou hebben. Onderdeel van de energiewaarde-som hierboven. |
| `realised_metered_export_kwh` | Hoeveel energie er vandaag **echt je meter uit is gegaan**. |
| `realised_metered_export_revenue_eur` | Wat die energie heeft opgebracht. |

Een tegel met *Verkoop aan net — 0,49 kWh · € 0,11* wil het tweede paar.

**Wat er in het metercijfer zit:** alles wat de meter uit is gegaan, of het nu van je
panelen kwam of uit de accu. Je meter kan dat verschil niet zien, en dit cijfer is dat van
je meter.

⚠️ **De hoeveelheid is gemeten; de prijs kan gereconstrueerd zijn.** De kilowattuur komen
van je netsensor en er zit geen enkele rendementsfactor op. Het tarief waartegen ze
gewaardeerd worden is het terugleverbedrag dat per kwartier is vastgelegd, en dat leidt
Alpha EMS af uit de marktprijs en jouw ingestelde teruglevercorrectie. Het is een goed
getal om op te sturen, geen definitieve afrekening — de jaarnota van je leverancier is
bepalend voor wat je werkelijk hebt ontvangen.

**Tel het meterpaar niet op bij de energiewaarde-som.** Het staat er bewust buiten. De
drie onderdelen dekken elke kilowattuur al precies één keer, en de meteropbrengst is
dezelfde stroom onder een andere afspraak.

Een kwartier waarin is teruggeleverd zonder dat er een terugleverbedrag is vastgelegd,
wordt **overgeslagen** en niet op nul gezet. Het paar kan dus iets te laag uitvallen op
een dag met een prijsgat, en nooit te hoog.

---

## Vergelijken met de Alpha-app

De Alpha-app die bij je omvormer hoort toont zijn eigen Impact-cijfers, en die komen
**niet** regel voor regel overeen met deze attributen. Dat hoort ook zo: de twee apps
verdelen dezelfde dag langs andere lijnen. Slechts één regel is in beide op dezelfde
manier gemeten.

Deze tabel gebruikt een echte doorgemeten dag, zodat de ordes van grootte concreet zijn.

| Regel in Alpha | Vergelijk met | Wat je kunt verwachten |
|---|---|---|
| Verkopen aan net — **9,7 kWh** | `realised_metered_export_kwh` | **Zou goed moeten kloppen.** Beide zijn totalen aan de meterkant — de enige gelijkwaardige vergelijking hier. |
| Verkopen aan net — **€ 5,65** | `realised_metered_export_revenue_eur` | Dezelfde fysieke energie, maar de euro's kunnen verschillen: de twee apps kunnen het terugleverbedrag anders behandelen. |
| Zelfverbruik — **18,0 kWh** | *er wordt geen tegenhanger gepubliceerd* | **Niet direct vergelijkbaar** — zie hieronder. |
| Zelfverbruik — **€ 2,98** | `realised_self_consumption_value_eur` (**€ 2,668** op die dag) | Er is een verschil. De oorzaak staat **niet vast** — zie hieronder. |
| Lastverschuiving — **€ 2,77** | `realised_load_shifting_value_eur` | **Niet te verwachten dat dit klopt.** Andere verdelingsas. |
| Totaal — **€ 11,40** | `realised_energy_value_eur` | De dichtstbijzijnde gerealiseerde vergelijking, maar met een **ander vertrekpunt**. |
| — | `realised_export_value_eur` | **Komt met geen enkele Alpha-regel overeen.** |

### Verkopen aan net is de regel die wél moet kloppen

*Verkopen aan net* in Alpha is je **gemeten** teruglevering — alles wat eruit is gegaan,
of het van de panelen kwam of uit de accu, want een netmeter kan de herkomst niet zien.
Het is dus `realised_metered_export_kwh` die daarmee zou moeten kloppen.

⚠️ **Vergelijk hem niet met `realised_export_value_eur`.** Dat attribuut beantwoordt een
andere vraag: wat een installatie als de jouwe *zonder* accu verkocht zou hebben. Het is
het teruglever­onderdeel van de energiewaarde-som, en het komt met **geen enkele regel in
de Alpha-app** overeen.

### Zelfverbruik is nog niet te vergelijken

Op de doorgemeten dag:

```
Alpha-app          zelfverbruik                          18,0 kWh   € 2,98
Alpha EMS Manager  realised_self_consumption_value_eur              € 2,668
```

Alpha EMS Manager **publiceert geen zelfverbruik in kWh**, dus er is niets om naast de
18,0 kWh van Alpha te zetten. Zonder dat cijfer valt niet aan te tonen dat de twee definities
dezelfde energie dekken — en dat doen ze misschien ook niet: het zelfverbruik van deze
integratie is zon die is gebruikt *op het moment dat hij werd gemaakt*, en laat productie
die eerst de accu in ging bewust buiten beschouwing; die zit één keer in de
lastverschuiving. Wat de 18,0 kWh van Alpha meet, is hier niet gedocumenteerd.

Twee dingen zouden elk een deel van het euroverschil kunnen verklaren — dat de twee apps
verschillende energie meetellen, en dat ze die tegen verschillende prijzen waarderen.
**Uit de cijfers die nu worden gepubliceerd zijn die twee niet te scheiden**, dus deze
pagina wijst het verschil aan geen van beide toe. Om dat vast te stellen is een
zelfverbruikvolume aan onze kant nodig, of een onafhankelijke opgave van wat de regel van
Alpha meet.

### Lastverschuiving en het totaal

Bij **lastverschuiving** lijken de twee het minst op elkaar. De drie onderdelen van deze
integratie zijn geen verdeling naar bestemming; het zijn twee opeenvolgende
vergelijkingen — eerst wat zonnepanelen hebben veranderd, en daarna wat een accu daar
bovenop verandert. Die accuvergelijking is `realised_load_shifting_value_eur`. Waar de
regel van de Alpha-app ook uit is opgebouwd, er is geen reden waarom die twee gelijk
zouden moeten zijn, en een verschil is hier geen fout in een van beide.

**Ook de totalen beantwoorden verschillende vragen.** `realised_energy_value_eur` wordt
gemeten tegen een huishouden met *geen* zonnepanelen en *geen* accu. Het totaal van Alpha
heeft zo'n vertrekpunt niet. Het is de dichtstbijzijnde gerealiseerde vergelijking, en
het is nog steeds niet dezelfde grootheid.

⚠️ `total_economic_value_today_eur` is **niet** het cijfer om met het totaal van Alpha te
vergelijken. Daar zit ook in wat het plan vóór middernacht nog verwacht en hoe opgeslagen
energie is geherwaardeerd, en dat is geen van beide gerealiseerd geld.

---

## Drie getallen die op elkaar lijken en dat niet zijn

Dit is de meest gemaakte denkfout, dus expliciet:

| Attribuut | Wat het is | Mag je optellen? |
|---|---|---|
| `realised_today_eur` | Werkelijk gerealiseerd geld in het afgesloten deel van vandaag | Ja, met de andere drie termen |
| `total_economic_value_today_eur` | De hele positie van vandaag: de som van de vier termen | Het ís de som |
| `decision_advantage_eur` | Wat het gekozen plan **vanaf nu** beter doet dan niets doen | **Nee** |

`decision_advantage_eur` is dezelfde waarde als de toestand van de sensor. Het is een
vergelijking vooruit, geen gerealiseerde hoeveelheid, en het optellen bij de vier
positietermen levert een getal op dat niets betekent.

Er is ook `net_cash_flow_eur` in de diagnostiek: inkoop min teruglevering. Een **negatieve**
waarde betekent daar dat er geld binnenkwam. Dat is geen winst en hoort niet in een
optelling met de rest.

### Welke bedragen mag je wél optellen?

Daar is een attribuut voor: **`figure_basis`**. Dat noemt van elk bedrag op de sensor de
grondslag:

| Grondslag | Betekenis |
|---|---|
| `measured` | Gemeten, uit echte metingen en gepubliceerde prijzen |
| `attributed` | Toegerekend volgens een vaste regel |
| `estimated` | Geschat, bijvoorbeeld een voorspelling |
| `unclassified` | Nog niet ingedeeld |

Bouw je een dashboard, kijk dan hier: bedragen met verschillende grondslag bij elkaar
optellen geeft een getal waar je niets aan hebt.

---

## Waarom er geen winst per handel is

Je zou verwachten: "deze laadbeurt leverde € 0,43 op". Dat publiceert Alpha EMS bewust
niet.

Om dat te berekenen moet je weten wélke opgeslagen kilowattuur je nu verkoopt — die van
vanmorgen tegen 0,20, of die van gisteren tegen 0,31. **Een batterij houdt dat niet bij.**
Er zit geen etiket op een elektron. Elk antwoord berust dus op een afspraak die je zelf
kiest (eerst-in-eerst-uit, gemiddelde prijs, of iets anders), en verschillende afspraken
geven verschillende winsten voor exact dezelfde dag.

Een getal dat afhangt van een willekeurige keuze hoort niet naast getallen die dat niet
doen. Wat wél wordt gepubliceerd, is het gemeten resultaat over een periode — en dat is
niet van een afspraak afhankelijk.

---

## Terugverdientijd: Battery Return

`sensor.alpha_ems_battery_return` beantwoordt de andere geldvraag: **hoeveel van mijn
batterij is inmiddels terugverdiend?**

```
netto-investering = bruto investering − subsidie − overige eenmalige creditering
```

Daartegenover staat het opgetelde, werkelijk gerealiseerde voordeel van alle **afgesloten**
dagen. De sensor toont welk percentage daarvan is terugverdiend.

**Alleen gemeten geld.** Geen voorspelling, geen plannerwaardering, geen lopende dag. Een
dag telt pas mee als hij is afgesloten, en dat gebeurt één keer — daarna verandert hij
niet meer.

**Dit is rapportage, geen sturing.** Je investeringsbedrag invullen verandert niets aan
wanneer je batterij handelt. Zie [Configuratie](configuration.md#economie-economics).

**Wanneer niet beschikbaar?**

| Reden | Betekenis |
|---|---|
| `no_investment_configured` | Je hebt geen bedrag ingevuld. Iets anders dan een investering van nul. |
| `no_finalised_days` | Er is nog geen enkele dag afgesloten. |
| `no_finalised_days_in_accounting_period` | Wel afgesloten dagen, maar geen binnen de periode sinds je aankoopdatum. |

De attributen `unsealed_past_days`, `unsealed_by_reason` en `sealed_through` vertellen
welke dagen nog niet zijn meegeteld en waarom — handig als het cijfer stil lijkt te staan.

---

## Twee grenzen die je niet moet verwarren

Bij een verkoop gaat er meer uit de batterij dan er bij de meter aankomt, omdat je huis
zijn deel er eerst afhaalt.

```
Uit de batterij:  1,0 kW
Huis gebruikt:    0,3 kW
Bij de meter:     0,7 kW
```

Alpha EMS publiceert allebei, elk aan de eigen kant, en zegt er expliciet bij welke het
is: `objective_boundary` is `battery` bij een aankoop en `meter` bij een verkoop.

⚠️ **Elk gepubliceerd doel is AC.** `objective_boundary` zegt aan welke meterkant het
gemeten wordt, niet of het AC of DC is. Doe er dus **geen** rendementsfactor overheen.

---

## Verder lezen

[Sensoren en entiteiten](entities.md) · [Hoe werkt Alpha EMS?](how-it-works.md) ·
[Diagnostiek](diagnostics.md) · [Trading Log](../TRADING_LOG.md)
