"""Alle verkeer naar de IPTV-provider loopt via deze module (en dus via de VPN)."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp_socks import ProxyConnector

from .settings import Settings
from .vpn import SOCKS_URL, Vpn

log = logging.getLogger("xf.upstream")

DEFAULT_UA = "VLC/3.0.21 LibVLC/3.0.21"
IP_CHECK_URLS = ("https://1.1.1.1/cdn-cgi/trace", "https://api.ipify.org")


class UpstreamError(Exception):
    """Fout met een melding die aan de gebruiker getoond mag worden."""


class VpnBlocked(UpstreamError):
    pass


class Upstream:
    def __init__(self, settings: Settings, vpn: Vpn):
        self.settings = settings
        self.vpn = vpn
        self._sessions: dict[bool, aiohttp.ClientSession] = {}
        self.last_client_ua = ""

    def _session(self, via_vpn: bool) -> aiohttp.ClientSession:
        sess = self._sessions.get(via_vpn)
        if sess is None or sess.closed:
            connector = (ProxyConnector.from_url(SOCKS_URL, rdns=True)
                         if via_vpn else aiohttp.TCPConnector())
            sess = aiohttp.ClientSession(connector=connector, trust_env=False)
            self._sessions[via_vpn] = sess
        return sess

    def _provider_session(self) -> aiohttp.ClientSession:
        if not self.settings.upstream_ready():
            raise UpstreamError("De gegevens van de provider zijn nog niet ingevuld.")
        if self.settings["vpn_enabled"]:
            if not self.vpn.configured:
                raise VpnBlocked(
                    "De VPN staat aan, maar er is geen geldige WireGuard-configuratie. "
                    "Er wordt geen contact gemaakt met de provider.")
            return self._session(True)
        return self._session(False)

    async def close(self) -> None:
        for sess in self._sessions.values():
            await sess.close()
        self._sessions.clear()

    def _headers(self, ua: str | None) -> dict:
        return {"User-Agent": ua or self.settings["user_agent"]
                or self.last_client_ua or DEFAULT_UA,
                "Accept": "*/*"}

    def _creds(self) -> dict:
        return {"username": self.settings["upstream_username"],
                "password": self.settings["upstream_password"]}

    @staticmethod
    def _wrap(exc: Exception) -> UpstreamError:
        if isinstance(exc, asyncio.TimeoutError):
            return UpstreamError("De provider reageert niet (time-out).")
        # Bewust alleen het type fout: de tekst kan een URL met inloggegevens bevatten.
        return UpstreamError(f"De provider is niet bereikbaar ({type(exc).__name__}).")

    async def api(self, params: dict, ua: str | None = None, php: str = "player_api.php",
                  timeout: int = 120) -> tuple[int, str, bytes]:
        sess = self._provider_session()
        url = f"{self.settings['upstream_url']}/{php}"
        try:
            async with sess.get(url, params={**params, **self._creds()},
                                headers=self._headers(ua),
                                timeout=aiohttp.ClientTimeout(total=timeout, connect=30)) as resp:
                body = await resp.read()
                return resp.status, resp.headers.get("Content-Type", "application/json"), body
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc) from None

    async def download(self, php: str, params: dict, dest: Path, ua: str | None = None) -> None:
        """Download een (grote) lijst naar schijf."""
        sess = self._provider_session()
        url = f"{self.settings['upstream_url']}/{php}"
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with sess.get(url, params={**params, **self._creds()},
                                headers=self._headers(ua),
                                timeout=aiohttp.ClientTimeout(total=900, connect=30)) as resp:
                if resp.status != 200:
                    raise UpstreamError(f"De provider gaf een foutcode ({resp.status}).")
                with open(tmp, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        fh.write(chunk)
            os.replace(tmp, dest)
        except UpstreamError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            raise self._wrap(exc) from None

    async def public_ip(self, via_vpn: bool) -> dict:
        """Publiek IP-adres, rechtstreeks of via de VPN. Gaat niet naar de provider."""
        if via_vpn and not self.vpn.configured:
            return {"ok": False, "error": "VPN is niet geconfigureerd."}
        sess = self._session(via_vpn)
        for url in IP_CHECK_URLS:
            try:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    text = (await resp.text()).strip()
                if "ip=" in text:
                    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
                    return {"ok": True, "ip": fields.get("ip", ""), "country": fields.get("loc", "")}
                if text and len(text) < 64:
                    return {"ok": True, "ip": text, "country": ""}
            except Exception:  # noqa: BLE001
                continue
        return {"ok": False, "error": "Geen verbinding."}
