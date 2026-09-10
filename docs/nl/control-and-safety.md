🇳🇱 **Nederlands** | 🇬🇧 [English](../en/control-and-safety.md)

# Veiligheid en bediening

**In één zin:** er moeten twee schakelaars aan staan voordat er ook maar iets naar je
batterij gaat, en daarna zijn er nog tien controles.

## Waarom dit belangrijk is

Alpha EMS kan je batterij echt aansturen. Deze pagina beschrijft precies wanneer wel en
wanneer niet — zodat je weet wat je aanzet, en waarom het soms niets doet.

---

## De drie standen

Je kiest de stand met de entiteit `select.alpha_ems_control_mode`.

| Getoond | Opgeslagen | Wat het doet |
|---|---|---|
| **Off** | `off` | Alpha EMS rekent en publiceert, maar stuurt niets. Loopt er nog een opdracht van hemzelf, dan wordt die netjes gestopt — daarna is het stil. |
| **Shadow** | `shadow` | De **volledige** keten draait: dezelfde vertaling, dezelfde veiligheidscontroles, dezelfde commandolijst als in Live. Alleen het versturen gebeurt niet. |
| **Live** | `active` | Toegestane acties mogen echt worden verstuurd. |

**Shadow is waar je begint.** Het beantwoordt precies de vraag die je wilt stellen: *zou
dit commando veilig zijn geweest, en wat had het exact verstuurd?* Daarom onderscheidt
`sensor.alpha_ems_control_state` ook `inhibited` (een veiligheidscontrole weigerde) van
`eligible` (er weigerde niets; alleen de stand of de schakelaar hield het tegen).

Van stand wisselen ververst het plan onmiddellijk, zonder de gebruikelijke vertraging.

Herkent Alpha EMS de opgeslagen stand niet — bijvoorbeeld na een handmatige aanpassing —
dan valt hij terug op `off`.

---

## Twee toestemmingen, en de één impliceert de ander niet

```
Control Mode = Live          ✓
        én
Allow Alpha EMS to send commands = aan    ✓
        ↓
    er mag iets verstuurd worden
```

Bij een nieuwe installatie staan **beide uit**. En daarnaast staan *Allow buying from the
grid* en *Allow selling from the battery* apart uit, dus zelfs met beide toestemmingen aan
doet een verse installatie nog niets uit zichzelf.

Zie [Configuratie](configuration.md#regeling-control).

---

## Wat er wel en niet uitgevoerd kan worden

| Actie | Uitvoerbaar? |
|---|---|
| Laden vanaf het net (`grid_charge`) | **Ja** |
| Terugleveren aan het net (`net_export`) | **Ja** |
| Ontladen om je huis te voeden (`serve_load`) | **Nee** |
| Zonneproductie terugregelen (`curtail_pv`) | **Nee** |

**Waarom `serve_load` niet?** Ontladen naar je huis is gewoon gedrag van je omvormer, en
die doet het beter dan Alpha EMS zou kunnen: de omvormer volgt je verbruik continu,
terwijl een commando met vast vermogen dat niet kan. Er is dus geen commando voor nodig.

**Waarom `curtail_pv` niet?** Daar bestaat in deze opzet geen aansturing voor.

Beide worden geweigerd, en niet op één plek: `serve_load` strandt bij de
richtingscontrole, opnieuw bij de tekencontrole en nog een keer bij het versturen zelf.
`curtail_pv` is niet eens een uitvoerbaar voornemen.

---

## De controleketen

Voordat er een commando vertrekt, moeten al deze horden genomen zijn — in deze volgorde,
en de eerste die weigert is de reden die je te zien krijgt.

**Eerst de veiligheidsbeoordeling.** Ontbrekende of onbereikbare bedieningsentiteiten,
een ontbrekende reset-automatisering, *Excess Export* of *Peak Shaving* die aan staat, een
opdracht die niet van ons is, een niet-geconfigureerde batterij, een verouderd plan,
onbruikbare of verouderde metingen, een vermogen onder het minimum of boven het maximum
van het apparaat, en bij ontladen ook een ongeldige netmeting of dreigende teruglevering.

**Daarna de toestemmingen.**

| # | Controle | Weigert met |
|---|---|---|
| 1 | Was de veiligheidsbeoordeling goed? | `unsafe` |
| 2 | Staat Control Mode op Live? | `mode_not_active` |
| 3 | Staat het versturen van commando's aan? | `execution_not_enabled` |
| 4 | Is uitvoeren in deze versie mogelijk? | `execution_unavailable` |
| 5 | Is er iets te versturen? | `no_commands` |
| 6 | Is deze richting toegestaan? | `live_action_not_permitted` |
| 7 | Is de wachttijd voorbij? | `cooldown` |

**En dan nog drie bij het versturen zelf:** opnieuw of uitvoeren mogelijk is, of alle
stappen binnen de toegestane entiteiten vallen, en of het teken en de modus bij het
voornemen passen. Die laatste drie zijn er expres — een fout in de eerste laag mag niet
volstaan om iets naar je batterij te sturen.

---

## Van wie is deze opdracht?

Dit is de kern van de veiligheid, en het is een echt probleem: het AlphaESS-pakket legt
nergens vast **wie** een opdracht heeft gestart. Of jij hem vanaf je dashboard aanzet of
Alpha EMS het doet, de achtergelaten waarden zijn identiek.

Daarom twee dingen samen: de helper `input_boolean.alpha_ems_dispatch_owner`, die als
eerste wordt aangezet en als laatste uit, én een vastgelegd verslag dat de opdracht moet
zijn begonnen op het moment dat Alpha EMS hem schreef.

Dat geeft zes toestanden:

| Toestand | Betekenis |
|---|---|
| `none` | Er loopt geen opdracht |
| `owned` | Markering aan én het verslag klopt — van ons |
| `degraded` | Markering weg, maar verslag en uitlezing kloppen nog |
| `releasing` | Onze eigen opdracht is aan het uitlopen |
| `unproven` | Markering aan, maar het verslag klopt niet |
| `foreign` | Markering uit en niet te bewijzen — **hier blijft Alpha EMS vanaf** |

⚠️ **`degraded` is nooit een synoniem voor `owned`.** Het geeft recht op precies één
schrijfactie, niet op doorgaan alsof er niets aan de hand is.

**Een opdracht waarvan Alpha EMS niet kan bewijzen dat hij van hemzelf is, wordt nooit
aangeraakt, gestopt of overschreven.** Dat is veilig, maar betekent ook dat een handmatig
gestarte laadsessie gewoon doorloopt.

---

## De dodemansknop

Een opdracht bij AlphaESS heeft een looptijd; verstrijkt die, dan valt je installatie
vanzelf terug naar normaal gedrag. Dat is je vangnet als Home Assistant of Alpha EMS zou
stoppen.

Alpha EMS zet die looptijd afwisselend op 20 en 25 minuten. Dat lijkt willekeurig maar is
het niet: het AlphaESS-pakket ververst de looptijd op een *verandering* van de waarde.
Twee keer 20 achter elkaar schrijven verandert niets, en dan zou de opdracht midden in een
laadsessie stilletjes aflopen. De bedoelde looptijd blijft ongeveer 20 minuten; de 25 is
geen langere sessie.

De controle die elke minuut het vermogen bijstuurt, **verlengt de looptijd bewust nooit**.
Anders zou een technische controle een laadsessie verlengen die de economie niet heeft
verlengd.

---

## Waarom Alpha EMS het vermogen verlaagt

Een commando wordt achtereenvolgens door acht grenzen gehaald, en de grens die
daadwerkelijk knelt is de reden die gerapporteerd wordt.

Bij laden: het vermogen van de omvormer → je ingestelde ondergrens → de dynamische
reserve → de nog toegestane netinkoop → de ruimte in de accu → de teruglever­veiligheid →
de netlimiet → afronding naar een stap die je omvormer accepteert.

Bij verkopen loopt een vergelijkbare rij, met daarin ook de resterende ontlading, het
resterende verkoopdoel en de tijd tot het einde van het kwartier.

Twee dingen mogen een commando alleen **verhogen**, nooit verlagen: het opvangen van
gratis zon, en het inhalen van een aankoop die om veiligheidsredenen verplicht was. Het
eerste koopt niets bij; het tweede blijft begrensd door wat er voor dat kwartier al was
vastgelegd.

Er wordt altijd naar **beneden** afgerond. Het uiteindelijke commando is nooit groter dan
wat gevraagd werd.

---

## Wanneer een plan wordt ingetrokken

Een lopende actie stopt als het doel is gehaald, als de accu vol is of leeg genoeg, als
het plan is ingetrokken, als het venster afloopt, als er een veiligheidssituatie ontstaat,
of als jij de stand terugzet.

Dat laatste is een echte noodrem: **Off** of **Shadow** kiezen stopt een lopende laadsessie
van Alpha EMS, niet alleen het starten van een volgende.

---

## Na een herstart

Start Home Assistant opnieuw op terwijl er een opdracht liep, dan neemt Alpha EMS die over
en **stopt hem** — hij gaat niet door.

Dat klinkt streng, maar het alternatief is erger. De voortgang binnen het lopende kwartier
wordt niet opgeslagen, dus na een herstart weet Alpha EMS niet hoeveel er al geleverd is.
Doorgaan op een onbekende stand is gokken. De kosten van stoppen zijn begrensd: hooguit de
rest van één kwartier, en bij het volgende kwartier maakt het plan gewoon een nieuwe.

**Niet elke overname is een herstart.** Loopt er een geldig plan dat precies deze opdracht
noemt, dan is dat een normale kwartierovergang en blijft de voortgang bekend.

**Kan het verslag niet bewijzen welke opdracht er loopt, dan gebeurt er niets.** Er wordt
geen stopcommando gestuurd naar een opdracht van onbekende herkomst; die laat Alpha EMS aan
de dodemansknop over.

Een herladen van de integratie gedraagt zich als een koude start.

---

## Let op: verouderde tekst in de instellingen

De omschrijvingen op de optiepagina's **Control** en **Economics** zeggen nog dat deze
versie geen commando naar je omvormer kan sturen. Die tekst stamt uit een eerdere fase en
klopt niet meer.

In *Live*, met het versturen van commando's aan, bereiken laden en terugleveren je
batterij wel degelijk. Wat op deze pagina staat, komt uit de huidige broncode en is
leidend. De teksten in de interface worden in een latere release gecorrigeerd.

---

## Verder lezen

[Configuratie](configuration.md) · [Sensoren en entiteiten](entities.md) ·
[Diagnostiek](diagnostics.md) · [Problemen oplossen](troubleshooting.md)
