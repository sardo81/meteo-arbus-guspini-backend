#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automazione robusta Simulatore Meteo Locale Arbus-Guspini v14.

Slot italiani:
08:00 verifica
10:30 crea 14:00 + 20:00
14:00 verifica
15:30 crea 20:00
20:00 verifica
21:30 crea 08:00 + 14:00 + 20:00 del giorno dopo

Questa versione NON contiene la modalità AUTOMATION_TEST.
Un run viene marcato come completato solo se l'operazione reale è riuscita.
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
CRON_VERSION = "2026-08-09-v4-1111"

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
    """Tollera fino a 9 minuti di ritardo sull'avvio Render."""
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
    meta["cronVersion"] = CRON_VERSION
    state["automationMeta"] = meta
    return state


def recovery_slot(now: datetime, completed_runs):
    """Retry a missed forecast slot later the same day.

    This lets a manual Trigger Run (or a later half-hour cron tick) recover
    a failed forecast without changing the official schedule.
    Verification slots are intentionally NOT recovered because station
    observations are time-sensitive.
    """
    mins = now.hour * 60 + now.minute
    candidates = [
        ((10, 30), "forecast_1030", 10 * 60 + 40, 13 * 60 + 29),
        ((15, 30), "forecast_1530", 15 * 60 + 40, 19 * 60 + 29),
        ((21, 30), "forecast_2130", 21 * 60 + 40, 23 * 60 + 59),
    ]
    for slot, action, start_min, end_min in candidates:
        run_key = f"{now.date().isoformat()}_{slot[0]:02d}{slot[1]:02d}"
        if start_min <= mins <= end_min and run_key not in completed_runs:
            return slot, action
    return None, None


def main() -> int:
    now = datetime.now(ROME)
    print(f"CRON VERSION {CRON_VERSION}")

    frontend = env("FRONTEND_URL")
    backend = env("BACKEND_URL")
    secret = env("AUTOMATION_SECRET")

    cloud = http_json(f"{backend}/automation/state")
    state = cloud.get("state") if isinstance(cloud.get("state"), dict) else {}
    meta = state.get("automationMeta") if isinstance(state.get("automationMeta"), dict) else {}
    completed_runs = meta.get("completedRuns") if isinstance(meta.get("completedRuns"), list) else []

    slot, action = normalized_slot(now)

    # If this is an ordinary NOOP time, recover a failed forecast slot if possible.
    if not action:
        slot, action = recovery_slot(now, completed_runs)
        if action:
            print(
                f"RECOVERY missed_slot={slot[0]:02d}:{slot[1]:02d} "
                f"triggered_at={now.strftime('%H:%M:%S')}"
            )

    if not action:
        print(f"NOOP {now.isoformat(timespec='seconds')} - nessuna operazione prevista")
        return 0

    run_key = f"{now.date().isoformat()}_{slot[0]:02d}{slot[1]:02d}"

    if run_key in completed_runs:
        print(f"SKIP {run_key} - già completato")
        return 0

    from playwright.sync_api import sync_playwright

    print(f"START {run_key} action={action}")
    history = state.get("history") if isinstance(state.get("history"), list) else []
    archive = state.get("archive") if isinstance(state.get("archive"), list) else []
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            timezone_id="Europe/Rome",
            locale="it-IT",
            service_workers="block",
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("requestfailed", lambda req: print(
            f"REQUEST FAILED method={req.method} url={req.url} error={req.failure}"
        ))
        page.on("response", lambda resp: (
            print(f"HTTP ERROR {resp.status} url={resp.url}")
            if resp.status >= 400 else None
        ))
        page.goto(frontend, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#forecastTime", timeout=30000)

        # Stato cloud -> localStorage del browser Cron.
        page.evaluate(
            """([h,a]) => {
                localStorage.setItem('meteoHistoryV5', JSON.stringify(h));
                localStorage.setItem('meteoForecastArchiveV1', JSON.stringify(a));
            }""",
            [history, archive],
        )
        page.reload(wait_until="domcontentloaded", timeout=120000)
        page.wait_for_selector("#forecastTime", timeout=30000)

        if settings:
            page.evaluate(
                """(st) => {
                    const set=(id,v)=>{
                        const e=document.getElementById(id);
                        if(e && v!==null && v!==undefined) e.value=v;
                    };
                    set('stationApiUrl', st.stationApiUrl);
                    for (const p of ['arbus','guspini']) {
                        set(`${p}-lat`, st[p]?.lat);
                        set(`${p}-lon`, st[p]?.lon);
                        set(`${p}-elev`, st[p]?.elev);
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

        def model_state():
            return page.evaluate(
                """() => ({
                    status: document.getElementById('status')?.innerText || '',
                    downloadedAt: downloadedAt || null,
                    arbusResult: !!latestResults?.arbus,
                    guspiniResult: !!latestResults?.guspini,
                    arbusGfs: !!store?.arbus?.gfs,
                    arbusEcmwf: !!store?.arbus?.ecmwf,
                    guspiniGfs: !!store?.guspini?.gfs,
                    guspiniEcmwf: !!store?.guspini?.ecmwf,
                    arbusEnsemble: !!(ensembleStore?.arbus?.gefs || ensembleStore?.arbus?.ens),
                    guspiniEnsemble: !!(ensembleStore?.guspini?.gefs || ensembleStore?.guspini?.ens)
                })"""
            )

        def clear_model_state():
            page.evaluate(
                """() => {
                    store.arbus = {};
                    store.guspini = {};
                    ensembleStore.arbus = {};
                    ensembleStore.guspini = {};
                    latestResults = {};
                    latestEnsemble = {};
                    downloadedAt = null;
                }"""
            )

        def load_models_with_retry(max_attempts: int = 3):
            last = None
            for attempt in range(1, max_attempts + 1):
                clear_model_state()
                page.locator("#status").evaluate("(e)=>e.textContent='Avvio scaricamento automatico…'")
                page.evaluate("async () => { await loadForecasts(); }")
                last = model_state()
                print(
                    f"MODELS attempt={attempt}/{max_attempts} "
                    f"status={last['status'][:300]} "
                    f"arbus={last['arbusResult']} guspini={last['guspiniResult']} "
                    f"ensA={last['arbusEnsemble']} ensG={last['guspiniEnsemble']}"
                )
                deterministic_ok = all([
                    last["arbusGfs"], last["arbusEcmwf"],
                    last["guspiniGfs"], last["guspiniEcmwf"],
                    last["arbusResult"], last["guspiniResult"],
                    bool(last["downloadedAt"]),
                ])
                ensemble_ok = last["arbusEnsemble"] and last["guspiniEnsemble"]
                if deterministic_ok and ensemble_ok:
                    return last
                if attempt < max_attempts:
                    page.wait_for_timeout(5000)
            raise RuntimeError(
                "Download modelli/spaghi non valido dopo 3 tentativi. "
                f"Ultimo stato: {(last or {}).get('status','')}"
            )

        def archive_snapshot(valid_time: str):
            return page.evaluate(
                """(valid) => {
                    const rows=JSON.parse(localStorage.getItem('meteoForecastArchiveV1')||'[]');
                    const found=rows.filter(x=>x.validTime===valid && !x.verified);
                    return {
                        total: rows.length,
                        count: found.length,
                        places: found.map(x=>x.place).sort(),
                        issueTimes: found.map(x=>x.issueTime)
                    };
                }""",
                valid_time,
            )

        def archive_one(valid_time: str):
            page.evaluate("archiveForecast()")
            status = page.locator("#status").inner_text()
            snap = archive_snapshot(valid_time)
            print(
                f"ARCHIVE {valid_time} status={status[:300]} "
                f"count={snap['count']} places={snap['places']}"
            )
            if snap["count"] != 2 or snap["places"] != ["Arbus", "Guspini"]:
                raise RuntimeError(
                    f"Archiviazione non riuscita per {valid_time}: "
                    f"status={status}; count={snap['count']}; places={snap['places']}"
                )

        def load_models_and_archive(targets):
            # Carica i modelli sulla prima scadenza; le successive usano gli stessi
            # dati scaricati ma ricalcolano l'ora desiderata.
            first = True
            for hour, day_offset in targets:
                valid_time = set_time(hour, 0, day_offset)
                if first:
                    load_models_with_retry(3)
                    first = False
                else:
                    page.evaluate("recalculate()")
                    ready = model_state()
                    if not (ready["arbusResult"] and ready["guspiniResult"]):
                        raise RuntimeError(
                            f"Ricalcolo non valido per {valid_time}: {ready['status']}"
                        )
                archive_one(valid_time)

        def station_state():
            return page.evaluate(
                """() => ({
                    status: document.getElementById('status')?.innerText || '',
                    arbusTemp: document.getElementById('arbus-obs-temp')?.value || '',
                    guspiniTemp: document.getElementById('guspini-obs-temp')?.value || ''
                })"""
            )

        def load_stations_with_retry(max_attempts: int = 3):
            last = None
            for attempt in range(1, max_attempts + 1):
                page.evaluate("async () => { await loadStationObservations(); }")
                last = station_state()
                print(
                    f"STATIONS attempt={attempt}/{max_attempts} "
                    f"status={last['status'][:300]}"
                )
                if (
                    last["status"].startswith("Stazioni aggiornate.")
                    and last["arbusTemp"]
                    and last["guspiniTemp"]
                ):
                    return last
                if attempt < max_attempts:
                    page.wait_for_timeout(5000)
            raise RuntimeError(
                "Aggiornamento stazioni non valido dopo 3 tentativi. "
                f"Ultimo stato: {(last or {}).get('status','')}"
            )

        def verify(hour: int):
            valid_time = set_time(hour)
            before = page.evaluate(
                """(valid) => {
                    const h=JSON.parse(localStorage.getItem('meteoHistoryV5')||'[]');
                    const a=JSON.parse(localStorage.getItem('meteoForecastArchiveV1')||'[]');
                    return {
                        history: h.length,
                        pending: a.filter(x=>x.validTime===valid && !x.verified).length
                    };
                }""",
                valid_time,
            )
            load_stations_with_retry(3)
            page.evaluate("saveObservation()")
            status = page.locator("#status").inner_text()
            after = page.evaluate(
                """(valid) => {
                    const h=JSON.parse(localStorage.getItem('meteoHistoryV5')||'[]');
                    const a=JSON.parse(localStorage.getItem('meteoForecastArchiveV1')||'[]');
                    return {
                        history: h.length,
                        pending: a.filter(x=>x.validTime===valid && !x.verified).length,
                        verified: a.filter(x=>x.validTime===valid && x.verified).length
                    };
                }""",
                valid_time,
            )
            print(
                f"VERIFY {valid_time} status={status[:300]} "
                f"history={before['history']}->{after['history']} "
                f"pending={before['pending']}->{after['pending']} "
                f"verified={after['verified']}"
            )
            if before["pending"] >= 2:
                if after["pending"] != 0 or after["history"] < before["history"] + 2:
                    raise RuntimeError(
                        f"Verifica incompleta per {valid_time}: {status}"
                    )
            else:
                # Non trasformiamo l'assenza di una previsione in un falso successo.
                raise RuntimeError(
                    f"Nessuna coppia di previsioni automatiche da verificare per {valid_time}."
                )

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

    # Solo ora il run viene segnato come completato e inviato al cloud.
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
        f"archive={len(new_state.get('archive', []))} "
        f"cloud={result.get('status', 'ok')}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
