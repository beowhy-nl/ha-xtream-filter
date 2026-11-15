# Xtream Filter

Een filter tussen je IPTV-app en je Xtream-provider. Je kiest welke categorieën
(live-tv, films en series) je app te zien krijgt. De video zelf loopt nooit via
Home Assistant: je app wordt per stream doorverwezen naar de provider.

## Hoe het werkt

```
IPTV-app ──► deze add-on (lijsten, tv-gids, doorverwijzing) ──VPN──► provider
IPTV-app ═══════════════ video rechtstreeks ═══════════════════════► provider
```

- Kanalenlijsten, film- en serielijsten en de tv-gids worden gefilterd en bewaard.
- Voor elke stream (live, terugkijken, film, aflevering) krijgt de app een
  doorverwijzing (HTTP 302) naar het echte adres bij de provider.
- Al het verkeer van de add-on naar de provider gaat door een WireGuard-tunnel.
  Werkt de tunnel niet, dan gaat er niets naar de provider.

## Instellen

1. Start de add-on en open **IPTV Filter** in de zijbalk.
2. **VPN en overig**: plak de inhoud van een WireGuard `.conf`-bestand en sla op.
   Bij Surfshark: *VPN → Manual setup → Router → WireGuard*, maak een sleutelpaar
   aan, kies een locatie en download het bestand.
3. **Providers → Provider toevoegen**: vul de server-URL, gebruikersnaam en het
   wachtwoord van je provider in. Kies daaronder een gebruikersnaam voor je app
   en laat een wachtwoord genereren.
4. Controleer op het tabblad **Status** of de VPN werkt en het inloggen lukt.
5. **Categorieën**: stel in wat je app te zien krijgt (zie hieronder) en klik op
   **Opslaan**.

Zolang de VPN aanstaat en er geen werkende WireGuard-configuratie is, haalt de
add-on niets op bij de provider.

## Categorieën filteren

Voor elke categorie wordt in deze volgorde gekeken:

1. **Handmatige keuze**: een vinkje dat je zelf in de lijst hebt gezet of
   weggehaald. Dit gaat altijd voor. Met ↺ haal je de keuze weer weg.
2. **Regels op naam**: bijvoorbeeld *naam begint met `| AR |` → verbergen*. De
   bovenste regel die past, telt. Hoofdletters maken niet uit. Een regel kan
   overal gelden of alleen voor live-tv, films of series.
3. **Standaard**: wat er gebeurt met categorieën waar geen regel voor geldt. Dit
   staat op *Tonen*.

Regels en de standaard gelden ook voor categorieën die de provider later
toevoegt. Een nieuwe categorie verschijnt dus vanzelf in je app, tenzij een regel
hem verbergt.

Onder **Gevonden voorvoegsels** staan de codes die de add-on in de namen
herkent, zoals `| NL |`. Met *Tonen* of *Verbergen* maak je er direct een regel
van.

Twee manieren om het in te richten:

- **Alles tonen, behalve…**: laat de standaard op *Tonen* en maak
  verberg-regels voor de talen die je niet wilt.
- **Alleen tonen wat ik kies**: zet de standaard op *Verbergen* en maak
  toon-regels, bijvoorbeeld voor `| NL |`. Nieuwe categorieën die niet aan een
  regel voldoen, blijven dan verborgen.

## Meerdere providers

Elke provider heeft een eigen filter en eigen inloggegevens voor je app. In je
app voeg je elke provider toe als apart account, met hetzelfde serveradres en de
inloggegevens van die provider.

Vervang je een provider door een andere, pas dan bij **Providers** de gegevens
aan. In je app hoef je niets te wijzigen. Je handmatige keuzes worden dan gewist,
omdat die bij de categorienummers van de oude provider horen. Je regels op naam
blijven staan.

## Bereikbaar maken via Cloudflare

Voeg in je bestaande tunnel een extra subdomein toe dat verwijst naar
`http://homeassistant:8899`.

In de Cloudflared-add-on:

```yaml
additional_hosts:
  - hostname: iptv.jouwdomein.nl
    service: http://homeassistant:8899
```

`homeassistant` is hier de vaste interne naam waaronder add-ons Home Assistant
bereiken. Het is niet de hostnaam die je bij *Instellingen → Systeem → Netwerk*
kunt aanpassen. Je kunt ook de interne naam van de add-on zelf gebruiken; die
staat onderaan het tabblad Status.

Beheer je de tunnel in het Cloudflare-dashboard, voeg het daar dan toe als
*Public Hostname* met hetzelfde doel.

Zet op dit subdomein geen Cloudflare Access: je IPTV-app kan daar niet doorheen.

## Je IPTV-app instellen

Kies in de app voor een Xtream-account en vul in:

- **Server:** `https://iptv.jouwdomein.nl`
- **Gebruikersnaam en wachtwoord:** de gegevens voor je app uit stap 3 (ze staan
  ook op het tabblad Status)

## Beveiliging en privacy

- De beheerpagina is alleen via Home Assistant bereikbaar, niet via poort 8899.
- Na 10 mislukte inlogpogingen binnen 10 minuten wordt een adres 15 minuten
  geblokkeerd.
- De add-on houdt niet bij welke streams worden opgevraagd.
- De WireGuard-privésleutel wordt na het opslaan niet meer getoond.
- De instellingen (met het wachtwoord van je provider en de WireGuard-sleutel)
  staan in de gegevensmap van de add-on en komen dus ook in back-ups van Home
  Assistant terecht.
- Cloudflare kan de verzoeken van je app aan dit subdomein inzien, inclusief de
  doorverwijzingen naar de provider.

## Goed om te weten

- Staat Home Assistant uit, dan kan je app geen lijsten of nieuwe streams
  opvragen. Een stream die al speelt, loopt door.
- Na het wijzigen van categorieën moet je in je app de lijst verversen.
- Lijsten worden standaard 6 uur bewaard. Met **Lijsten opnieuw ophalen** op het
  tabblad Status dwing je een verse versie af.
- Het aantal gelijktijdige streams van je account verandert niet.
