"""Instellingen van de add-on, opgeslagen in /data/settings.json."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

DATA_DIR = Path(os.environ.get("XF_DATA_DIR", "/data"))
TYPES = ("live", "vod", "series")
CRED_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DEFAULTS: dict = {
    "upstream_url": "",
    "upstream_username": "",
    "upstream_password": "",
    "proxy_username": "",
    "proxy_password": "",
    "vpn_enabled": True,
    "wg_config": "",
    "cache_hours": 6,
    "show_new_categories": False,
    "user_agent": "",
    "filters": {t: {"configured": False, "enabled": [], "known": []} for t in TYPES},
}


class SettingsError(ValueError):
    """Ongeldige invoer; de melding is bedoeld voor de gebruiker."""


def normalize_upstream_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    url = url.split("?", 1)[0]
    url = re.sub(r"/(player_api|get|xmltv|panel_api)\.php$", "", url, flags=re.I)
    return url.rstrip("/")


class Settings:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.path = self.data_dir / "settings.json"
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for key, default in DEFAULTS.items():
            if key == "filters":
                for t in TYPES:
                    self.data["filters"][t].update((stored.get("filters") or {}).get(t) or {})
            elif key in stored and isinstance(stored[key], type(default)):
                self.data[key] = stored[key]

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str):
        return self.data[key]

    # ---- wijzigen -----------------------------------------------------

    def update(self, new: dict) -> set[str]:
        """Valideer en bewaar; geeft de set van gewijzigde sleutels terug."""
        cur = self.data
        cand = dict(cur)
        for key in ("upstream_username", "upstream_password", "proxy_username",
                    "proxy_password", "user_agent", "wg_config"):
            if key in new:
                cand[key] = str(new[key] or "").strip()
        if "upstream_url" in new:
            cand["upstream_url"] = normalize_upstream_url(str(new["upstream_url"] or ""))
        for key in ("vpn_enabled", "show_new_categories"):
            if key in new:
                cand[key] = bool(new[key])
        if "cache_hours" in new:
            try:
                cand["cache_hours"] = max(1, min(168, int(new["cache_hours"])))
            except (TypeError, ValueError):
                raise SettingsError("Bewaartijd moet een getal in uren zijn.")

        if cand["proxy_username"] or cand["proxy_password"]:
            if not CRED_RE.match(cand["proxy_username"]) or not CRED_RE.match(cand["proxy_password"]):
                raise SettingsError(
                    "Gebruikersnaam en wachtwoord voor de app mogen alleen letters, "
                    "cijfers, punt, streepje en underscore bevatten.")
            if len(cand["proxy_password"]) < 10:
                raise SettingsError("Het wachtwoord voor de app moet minimaal 10 tekens zijn.")
        if cand["wg_config"]:
            from .vpn import parse_wg_config
            parse_wg_config(cand["wg_config"])  # gooit SettingsError

        changed = {k for k in cand if cand[k] != cur[k]}
        self.data = cand
        self.save()
        return changed

    def set_filter(self, kind: str, enabled: list, known: list) -> None:
        if kind not in TYPES:
            raise SettingsError("Onbekend type.")
        known_s = sorted({str(x) for x in known})
        enabled_s = sorted({str(x) for x in enabled} & set(known_s))
        self.data["filters"][kind] = {"configured": True, "enabled": enabled_s, "known": known_s}
        self.save()

    # ---- opvragen -----------------------------------------------------

    def upstream_ready(self) -> bool:
        d = self.data
        return bool(d["upstream_url"] and d["upstream_username"] and d["upstream_password"])

    def proxy_ready(self) -> bool:
        return bool(self.data["proxy_username"] and self.data["proxy_password"])

    def allowed_fn(self, kind: str):
        """Geeft een functie category_id -> bool."""
        flt = self.data["filters"][kind]
        if not flt["configured"]:
            return lambda cid: True
        enabled = set(flt["enabled"])
        known = set(flt["known"])
        show_new = self.data["show_new_categories"]

        def allowed(cid) -> bool:
            cid = str(cid)
            if cid in enabled:
                return True
            if cid in known:
                return False
            return show_new
        return allowed

    def filters_all(self, kind: str) -> bool:
        """True als er voor dit type (nog) niets gefilterd wordt."""
        return not self.data["filters"][kind]["configured"]
