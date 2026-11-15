"""Start de add-on: VPN-tunnel, Xtream-API (8899) en beheerpagina (8099)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .admin import PUBLIC_PORT, AdminApi
from .catalog import Catalogs
from .public import PublicApi
from .settings import Settings
from .upstream import Upstream
from .vpn import Vpn

ADMIN_PORT = 8099


def _log_level(data_dir: Path) -> int:
    level = os.environ.get("XF_LOG_LEVEL", "")
    try:
        level = level or json.loads((data_dir / "options.json").read_text())["log_level"]
    except (OSError, ValueError, KeyError):
        pass
    return getattr(logging, str(level).upper(), logging.INFO)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(level=_log_level(settings.data_dir),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("xf")

    vpn = Vpn(Path(os.environ.get("XF_RUN_DIR", "/run/xtream_filter")))
    await vpn.apply(settings["wg_config"], settings["vpn_enabled"])
    upstream = Upstream(settings, vpn)
    catalogs = Catalogs(settings, upstream)
    public = PublicApi(settings, upstream, catalogs)
    admin = AdminApi(settings, vpn, upstream, catalogs, public)

    runners = []
    for app, port in ((public.build(), int(os.environ.get("XF_PUBLIC_PORT", PUBLIC_PORT))),
                      (admin.build(), int(os.environ.get("XF_ADMIN_PORT", ADMIN_PORT)))):
        # access_log=None: er wordt niet bijgehouden wat er wordt opgevraagd.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("Xtream Filter gestart (VPN %s)",
             "aan" if settings["vpn_enabled"] else "UIT - verkeer gaat via je eigen IP-adres")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    for runner in runners:
        await runner.cleanup()
    await upstream.close()
    await vpn.stop()


if __name__ == "__main__":
    asyncio.run(main())
