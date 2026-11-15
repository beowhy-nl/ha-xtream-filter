"""Tests tegen een nagebootste Xtream-provider."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.admin import AdminApi
from app.catalog import Catalog
from app.public import MAX_FAILURES, PublicApi
from app.settings import Settings, SettingsError, normalize_upstream_url
from app.upstream import Upstream
from app.vpn import Vpn, parse_wg_config, render_wireproxy_config

UP_USER, UP_PASS = "realuser", "realpass"
PX_USER, PX_PASS = "thuis", "geheim-wachtwoord1"

CATS = {
    "live": [{"category_id": "1", "category_name": "NL", "parent_id": 0},
             {"category_id": "2", "category_name": "Sport", "parent_id": 0},
             {"category_id": "3", "category_name": "Arabic", "parent_id": 0}],
    "vod": [{"category_id": "10", "category_name": "Films NL", "parent_id": 0},
            {"category_id": "11", "category_name": "Films XX", "parent_id": 0}],
    "series": [{"category_id": "20", "category_name": "Series NL", "parent_id": 0},
               {"category_id": "21", "category_name": "Series XX", "parent_id": 0}],
}
STREAMS = {
    "live": [
        {"num": 1, "name": "NPO 1 één", "stream_id": 101, "category_id": "1",
         "epg_channel_id": "NPO1.nl", "tv_archive": 1, "tv_archive_duration": 7},
        {"num": 2, "name": "ESPN", "stream_id": 102, "category_id": "2",
         "epg_channel_id": "espn.nl", "tv_archive": 0},
        {"num": 3, "name": "Al Foo", "stream_id": 103, "category_id": "3",
         "epg_channel_id": "alfoo.ar", "tv_archive": 0},
        {"num": 4, "name": "Multi", "stream_id": 104, "category_id": "3",
         "category_ids": [3, 1], "epg_channel_id": None},
    ],
    "vod": [{"name": "Film A", "stream_id": 501, "category_id": "10", "container_extension": "mkv"},
            {"name": "Film B", "stream_id": 502, "category_id": "11", "container_extension": "mp4"}],
    "series": [{"name": "Serie A", "series_id": 701, "category_id": "20"},
               {"name": "Serie B", "series_id": 702, "category_id": "21"}],
}
XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="mock">
<channel id="NPO1.nl"><display-name>NPO 1</display-name></channel>
<channel id="espn.nl"><display-name>ESPN</display-name></channel>
<channel id="alfoo.ar"><display-name>Al Foo &amp; co</display-name></channel>
<programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="NPO1.nl"><title lang="nl">Journaal</title></programme>
<programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="alfoo.ar"><title>Foo</title></programme>
</tv>
"""


class Mock:
    def __init__(self):
        self.hits = []

    async def player_api(self, request):
        q = request.query
        self.hits.append(dict(q))
        if q.get("username") != UP_USER or q.get("password") != UP_PASS:
            return web.json_response({"user_info": {"auth": 0}})
        action = q.get("action")
        if not action:
            return web.json_response({
                "user_info": {"username": UP_USER, "password": UP_PASS, "auth": 1,
                              "status": "Active", "exp_date": "1893456000",
                              "max_connections": "1", "active_cons": "0"},
                "server_info": {"url": "provider.example", "port": "8080",
                                "https_port": "8443", "server_protocol": "http"}})
        for kind, key in (("live", "get_live_categories"), ("vod", "get_vod_categories"),
                          ("series", "get_series_categories")):
            if action == key:
                return web.json_response(CATS[kind])
        for kind, key in (("live", "get_live_streams"), ("vod", "get_vod_streams"),
                          ("series", "get_series")):
            if action == key:
                items = STREAMS[kind]
                if q.get("category_id"):
                    items = [i for i in items if i["category_id"] == q["category_id"]]
                return web.Response(text=json.dumps(items, ensure_ascii=False),
                                    content_type="application/json")
        if action == "get_vod_info":
            return web.json_response({"info": {"name": "Film A"}, "vod_id": q.get("vod_id")})
        return web.json_response([])

    async def xmltv(self, request):
        self.hits.append({"xmltv": True, **request.query})
        return web.Response(text=XMLTV, content_type="application/xml")


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XF_ADMIN_OPEN", "1")
    mock = Mock()
    mapp = web.Application()
    mapp.router.add_get("/player_api.php", mock.player_api)
    mapp.router.add_get("/xmltv.php", mock.xmltv)
    mserver = TestServer(mapp)
    await mserver.start_server()
    base = f"http://127.0.0.1:{mserver.port}"

    settings = Settings(tmp_path)
    settings.update({"upstream_url": base + "/player_api.php", "upstream_username": UP_USER,
                     "upstream_password": UP_PASS, "proxy_username": PX_USER,
                     "proxy_password": PX_PASS, "vpn_enabled": False})
    vpn = Vpn(tmp_path / "run", binary="bestaat-niet")
    upstream = Upstream(settings, vpn)
    catalog = Catalog(settings, upstream)
    public = PublicApi(settings, upstream, catalog)
    admin = AdminApi(settings, vpn, upstream, catalog, public)
    pub = TestClient(TestServer(public.build()))
    adm = TestClient(TestServer(admin.build()))
    await pub.start_server()
    await adm.start_server()

    class Env:
        pass
    e = Env()
    e.mock, e.base, e.settings, e.vpn, e.upstream = mock, base, settings, vpn, upstream
    e.pub, e.adm, e.catalog = pub, adm, catalog
    yield e
    await pub.close()
    await adm.close()
    await upstream.close()
    await mserver.close()


AUTH = {"username": PX_USER, "password": PX_PASS}


async def get_json(client, **params):
    resp = await client.get("/player_api.php", params={**AUTH, **params})
    assert resp.status == 200, await resp.text()
    return json.loads(await resp.text())


async def test_url_normalisation():
    assert normalize_upstream_url("provider.tv:8080/") == "http://provider.tv:8080"
    assert normalize_upstream_url("https://p.tv/player_api.php?username=a") == "https://p.tv"


async def test_auth_response_hides_provider(env):
    resp = await env.pub.get("/player_api.php", params=AUTH,
                             headers={"X-Forwarded-Proto": "https", "Host": "iptv.voorbeeld.nl"})
    data = await resp.json()
    assert data["user_info"]["auth"] == 1
    assert data["user_info"]["username"] == PX_USER
    assert data["user_info"]["password"] == PX_PASS
    assert data["server_info"]["url"] == "iptv.voorbeeld.nl"
    assert data["server_info"]["server_protocol"] == "https"
    assert data["server_info"]["port"] == "443"
    assert UP_PASS not in json.dumps(data)


async def test_wrong_password_and_lockout(env):
    for _ in range(MAX_FAILURES):
        resp = await env.pub.get("/player_api.php",
                                 params={"username": PX_USER, "password": "fout"})
        assert (await resp.json()) == {"user_info": {"auth": 0}}
    assert env.mock.hits == []
    resp = await env.pub.get("/player_api.php", params=AUTH)
    assert resp.status == 429


async def test_unfiltered_by_default(env):
    assert len(await get_json(env.pub, action="get_live_categories")) == 3
    assert len(await get_json(env.pub, action="get_live_streams")) == 4


async def test_filtering(env):
    env.settings.set_filter("live", enabled=["1"], known=["1", "2", "3"])
    env.settings.set_filter("vod", enabled=["10"], known=["10", "11"])
    env.settings.set_filter("series", enabled=[], known=["20", "21"])

    cats = await get_json(env.pub, action="get_live_categories")
    assert [c["category_id"] for c in cats] == ["1"]
    streams = await get_json(env.pub, action="get_live_streams")
    # 101 zit in categorie 1; 104 zit via category_ids ook in 1.
    assert [s["stream_id"] for s in streams] == [101, 104]
    assert streams[0]["name"] == "NPO 1 één"
    assert streams[0]["tv_archive"] == 1 and streams[0]["tv_archive_duration"] == 7

    assert [s["stream_id"] for s in await get_json(env.pub, action="get_vod_streams")] == [501]
    assert await get_json(env.pub, action="get_series") == []
    assert await get_json(env.pub, action="get_series_categories") == []

    # Eén categorie opvragen: verborgen categorie geeft niets, zonder de provider te raken.
    before = len(env.mock.hits)
    assert await get_json(env.pub, action="get_live_streams", category_id="3") == []
    assert len(env.mock.hits) == before
    one = await get_json(env.pub, action="get_live_streams", category_id="1")
    assert [s["stream_id"] for s in one] == [101]


async def test_lists_are_cached(env):
    await get_json(env.pub, action="get_live_streams")
    await get_json(env.pub, action="get_live_streams")
    await get_json(env.pub, action="get_live_categories")
    await get_json(env.pub, action="get_live_categories")
    actions = [h.get("action") for h in env.mock.hits]
    assert actions.count("get_live_streams") == 1
    assert actions.count("get_live_categories") == 1


async def test_new_category_policy(env):
    env.settings.set_filter("live", enabled=["1"], known=["1", "2"])  # 3 is 'nieuw'
    cats = await get_json(env.pub, action="get_live_categories")
    assert [c["category_id"] for c in cats] == ["1"]
    env.settings.update({"show_new_categories": True})
    env.catalog.invalidate_filtered()
    cats = await get_json(env.pub, action="get_live_categories")
    assert [c["category_id"] for c in cats] == ["1", "3"]


async def test_passthrough_uses_provider_credentials(env):
    data = await get_json(env.pub, action="get_vod_info", vod_id="501")
    assert data["vod_id"] == "501"
    hit = env.mock.hits[-1]
    assert hit["username"] == UP_USER and hit["password"] == UP_PASS


async def test_epg_is_filtered(env):
    env.settings.set_filter("live", enabled=["1"], known=["1", "2", "3"])
    resp = await env.pub.get("/xmltv.php", params=AUTH)
    text = await resp.text()
    assert resp.status == 200
    assert 'channel id="NPO1.nl"' in text and "Journaal" in text
    assert "espn.nl" not in text and "alfoo.ar" not in text
    assert text.count("<programme") == 1
    assert text.rstrip().endswith("</tv>")
    assert (await env.pub.get("/xmltv.php", params={"username": "x", "password": "y"})).status == 403


@pytest.mark.parametrize("path", [
    "/live/{u}/{p}/101.ts",
    "/live/{u}/{p}/101.m3u8",
    "/{u}/{p}/101",
    "/movie/{u}/{p}/501.mkv",
    "/series/{u}/{p}/9001.mp4",
    "/timeshift/{u}/{p}/60/2026-01-01:20-00/101.ts",
])
async def test_streams_are_redirected_not_proxied(env, path):
    resp = await env.pub.get(path.format(u=PX_USER, p=PX_PASS), allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == env.base + path.format(u=UP_USER, p=UP_PASS)
    assert env.mock.hits == []  # de proxy zelf haalt niets op bij de provider


async def test_timeshift_php_redirect(env):
    resp = await env.pub.get("/streaming/timeshift.php", allow_redirects=False,
                             params={**AUTH, "stream": "101", "start": "2026-01-01:20-00",
                                     "duration": "60"})
    assert resp.status == 302
    loc = resp.headers["Location"]
    assert loc.startswith(env.base + "/streaming/timeshift.php?")
    assert f"username={UP_USER}" in loc and f"password={UP_PASS}" in loc and "stream=101" in loc


async def test_stream_wrong_credentials(env):
    resp = await env.pub.get(f"/live/{PX_USER}/fout/101.ts", allow_redirects=False)
    assert resp.status == 403
    assert (await env.pub.get("/favicon.ico", allow_redirects=False)).status == 404


async def test_kill_switch_without_vpn_config(env):
    env.settings.update({"vpn_enabled": True})
    resp = await env.pub.get("/player_api.php", params={**AUTH, "action": "get_live_categories"})
    assert resp.status == 502
    assert "VPN" in (await resp.json())["error"]
    assert env.mock.hits == []


async def test_kill_switch_when_tunnel_is_down(env):
    """VPN aan en geconfigureerd, maar de tunnel draait niet: niets mag naar buiten."""
    env.settings.update({"vpn_enabled": True})
    env.vpn._wg_text = "geconfigureerd"
    await env.upstream.close()
    for params in ({}, {"action": "get_live_streams"}, {"action": "get_vod_info", "vod_id": "1"}):
        resp = await env.pub.get("/player_api.php", params={**AUTH, **params})
        assert resp.status == 502
    assert (await env.pub.get("/xmltv.php", params=AUTH)).status == 502
    assert env.mock.hits == []


WG = """[Interface]
Address = 10.14.0.2/16
PrivateKey = YFAbmZzOa0O0SsCMJgHPXBd1CuQCJs1Jr0OfBhqXRU8=
DNS = 162.252.172.57, 149.154.159.92
PostUp = rm -rf /

[Peer]
PublicKey = Lxg3jAOKcBA9tGBtB6vEWMFl5LUEB6AwOpuniYn1cig=
AllowedIPs = 0.0.0.0/0
Endpoint = nl-ams.prod.surfshark.com:51820
"""


async def test_wireguard_config():
    out = render_wireproxy_config(parse_wg_config(WG))
    assert "Endpoint = nl-ams.prod.surfshark.com:51820" in out
    assert "BindAddress = 127.0.0.1:1080" in out
    assert "PersistentKeepalive = 25" in out
    assert "PostUp" not in out
    with pytest.raises(SettingsError):
        parse_wg_config("[Interface]\nAddress = 10.0.0.2/32\n")


async def test_admin_flow(env):
    state = await (await env.adm.get("/api/state")).json()
    assert state["upstream_ready"] and state["proxy_ready"]
    assert "wg_config" not in state["settings"]

    cats = await (await env.adm.get("/api/categories?type=live")).json()
    assert cats["configured"] is False and len(cats["categories"]) == 3
    counts = await (await env.adm.get("/api/counts?type=live")).json()
    assert counts == {"1": 1, "2": 1, "3": 2}

    resp = await env.adm.post("/api/categories", json={
        "type": "live", "enabled": ["2"], "known": ["1", "2", "3"]})
    assert resp.status == 200
    streams = await get_json(env.pub, action="get_live_streams")
    assert [s["stream_id"] for s in streams] == [102]

    # WireGuard-sleutel opslaan en daarna niet meer teruggeven.
    resp = await env.adm.post("/api/settings", json={"wg_config": WG})
    state = await resp.json()
    assert state["wg"]["endpoint"] == "nl-ams.prod.surfshark.com:51820"
    assert "YFAbmZz" not in json.dumps(state)
    # Leeg laten (null) behoudt de configuratie.
    state = await (await env.adm.post("/api/settings", json={"wg_config": None})).json()
    assert state["wg"] is not None

    resp = await env.adm.post("/api/settings", json={"proxy_password": "kort"})
    assert resp.status == 400

    status = await (await env.adm.get("/api/status")).json()
    assert status["account"]["ok"] is True and status["account"]["status"] == "Active"


async def test_admin_refuses_non_ingress(env, monkeypatch):
    env.adm.app  # noqa: B018
    from app import admin as admin_mod
    api = admin_mod.AdminApi(env.settings, env.vpn, env.upstream, env.catalog, None)
    api.open = False
    client = TestClient(TestServer(api.build()))
    await client.start_server()
    try:
        assert (await client.get("/api/state")).status == 403
    finally:
        await client.close()
