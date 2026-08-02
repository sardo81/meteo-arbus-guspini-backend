#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import FastAPI
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

api = FastAPI(title="Meteo Arbus-Guspini API", version="6.0-feed")
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD"],
    allow_headers=["*"],
)

CACHE_SECONDS = 300
_cache: Dict[str, Any] = {"timestamp": 0.0, "data": None}


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

    precip_mm = safe_float(first_value([metric, uk_hybrid], ("precipTotal", "precipRate", "precip_total")))
    if precip_mm is None:
        precip_mm = inches_to_mm(safe_float(first_value([imperial], ("precipTotal", "precipRate", "precip_total"))))

    wind_dir = first_value([obs, metric, imperial], ("winddirCardinal", "windDirectionCardinal"))
    if wind_dir is not None:
        wind_dir = str(wind_dir).upper().strip()
        if wind_dir not in VALID_WIND_DIRS:
            wind_dir = None

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

    return {
        "source": "wunderground_background_feed",
        "parser_version": "6.0-feed",
        "status": "feed_read",
        "updated": first_value([obs], ("obsTimeLocal", "obsTimeUtc", "validTimeUtc")),
        "epoch": first_value([obs], ("epoch", "validTimeUtc")),
        "temperature_c": None if temp_c is None else round(temp_c, 1),
        "dewpoint_c": None if dew_c is None else round(dew_c, 1),
        "humidity_pct": None if humidity is None else round(humidity, 1),
        "wind_dir": wind_dir,
        "wind_speed_kmh": None if wind_kmh is None else round(wind_kmh, 1),
        "wind_gust_kmh": None if gust_kmh is None else round(gust_kmh, 1),
        "pressure_hpa": None if pressure_hpa is None else round(pressure_hpa, 1),
        "rain_today_mm": None if precip_mm is None else round(precip_mm, 1),
        "quality_warnings": warnings,
    }


def scrape_station(browser: Browser, code: str, meta: Dict[str, str]) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "station_code": code,
        "station_name": meta["name"],
        "url": meta["url"],
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "parser_version": "6.0-feed",
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

        output["diagnostics"] = {
            "json_payloads_captured": len(payloads),
            "observation_lists_found": len(lists),
            "feed_urls": feed_urls[-8:],
        }

        if latest is None:
            raise RuntimeError("Nessun feed con lista observations intercettato")

        output["data"] = parse_observation(latest)
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
        "parser": "Weather Underground background feed v6",
        "endpoint": "/stations",
    }


@api.get("/health")
@api.head("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


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
