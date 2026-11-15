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
2. **Instellingen → Je IPTV-provider**: vul de server-URL, gebruikersnaam en het
   wachtwoord van je provider in.
3. **Instellingen → Inloggegevens voor je IPTV-app**: kies een gebruikersnaam en
   laat een wachtwoord genereren. Deze gegevens vul je straks in je app in.
4. **Instellingen → VPN**: plak de inhoud van een WireGuard `.conf`-bestand.
   Bij Surfshark: *VPN → Manual setup → Router → WireGuard*, maak een sleutelpaar
   aan, kies een locatie en download het bestand.
5. Klik op **Instellingen opslaan** en controleer op het tabblad **Status** of de
   VPN werkt en het inloggen bij de provider lukt.
6. **Categorieën**: vink uit wat je niet wilt zien en klik op **Opslaan**. Doe dit
   voor Live-tv, Films en Series.

Zolang je voor een type nog niets hebt opgeslagen, ziet je app daar alles.

## Bereikbaar maken via Cloudflare

Voeg in je bestaande tunnel een extra subdomein toe dat verwijst naar
`http://homeassistant:8899`.

In de Cloudflared-add-on:

```yaml
additional_hosts:
  - hostname: iptv.jouwdomein.nl
    service: http://homeassistant:8899
```

Beheer je de tunnel in het Cloudflare-dashboard, voeg het daar dan toe als
*Public Hostname* met hetzelfde doel.

Zet op dit subdomein geen Cloudflare Access: je IPTV-app kan daar niet doorheen.

## Je IPTV-app instellen

Kies in de app voor een Xtream-account en vul in:

- **Server:** `https://iptv.jouwdomein.nl`
- **Gebruikersnaam en wachtwoord:** de gegevens uit stap 3

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
