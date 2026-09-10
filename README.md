🇳🇱 **Nederlands** | 🇬🇧 [English](docs/en/README.md)

# Alpha EMS Manager

[![CI](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Bennie-JC/ha-alpha-ems-manager/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Bennie-JC/ha-alpha-ems-manager?include_prereleases&sort=semver)](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-41BDF5.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Een Energy Management System (EMS) in Home Assistant voor een AlphaESS-thuisbatterij
en de dynamische kwartierprijzen van Frank Energie.**

Alpha EMS Manager leert hoeveel stroom je huis normaal gebruikt, kijkt vooruit naar de
stroomprijzen en de verwachte zonneproductie, en bepaalt per kwartier wat het slimste
is om met je batterij te doen: goedkoop inkopen, gratis zon bewaren, dure netinkoop
vermijden of energie terugleveren aan het net.

---

## Wat is dit?

Een integratie die de losse onderdelen die je al hebt — je AlphaESS-batterij, je
P1-meter, je Frank Energie kwartierprijzen en (optioneel) je zonnepanelen via Solcast —
samenbrengt tot één plan.

Alpha EMS praat zelf niet met je omvormer of je slimme meter: het leest de entiteiten
die andere integraties al publiceren, rekent daar een plan mee uit, en kan dat plan
vervolgens uitvoeren.

## Waarom bestaat dit?

Een thuisbatterij die alleen je eigen zon opslaat, laat geld liggen. Met een dynamisch
contract verschilt de stroomprijs per kwartier — soms een factor drie op één dag. De
vraag is dan niet meer "is de batterij vol?", maar:

> De batterij hoeft niet zomaar vol te zijn; hij moet op het juiste moment genoeg
> energie hebben.

Dat is een vraag over je hele huis en over de rest van de dag, niet over dit moment.

## Meer dan slim laden

Een gewone slim-laadfunctie kijkt vooral naar wanneer één apparaat goedkoop kan laden.
Alpha EMS Manager kijkt naar je hele energiesysteem: je huisverbruik, de
AlphaESS-batterij, de P1-meter, de kwartierprijzen van Frank Energie, de verwachte
zonneproductie, de batterijreserve en mogelijke teruglevering.

| | Vraag |
|---|---|
| **Slim laden** | Wanneer kan ik goedkoop laden? |
| **Alpha EMS Manager** | Wat is nu én straks de slimste energiekeuze voor mijn hele huis en batterij? |

Daardoor kan Alpha EMS ook besluiten om iets *niet* te doen: goedkope stroom laten
liggen omdat er gratis zon aankomt, of een hoge terugleverprijs voorbij laten gaan omdat
je die energie vanavond zelf nodig hebt.

---

## Wat heb ik nodig?

### Vereist

**1. De AlphaESS-integratie van Hillview Lodge** →
<https://projects.hillviewlodge.ie/alphaess/>

Alpha EMS Manager communiceert **niet zelf** met je AlphaESS-omvormer. Dit pakket
levert de AlphaESS-entiteiten en de bedieningsinterface die Alpha EMS gebruikt.
Installeer en configureer het eerst, volgens hun eigen documentatie.

**2. Een helper die je zelf aanmaakt**

Maak een toggle-helper aan met precies deze naam:
`input_boolean.alpha_ems_dispatch_owner`

*Instellingen → Apparaten en diensten → Helpers → Schakelaar.* Deze helper hoort **niet**
bij het AlphaESS-pakket. Alpha EMS zet hem aan als eerste stap van een eigen opdracht en
uit als laatste stap — zo weet het welke opdracht van hemzelf is en welke jij met de hand
hebt gestart. Zonder deze helper voert Alpha EMS niets uit.

De automatisering `automation.alphaess_dispatch_reset_full` uit het AlphaESS-pakket moet
bestaan **en aan staan**.

**3. Frank Quarter Prices** → <https://github.com/Bennie-JC/ha-frank-quarter-prices>

Levert de kwartierprijzen van Frank Energie. Alpha EMS gebruikt die om te bepalen
wanneer kopen, bewaren of verkopen loont. Installeer en configureer dit **vóór** Alpha
EMS Manager — zonder Frank kun je Alpha EMS niet toevoegen.

**4. Een slimme meter / P1-meting**

Elke Home Assistant-integratie die het netvermogen publiceert voldoet — bijvoorbeeld
HomeWizard P1, DSMR of SlimmeLezer. Er is geen merk verplicht. Tijdens het instellen
kies je zelf welke sensor dat is. Alpha EMS praat niet met de meter zelf.

**5. Home Assistant**

Minimum Home Assistant version: 2025.1.0

### Optioneel

**Solcast PV Forecast** → <https://github.com/BJReplay/ha-solcast-solar>

Nodig als je wilt dat verwachte zonneproductie meetelt in het plan. Configureer de API
en je daken eerst bij Solcast zelf; daarna kies je in Alpha EMS welke locaties bij dit
systeem horen. Alpha EMS verbruikt geen extra Solcast-aanroepen van je limiet. Zonder
Solcast werkt alles behalve het vooruitkijken naar zon.

**Een EV-laderssensor** — houdt het laden van je auto buiten het geleerde huisverbruik,
zodat een laadsessie niet als normaal verbruik wordt geleerd. Het is **geen**
laadplanner: Alpha EMS start of stopt je laadpaal niet.

---

## Wat kan het?

- 🧠 leert het normale stroomverbruik van je huis
- 📅 voorspelt je verbruik voor vandaag en morgen
- 💶 gebruikt de kwartierprijzen van Frank Energie
- ☀️ houdt rekening met verwachte zonneproductie (met Solcast)
- 🔋 bewaakt de batterijreserve en de beschikbare ruimte
- 🛒 kan goedkope energie inkopen in de batterij
- ☀️ kan vrije zonne-energie in de batterij bewaren in plaats van terug te leveren
- 🏠 helpt dure netinkoop voorkomen
- 📤 kan batterij-energie verkopen bij aantrekkelijke prijzen
- 📊 laat zien wat het energieplan economisch oplevert
- 💰 houdt de terugverdientijd bij (Battery Return)
- 🧾 legt campagnes en beslissingen uit in logboek en diagnostiek

---

## Praktijkvoorbeelden

*De bedragen en tijden hieronder zijn voorbeelden om het idee te laten zien, geen belofte
over wat jouw installatie oplevert.*

**Goedkope stroom bewaren voor later** — stroom kost om 13:00 € 0,20/kWh en om 20:00
€ 0,55/kWh.

→ Alpha EMS kan om 13:00 laden, zodat je om 20:00 geen dure stroom hoeft te kopen.

**Ruimte bewaren voor de zon** — de batterij is al 80 % vol, maar Solcast verwacht veel
zon rond het middaguur.

→ Alpha EMS kan besluiten niet helemaal vol te laden vanaf het net, zodat gratis
zonne-energie later nog in de batterij past.

**Energie verkopen** — de batterij is ruim gevuld, de reserve blijft veilig en de
terugleverprijs is 's avonds hoog.

→ Alpha EMS kan dan batterij-energie verkopen aan het net.

[Bekijk meer praktijkvoorbeelden](docs/nl/how-it-works.md)

---

## Hoe werkt het?

Elk kwartier kijkt Alpha EMS naar je verwachte huisverbruik, de laadtoestand van de
batterij, de stroomprijzen, de verwachte zonneproductie, de vrije ruimte in de accu en
de reserve die de batterij moet aanhouden. Daaruit maakt het het goedkoopste plan dat
nog steeds veilig is. Veiligheid gaat altijd vóór geld: dreigt de batterij te leeg te
raken, dan koopt Alpha EMS energie — ook als dat kwartier niet het goedkoopste is.

Plannen voor de toekomst kunnen veranderen zodra de zon, het verbruik of de prijzen
veranderen. Een kwartier dat al bezig is, ligt vast en wordt niet stiekem herschreven.

→ [Hoe werkt Alpha EMS?](docs/nl/how-it-works.md)

---

## Is het veilig?

Er zijn drie standen, die je zelf kiest met de entiteit **Control Mode**:

| Stand | Wat het doet |
|---|---|
| **Off** | Alpha EMS kijkt mee en rekent, maar stuurt niets. |
| **Shadow** | Alpha EMS neemt de volledige beslissing en berekent het exacte commando — en verstuurt het niet. |
| **Live** | Toegestane acties mogen echt naar de batterij. |

**Er gebeurt niets tot je er twee keer om vraagt.** Naast *Live* moet je in de opties ook
*Allow Alpha EMS to send commands* aanzetten. Bij een nieuwe installatie staan beide uit,
en inkopen van het net en verkopen aan het net staan daarnaast apart uit.

Alpha EMS ontlaadt **nooit** om je huis te voeden en schakelt **nooit** je zonnepanelen
terug — voor beide bestaat hier geen aansturing. Een opdracht waarvan het niet kan
bewijzen dat hij van hemzelf is, laat het met rust. Begin in **Shadow** en kijk een paar
dagen mee voordat je *Live* aanzet.

→ [Veiligheid en bediening](docs/nl/control-and-safety.md)

---

## Installatie

Alpha EMS Manager is **not in the HACS default repository**, dus je voegt het toe als
*custom repository*.

1. Ga in Home Assistant naar **HACS**.
2. Open het **⋮**-menu rechtsboven → **Custom repositories**.
3. Voeg toe:
   - **Repository:** `https://github.com/Bennie-JC/ha-alpha-ems-manager`
   - **Type:** `Integration`
4. Klik **Add**, zoek daarna in HACS naar **Alpha EMS Manager** en installeer het.
   Zet **Show beta versions** aan als `1.0.0-beta.56` niet wordt aangeboden.
5. **Herstart Home Assistant.**

Aanmelding voor de standaardlijst van HACS volgt zodra er een stabiele 1.0-release is.
Handmatig kan ook: kopieer `custom_components/alpha_ems_manager/` naar je
`config/custom_components/` map en herstart.

→ [Installatie en upgraden](docs/nl/installation.md)

---

## Eerste installatie

1. Installeer en configureer het **AlphaESS-pakket** van Hillview Lodge.
2. Maak de helper `input_boolean.alpha_ems_dispatch_owner` aan.
3. Controleer dat `automation.alphaess_dispatch_reset_full` bestaat en aan staat.
4. Installeer en configureer **Frank Quarter Prices**.
5. Zorg dat je **P1-meter** in Home Assistant zichtbaar is.
6. Heb je zonnepanelen? Installeer en configureer **Solcast**.
7. Installeer **Alpha EMS Manager** en herstart.
8. Voeg het toe via **Instellingen → Apparaten en diensten → Integratie toevoegen**.
9. Kies de gevraagde sensoren en integraties, en let goed op de twee
   **sign conventions** — die bepalen of Alpha EMS laden van ontladen kan onderscheiden.
10. Zet **Control Mode** op **Shadow**, kijk een paar dagen mee, en ga pas daarna naar
    **Live**.

→ [Alle instellingen, veld voor veld](docs/nl/configuration.md)

---

## Wat je ziet in Home Assistant

Alpha EMS maakt 17 sensoren en één keuze-entiteit aan. De belangrijkste:

| Entiteit | Wat het vertelt |
|---|---|
| **Economic Action** | Wat er op dit moment economisch gebeurt |
| **Next Planned Action** | Wat er hierna gepland staat, met tijdstippen |
| **Current Campaign** | De actie die nu loopt |
| **Last Campaign Result** | Hoe de vorige actie is afgelopen |
| **Economic Value** | Wat het plan en de dag economisch opleveren |
| **Battery Return** | Hoeveel van je batterij-investering is terugverdiend |
| **Control Mode** | Off, Shadow of Live |

→ [Alle entiteiten en attributen](docs/nl/entities.md)

---

## Documentatie

| Handleiding | Wat je hier vindt |
|---|---|
| [Vereisten](docs/nl/requirements.md) | Wat je eerst nodig hebt |
| [Installatie](docs/nl/installation.md) | Alpha EMS installeren en upgraden |
| [Configuratie](docs/nl/configuration.md) | Alle instellingen stap voor stap |
| [Hoe werkt Alpha EMS?](docs/nl/how-it-works.md) | Kopen, zon, batterij en verkopen |
| [Sensoren en entiteiten](docs/nl/entities.md) | Wat elke entiteit betekent |
| [Economie](docs/nl/economics.md) | Energiewaarde, lastverschuiving en terugverdientijd |
| [Veiligheid en bediening](docs/nl/control-and-safety.md) | Off, Shadow en Live |
| [Zonnepanelen](docs/nl/solar.md) | Solcast, voorspelling en headroom |
| [Diagnostiek](docs/nl/diagnostics.md) | Waarom deed Alpha EMS dit? |
| [Problemen oplossen](docs/nl/troubleshooting.md) | Veelvoorkomende problemen |

Technische documentatie voor ontwikkelaars (Engelstalig):
[Architecture](docs/ARCHITECTURE.md) · [Trading Log](docs/TRADING_LOG.md)

---

## Beta-status en beperkingen

`1.0.0-beta.56` is een **public beta**. Laden vanaf het net en terugleveren zijn beide
op echte hardware uitgevoerd. Het leermodel is nog niet over genoeg volledige dagen
gevolgd, en nog niet door een echte zomer-/wintertijdovergang, om stabiel genoemd te
worden. Begin in Shadow en houd de eerste Live-runs in de gaten.

Verdere bekende beperkingen — waaronder een terugkerend klein verschil in de
energiebalans bij laag vermogen — staan in
[Problemen oplossen](docs/nl/troubleshooting.md).

---

## Ontwikkeling en licentie

```bash
pip install -r requirements-test.txt
python -m pytest && ruff check . && ruff format --check .
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is de technische referentie — lees dat
voordat je de leer-, opslag- of energiebalanslagen aanpast. Licentie: [MIT](LICENSE).
