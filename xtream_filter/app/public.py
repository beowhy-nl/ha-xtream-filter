"""De Xtream-API zoals de IPTV-app die ziet (poort 8899)."""
from __future__ import annotations

import hmac
import json
import logging
import time
from collections import defaultdict, deque
from urllib.parse import quote

from aiohttp import web

from .catalog import CATEGORY_ACTIONS, STREAM_ACTIONS, Catalogs
from .settings import Settings, provider_ready, proxy_ready
from .upstream import Upstream, UpstreamError

log = logging.getLogger("xf.public")

MAX_FAILURES = 10
FAILURE_WINDOW = 600
BLOCK_SECONDS = 900


class LoginGuard:
    """Blokkeert een adres tijdelijk na te veel mislukte inlogpogingen."""

    def __init__(self):
        self._failures: dict[str, deque] = defaultdict(deque)
        self._blocked: dict[str, float] = {}

    def is_blocked(self, ip: str) -> bool:
        until = self._blocked.get(ip)
        if until and until > time.time():
            return True
        self._blocked.pop(ip, None)
        return False

    def failure(self, ip: str) -> None:
        now = time.time()
        if len(self._failures) > 5000:
            self._failures.clear()
        q = self._failures[ip]
        q.append(now)
        while q and q[0] < now - FAILURE_WINDOW:
            q.popleft()
        if len(q) >= MAX_FAILURES:
            self._blocked[ip] = now + BLOCK_SECONDS
            q.clear()
            log.warning("Te veel mislukte inlogpogingen; adres tijdelijk geblokkeerd")

    def success(self, ip: str) -> None:
        self._failures.pop(ip, None)


def client_ip(request: web.Request) -> str:
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote or "?")


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class PublicApi:
    def __init__(self, settings: Settings, upstream: Upstream, catalogs: Catalogs):
        self.settings = settings
        self.upstream = upstream
        self.catalogs = catalogs
        self.guard = LoginGuard()
        self._auth_cache: dict[str, tuple[float, dict]] = {}

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_route("*", "/player_api.php", self.player_api)
        app.router.add_route("*", "/xmltv.php", self.xmltv)
        app.router.add_route("*", "/{tail:.*}", self.stream)
        return app

    # ---- algemeen -----------------------------------------------------

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if self.guard.is_blocked(client_ip(request)):
            return web.Response(status=429, text="Too many attempts")
        try:
            return await handler(request)
        except UpstreamError as exc:
            log.warning("Verzoek mislukt: %s", exc)
            return web.json_response({"error": str(exc)}, status=502)

    def _check(self, request: web.Request, username: str, password: str) -> dict | None:
        """Zoek de provider die bij deze inloggegevens van de app hoort."""
        ip = client_ip(request)
        match = None
        for prov in self.settings.providers:
            if (proxy_ready(prov) and _eq(username, prov["proxy_username"])
                    and _eq(password, prov["proxy_password"])):
                match = prov
        if match:
            self.guard.success(ip)
        else:
            self.guard.failure(ip)
        return match

    def _ua(self, request: web.Request) -> str | None:
        ua = request.headers.get("User-Agent")
        if ua:
            self.upstream.last_client_ua = ua
        return ua

    @staticmethod
    async def _params(request: web.Request) -> dict:
        params = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                params.update({k: v for k, v in (await request.post()).items()
                               if isinstance(v, str)})
            except Exception:  # noqa: BLE001
                pass
        return params

    # ---- player_api.php -----------------------------------------------

    async def player_api(self, request: web.Request) -> web.StreamResponse:
        params = await self._params(request)
        prov = self._check(request, params.get("username", ""), params.get("password", ""))
        if not prov:
            return web.json_response({"user_info": {"auth": 0}})
        catalog = self.catalogs.get(prov["id"])
        ua = self._ua(request)
        action = params.get("action", "")

        if not action:
            return web.json_response(await self._auth_info(request, prov, ua))

        if action in CATEGORY_ACTIONS:
            cats = await catalog.categories_filtered(CATEGORY_ACTIONS[action], ua)
            return web.Response(body=json.dumps(cats, ensure_ascii=False).encode("utf-8"),
                                content_type="application/json")

        if action in STREAM_ACTIONS:
            kind = STREAM_ACTIONS[action]
            cid = params.get("category_id", "")
            if cid and cid not in ("0", "*", "all"):
                status, ctype, body = await catalog.streams_for_category(kind, cid, ua)
                return web.Response(status=status, body=body,
                                    headers={"Content-Type": ctype})
            path = await catalog.streams_file(kind, ua)
            return web.FileResponse(path, headers={"Content-Type": "application/json"})

        # Al het andere (film-/serie-info, korte gids, ...) gaat ongewijzigd door.
        extra = {k: v for k, v in params.items() if k not in ("username", "password")}
        status, ctype, body = await self.upstream.api(prov, extra, ua)
        return web.Response(status=status, body=body, headers={"Content-Type": ctype})

    async def _auth_info(self, request: web.Request, prov: dict, ua: str | None) -> dict:
        now = time.time()
        cached = self._auth_cache.get(prov["id"])
        if cached and now - cached[0] < 60:
            info = json.loads(json.dumps(cached[1]))
        else:
            status, _, body = await self.upstream.api(prov, {}, ua)
            try:
                info = json.loads(body)
            except ValueError:
                info = None
            if status != 200 or not isinstance(info, dict) or "user_info" not in info:
                raise UpstreamError("Inloggen bij de provider is mislukt.")
            self._auth_cache[prov["id"]] = (now, json.loads(json.dumps(info)))
        user = info.get("user_info")
        if isinstance(user, dict):
            user["username"] = prov["proxy_username"]
            user["password"] = prov["proxy_password"]
        # De app moet ook voor streams bij deze proxy uitkomen (die stuurt dan door).
        proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
        host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
        name, _, port = host.partition(":")
        server = info.get("server_info")
        if not isinstance(server, dict):
            server = info["server_info"] = {}
        server["url"] = name
        server["server_protocol"] = proto
        if proto == "https":
            server["port"] = server["https_port"] = port or "443"
        else:
            server["port"] = port or "80"
        return info

    def reset(self) -> None:
        self._auth_cache.clear()

    # ---- xmltv.php ----------------------------------------------------

    async def xmltv(self, request: web.Request) -> web.StreamResponse:
        params = await self._params(request)
        prov = self._check(request, params.get("username", ""), params.get("password", ""))
        if not prov:
            return web.Response(status=403, text="Forbidden")
        path = await self.catalogs.get(prov["id"]).epg_file(self._ua(request))
        return web.FileResponse(path, headers={"Content-Type": "application/xml"})

    # ---- streams: alleen doorverwijzen --------------------------------

    async def stream(self, request: web.Request) -> web.StreamResponse:
        """Stuur de app door naar de provider. Er loopt geen video via deze server."""
        segments = request.rel_url.raw_path.split("/")
        query = dict(request.query)
        presented = len([s for s in segments if s]) >= 3 or "username" in query

        prov = None
        for cand in self.settings.providers:
            if not proxy_ready(cand):
                continue
            user, pw = cand["proxy_username"], cand["proxy_password"]
            for i in range(len(segments) - 1):
                if _eq(segments[i], user) and _eq(segments[i + 1], pw):
                    segments[i] = quote(cand["upstream_username"], safe="")
                    segments[i + 1] = quote(cand["upstream_password"], safe="")
                    prov = cand
                    break
            if not prov and _eq(query.get("username", ""), user) \
                    and _eq(query.get("password", ""), pw):
                query["username"] = cand["upstream_username"]
                query["password"] = cand["upstream_password"]
                prov = cand
            if prov:
                break

        if not prov:
            if presented:
                self.guard.failure(client_ip(request))
                return web.Response(status=403, text="Forbidden")
            return web.Response(status=404, text="Not found")
        self.guard.success(client_ip(request))
        if not provider_ready(prov):
            return web.Response(status=503, text="Not configured")

        target = prov["upstream_url"] + "/".join(segments)
        if query:
            target += "?" + "&".join(f"{quote(k, safe='')}={quote(v, safe='')}"
                                     for k, v in query.items())
        return web.Response(status=302, headers={"Location": target,
                                                 "Cache-Control": "no-store"})
