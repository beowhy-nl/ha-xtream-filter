# Changelog

## 0.2.3

- Kanalen, films en series zonder categorie hebben een eigen regel "Zonder
  categorie" in de lijst. Ze worden standaard getoond en vallen buiten de
  regels en de standaard voor categorieën.

## 0.2.2

- Voorvoegsels met andere scheidingstekens (zoals `┃NL┃`) en met spaties
  (zoals `┃MULTI / LINGO┃`) worden nu ook herkend.

## 0.2.1

- Het tabblad Categorieën laadt opnieuw nadat je de VPN-instellingen hebt
  opgeslagen; een oude foutmelding bleef daar staan.
- Duidelijker uitleg over het interne adres voor Cloudflare.

## 0.2.0

- Meerdere providers, elk met eigen filter en eigen inloggegevens voor de app.
- Regels op naam ("begint met" of "bevat"), die ook gelden voor categorieën die
  de provider later toevoegt.
- Per provider instelbaar of categorieën zonder regel getoond of verborgen
  worden; standaard worden ze getoond.
- Voorvoegsels in categorienamen worden herkend en zijn met één klik een regel.

## 0.1.0

- Eerste versie: categoriefilter voor live-tv, films en series, gefilterde
  tv-gids, doorverwijzing van streams en ingebouwde WireGuard-tunnel.
