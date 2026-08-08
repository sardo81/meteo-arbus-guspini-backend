#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automazione giornaliera Simulatore Meteo Locale Arbus-Guspini.

Il job viene avviato da Render in più combinazioni UTC. Solo le sei combinazioni
corrispondenti agli orari italiani 08:00, 10:30, 14:00, 15:30, 20:00 e 21:30
eseguono il browser; gli altri avvii terminano subito.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
TARGETS = {
    (8, 0): "verify_0800",
    (10, 30): "forecast_1030",
    (14, 0): "verify_1400",
    (15, 30): "forecast_1530",
    (20, 0): "verify_2000",
    (21, 30): "forecast_2130",
}


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variabile ambiente mancante: {name}")
    return value.rstrip("/")


def http_json(url: str, method: str = "GET", payload=None, headers=None, timeout=90):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    h = {"Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = Request(url, data=body, headers=h, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} su {url}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Connessione fallita verso {url}: {exc}") from exc


def normalized_slot(now: datetime):
    """Accetta un ritardo di avvio fino a 9 minuti rispetto allo slot Render."""
    minute = now.minute
    if 0 <= minute <= 9:
        key = (now.hour, 0)
    elif 30 <= minute <= 39:
        key = (now.hour, 30)
    else:
        return None, None
    return key, TARGETS.get(key)


def iso_local(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def merge_meta(state: dict, run_key: str) -> dict:
    meta = state.get("automationMeta") if isinstance(state.get("automationMeta"), dict) else {}
    completed = meta.get("completedRuns") if isinstance(meta.get("completedRuns"), list) else []
    completed = [x for x in completed if isinstance(x, str)][-120:]
    if run_key not in completed:
        completed.append(run_key)
    meta["completedRuns"] = completed[-120:]
    meta["lastRun"] = run_key
    meta["lastSuccessAt"] = datetime.now(ROME).isoformat(timespec="seconds")
    state["automationMeta"] = meta
    return state


def main() -> int:
    now = datetime.now(ROME)
    slot, action = normalized_slot(now)

    # Test manuale sicuro: abilitarlo con AUTOMATION_TEST=1.
    # Fuori dagli slot reali verifica Chromium + frontend + lettura cloud,
    # senza modificare archivio, storico o stato Supabase.
    test_requested = os.getenv("AUTOMATION_TEST", "").strip().lower() in {"1", "true", "yes", "on"}

    if not action and not test_requested:
        print(f"NOOP {now.isoformat(timespec='seconds')} - nessuna operazione prevista")
        return 0

    frontend = env("FRONTEND_URL")
    backend = env("BACKEND_URL")
    secret = env("AUTOMATION_SECRET") if action else ""
    run_key = (
        f"{now.date().isoformat()}_{slot[0]:02d}{slot[1]:02d}"
        if action
        else f"TEST_{now.strftime('%Y%m%d_%H%M%S')}"
    )

    cloud = http_json(f"{backend}/automation/state")
    state = cloud.get("state") if isinstance(cloud.get("state"), dict) else {}
    meta = state.get("automationMeta") if isinstance(state.get("automationMeta"), dict) else {}

    # Smoke test manuale fuori dagli slot operativi.
    if not action and test_requested:
        from playwright.sync_api import sync_playwright

        history = state.get("history") if isinstance(state.get("history"), list) else []
        archive = state.get("archive") if isinstance(state.get("archive"), list) else []
        print(
            f"TEST START {now.isoformat(timespec='seconds')} "
            f"cloud_history={len(history)} cloud_archive={len(archive)}"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(timezone_id="Europe/Rome", locale="it-IT")
            page = context.new_page()
            page.goto(frontend, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_selector("#forecastTime", timeout=30000)
            title = page.title()
            browser.close()

        print(
            f"TEST OK chromium=ok frontend=ok cloud=ok "
            f"title={title!r} history={len(history)} archive={len(archive)}"
        )
        return 0

    if run_key in (meta.get("completedRuns") or []):
        print(f"SKIP {run_key} - già completato")
        return 0

    # Importiamo Playwright solo per i sei avvii utili: i NOOP rimangono leggerissimi.
    from playwright.sync_api import sync_playwright

    print(f"START {run_key} action={action}")
    history = state.get("history") if isinstance(state.get("history"), list) else []
    archive = state.get("archive") if isinstance(state.get("archive"), list) else []
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = browser.new_context(timezone_id="Europe/Rome", locale="it-IT")
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(frontend, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#forecastTime", timeout=30000)

        # Carica lo stato cloud nel localStorage della stessa app e ricarica.
        page.evaluate(
            """([h,a]) => {
                localStorage.setItem('meteoHistoryV5', JSON.stringify(h));
                localStorage.setItem('meteoForecastArchiveV1', JSON.stringify(a));
            }""",
            [history, archive],
        )
        page.reload(wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#forecastTime", timeout=30000)

        # Ripristina impostazioni eventualmente salvate nel cloud.
        if settings:
            page.evaluate(
                """(st) => {
                    const set=(id,v)=>{const e=document.getElementById(id); if(e && v!==null && v!==undefined) e.value=v};
                    set('stationApiUrl', st.stationApiUrl);
                    for (const p of ['arbus','guspini']) {
                        set(`${p}-lat`, st[p]?.lat); set(`${p}-lon`, st[p]?.lon); set(`${p}-elev`, st[p]?.elev);
                    }
                }""",
                settings,
            )

        today = now.replace(second=0, microsecond=0)

        def set_time(hour: int, minute: int = 0, day_offset: int = 0):
            target = (today + timedelta(days=day_offset)).replace(hour=hour, minute=minute)
            value = iso_local(target)
            page.locator("#forecastTime").fill(value)
            return value

        def load_models_and_archive(targets):
            first = True
            for hour, day_offset in targets:
                value = set_time(hour, 0, day_offset)
                if first:
                    page.evaluate("async () => await loadForecasts()")
                    first = False
                else:
                    page.evaluate("recalculate()")
                page.evaluate("archiveForecast()")
                print(f"ARCHIVE {value} status={page.locator('#status').inner_text()[:220]}")

        def verify(hour: int):
            value = set_time(hour)
            page.evaluate("async () => await loadStationObservations()")
            station_status = page.locator("#status").inner_text()
            page.evaluate("saveObservation()")
            save_status = page.locator("#status").inner_text()
            print(f"VERIFY {value} stations={station_status[:220]}")
            print(f"VERIFY {value} save={save_status[:220]}")

        if action == "verify_0800":
            verify(8)
        elif action == "forecast_1030":
            load_models_and_archive([(14, 0), (20, 0)])
        elif action == "verify_1400":
            verify(14)
        elif action == "forecast_1530":
            load_models_and_archive([(20, 0)])
        elif action == "verify_2000":
            verify(20)
        elif action == "forecast_2130":
            load_models_and_archive([(8, 1), (14, 1), (20, 1)])

        new_state = page.evaluate(
            """() => ({
                history: JSON.parse(localStorage.getItem('meteoHistoryV5') || '[]'),
                archive: JSON.parse(localStorage.getItem('meteoForecastArchiveV1') || '[]'),
                settings: typeof currentSettings === 'function' ? currentSettings() : {}
            })"""
        )
        browser.close()

    # Mantiene i metadati del cloud e marca lo slot come completato solo dopo il successo.
    new_state["automationMeta"] = meta
    new_state = merge_meta(new_state, run_key)
    result = http_json(
        f"{backend}/automation/state",
        method="POST",
        payload={"state": new_state},
        headers={"X-Automation-Secret": secret},
        timeout=90,
    )
    print(
        f"DONE {run_key} history={len(new_state.get('history', []))} "
        f"archive={len(new_state.get('archive', []))} cloud={result.get('status', 'ok')}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
