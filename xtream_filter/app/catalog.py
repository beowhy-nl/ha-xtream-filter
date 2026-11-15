"""Ophalen, bewaren en filteren van de lijsten van de provider."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from .settings import TYPES, Settings, decide, filters_active
from .upstream import Upstream, UpstreamError

log = logging.getLogger("xf.catalog")

ACTIONS = {
    "live": {"categories": "get_live_categories", "streams": "get_live_streams"},
    "vod": {"categories": "get_vod_categories", "streams": "get_vod_streams"},
    "series": {"categories": "get_series_categories", "streams": "get_series"},
}
CATEGORY_ACTIONS = {v["categories"]: k for k, v in ACTIONS.items()}
STREAM_ACTIONS = {v["streams"]: k for k, v in ACTIONS.items()}
_ENC = {"encoding": "utf-8", "errors": "surrogateescape"}


def item_category_ids(item: dict) -> list[str]:
    ids = []
    cid = item.get("category_id")
    if cid not in (None, ""):
        ids.append(str(cid))
    extra = item.get("category_ids")
    if isinstance(extra, list):
        ids += [str(x) for x in extra if x not in (None, "") and str(x) not in ids]
    return ids or [""]


def looks_like_array(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return False
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"[")


def filter_array_file(src: Path, dst: Path | None, allowed) -> dict:
    """Loop door een JSON-lijst, tel per categorie en schrijf de toegestane items weg.

    De items worden één voor één gelezen en ongewijzigd overgenomen, zodat ook
    lijsten van tienduizenden films weinig geheugen kosten.
    """
    text = src.read_text(**_ENC)
    decoder = json.JSONDecoder()
    n = len(text)
    i = text.index("[") + 1
    counts: dict[str, int] = defaultdict(int)
    out = None
    tmp = None
    if dst is not None:
        tmp = dst.with_name(dst.name + ".part")
        out = open(tmp, "w", **_ENC)
        out.write("[")
    first = True
    try:
        while True:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i >= n or text[i] == "]":
                break
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                cids = item_category_ids(obj)
                counts[cids[0]] += 1
                if out is not None and any(allowed(c) for c in cids):
                    if not first:
                        out.write(",")
                    out.write(text[i:end])
                    first = False
            i = end
        if out is not None:
            out.write("]")
    except BaseException:
        if out is not None:
            out.close()
            tmp.unlink(missing_ok=True)
        raise
    if out is not None:
        out.close()
        os.replace(tmp, dst)
    return dict(counts)


def filter_xmltv(src: Path, dst: Path, channel_ids: set[str]) -> None:
    """Houd alleen de zenders en programma's over van de toegestane kanalen."""
    ids = {c.lower() for c in channel_ids}
    with open(src, "rb") as probe:
        gz = probe.read(2) == b"\x1f\x8b"
    tmp = dst.with_name(dst.name + ".part")
    try:
        with (gzip.open if gz else open)(src, "rb") as fh, open(tmp, "wb") as out:
            out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            depth = 0
            root = None
            for event, el in ET.iterparse(fh, events=("start", "end")):
                if event == "start":
                    depth += 1
                    if depth == 1:
                        root = el
                        attrs = "".join(
                            f' {k}="{_xml_attr(v)}"' for k, v in el.attrib.items())
                        out.write(f"<{el.tag}{attrs}>\n".encode("utf-8"))
                    continue
                depth -= 1
                if depth != 1:
                    continue
                if el.tag == "channel":
                    keep = (el.get("id") or "").lower() in ids
                elif el.tag == "programme":
                    keep = (el.get("channel") or "").lower() in ids
                else:
                    keep = True
                if keep:
                    el.tail = "\n"
                    out.write(ET.tostring(el, encoding="unicode").encode("utf-8"))
                root.clear()
            if root is not None:
                out.write(f"</{root.tag}>\n".encode("utf-8"))
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _xml_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class Catalog:
    """De lijsten van één provider."""

    def __init__(self, settings: Settings, upstream: Upstream, pid: str):
        self.settings = settings
        self.upstream = upstream
        self.pid = pid
        self.dir = settings.data_dir / "cache" / pid
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._mem: dict[str, tuple[float, tuple]] = {}

    @property
    def prov(self) -> dict:
        return self.settings.provider(self.pid)

    # ---- hulpfuncties -------------------------------------------------

    def _fresh(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime < self.settings["cache_hours"] * 3600
        except OSError:
            return False

    def invalidate_filtered(self) -> None:
        for path in self.dir.glob("*.filtered.*"):
            path.unlink(missing_ok=True)

    def clear(self) -> None:
        if self.dir.exists():
            for path in self.dir.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
        self._mem.clear()

    def info(self) -> list[dict]:
        rows = []
        for path in sorted(self.dir.glob("*.raw.*")):
            st = path.stat()
            rows.append({"name": path.name.split(".")[0], "size": st.st_size,
                         "age_minutes": int((time.time() - st.st_mtime) / 60)})
        return rows

    async def _raw(self, name: str, php: str, params: dict, ua: str | None,
                   is_json: bool = True) -> Path:
        """Zorg dat de onbewerkte lijst op schijf staat en niet te oud is."""
        path = self.dir / f"{name}.raw.{'json' if is_json else 'xml'}"
        async with self._locks[name]:
            if self._fresh(path):
                return path
            new = path.with_name(path.name + ".new")
            try:
                await self.upstream.download(self.prov, php, params, new, ua)
                if is_json and not looks_like_array(new):
                    new.unlink(missing_ok=True)
                    raise UpstreamError("De provider gaf een onverwacht antwoord.")
                os.replace(new, path)
                (self.dir / f"{name}.counts.json").unlink(missing_ok=True)
                log.info("Lijst '%s' opnieuw opgehaald", name)
            except UpstreamError as exc:
                if not path.exists():
                    raise
                log.warning("Lijst '%s' kon niet ververst worden (%s); oude versie gebruikt",
                            name, exc)
        return path

    # ---- categorieën --------------------------------------------------

    async def _categories_raw(self, kind: str, ua: str | None) -> Path:
        action = ACTIONS[kind]["categories"]
        return await self._raw(action, "player_api.php", {"action": action}, ua)

    async def categories(self, kind: str, ua: str | None = None) -> list[dict]:
        path = await self._categories_raw(kind, ua)
        data = json.loads(path.read_text(**_ENC))
        return [c for c in data if isinstance(c, dict)]

    async def categories_filtered(self, kind: str, ua: str | None = None) -> list[dict]:
        prov = self.prov
        return [c for c in await self.categories(kind, ua)
                if decide(prov, kind, c.get("category_id"), c.get("category_name"))[0]]

    async def allowed_fn(self, kind: str, ua: str | None = None):
        """Functie category_id -> bool, op basis van regels, keuzes en standaard."""
        prov = self.prov
        known = {str(c.get("category_id")):
                 decide(prov, kind, c.get("category_id"), c.get("category_name"))[0]
                 for c in await self.categories(kind, ua)}

        def allowed(cid) -> bool:
            # Hoort een item bij geen enkele (bekende) categorie, dan geldt de
            # aparte keuze "Zonder categorie" en niet de standaard voor categorieën.
            return known.get(str(cid), prov["uncategorized"][kind])
        return allowed

    # ---- kanalen, films en series -------------------------------------

    async def streams_file(self, kind: str, ua: str | None = None) -> Path:
        """Pad naar de (gefilterde) volledige lijst van dit type."""
        action = ACTIONS[kind]["streams"]
        raw = await self._raw(action, "player_api.php", {"action": action}, ua)
        if not filters_active(self.prov, kind):
            return raw
        cats = await self._categories_raw(kind, ua)
        filtered = self.dir / f"{action}.filtered.json"
        async with self._locks[action + ":filter"]:
            newest = max(raw.stat().st_mtime, cats.stat().st_mtime)
            if filtered.exists() and filtered.stat().st_mtime >= newest:
                return filtered
            allowed = await self.allowed_fn(kind, ua)
            try:
                counts = await asyncio.to_thread(filter_array_file, raw, filtered, allowed)
            except ValueError:
                raw.unlink(missing_ok=True)
                raise UpstreamError("De lijst van de provider was onvolledig; probeer opnieuw.")
            (self.dir / f"{action}.counts.json").write_text(json.dumps(counts))
        return filtered

    async def counts(self, kind: str, ua: str | None = None) -> dict:
        action = ACTIONS[kind]["streams"]
        raw = await self._raw(action, "player_api.php", {"action": action}, ua)
        cpath = self.dir / f"{action}.counts.json"
        async with self._locks[action + ":filter"]:
            if cpath.exists() and cpath.stat().st_mtime >= raw.stat().st_mtime:
                return json.loads(cpath.read_text())
            try:
                counts = await asyncio.to_thread(filter_array_file, raw, None, None)
            except ValueError:
                raw.unlink(missing_ok=True)
                raise UpstreamError("De lijst van de provider was onvolledig; probeer opnieuw.")
            cpath.write_text(json.dumps(counts))
        return counts

    async def streams_for_category(self, kind: str, category_id: str,
                                   ua: str | None = None) -> tuple[int, str, bytes]:
        """Eén categorie opvragen: klein, dus rechtstreeks bij de provider (kort bewaard)."""
        if not (await self.allowed_fn(kind, ua))(category_id):
            return 200, "application/json", b"[]"
        key = f"{kind}:{category_id}"
        hit = self._mem.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        result = await self.upstream.api(
            self.prov, {"action": ACTIONS[kind]["streams"], "category_id": category_id}, ua)
        if result[0] == 200:
            if len(self._mem) > 200:
                self._mem.clear()
            self._mem[key] = (time.time(), result)
        return result

    # ---- tv-gids ------------------------------------------------------

    async def epg_file(self, ua: str | None = None) -> Path:
        raw = await self._raw("xmltv", "xmltv.php", {}, ua, is_json=False)
        if not filters_active(self.prov, "live"):
            return raw
        streams = await self.streams_file("live", ua)
        filtered = self.dir / "xmltv.filtered.xml"
        async with self._locks["xmltv:filter"]:
            newest = max(raw.stat().st_mtime, streams.stat().st_mtime)
            if filtered.exists() and filtered.stat().st_mtime >= newest:
                return filtered
            items = json.loads(streams.read_text(**_ENC))
            ids = {str(i["epg_channel_id"]) for i in items
                   if isinstance(i, dict) and i.get("epg_channel_id")}
            try:
                await asyncio.to_thread(filter_xmltv, raw, filtered, ids)
            except (ET.ParseError, OSError, EOFError) as exc:
                log.warning("Tv-gids kon niet gefilterd worden (%s); ongefilterd doorgegeven",
                            type(exc).__name__)
                return raw
        return filtered


class Catalogs:
    """Eén Catalog per provider."""

    def __init__(self, settings: Settings, upstream: Upstream):
        self.settings = settings
        self.upstream = upstream
        self._items: dict[str, Catalog] = {}

    def get(self, pid: str) -> Catalog:
        self.settings.provider(pid)  # bestaat hij nog?
        if pid not in self._items:
            self._items[pid] = Catalog(self.settings, self.upstream, pid)
        return self._items[pid]

    def drop(self, pid: str) -> None:
        cat = self._items.pop(pid, None) or Catalog(self.settings, self.upstream, pid)
        cat.clear()
        try:
            cat.dir.rmdir()
        except OSError:
            pass

    def clear_all(self) -> None:
        for prov in self.settings.providers:
            self.get(prov["id"]).clear()


__all__ = ["Catalog", "Catalogs", "ACTIONS", "CATEGORY_ACTIONS", "STREAM_ACTIONS", "TYPES"]
