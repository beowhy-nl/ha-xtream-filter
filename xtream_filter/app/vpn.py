"""WireGuard via wireproxy: een SOCKS5-proxy op localhost die alles door de tunnel stuurt.

De add-on praat uitsluitend via deze SOCKS5-proxy met de provider. Draait de
tunnel niet, dan mislukt de verbinding: er is geen pad om de VPN heen.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from .settings import SettingsError

log = logging.getLogger("xf.vpn")

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 1080
SOCKS_URL = f"socks5://{SOCKS_HOST}:{SOCKS_PORT}"

_INTERFACE_KEYS = {"address": "Address", "privatekey": "PrivateKey", "dns": "DNS", "mtu": "MTU"}
_PEER_KEYS = {"publickey": "PublicKey", "presharedkey": "PreSharedKey",
              "endpoint": "Endpoint", "allowedips": "AllowedIPs",
              "persistentkeepalive": "PersistentKeepalive"}
_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


def parse_wg_config(text: str) -> dict:
    """Lees een WireGuard .conf en geef alleen de bekende velden terug."""
    section = None
    iface: dict[str, str] = {}
    peers: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "peer":
                peers.append({})
            continue
        if "=" not in line or section not in ("interface", "peer"):
            continue
        key, value = (p.strip() for p in line.split("=", 1))
        if "\n" in value or "\r" in value:
            continue
        if section == "interface" and key.lower() in _INTERFACE_KEYS:
            name = _INTERFACE_KEYS[key.lower()]
            iface[name] = f"{iface[name]}, {value}" if name in iface and name in ("Address", "DNS") else value
        elif section == "peer" and key.lower() in _PEER_KEYS:
            peers[-1][_PEER_KEYS[key.lower()]] = value

    if not iface.get("PrivateKey") or not _KEY_RE.match(iface["PrivateKey"]):
        raise SettingsError("WireGuard-configuratie: PrivateKey ontbreekt of is ongeldig.")
    if not iface.get("Address"):
        raise SettingsError("WireGuard-configuratie: Address ontbreekt.")
    if len(peers) != 1:
        raise SettingsError("WireGuard-configuratie: er moet precies één [Peer] in staan.")
    peer = peers[0]
    if not peer.get("PublicKey") or not _KEY_RE.match(peer["PublicKey"]):
        raise SettingsError("WireGuard-configuratie: PublicKey van de peer ontbreekt of is ongeldig.")
    if not peer.get("Endpoint"):
        raise SettingsError("WireGuard-configuratie: Endpoint ontbreekt.")
    peer.setdefault("PersistentKeepalive", "25")
    return {"interface": iface, "peer": peer}


def render_wireproxy_config(parsed: dict) -> str:
    lines = ["[Interface]"]
    lines += [f"{k} = {v}" for k, v in parsed["interface"].items()]
    lines += ["", "[Peer]"]
    lines += [f"{k} = {v}" for k, v in parsed["peer"].items()]
    lines += ["", "[Socks5]", f"BindAddress = {SOCKS_HOST}:{SOCKS_PORT}", ""]
    return "\n".join(lines)


class Vpn:
    """Beheert het wireproxy-proces."""

    def __init__(self, run_dir: Path, binary: str = "wireproxy"):
        self.conf_path = Path(run_dir) / "wireproxy.conf"
        self.binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._wg_text = ""
        self.endpoint = ""
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self._wg_text)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def apply(self, wg_text: str, enabled: bool) -> None:
        """(Her)start de tunnel met deze configuratie, of stop hem."""
        await self.stop()
        self._wg_text = ""
        self.endpoint = ""
        self.last_error = ""
        if not enabled or not wg_text.strip():
            return
        try:
            parsed = parse_wg_config(wg_text)
        except SettingsError as exc:
            self.last_error = str(exc)
            return
        if not shutil.which(self.binary):
            self.last_error = "wireproxy is niet geïnstalleerd in deze container."
            log.error(self.last_error)
            return
        self.conf_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render_wireproxy_config(parsed))
        self._wg_text = wg_text
        self.endpoint = parsed["peer"]["Endpoint"]
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        delay = 2
        while True:
            try:
                # -s: wireproxy logt zelf niets (dus ook geen bestemmingen).
                self._proc = await asyncio.create_subprocess_exec(
                    self.binary, "-s", "-c", str(self.conf_path),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                log.info("VPN-tunnel gestart (endpoint %s)", self.endpoint)
                started = asyncio.get_running_loop().time()
                _, err = await self._proc.communicate()
                code = self._proc.returncode
                self.last_error = (err or b"").decode("utf-8", "replace").strip()[-300:] \
                    or f"wireproxy stopte met code {code}"
                log.warning("VPN-tunnel gestopt: %s", self.last_error)
                if asyncio.get_running_loop().time() - started > 60:
                    delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.error("VPN-tunnel kon niet starten: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
