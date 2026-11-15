"""Beheerpagina in de Home Assistant-zijbalk (Ingress, poort 8099)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path

from aiohttp import web

from .catalog import Catalog
from .public import PublicApi
from .settings import TYPES, Settings, SettingsError
from .upstream import Upstream, UpstreamError
from .vpn import Vpn, parse_wg_config

log = logging.getLogger("xf.admin")
STATIC = Path(__file__).parent / "static"
INGRESS_IP = "172.30.32.2"
PUBLIC_PORT = 8899


class AdminApi:
    def __init__(self, settings: Settings, vpn: Vpn, upstream: Upstream,
                 catalog: Catalog, public: PublicApi):
        self.settings = settings
        self.vpn = vpn
        self.upstream = upstream
        self.catalog = catalog
        self.public = public
        self.open = os.environ.get("XF_ADMIN_OPEN") == "1"

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.state)
        r.add_post("/api/settings", self.save_settings)
        r.add_get("/api/categories", self.categories)
        r.add_get("/api/counts", self.counts)
        r.add_post("/api/categories", self.save_categories)
        r.add_get("/api/status", self.status)
        r.add_post("/api/cache/clear", self.clear_cache)
        r.add_post("/api/password", self.new_password)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        # Alleen Home Assistant (Ingress) mag bij de beheerpagina.
        if not self.open and request.remote != INGRESS_IP:
            return web.Response(status=403, text="Forbidden")
        try:
            return await handler(request)
        except (UpstreamError, SettingsError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html",
                                headers={"Cache-Control": "no-store"})

    def _state(self) -> dict:
        d = self.settings.data
        wg = None
        if d["wg_config"]:
            try:
                parsed = parse_wg_config(d["wg_config"])
                wg = {"endpoint": parsed["peer"]["Endpoint"],
                      "address": parsed["interface"]["Address"]}
            except SettingsError as exc:
                wg = {"error": str(exc)}
        return {
            "settings": {k: d[k] for k in (
                "upstream_url", "upstream_username", "upstream_password",
                "proxy_username", "proxy_password", "vpn_enabled",
                "cache_hours", "show_new_categories", "user_agent")},
            "wg": wg,
            "hostname": socket.gethostname(),
            "port": PUBLIC_PORT,
            "upstream_ready": self.settings.upstream_ready(),
            "proxy_ready": self.settings.proxy_ready(),
            "filters": {t: {"configured": d["filters"][t]["configured"],
                            "enabled": len(d["filters"][t]["enabled"]),
                            "known": len(d["filters"][t]["known"])} for t in TYPES},
        }

    async def state(self, request: web.Request) -> web.Response:
        return web.json_response(self._state())

    async def save_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("wg_config") is None:
            body.pop("wg_config", None)  # niet aangeraakt: bestaande sleutel behouden
        changed = self.settings.update(body)
        if changed & {"wg_config", "vpn_enabled"}:
            await self.vpn.apply(self.settings["wg_config"], self.settings["vpn_enabled"])
            await self.upstream.close()
        if changed & {"upstream_url", "upstream_username", "upstream_password"}:
            self.catalog.clear()
            self.public.reset()
        if "show_new_categories" in changed:
            self.catalog.invalidate_filtered()
        return web.json_response(self._state())

    async def new_password(self, request: web.Request) -> web.Response:
        return web.json_response({"password": secrets.token_urlsafe(18).replace("-", "x")
                                  .replace("_", "y")})

    async def categories(self, request: web.Request) -> web.Response:
        kind = request.query.get("type", "live")
        if kind not in TYPES:
            raise SettingsError("Onbekend type.")
        cats = await self.catalog.categories(kind)
        flt = self.settings["filters"][kind]
        allowed = self.settings.allowed_fn(kind)
        known = set(flt["known"])
        rows = [{"id": str(c.get("category_id")),
                 "name": str(c.get("category_name") or c.get("category_id")),
                 "enabled": allowed(c.get("category_id")),
                 "new": flt["configured"] and str(c.get("category_id")) not in known}
                for c in cats]
        return web.json_response({"configured": flt["configured"], "categories": rows})

    async def counts(self, request: web.Request) -> web.Response:
        kind = request.query.get("type", "live")
        if kind not in TYPES:
            raise SettingsError("Onbekend type.")
        return web.json_response(await self.catalog.counts(kind))

    async def save_categories(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.settings.set_filter(body.get("type"), body.get("enabled") or [],
                                 body.get("known") or [])
        self.catalog.invalidate_filtered()
        return web.json_response({"ok": True})

    async def clear_cache(self, request: web.Request) -> web.Response:
        self.catalog.clear()
        self.public.reset()
        return web.json_response({"ok": True})

    async def status(self, request: web.Request) -> web.Response:
        vpn_on = self.settings["vpn_enabled"]
        home, tunnel = await asyncio.gather(
            self.upstream.public_ip(False),
            self.upstream.public_ip(True) if vpn_on else asyncio.sleep(0, None))
        vpn = {"enabled": vpn_on, "configured": self.vpn.configured,
               "running": self.vpn.running, "endpoint": self.vpn.endpoint,
               "error": self.vpn.last_error, "home": home, "tunnel": tunnel}
        if vpn_on:
            vpn["safe"] = bool(tunnel and tunnel.get("ok") and home.get("ok")
                               and tunnel["ip"] != home["ip"])
        account = None
        if self.settings.upstream_ready():
            try:
                status, _, body = await self.upstream.api({}, timeout=30)
                info = json.loads(body).get("user_info", {}) if status == 200 else {}
                if info.get("auth") in (1, "1"):
                    exp = info.get("exp_date")
                    account = {"ok": True, "status": info.get("status"),
                               "expires": (time.strftime("%d-%m-%Y", time.localtime(int(exp)))
                                           if exp and str(exp).isdigit() else None),
                               "max_connections": info.get("max_connections"),
                               "active_connections": info.get("active_cons")}
                else:
                    account = {"ok": False, "error": "De provider weigert deze inloggegevens."}
            except UpstreamError as exc:
                account = {"ok": False, "error": str(exc)}
            except ValueError:
                account = {"ok": False, "error": "De provider gaf een onverwacht antwoord."}
        return web.json_response({"vpn": vpn, "account": account,
                                  "cache": self.catalog.info()})
