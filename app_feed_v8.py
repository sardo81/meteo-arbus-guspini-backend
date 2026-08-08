#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Dict, Iterable, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import Browser, Page, Response, sync_playwright

STATIONS = {
    "IARBUS7": {
        "name": "Arbus (SU)",
        "url": "https://www.wunderground.com/dashboard/pws/IARBUS7",
    },
    "IGUSPI1": {
        "name": "Guspini (SU)",
        "url": "https://www.wunderground.com/dashboard/pws/IGUSPI1",
    },
}

VALID_WIND_DIRS = {
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
}

api = FastAPI(title="Meteo Arbus-Guspini API", version="9.0-rain-rate-accumulation")
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["*"],
)

CACHE_SECONDS = 300
_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}


# --- Archivio cloud per automazione -----------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
AUTOMATION_SECRET = os.getenv("AUTOMATION_SECRET", "").strip()
AUTOMATION_STATE_ID = "main"


def _cloud_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _supabase_request(path: str, method: str = "GET", payload: Any = None, prefer: Optional[str] = None) -> Any:
    if not _cloud_configured():
        raise RuntimeError("Archivio cloud non configurato: mancano SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Accept": "application/json",
    }
    # Le nuove chiavi Supabase sb_secret_* non sono JWT e non devono
    # essere inviate come Bearer token. Manteniamo invece compatibilita'
    # con la vecchia service_role JWT, se in futuro venisse usata.
    if not SUPABASE_SERVICE_ROLE_KEY.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    request = Request(f"{SUPABASE_URL}/rest/v1/{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Supabase non raggiungibile: {exc}") from exc


def cloud_state_get() -> Dict[str, Any]:
    rows = _supabase_request(
        f"automation_state?id=eq.{AUTOMATION_STATE_ID}&select=payload,updated_at",
        method="GET",
    )
    if not rows:
        return {"state": {"history": [], "archive": [], "settings": {}}, "updated_at": None}
    row = rows[0]
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        payload = {"history": [], "archive": [], "settings": {}}
    return {"state": payload, "updated_at": row.get("updated_at")}


def cloud_state_put(state: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "id": AUTOMATION_STATE_ID,
        "payload": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = _supabase_request(
        "automation_state?on_conflict=id",
        method="POST",
        payload=[row],
        prefer="resolution=merge-duplicates,return=representation",
    )
    return {"status": "ok", "rows": len(result or []), "updated_at": row["updated_at"]}


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def f_to_c(value: Optional[float]) -> Optional[float]:
    return None if value is None else round((value - 32.0) * 5.0 / 9.0, 1)


def mph_to_kmh(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 1.609344, 1)


def inhg_to_hpa(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 33.8638867, 1)


def inches_to_mm(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 25.4, 1)


def dismiss_cookies(page: Page) -> None:
    for label in ("Accept All", "I Accept", "Accept", "Agree", "Accetta tutto", "Accetto"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if button.count():
                button.first.click(timeout=2500)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def find_observation_lists(payloads: List[Any]) -> List[List[Dict[str, Any]]]:
    candidates: List[List[Dict[str, Any]]] = []
    for payload in payloads:
        for obj in walk_dicts(payload):
            observations = obj.get("observations")
            if isinstance(observations, list) and observations:
                clean = [x for x in observations if isinstance(x, dict)]
                if clean:
                    candidates.append(clean)
    return candidates


def observation_timestamp(obs: Dict[str, Any]) -> str:
    for key in ("obsTimeUtc", "obsTimeLocal", "validTimeUtc", "epoch", "dateTime"):
        value = obs.get(key)
        if value is not None:
            return str(value)
    return ""


def pick_latest_observation(lists: List[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    observations = [obs for group in lists for obs in group]
    if not observations:
        return None

    # Le API Weather Company restituiscono generalmente le osservazioni in
    # ordine cronologico. Come sicurezza ordiniamo usando epoch/validTimeUtc
    # quando disponibili.
    def sort_key(obs: Dict[str, Any]) -> float:
        for key in ("epoch", "validTimeUtc"):
            value = safe_float(obs.get(key))
            if value is not None:
                return value
        text = observation_timestamp(obs)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    return max(observations, key=sort_key)

def observation_epoch(obs: Dict[str, Any]) -> Optional[float]:
    for key in ("epoch", "validTimeUtc"):
        value = safe_float(obs.get(key))
        if value is not None:
            return value
    stamp = observation_timestamp(obs)
    if stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def get_numeric_wind_direction(obs: Dict[str, Any]) -> Optional[float]:
    metric = nested(obs, "metric", "metric_si")
    imperial = nested(obs, "imperial")
    uk_hybrid = nested(obs, "uk_hybrid")
    value = safe_float(first_value(
        [obs, metric, uk_hybrid, imperial],
        ("winddir", "windDirection", "wind_direction",
         "windDirectionDegrees", "windDir")
    ))
    if value is not None:
        return value % 360.0

    cardinal = first_value(
        [obs, metric, uk_hybrid, imperial],
        ("winddirCardinal", "windDirectionCardinal")
    )
    if cardinal is not None:
        return cardinal_to_degrees(str(cardinal).upper().strip())
    return None


def enrich_latest_with_wind_direction(
    latest: Dict[str, Any],
    lists: List[List[Dict[str, Any]]],
) -> tuple[Dict[str, Any], Optional[str], Optional[float]]:
    """Recupera la direzione da un feed parallelo o da un'osservazione vicina.

    WU talvolta omette winddir nel feed current ma lo include nel feed storico.
    Non sostituiamo temperatura o vento: copiamo soltanto la direzione.
    """
    if get_numeric_wind_direction(latest) is not None:
        return latest, "current_observation", 0.0

    latest_epoch = observation_epoch(latest)
    candidates: List[tuple[float, Dict[str, Any], float]] = []

    for group in lists:
        for obs in group:
            direction = get_numeric_wind_direction(obs)
            if direction is None:
                continue
            obs_epoch = observation_epoch(obs)
            if latest_epoch is None or obs_epoch is None:
                distance = 999999999.0
            else:
                distance = abs(obs_epoch - latest_epoch)
            candidates.append((distance, obs, direction))

    if not candidates:
        return latest, None, None

    candidates.sort(key=lambda item: item[0])
    distance, source_obs, direction = candidates[0]

    # Accettiamo solo dati temporalmente vicini: massimo 2 ore.
    if latest_epoch is not None and distance > 7200:
        return latest, None, None

    enriched = dict(latest)
    enriched["winddir"] = direction
    cardinal = first_value(
        [source_obs, nested(source_obs, "metric", "metric_si"),
         nested(source_obs, "uk_hybrid"), nested(source_obs, "imperial")],
        ("winddirCardinal", "windDirectionCardinal")
    )
    if cardinal is not None:
        enriched["winddirCardinal"] = str(cardinal).upper().strip()

    return enriched, "nearest_captured_observation", distance


def nested(obs: Dict[str, Any], *keys: str) -> Optional[Dict[str, Any]]:
    for key in keys:
        value = obs.get(key)
        if isinstance(value, dict):
            return value
    return None


def first_value(objects: Iterable[Optional[Dict[str, Any]]], keys: Iterable[str]) -> Any:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in keys:
            if key in obj and obj[key] is not None:
                return obj[key]
    return None


def cardinal_to_degrees(cardinal: Optional[str]) -> Optional[float]:
    """Converte una direzione cardinale WU in gradi, come ripiego."""
    if cardinal is None:
        return None
    mapping = {
        "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5,
        "E": 90.0, "ESE": 112.5, "SE": 135.0, "SSE": 157.5,
        "S": 180.0, "SSW": 202.5, "SW": 225.0, "WSW": 247.5,
        "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
    }
    return mapping.get(str(cardinal).upper().strip())



def rain_rate_mm_h(obs: Dict[str, Any]) -> Optional[float]:
    metric = nested(obs, "metric", "metric_si")
    imperial = nested(obs, "imperial")
    uk_hybrid = nested(obs, "uk_hybrid")
    value = safe_float(first_value([metric, uk_hybrid, obs], ("precipRate", "precip_rate", "rainRate")))
    if value is None:
        value = inches_to_mm(safe_float(first_value([imperial], ("precipRate", "precip_rate", "rainRate"))))
    return value


def rain_total_mm(obs: Dict[str, Any]) -> Optional[float]:
    metric = nested(obs, "metric", "metric_si")
    imperial = nested(obs, "imperial")
    uk_hybrid = nested(obs, "uk_hybrid")
    value = safe_float(first_value([metric, uk_hybrid, obs], ("precipTotal", "precip_total", "rainTotal")))
    if value is None:
        value = inches_to_mm(safe_float(first_value([imperial], ("precipTotal", "precip_total", "rainTotal"))))
    return value


def ordered_observations(lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    by_epoch: Dict[int, Dict[str, Any]] = {}
    for group in lists:
        for obs in group:
            epoch = observation_epoch(obs)
            if epoch is None:
                continue
            key = int(epoch)
            current = by_epoch.get(key)
            if current is None or len(obs) > len(current):
                by_epoch[key] = obs
    return [by_epoch[k] for k in sorted(by_epoch)]


def rain_window_stats(
    lists: List[List[Dict[str, Any]]],
    latest: Dict[str, Any],
    hours: int,
) -> Dict[str, Any]:
    latest_epoch = observation_epoch(latest)
    if latest_epoch is None:
        return {"accumulation_mm": None, "max_rate_mm_h": None, "complete": False, "samples": 0}

    start_epoch = latest_epoch - hours * 3600
    observations = ordered_observations(lists)
    baseline: Optional[Dict[str, Any]] = None
    window: List[Dict[str, Any]] = []

    for obs in observations:
        epoch = observation_epoch(obs)
        if epoch is None or epoch > latest_epoch + 60:
            continue
        if epoch <= start_epoch:
            baseline = obs
        elif epoch <= latest_epoch + 60:
            window.append(obs)

    sequence: List[Dict[str, Any]] = []
    if baseline is not None:
        sequence.append(baseline)
    sequence.extend(window)

    points: List[tuple[float, float]] = []
    for obs in sequence:
        epoch = observation_epoch(obs)
        total = rain_total_mm(obs)
        if epoch is not None and total is not None and total >= 0:
            points.append((epoch, total))

    accumulation: Optional[float] = None
    if len(points) >= 2:
        total_acc = 0.0
        previous = points[0][1]
        for _, current in points[1:]:
            if current + 0.05 >= previous:
                delta = max(0.0, current - previous)
            else:
                # Il contatore giornaliero si è azzerato a mezzanotte.
                delta = max(0.0, current)
            if delta <= 500:
                total_acc += delta
            previous = current
        accumulation = round(total_acc, 2)

    rates: List[float] = []
    for obs in window:
        rate = rain_rate_mm_h(obs)
        if rate is not None and 0 <= rate <= 1000:
            rates.append(rate)
    current_rate = rain_rate_mm_h(latest)
    if current_rate is not None and 0 <= current_rate <= 1000:
        rates.append(current_rate)

    max_rate = round(max(rates), 2) if rates else None
    complete = baseline is not None and bool(window)
    return {
        "accumulation_mm": accumulation,
        "max_rate_mm_h": max_rate,
        "complete": complete,
        "samples": len(window),
    }


def parse_observation(obs: Dict[str, Any]) -> Dict[str, Any]:
    metric = nested(obs, "metric", "metric_si")
    imperial = nested(obs, "imperial")
    uk_hybrid = nested(obs, "uk_hybrid")

    # Temperatura: privilegia metric, poi converte imperial.
    temp_c = safe_float(first_value([metric, obs], ("temp", "temperature", "temperature_c")))
    if temp_c is None:
        temp_c = f_to_c(safe_float(first_value([imperial], ("temp", "temperature"))))

    dew_c = safe_float(first_value([metric, obs], ("dewpt", "dewPoint", "dewpoint")))
    if dew_c is None:
        dew_c = f_to_c(safe_float(first_value([imperial], ("dewpt", "dewPoint", "dewpoint"))))

    humidity = safe_float(first_value([obs, metric, imperial], ("humidity", "relativeHumidity")))

    wind_kmh = safe_float(first_value([metric, uk_hybrid], ("windSpeed", "wind_speed")))
    if wind_kmh is None:
        wind_kmh = mph_to_kmh(safe_float(first_value([imperial], ("windSpeed", "wind_speed"))))

    gust_kmh = safe_float(first_value([metric, uk_hybrid], ("windGust", "wind_gust")))
    if gust_kmh is None:
        gust_kmh = mph_to_kmh(safe_float(first_value([imperial], ("windGust", "wind_gust"))))

    pressure_hpa = safe_float(first_value([metric, uk_hybrid], ("pressure", "pressureMeanSeaLevel")))
    if pressure_hpa is None:
        pressure_hpa = inhg_to_hpa(safe_float(first_value([imperial], ("pressure", "pressureMeanSeaLevel"))))

    precip_rate = rain_rate_mm_h(obs)
    precip_total = rain_total_mm(obs)

    wind_dir = first_value([obs, metric, imperial], ("winddirCardinal", "windDirectionCardinal"))
    if wind_dir is not None:
        wind_dir = str(wind_dir).upper().strip()
        if wind_dir not in VALID_WIND_DIRS:
            wind_dir = None

    # Weather Underground normalmente pubblica anche la direzione numerica.
    wind_direction_deg = safe_float(first_value(
        [obs, metric, uk_hybrid, imperial],
        ("winddir", "windDirection", "wind_direction", "windDirectionDegrees", "windDir")
    ))
    if wind_direction_deg is not None:
        wind_direction_deg %= 360.0
    else:
        # Ripiego: converte la sigla cardinale nel centro del settore.
        wind_direction_deg = cardinal_to_degrees(wind_dir)

    warnings: List[str] = []
    if temp_c is not None and not -35 <= temp_c <= 55:
        warnings.append("Temperatura fuori intervallo plausibile")
        temp_c = None
    if dew_c is not None and temp_c is not None and dew_c > temp_c + 0.5:
        warnings.append("Dew point superiore alla temperatura")
        dew_c = None
    if humidity is not None and not 0 <= humidity <= 100:
        warnings.append("Umidità fuori intervallo")
        humidity = None
    if wind_kmh is not None and not 0 <= wind_kmh <= 250:
        warnings.append("Vento fuori intervallo")
        wind_kmh = None
    if gust_kmh is not None and not 0 <= gust_kmh <= 300:
        warnings.append("Raffica fuori intervallo")
        gust_kmh = None
    if pressure_hpa is not None and not 850 <= pressure_hpa <= 1100:
        warnings.append("Pressione fuori intervallo")
        pressure_hpa = None
    if precip_rate is not None and not 0 <= precip_rate <= 1000:
        warnings.append("Rain rate fuori intervallo")
        precip_rate = None
    if precip_total is not None and not 0 <= precip_total <= 3000:
        warnings.append("Accumulo giornaliero fuori intervallo")
        precip_total = None

    return {
        "source": "wunderground_background_feed",
        "parser_version": "9.0-rain-rate-accumulation",
        "status": "feed_read",
        "updated": first_value([obs], ("obsTimeLocal", "obsTimeUtc", "validTimeUtc")),
        "epoch": first_value([obs], ("epoch", "validTimeUtc")),
        "temperature_c": None if temp_c is None else round(temp_c, 1),
        "dewpoint_c": None if dew_c is None else round(dew_c, 1),
        "humidity_pct": None if humidity is None else round(humidity, 1),
        "wind_dir": wind_dir,
        "wind_direction_deg": None if wind_direction_deg is None else round(wind_direction_deg, 1),
        "wind_speed_kmh": None if wind_kmh is None else round(wind_kmh, 1),
        "wind_gust_kmh": None if gust_kmh is None else round(gust_kmh, 1),
        "pressure_hpa": None if pressure_hpa is None else round(pressure_hpa, 1),
        "rain_rate_mm_h": None if precip_rate is None else round(precip_rate, 1),
        "rain_today_mm": None if precip_total is None else round(precip_total, 1),
        "quality_warnings": warnings,
    }


def scrape_station(browser: Browser, code: str, meta: Dict[str, str]) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "station_code": code,
        "station_name": meta["name"],
        "url": meta["url"],
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "parser_version": "9.0-rain-rate-accumulation",
        "data": None,
        "errors": [],
        "diagnostics": {},
    }

    page = browser.new_page(
        viewport={"width": 1440, "height": 2600},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
    )

    payloads: List[Any] = []
    feed_urls: List[str] = []

    def capture(response: Response) -> None:
        url = response.url
        lower = url.lower()
        content_type = (response.headers.get("content-type") or "").lower()
        interesting = (
            "observation" in lower
            or "/pws/" in lower
            or "history" in lower
            or "application/json" in content_type
        )
        if not interesting:
            return
        try:
            payload = response.json()
        except Exception:
            return
        payloads.append(payload)
        feed_urls.append(url)

    page.on("response", capture)

    try:
        page.goto(meta["url"], wait_until="domcontentloaded", timeout=90000)
        dismiss_cookies(page)
        page.wait_for_timeout(15000)

        # Scorrere fino allo storico induce il caricamento dei dati giornalieri.
        try:
            history = page.get_by_text("Weather History", exact=False).first
            history.scroll_into_view_if_needed(timeout=10000)
        except Exception:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(10000)

        lists = find_observation_lists(payloads)
        latest = pick_latest_observation(lists)

        if latest is None:
            raise RuntimeError("Nessun feed con lista observations intercettato")

        latest, wind_direction_source, wind_direction_age_seconds = (
            enrich_latest_with_wind_direction(latest, lists)
        )

        output["diagnostics"] = {
            "json_payloads_captured": len(payloads),
            "observation_lists_found": len(lists),
            "feed_urls": feed_urls[-8:],
            "wind_direction_source": wind_direction_source,
            "wind_direction_age_seconds": (
                None if wind_direction_age_seconds is None
                else round(wind_direction_age_seconds)
            ),
        }

        output["data"] = parse_observation(latest)
        if output["data"] is not None:
            output["data"]["wind_direction_source"] = wind_direction_source
            output["data"]["wind_direction_age_seconds"] = (
                None if wind_direction_age_seconds is None
                else round(wind_direction_age_seconds)
            )
            for hours in (1, 6, 12, 24):
                stats = rain_window_stats(lists, latest, hours)
                output["data"][f"rain_{hours}h_mm"] = stats["accumulation_mm"]
                output["data"][f"rain_rate_max_{hours}h_mm_h"] = stats["max_rate_mm_h"]
                output["data"][f"rain_window_{hours}h_complete"] = stats["complete"]
                output["data"][f"rain_window_{hours}h_samples"] = stats["samples"]
        return output

    except Exception as exc:
        output["errors"].append(f"{type(exc).__name__}: {exc}")
        return output
    finally:
        page.close()


def collect_all() -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            for code, meta in STATIONS.items():
                results[code] = scrape_station(browser, code, meta)
        finally:
            browser.close()
    return results


@api.get("/")
@api.head("/")
def root() -> Dict[str, str]:
    return {
        "status": "ok",
        "message": "Backend meteo Arbus-Guspini attivo",
        "parser": "Weather Underground background feed v9 con rain rate e accumuli",
        "endpoint": "/stations",
    }


@api.get("/health")
@api.head("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}




@api.get("/automation/state")
def automation_state_get() -> Dict[str, Any]:
    if not _cloud_configured():
        return {
            "configured": False,
            "state": {"history": [], "archive": [], "settings": {}},
            "updated_at": None,
        }
    result = cloud_state_get()
    result["configured"] = True
    return result


@api.post("/automation/state")
def automation_state_post(payload: Dict[str, Any], x_automation_secret: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not AUTOMATION_SECRET:
        raise HTTPException(status_code=503, detail="AUTOMATION_SECRET non configurato")
    if not x_automation_secret or not secrets.compare_digest(x_automation_secret, AUTOMATION_SECRET):
        raise HTTPException(status_code=401, detail="Chiave automazione non valida")
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="Payload state non valido")
    history = state.get("history")
    archive = state.get("archive")
    if history is not None and not isinstance(history, list):
        raise HTTPException(status_code=400, detail="history deve essere una lista")
    if archive is not None and not isinstance(archive, list):
        raise HTTPException(status_code=400, detail="archive deve essere una lista")
    return cloud_state_put(state)


@api.get("/automation/status")
def automation_status() -> Dict[str, Any]:
    return {
        "configured": _cloud_configured(),
        "secret_configured": bool(AUTOMATION_SECRET),
        "state_id": AUTOMATION_STATE_ID,
    }


@api.get("/stations")
def stations(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if (
        not force
        and _cache["data"] is not None
        and now - _cache["timestamp"] < CACHE_SECONDS
    ):
        return {
            "cached": True,
            "cache_age_seconds": round(now - _cache["timestamp"]),
            "stations": _cache["data"],
        }

    data = collect_all()
    _cache["timestamp"] = time.time()
    _cache["data"] = data
    return {
        "cached": False,
        "cache_age_seconds": 0,
        "stations": data,
    }
