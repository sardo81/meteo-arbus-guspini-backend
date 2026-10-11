"""Opt-in, date-bounded recovery of tomorrow's forecasts using the normal Cron.

The caller must set METEO_FORECAST_RECOVERY=YYYY-MM-DD:identifier on the
existing backend. No past slot is recovered and no observation is generated.
The ordinary Cron records a distinct, idempotent recovery run key.
"""
from __future__ import annotations
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.request import urlopen
from zoneinfo import ZoneInfo

log = logging.getLogger("meteo.forecast_recovery")

def recovery_identifier(requested, now):
    if not requested:
        return None
    date, separator, identifier = requested.partition(":")
    if not separator or not re.fullmatch(r"[A-Za-z0-9-]{1,48}", identifier):
        raise ValueError("Invalid recovery request")
    if date != now.astimezone(ZoneInfo("Europe/Rome")).date().isoformat():
        return None
    return identifier

def install_recovery(api):
    def run():
        try:
            identifier = recovery_identifier(os.getenv("METEO_FORECAST_RECOVERY", ""), datetime.now(ZoneInfo("Europe/Rome")))
            if not identifier:
                return
            if not os.getenv("AUTOMATION_SECRET", "").strip():
                raise RuntimeError("Automation key missing")
            port = int(os.getenv("PORT", "10000"))
            backend = f"http://127.0.0.1:{port}"
            # Startup callbacks finish before Uvicorn starts accepting requests.
            for attempt in range(30):
                try:
                    with urlopen(backend + "/health", timeout=3) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(1)
            else:
                raise RuntimeError("Backend did not become ready")
            env = os.environ.copy()
            env["BACKEND_URL"] = backend
            env["FRONTEND_URL"] = env.get("PUBLIC_APP_URL") or "https://meteo-arbus-guspini.netlify.app/"
            env["AUTOMATION_TEST"] = "0"
            log.warning("RECOVERY_START id=%s prospective_only=true", identifier)
            result = subprocess.run([sys.executable, "-u", "-c", "import sys,cron_automation; sys.exit(cron_automation.main(recovery_id=sys.argv[1]))", identifier], env=env, cwd=os.path.dirname(os.path.abspath(__file__)), timeout=420, check=False)
            log.warning("RECOVERY_EXIT id=%s code=%s", identifier, result.returncode)
        except Exception as exc:
            log.warning("RECOVERY_FAILED %s: %s", type(exc).__name__, str(exc)[:200])

    @api.on_event("startup")
    async def start_requested_recovery():
        if os.getenv("METEO_FORECAST_RECOVERY", ""):
            threading.Thread(target=run, name="meteo-prospective-recovery", daemon=True).start()
