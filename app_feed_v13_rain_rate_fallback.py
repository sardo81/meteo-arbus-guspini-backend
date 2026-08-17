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

api = FastAPI(title="Meteo Arbus-Guspini API", version="13.0-rain-rate-fallback")
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
MANUAL_EDIT_SECRET = os.getenv("MANUAL_EDIT_SECRET", "").strip()
AUTOMATION_STATE_ID = "main"

RAIN_EVENT_START_RATE_MM_H = 0.1
RAIN_EVENT_MIN_DELTA_MM = 0.05
RAIN_EVENT_DRY_END_MINUTES = 20
RAIN_EVENT_MAX_STORED = 500
RAIN_RATE_INTEGRATION_MAX_GAP_MINUTES = 20
RAIN_GAUGE_LATE_GRACE_MINUTES = 90
RAIN_ACCUM_AGREEMENT_REL_TOL = 0.35
STATION_OBSERVATIONS_TABLE = "station_observations"
STATION_OBSERVATIONS_RECENT_DEFAULT = 200
STATION_OBSERVATIONS_RECENT_MAX = 5000


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
        "parser_version": "13.0-rain-rate-fallback",
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
        "parser_version": "13.0-rain-rate-fallback",
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




def _normalized_epoch(value: Any) -> Optional[float]:
    epoch = safe_float(value)
    if epoch is None:
        return None
    # alcuni feed possono esporre millisecondi Unix
    if epoch > 10_000_000_000:
        epoch /= 1000.0
    return epoch


def _station_observed_at_utc(data: Dict[str, Any], payload: Dict[str, Any]) -> str:
    epoch = _normalized_epoch(data.get("epoch"))
    if epoch is not None:
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except Exception:
            pass

    for candidate in (
        data.get("updated"),
        payload.get("captured_at_utc"),
    ):
        if not candidate:
            continue
        try:
            parsed = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Se il feed non specifica il fuso, usiamo il timestamp di cattura
                # per evitare di inventare un offset.
                continue
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            continue

    return datetime.now(timezone.utc).isoformat()


def _station_observation_row(
    station_code: str,
    station_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(station_payload, dict):
        return None
    data = station_payload.get("data")
    if not isinstance(data, dict):
        return None

    return {
        "station_code": station_code,
        "station_name": station_payload.get("station_name") or STATIONS.get(station_code, {}).get("name") or station_code,
        "observed_at_utc": _station_observed_at_utc(data, station_payload),
        "captured_at_utc": station_payload.get("captured_at_utc") or datetime.now(timezone.utc).isoformat(),
        "temperature_c": safe_float(data.get("temperature_c")),
        "dewpoint_c": safe_float(data.get("dewpoint_c")),
        "humidity_pct": safe_float(data.get("humidity_pct")),
        "pressure_hpa": safe_float(data.get("pressure_hpa")),
        "wind_speed_kmh": safe_float(data.get("wind_speed_kmh")),
        "wind_gust_kmh": safe_float(data.get("wind_gust_kmh")),
        "wind_direction_deg": safe_float(data.get("wind_direction_deg")),
        "wind_dir": data.get("wind_dir"),
        "rain_rate_mm_h": safe_float(data.get("rain_rate_mm_h")),
        "rain_today_mm": safe_float(data.get("rain_today_mm")),
        "rain_1h_mm": safe_float(data.get("rain_1h_mm")),
        "rain_6h_mm": safe_float(data.get("rain_6h_mm")),
        "rain_12h_mm": safe_float(data.get("rain_12h_mm")),
        "rain_24h_mm": safe_float(data.get("rain_24h_mm")),
        "rain_rate_max_1h_mm_h": safe_float(data.get("rain_rate_max_1h_mm_h")),
        "rain_rate_max_6h_mm_h": safe_float(data.get("rain_rate_max_6h_mm_h")),
        "rain_rate_max_12h_mm_h": safe_float(data.get("rain_rate_max_12h_mm_h")),
        "rain_rate_max_24h_mm_h": safe_float(data.get("rain_rate_max_24h_mm_h")),
        "source": data.get("source") or station_payload.get("source") or "wunderground_background_feed",
        "parser_version": data.get("parser_version") or station_payload.get("parser_version") or "13.0-rain-rate-fallback",
        "quality_warnings": data.get("quality_warnings") if isinstance(data.get("quality_warnings"), list) else [],
    }


def save_station_observation_snapshots(
    observations: Dict[str, Any],
) -> Dict[str, Any]:
    """Salva una fotografia completa di ogni stazione ad ogni ciclo automatico.

    L'upsert su (station_code, observed_at_utc) rende idempotente il salvataggio:
    se il feed restituisce lo stesso campione due volte, non creiamo duplicati.
    """
    if not _cloud_configured():
        return {"configured": False, "saved": 0, "skipped": len(observations)}

    rows: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for code, payload in observations.items():
        row = _station_observation_row(code, payload)
        if row is None:
            skipped.append(code)
        else:
            rows.append(row)

    if not rows:
        return {"configured": True, "saved": 0, "skipped": skipped}

    try:
        result = _supabase_request(
            f"{STATION_OBSERVATIONS_TABLE}?on_conflict=station_code,observed_at_utc",
            method="POST",
            payload=rows,
            prefer="resolution=merge-duplicates,return=representation",
        )
        return {
            "configured": True,
            "saved": len(result or rows),
            "attempted": len(rows),
            "skipped": skipped,
            "error": None,
        }
    except Exception as exc:
        return {
            "configured": True,
            "saved": 0,
            "attempted": len(rows),
            "skipped": skipped,
            "error": f"{type(exc).__name__}: {exc}",
        }


def recent_station_observations(limit: int = STATION_OBSERVATIONS_RECENT_DEFAULT) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), STATION_OBSERVATIONS_RECENT_MAX))
    rows = _supabase_request(
        f"{STATION_OBSERVATIONS_TABLE}?select=*&order=observed_at_utc.desc&limit={limit}",
        method="GET",
    )
    return rows if isinstance(rows, list) else []


def _iso_from_epoch(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _positive_rain_delta(previous: Optional[float], current: Optional[float]) -> float:
    if previous is None or current is None:
        return 0.0
    previous = float(previous)
    current = float(current)
    # normale incremento del contatore giornaliero
    if current + 0.05 >= previous:
        return max(0.0, current - previous)
    # reset del contatore (tipicamente a mezzanotte)
    return max(0.0, current)


def _integrated_rain_delta(
    previous_rate_mm_h: Optional[float],
    current_rate_mm_h: Optional[float],
    previous_epoch: Optional[float],
    current_epoch: Optional[float],
) -> Dict[str, Any]:
    """Integra il rain rate tra due campioni con la regola del trapezio.

    Non estrapola su buchi troppo lunghi: oltre 20 minuti il campione viene
    marcato come non integrabile, così un'interruzione del Cron non inventa
    millimetri di pioggia.
    """
    prev_epoch = safe_float(previous_epoch)
    curr_epoch = safe_float(current_epoch)
    if prev_epoch is None or curr_epoch is None or curr_epoch <= prev_epoch:
        return {"mm": 0.0, "minutes": 0.0, "used": False, "reason": "no_forward_interval"}

    minutes = (curr_epoch - prev_epoch) / 60.0
    if minutes > RAIN_RATE_INTEGRATION_MAX_GAP_MINUTES:
        return {
            "mm": 0.0,
            "minutes": round(minutes, 2),
            "used": False,
            "reason": "gap_too_long",
        }

    prev_rate = max(0.0, safe_float(previous_rate_mm_h) or 0.0)
    curr_rate = max(0.0, safe_float(current_rate_mm_h) or 0.0)
    mean_rate = (prev_rate + curr_rate) / 2.0
    mm = mean_rate * minutes / 60.0
    return {
        "mm": round(max(0.0, mm), 4),
        "minutes": round(minutes, 2),
        "used": True,
        "reason": "trapezoid",
    }


def _ensure_event_accumulation_fields(event: Dict[str, Any]) -> None:
    """Migra in modo compatibile eventi creati dalle versioni precedenti."""
    old_accum = max(0.0, safe_float(event.get("accumulation_mm")) or 0.0)
    if "gauge_accumulation_mm" not in event:
        # Fino alla v12 accumulation_mm proveniva dal delta di rain_today_mm.
        event["gauge_accumulation_mm"] = round(old_accum, 2)
    if "rate_integrated_mm" not in event:
        event["rate_integrated_mm"] = 0.0
    event.setdefault("rate_integration_minutes", 0.0)
    event.setdefault("rate_integration_skipped_minutes", 0.0)
    event.setdefault("rate_evidence_seen", bool((safe_float(event.get("max_rate_mm_h")) or 0.0) >= RAIN_EVENT_START_RATE_MM_H))
    event.setdefault("late_gauge_reconciled_mm", 0.0)


def _resolve_event_accumulation(event: Dict[str, Any], final: bool = False) -> None:
    """Sceglie l'accumulo operativo mantenendo entrambe le misure.

    - Il pluviometro cumulativo resta la fonte primaria quando disponibile.
    - Durante l'evento, se il cumulativo appare chiaramente in ritardo, si usa
      provvisoriamente l'integrazione del rain rate.
    - A chiusura evento il cumulativo prevale se ha registrato pioggia.
    - Se il cumulativo resta a zero, l'integrazione del rain rate diventa
      fallback esplicito (mai nascosto).
    """
    _ensure_event_accumulation_fields(event)
    gauge = max(0.0, safe_float(event.get("gauge_accumulation_mm")) or 0.0)
    integrated = max(0.0, safe_float(event.get("rate_integrated_mm")) or 0.0)

    method = "none"
    quality = "insufficient"
    chosen = 0.0

    if final:
        if gauge >= RAIN_EVENT_MIN_DELTA_MM:
            chosen = gauge
            if integrated >= RAIN_EVENT_MIN_DELTA_MM:
                rel = abs(gauge - integrated) / max(gauge, integrated, 0.01)
                method = "gauge_primary_confirmed" if rel <= RAIN_ACCUM_AGREEMENT_REL_TOL else "gauge_primary_disagreement"
                quality = "high" if rel <= RAIN_ACCUM_AGREEMENT_REL_TOL else "check"
            else:
                method = "gauge_primary"
                quality = "high"
        elif integrated >= RAIN_EVENT_MIN_DELTA_MM:
            chosen = integrated
            method = "rate_fallback_no_gauge"
            quality = "fallback"
    else:
        if gauge >= RAIN_EVENT_MIN_DELTA_MM:
            # Se il contatore è ancora molto sotto l'integrale, è plausibile
            # che WU stia aggiornando il cumulativo in ritardo.
            if integrated >= 0.10 and gauge < integrated * 0.50:
                chosen = integrated
                method = "rate_fallback_gauge_lag"
                quality = "provisional"
            else:
                chosen = gauge
                method = "gauge_live"
                quality = "high"
        elif integrated >= RAIN_EVENT_MIN_DELTA_MM:
            chosen = integrated
            method = "rate_fallback_no_gauge"
            quality = "provisional"

    event["gauge_accumulation_mm"] = round(gauge, 2)
    event["rate_integrated_mm"] = round(integrated, 2)
    event["accumulation_mm"] = round(chosen, 2)
    event["accumulation_method"] = method
    event["accumulation_quality"] = quality
    event["gauge_rate_difference_mm"] = round(gauge - integrated, 2)


def _reconcile_late_gauge_delta(
    events: List[Dict[str, Any]],
    station_code: str,
    epoch: float,
    delta_mm: float,
) -> Optional[Dict[str, Any]]:
    """Attribuisce un cumulativo WU arrivato in ritardo all'ultimo evento.

    Lo facciamo solo se quell'evento era stato chiuso usando il fallback del
    rain rate e solo entro una finestra breve. Così un nuovo rovescio reale
    non viene assorbito per errore dal temporale precedente.
    """
    if delta_mm < RAIN_EVENT_MIN_DELTA_MM:
        return None

    for event in reversed(events):
        if not isinstance(event, dict) or event.get("station_code") != station_code:
            continue
        if event.get("status") != "closed":
            continue

        _ensure_event_accumulation_fields(event)
        method = str(event.get("accumulation_method") or "")
        gauge = max(0.0, safe_float(event.get("gauge_accumulation_mm")) or 0.0)
        if not method.startswith("rate_fallback") and gauge >= RAIN_EVENT_MIN_DELTA_MM:
            return None

        end_epoch = safe_float(event.get("end_epoch"))
        if end_epoch is None:
            return None
        minutes = (epoch - end_epoch) / 60.0
        if minutes < 0 or minutes > RAIN_GAUGE_LATE_GRACE_MINUTES:
            return None

        event["gauge_accumulation_mm"] = round(gauge + delta_mm, 2)
        event["late_gauge_reconciled_mm"] = round(
            max(0.0, safe_float(event.get("late_gauge_reconciled_mm")) or 0.0) + delta_mm,
            2,
        )
        event["last_gauge_reconcile_utc"] = _iso_from_epoch(epoch)
        _resolve_event_accumulation(event, final=True)

        duration_minutes = max(1.0, safe_float(event.get("duration_minutes")) or 1.0)
        accum = max(0.0, safe_float(event.get("accumulation_mm")) or 0.0)
        max_rate = max(0.0, safe_float(event.get("max_rate_mm_h")) or 0.0)
        event["average_rate_mm_h"] = round(accum * 60.0 / duration_minutes, 2)
        if event.get("hail") == "yes":
            event["classification"] = "convective"
        else:
            event["classification"] = _rain_event_classification(accum, max_rate, duration_minutes)
        return event

    return None


def _rain_event_classification(accum_mm: float, max_rate_mm_h: float, duration_minutes: float) -> str:
    avg_rate = accum_mm * 60.0 / duration_minutes if duration_minutes > 0 else 0.0
    if max_rate_mm_h >= 12.0 or avg_rate >= 12.0:
        return "convective"
    if max_rate_mm_h <= 4.0 and avg_rate <= 1.5 and accum_mm >= 0.5:
        return "stratiform"
    return "mixed"


def _update_rain_monitor_state(
    cloud_state: Dict[str, Any],
    station_code: str,
    station_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Aggiorna l'evento pioggia con due misure indipendenti di accumulo.

    1) delta del cumulativo rain_today_mm (pluviometro WU), fonte primaria;
    2) integrazione trapezoidale del rain rate, usata come fallback dichiarato.

    La grandine resta "unknown" finché non viene confermata manualmente.
    """
    data = station_payload.get("data") if isinstance(station_payload, dict) else None
    if not isinstance(data, dict):
        return {"station": station_code, "status": "no_data"}

    epoch = safe_float(data.get("epoch"))
    if epoch is None:
        stamp = data.get("updated")
        if stamp:
            try:
                epoch = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except Exception:
                epoch = time.time()
        else:
            epoch = time.time()

    rate = max(0.0, safe_float(data.get("rain_rate_mm_h")) or 0.0)
    total = safe_float(data.get("rain_today_mm"))

    monitor = cloud_state.setdefault("rain_monitor", {})
    events = cloud_state.setdefault("rain_events", [])
    station = monitor.setdefault(station_code, {
        "active": False,
        "last_total_mm": total,
        "last_rate_mm_h": 0.0,
        "last_epoch": epoch,
        "dry_since_epoch": None,
        "current_event": None,
    })

    previous_total = safe_float(station.get("last_total_mm"))
    previous_rate = safe_float(station.get("last_rate_mm_h"))
    previous_epoch = safe_float(station.get("last_epoch"))

    gauge_delta = _positive_rain_delta(previous_total, total)
    integrated = _integrated_rain_delta(previous_rate, rate, previous_epoch, epoch)
    integrated_delta = float(integrated.get("mm") or 0.0)

    rate_wet = rate >= RAIN_EVENT_START_RATE_MM_H
    gauge_wet = gauge_delta >= RAIN_EVENT_MIN_DELTA_MM
    wet_now = rate_wet or gauge_wet

    current = station.get("current_event")
    active = bool(station.get("active") and isinstance(current, dict))

    # Se il pluviometro aggiorna il cumulativo dopo che un evento chiuso era
    # stato salvato tramite fallback rain-rate, riconciliamo l'evento invece
    # di crearne uno fittizio nuovo.
    late_reconciled = None
    if not active and gauge_wet and not rate_wet:
        late_reconciled = _reconcile_late_gauge_delta(
            events, station_code, float(epoch), gauge_delta
        )
        if late_reconciled is not None:
            wet_now = False
            gauge_wet = False
            gauge_delta = 0.0

    if not active and wet_now:
        current = {
            "id": f"{station_code}-{int(epoch)}-{secrets.token_hex(3)}",
            "station_code": station_code,
            "station_name": station_payload.get("station_name"),
            "start_utc": _iso_from_epoch(epoch),
            "start_epoch": epoch,
            "last_wet_utc": _iso_from_epoch(epoch),
            "last_wet_epoch": epoch,
            "end_utc": None,
            "end_epoch": None,
            "gauge_accumulation_mm": round(gauge_delta, 2),
            "rate_integrated_mm": round(integrated_delta, 2),
            "rate_integration_minutes": round(float(integrated.get("minutes") or 0.0), 2) if integrated.get("used") else 0.0,
            "rate_integration_skipped_minutes": round(float(integrated.get("minutes") or 0.0), 2) if not integrated.get("used") and integrated.get("reason") == "gap_too_long" else 0.0,
            "rate_evidence_seen": bool(rate_wet),
            "accumulation_mm": 0.0,
            "accumulation_method": "none",
            "accumulation_quality": "insufficient",
            "max_rate_mm_h": round(rate, 2),
            "hail": "unknown",
            "status": "active",
            "classification": None,
            "average_rate_mm_h": None,
            "duration_minutes": None,
            "source": "wunderground_background_feed+rain_rate_integration",
        }
        _resolve_event_accumulation(current, final=False)
        station["active"] = True
        station["current_event"] = current
        station["dry_since_epoch"] = None
        active = True

    elif active:
        _ensure_event_accumulation_fields(current)

        current["gauge_accumulation_mm"] = round(
            max(0.0, safe_float(current.get("gauge_accumulation_mm")) or 0.0) + gauge_delta,
            2,
        )
        current["rate_integrated_mm"] = round(
            max(0.0, safe_float(current.get("rate_integrated_mm")) or 0.0) + integrated_delta,
            4,
        )
        if integrated.get("used"):
            current["rate_integration_minutes"] = round(
                max(0.0, safe_float(current.get("rate_integration_minutes")) or 0.0)
                + float(integrated.get("minutes") or 0.0),
                2,
            )
        elif integrated.get("reason") == "gap_too_long":
            current["rate_integration_skipped_minutes"] = round(
                max(0.0, safe_float(current.get("rate_integration_skipped_minutes")) or 0.0)
                + float(integrated.get("minutes") or 0.0),
                2,
            )

        if rate_wet:
            current["rate_evidence_seen"] = True

        current["max_rate_mm_h"] = round(
            max(max(0.0, safe_float(current.get("max_rate_mm_h")) or 0.0), rate),
            2,
        )
        _resolve_event_accumulation(current, final=False)

        # Una variazione tardiva del cumulativo non deve spostare artificialmente
        # la fine del temporale se il rain rate ha già fornito evidenza diretta.
        physical_wet = rate_wet or integrated_delta >= RAIN_EVENT_MIN_DELTA_MM or (
            not bool(current.get("rate_evidence_seen")) and gauge_wet
        )

        if physical_wet:
            current["last_wet_epoch"] = epoch
            current["last_wet_utc"] = _iso_from_epoch(epoch)
            station["dry_since_epoch"] = None
        else:
            if station.get("dry_since_epoch") is None:
                station["dry_since_epoch"] = epoch

            dry_minutes = (epoch - float(station["dry_since_epoch"])) / 60.0
            if dry_minutes >= RAIN_EVENT_DRY_END_MINUTES:
                end_epoch = safe_float(current.get("last_wet_epoch")) or epoch
                start_epoch = safe_float(current.get("start_epoch")) or end_epoch
                duration_minutes = max(1.0, (end_epoch - start_epoch) / 60.0)

                _resolve_event_accumulation(current, final=True)
                accum = max(0.0, safe_float(current.get("accumulation_mm")) or 0.0)
                max_rate = max(0.0, safe_float(current.get("max_rate_mm_h")) or 0.0)
                avg_rate = accum * 60.0 / duration_minutes

                current.update({
                    "end_epoch": end_epoch,
                    "end_utc": _iso_from_epoch(end_epoch),
                    "duration_minutes": round(duration_minutes, 1),
                    "average_rate_mm_h": round(avg_rate, 2),
                    "classification": "convective" if current.get("hail") == "yes" else _rain_event_classification(
                        accum, max_rate, duration_minutes
                    ),
                    "status": "closed",
                    "closed_at_utc": datetime.now(timezone.utc).isoformat(),
                })
                events.append(dict(current))
                cloud_state["rain_events"] = events[-RAIN_EVENT_MAX_STORED:]
                station["active"] = False
                station["current_event"] = None
                station["dry_since_epoch"] = None
                active = False

    station["last_total_mm"] = total
    station["last_rate_mm_h"] = round(rate, 2)
    station["last_epoch"] = epoch
    station["updated_at_utc"] = datetime.now(timezone.utc).isoformat()

    return {
        "station": station_code,
        "active": bool(station.get("active")),
        "rain_rate_mm_h": round(rate, 2),
        "rain_today_mm": total,
        "gauge_delta_mm": round(gauge_delta, 2),
        "rate_integrated_delta_mm": round(integrated_delta, 4),
        "rate_integration": integrated,
        "late_gauge_reconciled_event": late_reconciled.get("id") if late_reconciled else None,
        "current_event": station.get("current_event"),
    }



def collect_and_track_rain_events() -> Dict[str, Any]:
    """Legge le stazioni, aggiorna gli eventi e persiste tutto su Supabase."""
    observations = collect_all()
    station_archive = save_station_observation_snapshots(observations)

    if _cloud_configured():
        cloud = cloud_state_get().get("state") or {}
        if not isinstance(cloud, dict):
            cloud = {"history": [], "archive": [], "settings": {}}
    else:
        cloud = {"history": [], "archive": [], "settings": {}}

    results: Dict[str, Any] = {}
    for code, payload in observations.items():
        results[code] = _update_rain_monitor_state(cloud, code, payload)

    saved = False
    save_error = None
    if _cloud_configured():
        try:
            cloud_state_put(cloud)
            saved = True
        except Exception as exc:
            save_error = f"{type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "cloud_saved": saved,
        "cloud_error": save_error,
        "stations": results,
        "station_archive": station_archive,
        "rain_events": cloud.get("rain_events", []),
        "rain_monitor": cloud.get("rain_monitor", {}),
    }


@api.get("/automation/rain-events")
def automation_rain_events(
    x_automation_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    if not AUTOMATION_SECRET:
        raise HTTPException(status_code=503, detail="AUTOMATION_SECRET non configurato")
    if not x_automation_secret or not secrets.compare_digest(x_automation_secret, AUTOMATION_SECRET):
        raise HTTPException(status_code=401, detail="Chiave automazione non valida")
    return collect_and_track_rain_events()



def _apply_hail_correction(event: Dict[str, Any], hail: str) -> Dict[str, Any]:
    """Applica una correzione manuale della grandine senza perdere
    la classificazione automatica originale dell'evento."""
    if "classification_auto" not in event:
        event["classification_auto"] = event.get("classification")

    event["hail"] = hail
    if hail == "yes":
        event["classification"] = "convective"
    else:
        auto_class = event.get("classification_auto")
        if auto_class in {"convective", "mixed", "stratiform"}:
            event["classification"] = auto_class

    event["manual_hail_updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    event["manual_hail_source"] = "frontend_v20"
    return event


@api.post("/automation/rain-events/hail")
def automation_rain_events_hail(
    payload: Dict[str, Any],
    x_manual_edit_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    if not MANUAL_EDIT_SECRET:
        raise HTTPException(status_code=503, detail="MANUAL_EDIT_SECRET non configurato")
    if not x_manual_edit_secret or not secrets.compare_digest(
        x_manual_edit_secret, MANUAL_EDIT_SECRET
    ):
        raise HTTPException(status_code=401, detail="Chiave correzioni non valida")

    event_id = str(payload.get("event_id") or "").strip()
    hail = str(payload.get("hail") or "").strip().lower()
    if not event_id:
        raise HTTPException(status_code=400, detail="event_id mancante")
    if hail not in {"yes", "no", "unknown"}:
        raise HTTPException(status_code=400, detail="Valore grandine non valido")

    if not _cloud_configured():
        raise HTTPException(status_code=503, detail="Archivio cloud non configurato")

    cloud = cloud_state_get().get("state") or {}
    if not isinstance(cloud, dict):
        raise HTTPException(status_code=500, detail="Stato cloud non valido")

    found: Optional[Dict[str, Any]] = None

    events = cloud.get("rain_events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and str(event.get("id") or "") == event_id:
                found = _apply_hail_correction(event, hail)
                break

    # Consente anche di segnare la grandine mentre un evento è ancora in corso.
    monitor = cloud.get("rain_monitor")
    if isinstance(monitor, dict):
        for station_state in monitor.values():
            if not isinstance(station_state, dict):
                continue
            current = station_state.get("current_event")
            if isinstance(current, dict) and str(current.get("id") or "") == event_id:
                found = _apply_hail_correction(current, hail)
                break

    if found is None:
        raise HTTPException(status_code=404, detail="Evento automatico non trovato")

    cloud_state_put(cloud)
    return {
        "status": "ok",
        "event": found,
        "hail": hail,
    }



@api.get("/automation/observations/status")
def automation_observations_status() -> Dict[str, Any]:
    if not _cloud_configured():
        return {"configured": False, "table": STATION_OBSERVATIONS_TABLE}

    try:
        rows = recent_station_observations(limit=20)
        latest: Dict[str, Any] = {}
        for row in rows:
            code = str(row.get("station_code") or "")
            if code and code not in latest:
                latest[code] = row
        return {
            "configured": True,
            "table": STATION_OBSERVATIONS_TABLE,
            "storage_ok": True,
            "latest": latest,
        }
    except Exception as exc:
        return {
            "configured": True,
            "table": STATION_OBSERVATIONS_TABLE,
            "storage_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latest": {},
        }


@api.get("/automation/observations/recent")
def automation_observations_recent(limit: int = STATION_OBSERVATIONS_RECENT_DEFAULT) -> Dict[str, Any]:
    if not _cloud_configured():
        raise HTTPException(status_code=503, detail="Archivio cloud non configurato")
    try:
        rows = recent_station_observations(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Archivio osservazioni non disponibile: {exc}") from exc
    return {
        "status": "ok",
        "count": len(rows),
        "observations": rows,
    }


@api.get("/automation/rain-events/status")
def automation_rain_events_status() -> Dict[str, Any]:
    if not _cloud_configured():
        return {
            "configured": False,
            "events": [],
            "monitor": {},
        }
    state = cloud_state_get().get("state") or {}
    return {
        "configured": True,
        "manual_edit_configured": bool(MANUAL_EDIT_SECRET),
        "events": state.get("rain_events", []),
        "monitor": state.get("rain_monitor", {}),
    }


@api.get("/")
@api.head("/")
def root() -> Dict[str, str]:
    return {
        "status": "ok",
        "message": "Backend meteo Arbus-Guspini attivo",
        "parser": "Weather Underground background feed v13 con archivio completo stazioni e doppio accumulo pioggia (pluviometro + integrazione rain rate)",
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

    station_archive = save_station_observation_snapshots(data)

    # Aggiornamento opportunistico: se l'archivio cloud è configurato,
    # una normale lettura /stations contribuisce anche al monitor pioggia.
    rain_tracking = None
    if _cloud_configured():
        try:
            cloud = cloud_state_get().get("state") or {}
            if not isinstance(cloud, dict):
                cloud = {"history": [], "archive": [], "settings": {}}
            rain_tracking = {}
            for code, payload in data.items():
                rain_tracking[code] = _update_rain_monitor_state(cloud, code, payload)
            cloud_state_put(cloud)
        except Exception as exc:
            rain_tracking = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "cached": False,
        "cache_age_seconds": 0,
        "stations": data,
        "station_archive": station_archive,
        "rain_tracking": rain_tracking,
    }
