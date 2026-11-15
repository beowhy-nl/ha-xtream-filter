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

from .catalog import Catalogs
from .public import PublicApi
from .settings import (TYPES, Settings, SettingsError, provider_ready, proxy_ready)
from .upstream import Upstream, UpstreamError
from .vpn import Vpn, parse_wg_config

log = logging.getLogger("xf.admin")
STATIC = Path(__file__).parent / "static"
INGRESS_IP = "172.30.32.2"
PUBLIC_PORT = 8899
PROVIDER_FIELDS = ("id", "name", "upstream_url", "upstream_username", "upstream_password",
                   "proxy_username", "proxy_password")


class AdminApi:
    def __init__(self, settings: Settings, vpn: Vpn, upstream: Upstream,
                 catalogs: Catalogs, public: PublicApi | None):
        self.settings = settings
        self.vpn = vpn
        self.upstream = upstream
        self.catalogs = catalogs
        self.public = public
        self.open = os.environ.get("XF_ADMIN_OPEN") == "1"

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.state)
        r.add_post("/api/settings", self.save_settings)
        r.add_post("/api/provider", self.save_provider)
        r.add_post("/api/provider/delete", self.delete_provider)
        r.add_get("/api/filter", self.get_filter)
        r.add_post("/api/filter", self.save_filter)
        r.add_get("/api/counts", self.counts)
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

    # ---- instellingen -------------------------------------------------

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
            "settings": {k: d[k] for k in ("vpn_enabled", "cache_hours", "user_agent")},
            "wg": wg,
            "hostname": socket.gethostname(),
            "port": PUBLIC_PORT,
            "providers": [{**{k: p[k] for k in PROVIDER_FIELDS},
                           "upstream_ready": provider_ready(p),
                           "proxy_ready": proxy_ready(p)}
                          for p in self.settings.providers],
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
        return web.json_response(self._state())

    async def new_password(self, request: web.Request) -> web.Response:
        return web.json_response({"password": secrets.token_urlsafe(18).replace("-", "x")
                                  .replace("_", "y")})

    # ---- providers ----------------------------------------------------

    async def save_provider(self, request: web.Request) -> web.Response:
        body = await request.json()
        prov, changed = self.settings.save_provider(body.get("id") or None, body)
        reset = bool(changed & {"upstream_url", "upstream_username", "upstream_password"})
        if reset and body.get("id"):
            self.catalogs.get(prov["id"]).clear()
        if self.public:
            self.public.reset()
        return web.json_response({**self._state(), "saved_id": prov["id"],
                                  "choices_reset": "overrides" in changed and bool(body.get("id"))})

    async def delete_provider(self, request: web.Request) -> web.Response:
        pid = (await request.json()).get("id") or ""
        self.settings.delete_provider(pid)
        self.catalogs.drop(pid)
        if self.public:
            self.public.reset()
        return web.json_response(self._state())

    # ---- filter -------------------------------------------------------

    async def get_filter(self, request: web.Request) -> web.Response:
        pid = request.query.get("provider", "")
        prov = self.settings.provider(pid)
        catalog = self.catalogs.get(pid)
        lists = await asyncio.gather(*(catalog.categories(t) for t in TYPES))
        return web.json_response({
            "default_show": prov["default_show"],
            "rules": prov["rules"],
            "overrides": prov["overrides"],
            "categories": {t: [{"id": str(c.get("category_id")),
                                "name": str(c.get("category_name") or c.get("category_id"))}
                               for c in cats] for t, cats in zip(TYPES, lists)},
        })

    async def save_filter(self, request: web.Request) -> web.Response:
        body = await request.json()
        pid = body.get("provider") or ""
        self.settings.set_filter(pid, default_show=body.get("default_show"),
                                 rules=body.get("rules"), overrides=body.get("overrides"))
        self.catalogs.get(pid).invalidate_filtered()
        return web.json_response({"ok": True})

    async def counts(self, request: web.Request) -> web.Response:
        kind = request.query.get("type", "live")
        if kind not in TYPES:
            raise SettingsError("Onbekend type.")
        catalog = self.catalogs.get(request.query.get("provider", ""))
        return web.json_response(await catalog.counts(kind))

    async def clear_cache(self, request: web.Request) -> web.Response:
        self.catalogs.clear_all()
        if self.public:
            self.public.reset()
        return web.json_response({"ok": True})

    # ---- status -------------------------------------------------------

    async def _account(self, prov: dict) -> dict | None:
        if not provider_ready(prov):
            return None
        try:
            status, _, body = await self.upstream.api(prov, {}, timeout=30)
            info = json.loads(body).get("user_info", {}) if status == 200 else {}
            if info.get("auth") not in (1, "1"):
                return {"ok": False, "error": "De provider weigert deze inloggegevens."}
            exp = info.get("exp_date")
            return {"ok": True, "status": info.get("status"),
                    "expires": (time.strftime("%d-%m-%Y", time.localtime(int(exp)))
                                if exp and str(exp).isdigit() else None),
                    "max_connections": info.get("max_connections"),
                    "active_connections": info.get("active_cons")}
        except UpstreamError as exc:
            return {"ok": False, "error": str(exc)}
        except (ValueError, AttributeError):
            return {"ok": False, "error": "De provider gaf een onverwacht antwoord."}

    async def status(self, request: web.Request) -> web.Response:
        vpn_on = self.settings["vpn_enabled"]
        provs = list(self.settings.providers)
        home, tunnel, *accounts = await asyncio.gather(
            self.upstream.public_ip(False),
            self.upstream.public_ip(True) if vpn_on else asyncio.sleep(0, None),
            *(self._account(p) for p in provs))
        vpn = {"enabled": vpn_on, "configured": self.vpn.configured,
               "running": self.vpn.running, "endpoint": self.vpn.endpoint,
               "error": self.vpn.last_error, "home": home, "tunnel": tunnel}
        if vpn_on:
            vpn["safe"] = bool(tunnel and tunnel.get("ok")
                               and not (home.get("ok") and tunnel["ip"] == home["ip"]))
        return web.json_response({"vpn": vpn, "providers": [
            {"id": p["id"], "name": p["name"], "account": acc,
             "cache": self.catalogs.get(p["id"]).info()}
            for p, acc in zip(provs, accounts)]})
