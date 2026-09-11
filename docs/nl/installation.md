🇳🇱 **Nederlands** | 🇬🇧 [English](../en/installation.md)

# Installatie

**In één zin:** Alpha EMS Manager installeer je via HACS als *custom repository*, en pas
daarna voeg je het toe als integratie.

## Waarom dit zo gaat

Alpha EMS Manager zit **not in the HACS default repository**. Dat betekent dat je de
repository eerst met de hand toevoegt; daarna gedraagt het zich als elke andere
HACS-integratie, inclusief updates.

Aanmelding voor de standaardlijst volgt zodra er een stabiele 1.0-release is. Zodra dat
gebeurt, kunnen de stappen hieronder korter.

---

## Voordat je begint

Controleer eerst de [Vereisten](requirements.md). In het bijzonder: **Frank Quarter
Prices moet al werken**, anders kun je Alpha EMS niet toevoegen.

---

## Installeren via HACS

1. Ga in Home Assistant naar **HACS**.
2. Open het **⋮**-menu rechtsboven → **Custom repositories**.
3. Vul in:
   - **Repository:** `https://github.com/Bennie-JC/ha-alpha-ems-manager`
   - **Type:** `Integration`
4. Klik **Add**.
5. Zoek in HACS naar **Alpha EMS Manager** en klik op **Download**.
   - Dit is een pre-release. Zet **Show beta versions** aan als `1.0.0-beta.58` niet
     wordt aangeboden.
6. **Herstart Home Assistant.**
7. Ga verder met [Configuratie](configuration.md).

## Handmatig installeren

Ook mogelijk, maar je krijgt dan geen updatemeldingen.

1. Download de broncode van de gewenste release via de
   [Releases-pagina](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases).
2. Kopieer de map `custom_components/alpha_ems_manager/` naar de map
   `config/custom_components/` van je Home Assistant, zodat het bestand
   `config/custom_components/alpha_ems_manager/manifest.json` bestaat.
3. **Herstart Home Assistant.**
4. Ga verder met [Configuratie](configuration.md).

---

## De integratie toevoegen

Na de herstart:

**Instellingen → Apparaten en diensten → Integratie toevoegen → Alpha EMS Manager**

Je doorloopt dan vier of vijf korte stappen. Zie [Configuratie](configuration.md) voor
elk veld afzonderlijk.

---

## Updaten

Update via HACS (of vervang de map bij een handmatige installatie) en herstart.

**Je geleerde geschiedenis blijft behouden.** Die staat in `.storage`, per config-entry,
en wordt bij een update niet aangeraakt. Ook je instellingen blijven staan.

Na een update is het verstandig om de release-notities te lezen: soms verandert de
betekenis van een gepubliceerde waarde, of komt er een instelling bij.

---

## Upgraden vanaf 0.1.0

**Dit kan niet ter plekke.**

De twee configuratiemodellen delen geen enkele sleutel, dus een oude entry kan niet
worden omgezet. Alpha EMS herkent zo'n entry en weigert hem te laden, met een duidelijke
foutmelding, in plaats van hem verkeerd te interpreteren.

**Wat je doet:**

1. Verwijder de integratie.
2. Voeg hem opnieuw toe.

Het oude opslagbestand (`alpha_ems_manager_learning`) wordt niet aangeraakt en kun je
met de hand verwijderen.

⚠️ **Let op:** de leergeschiedenis is gekoppeld aan de config-entry. Een nieuwe entry
begint met een **lege** geschiedenis, dus het leermodel start opnieuw op.

---

## Verwijderen

Verwijder de integratie via **Instellingen → Apparaten en diensten**. Daarmee verdwijnen
de entiteiten en de opgeslagen geschiedenis van die entry.

Draaide Alpha EMS op dat moment in *Live*? Zet dan eerst **Control Mode** op **Off** en
controleer dat er geen opdracht meer loopt. De reset-automatisering van het
AlphaESS-pakket vangt dit ook op, maar netjes afsluiten is beter.

---

## Volgende stap

[Configuratie](configuration.md) — alle instellingen, veld voor veld.
