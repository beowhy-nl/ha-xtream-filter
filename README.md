# ha-xtream-filter

Home Assistant-add-on die als filter tussen een IPTV-app en een Xtream-provider
staat. Je kiest welke categorieën de app te zien krijgt. Video loopt nooit via
Home Assistant en het verkeer naar de provider gaat door een WireGuard-tunnel.

## Installeren

1. Ga in Home Assistant naar **Instellingen → Add-ons → Add-on-winkel → ⋮ →
   Repositories** en voeg het adres van deze repository toe.
2. Installeer **Xtream Filter** en start de add-on.
3. Volg de stappen in de documentatie: [xtream_filter/DOCS.md](xtream_filter/DOCS.md).

De eerste installatie duurt een paar minuten, omdat de WireGuard-client dan
wordt gebouwd.

## Ontwikkelen

```bash
pip install -r xtream_filter/requirements.txt pytest pytest-asyncio
```

```bash
pytest
```
