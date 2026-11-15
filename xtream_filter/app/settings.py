"""Instellingen van de add-on, opgeslagen in /data/settings.json."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("XF_DATA_DIR", "/data"))
TYPES = ("live", "vod", "series")
CRED_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_RULES = 300

GLOBAL_DEFAULTS: dict = {
    "vpn_enabled": True,
    "wg_config": "",
    "cache_hours": 6,
    "user_agent": "",
}
PROVIDER_DEFAULTS: dict = {
    "id": "",
    "name": "",
    "upstream_url": "",
    "upstream_username": "",
    "upstream_password": "",
    "proxy_username": "",
    "proxy_password": "",
    # Categorieën waarvoor geen regel of handmatige keuze geldt (dus ook nieuwe).
    "default_show": True,
    # Regels op naam; de eerste die past, telt.
    "rules": [],
    # Handmatige keuze per categorie-id; gaat voor op de regels.
    "overrides": {t: {} for t in TYPES},
}
PROVIDER_TEXT = ("name", "upstream_username", "upstream_password",
                 "proxy_username", "proxy_password")


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


def provider_ready(prov: dict) -> bool:
    return bool(prov["upstream_url"] and prov["upstream_username"] and prov["upstream_password"])


def proxy_ready(prov: dict) -> bool:
    return bool(prov["proxy_username"] and prov["proxy_password"])


def decide(prov: dict, kind: str, category_id, name: str | None) -> tuple[bool, str]:
    """Is deze categorie zichtbaar, en waarom ('manual', 'rule:<n>' of 'default')?"""
    override = prov["overrides"][kind].get(str(category_id))
    if override is not None:
        return bool(override), "manual"
    lowered = (name or "").strip().lower()
    for idx, rule in enumerate(prov["rules"]):
        if rule["kind"] not in ("all", kind):
            continue
        text = rule["text"].strip().lower()
        if (rule["mode"] == "prefix" and lowered.startswith(text)) or \
                (rule["mode"] == "contains" and text in lowered):
            return rule["action"] == "show", f"rule:{idx}"
    return bool(prov["default_show"]), "default"


def filters_active(prov: dict, kind: str) -> bool:
    """False als er voor dit type niets gefilterd hoeft te worden."""
    return (not prov["default_show"] or bool(prov["overrides"][kind])
            or any(r["kind"] in ("all", kind) for r in prov["rules"]))


def clean_rules(rules) -> list[dict]:
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise SettingsError("Ongeldige lijst met regels.")
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise SettingsError("Ongeldige regel.")
        text = str(rule.get("text") or "").strip()
        if not text:
            continue
        if (len(text) > 100 or rule.get("mode") not in ("prefix", "contains")
                or rule.get("action") not in ("show", "hide")
                or rule.get("kind") not in ("all",) + TYPES):
            raise SettingsError(f"Ongeldige regel: {text[:40]}")
        out.append({"mode": rule["mode"], "text": text, "action": rule["action"],
                    "kind": rule["kind"]})
    return out


def clean_overrides(overrides) -> dict:
    out = {t: {} for t in TYPES}
    if not isinstance(overrides, dict):
        return out
    for t in TYPES:
        for cid, value in (overrides.get(t) or {}).items():
            if isinstance(value, bool) and len(str(cid)) <= 64:
                out[t][str(cid)] = value
    return out


class Settings:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.path = self.data_dir / "settings.json"
        self.data: dict = copy.deepcopy(GLOBAL_DEFAULTS)
        self.providers: list[dict] = []
        self.load()

    # ---- laden en bewaren ---------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for key, default in GLOBAL_DEFAULTS.items():
            if isinstance(stored.get(key), type(default)):
                self.data[key] = stored[key]
        for raw in stored.get("providers") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            prov = copy.deepcopy(PROVIDER_DEFAULTS)
            for key, default in PROVIDER_DEFAULTS.items():
                if key not in ("rules", "overrides") and isinstance(raw.get(key), type(default)):
                    prov[key] = raw[key]
            try:
                prov["rules"] = clean_rules(raw.get("rules") or [])
            except SettingsError:
                prov["rules"] = []
            prov["overrides"] = clean_overrides(raw.get("overrides"))
            self.providers.append(prov)

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({**self.data, "providers": self.providers}, fh, indent=1)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str):
        return self.data[key]

    # ---- algemene instellingen ----------------------------------------

    def update(self, new: dict) -> set[str]:
        """Valideer en bewaar; geeft de set van gewijzigde sleutels terug."""
        cand = dict(self.data)
        for key in ("user_agent", "wg_config"):
            if key in new:
                cand[key] = str(new[key] or "").strip()
        if "vpn_enabled" in new:
            cand["vpn_enabled"] = bool(new["vpn_enabled"])
        if "cache_hours" in new:
            try:
                cand["cache_hours"] = max(1, min(168, int(new["cache_hours"])))
            except (TypeError, ValueError):
                raise SettingsError("Bewaartijd moet een getal in uren zijn.")
        if cand["wg_config"]:
            from .vpn import parse_wg_config
            parse_wg_config(cand["wg_config"])  # gooit SettingsError
        changed = {k for k in cand if cand[k] != self.data[k]}
        self.data = cand
        self.save()
        return changed

    # ---- providers ----------------------------------------------------

    def provider(self, pid: str) -> dict:
        for prov in self.providers:
            if prov["id"] == pid:
                return prov
        raise SettingsError("Deze provider bestaat niet (meer).")

    def save_provider(self, pid: str | None, new: dict) -> tuple[dict, set[str]]:
        """Maak een provider aan of werk hem bij. Geeft (provider, gewijzigde velden)."""
        if pid:
            prov = self.provider(pid)
        else:
            prov = copy.deepcopy(PROVIDER_DEFAULTS)
            prov["id"] = secrets.token_hex(4)
        cand = dict(prov)
        for key in PROVIDER_TEXT:
            if key in new:
                cand[key] = str(new[key] or "").strip()
        if "upstream_url" in new:
            cand["upstream_url"] = normalize_upstream_url(str(new["upstream_url"] or ""))
        cand["name"] = cand["name"][:60] or f"Provider {len(self.providers) + (0 if pid else 1)}"

        if cand["proxy_username"] or cand["proxy_password"]:
            if not CRED_RE.match(cand["proxy_username"]) or not CRED_RE.match(cand["proxy_password"]):
                raise SettingsError(
                    "Gebruikersnaam en wachtwoord voor de app mogen alleen letters, "
                    "cijfers, punt, streepje en underscore bevatten.")
            if len(cand["proxy_password"]) < 10:
                raise SettingsError("Het wachtwoord voor de app moet minimaal 10 tekens zijn.")
            if any(p["id"] != cand["id"] and p["proxy_username"] == cand["proxy_username"]
                   for p in self.providers):
                raise SettingsError(
                    "Deze gebruikersnaam voor de app is al in gebruik bij een andere provider.")

        changed = {k for k in cand if cand[k] != prov[k]}
        if pid and changed & {"upstream_url", "upstream_username"}:
            # Andere provider of ander account: de categorienummers kloppen niet meer.
            # Regels op naam blijven staan.
            cand["overrides"] = {t: {} for t in TYPES}
            changed.add("overrides")
        prov.clear()
        prov.update(cand)
        if not pid:
            self.providers.append(prov)
        self.save()
        return prov, changed

    def delete_provider(self, pid: str) -> None:
        self.provider(pid)
        self.providers = [p for p in self.providers if p["id"] != pid]
        self.save()

    def set_filter(self, pid: str, default_show=None, rules=None, overrides=None) -> None:
        prov = self.provider(pid)
        if rules is not None:
            prov["rules"] = clean_rules(rules)
        if overrides is not None:
            prov["overrides"] = clean_overrides(overrides)
        if default_show is not None:
            prov["default_show"] = bool(default_show)
        self.save()
