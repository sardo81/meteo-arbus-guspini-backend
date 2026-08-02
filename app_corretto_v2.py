#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

STATIONS = {
    "IARBUS7": {
        "name": "Arbus (SU)",
        "lineameteo_id": 2620,
        "wu_url": "https://www.wunderground.com/dashboard/pws/IARBUS7",
        "fallback_url": "https://mobile.lineameteo.it/rete_inside.php?id=2620",
    },
    "IGUSPI1": {
        "name": "Guspini (SU)",
        "lineameteo_id": 160,
        "wu_url": "https://www.wunderground.com/dashboard/pws/IGUSPI1",
        "fallback_url": "https://mobile.lineameteo.it/rete_inside.php?id=160",
    },
}

CARDINAL_RE = r"(?:NNE|ENE|ESE|SSE|SSW|WSW|WNW|NNW|NE|SE|SW|NW|N|E|S|W)"
VALID_WIND_DIRS = {"N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"}
NUMBER_RE = r"-?\d+(?:[.,]\d+)?"


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value.replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def find_float(pattern: str, text: str, flags: int = re.I | re.S, group: int = 1) -> Optional[float]:
    match = re.search(pattern, text, flags)
    return to_float(match.group(group)) if match else None


def find_text(pattern: str, text: str, flags: int = re.I | re.S, group: int = 1) -> Optional[str]:
    match = re.search(pattern, text, flags)
    return match.group(group).strip() if match else None


def f_to_c(value: Optional[float]) -> Optional[float]:
    return None if value is None else round((value - 32.0) * 5.0 / 9.0, 1)


def mph_to_kmh(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 1.609344, 1)


def knots_to_kmh(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 1.852, 1)


def inhg_to_hpa(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 33.8638866667, 1)


def inches_to_mm(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 25.4, 1)


def parse_temperature(pattern: str, text: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    match = re.search(pattern, text, re.I | re.S)
    if not match:
        return None, None, None
    value = to_float(match.group(1))
    unit = match.group(2).upper()
    if value is None:
        return None, None, unit
    return (f_to_c(value) if unit == "F" else round(value, 1), value, unit)


def parse_speed(pattern: str, text: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    match = re.search(pattern, text, re.I | re.S)
    if not match:
        return None, None, None
    value = to_float(match.group(1))
    unit = match.group(2).lower().replace(" ", "")
    if value is None:
        return None, None, unit
    if unit == "mph":
        kmh = mph_to_kmh(value)
    elif unit in {"kt", "kts", "knot", "knots"}:
        kmh = knots_to_kmh(value)
    else:
        kmh = round(value, 1)
    return kmh, value, unit


def parse_pressure(pattern: str, text: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    match = re.search(pattern, text, re.I | re.S)
    if not match:
        return None, None, None
    value = to_float(match.group(1))
    unit = match.group(2).lower()
    if value is None:
        return None, None, unit
    hpa = inhg_to_hpa(value) if unit in {"in", "inhg"} else round(value, 1)
    return hpa, value, unit


def parse_rain(pattern: str, text: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    match = re.search(pattern, text, re.I | re.S)
    if not match:
        return None, None, None
    value = to_float(match.group(1))
    unit = match.group(2).lower()
    if value is None:
        return None, None, unit
    mm = inches_to_mm(value) if unit in {"in", "inch", "inches"} else round(value, 1)
    return mm, value, unit


def parse_relative_age_minutes(updated_text: Optional[str]) -> Optional[int]:
    if not updated_text:
        return None
    text = updated_text.lower()
    m = re.search(rf"({NUMBER_RE})\s*(?:minute|minutes|minuto|minuti|min)\b", text)
    if m:
        v = to_float(m.group(1))
        return round(v) if v is not None else None
    m = re.search(rf"({NUMBER_RE})\s*(?:hour|hours|ora|ore)\b", text)
    if m:
        v = to_float(m.group(1))
        return round(v * 60) if v is not None else None
    if re.search(r"\b(?:just now|adesso|pochi secondi fa)\b", text):
        return 0
    return None


def dismiss_cookie_banners(page: Page) -> None:
    for label in ("Accept All", "I Accept", "Accetta tutto", "Accetto", "Accept", "Agree"):
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I))
            if button.count() > 0:
                button.first.click(timeout=2500)
                page.wait_for_timeout(500)
                return
        except Exception:
            pass


def fetch_rendered_text(page: Page, url: str, wait_text: Optional[str] = None) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    dismiss_cookie_banners(page)
    if wait_text:
        try:
            page.get_by_text(wait_text, exact=False).first.wait_for(timeout=20000)
        except Exception:
            pass
    page.wait_for_timeout(6000)
    body = page.locator("body")
    if body.count() == 0:
        raise RuntimeError("Pagina senza elemento body")
    return clean_text(body.inner_text(timeout=30000))


def isolate_current_conditions(text: str) -> str:
    for pattern in (
        r"PWS CURRENT CONDITIONS(.*?)(?:Weather History|10-Day Weather Forecast|Hourly Forecast|$)",
        r"CURRENT CONDITIONS(.*?)(?:Weather History|Forecast|$)",
        r"CONDIZIONI ATTUALI(.*?)(?:Storico|Previsioni|$)",
    ):
        block = find_text(pattern, text)
        if block:
            return block
    return text


def parse_wunderground(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "source": "wunderground",
        "status": None,
        "updated": None,
        "age_minutes": None,
        "stale": None,
        "temperature_c": None,
        "temperature_original": None,
        "temperature_unit": None,
        "dewpoint_c": None,
        "dewpoint_original": None,
        "dewpoint_unit": None,
        "humidity_pct": None,
        "wind_dir": None,
        "wind_speed_kmh": None,
        "wind_speed_original": None,
        "wind_speed_unit": None,
        "wind_gust_kmh": None,
        "wind_gust_original": None,
        "wind_gust_unit": None,
        "pressure_hpa": None,
        "pressure_original": None,
        "pressure_unit": None,
        "rain_rate_mm": None,
        "rain_today_mm": None,
        "quality_warnings": [],
    }

    result["status"] = find_text(r"Station Summary\s+(Online|Offline)", text) or find_text(r"\b(Online|Offline)\b", text)
    updated = (
        find_text(r"Station Summary\s+Online\s*\((updated [^)]+)\)", text)
        or find_text(r"\b(updated\s+[^)\n]+(?:ago)?)", text)
        or find_text(r"\b(aggiornato\s+[^)\n]+(?:fa)?)", text)
    )
    result["updated"] = updated
    result["age_minutes"] = parse_relative_age_minutes(updated)
    result["stale"] = result["age_minutes"] is not None and result["age_minutes"] > 30

    block = isolate_current_conditions(text)

    tc, torig, tunit = parse_temperature(rf"(?:CURRENT\s+)?({NUMBER_RE})\s*°?\s*([CF])\b", block)
    if tc is None:
        tc, torig, tunit = parse_temperature(rf"(?:Temperature|Temperatura)\s*:?\s*({NUMBER_RE})\s*°?\s*([CF])\b", block)

    dc, dorig, dunit = parse_temperature(rf"(?:Dew Point|Dewpoint|Punto di rugiada)\s*:?\s*({NUMBER_RE})\s*°?\s*([CF])\b", block)
    humidity = find_float(rf"(?:Humidity|Umidità)\s*:?\s*({NUMBER_RE})\s*%", block)
    if humidity is None:
        humidity = find_float(rf"(?:CURRENT.*?)({NUMBER_RE})\s*%", block)

    wind_dir = (
        find_text(rf"(?:Wind|Vento)\s*:?\s*({CARDINAL_RE})\b", block)
        or find_text(rf"\b({CARDINAL_RE})\b\s+(?:{NUMBER_RE})\s*(?:mph|km/h|kph|kt|knots)", block)
    )

    if wind_dir:
        wind_dir = wind_dir.upper().strip()
        if wind_dir not in VALID_WIND_DIRS:
            wind_dir = None

    ws, wsorig, wsunit = parse_speed(
        rf"(?:Wind Speed|Wind|Vento)\s*:?\s*(?:{CARDINAL_RE}\s+)?({NUMBER_RE})\s*(mph|km/h|kph|kt|kts|knots?)\b",
        block,
    )
    if ws is None and wind_dir:
        ws, wsorig, wsunit = parse_speed(rf"\b{re.escape(wind_dir)}\b\s+({NUMBER_RE})\s*(mph|km/h|kph|kt|kts|knots?)\b", block)

    gust, gustorig, gustunit = parse_speed(rf"(?:Wind Gust|Gust|Gusts|Raffica|Raffiche)\s*:?\s*({NUMBER_RE})\s*(mph|km/h|kph|kt|kts|knots?)\b", block)
    phpa, porig, punit = parse_pressure(rf"(?:Pressure|Pressione)\s*:?\s*({NUMBER_RE})\s*(inHg|in|hPa|mb)\b", block)
    rain_rate, _, _ = parse_rain(rf"(?:Rain Rate|Precipitation Rate|Intensità pioggia)\s*:?\s*({NUMBER_RE})\s*(mm|in|inch|inches)\b", block)
    rain_today, _, _ = parse_rain(rf"(?:Rain Today|Daily Rain|Precip\.? Today|Pioggia oggi)\s*:?\s*({NUMBER_RE})\s*(mm|in|inch|inches)\b", block)

    result.update({
        "temperature_c": tc,
        "temperature_original": torig,
        "temperature_unit": tunit,
        "dewpoint_c": dc,
        "dewpoint_original": dorig,
        "dewpoint_unit": dunit,
        "humidity_pct": humidity,
        "wind_dir": wind_dir,
        "wind_speed_kmh": ws,
        "wind_speed_original": wsorig,
        "wind_speed_unit": wsunit,
        "wind_gust_kmh": gust,
        "wind_gust_original": gustorig,
        "wind_gust_unit": gustunit,
        "pressure_hpa": phpa,
        "pressure_original": porig,
        "pressure_unit": punit,
        "rain_rate_mm": rain_rate,
        "rain_today_mm": rain_today,
    })

    warnings = result["quality_warnings"]
    if result["status"] and result["status"].lower() == "offline":
        warnings.append("Stazione indicata offline")
    if result["stale"]:
        warnings.append(f"Osservazione vecchia di circa {result['age_minutes']} minuti")
    if tc is not None and not (-35 <= tc <= 55):
        warnings.append("Temperatura fuori intervallo plausibile")
        result["temperature_c"] = None
    if humidity is not None and not (0 <= humidity <= 100):
        warnings.append("Umidità fuori intervallo 0–100%")
        result["humidity_pct"] = None
    if ws is not None and not (0 <= ws <= 250):
        warnings.append("Velocità vento fuori intervallo plausibile")
        result["wind_speed_kmh"] = None
    if gust is not None and not (0 <= gust <= 300):
        warnings.append("Raffica fuori intervallo plausibile")
        result["wind_gust_kmh"] = None
    if phpa is not None and not (850 <= phpa <= 1100):
        warnings.append("Pressione fuori intervallo plausibile")
        result["pressure_hpa"] = None
    return result


def parse_lineameteo_fallback(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "source": "lineameteo_fallback",
        "status": None,
        "updated": None,
        "age_minutes": None,
        "stale": None,
        "temperature_c": None,
        "humidity_pct": None,
        "wind_dir": None,
        "wind_speed_kmh": None,
        "wind_gust_kmh": None,
        "pressure_hpa": None,
        "rain_today_mm": None,
        "quality_warnings": [],
    }

    updated = find_text(r"Ultimo aggiornamento\s*:?\s*([0-9:/.\- ]+)", text) or find_text(r"Aggiornato\s*:?\s*([0-9:/.\- ]+)", text)
    temp = find_float(rf"Temperatura\s*:?\s*({NUMBER_RE})\s*°?\s*C\b", text)
    hum = find_float(rf"Umidit[aà]\s*:?\s*({NUMBER_RE})\s*%", text)
    wdir = find_text(rf"(?:Direzione vento|Vento)\s*:?\s*({CARDINAL_RE})\b", text)
    wind, _, _ = parse_speed(rf"(?:Velocit[aà] vento|Vento)\s*:?\s*(?:{CARDINAL_RE}\s+)?({NUMBER_RE})\s*(km/h|kph|mph|kt|kts|knots?)\b", text)
    gust, _, _ = parse_speed(rf"(?:Raffica|Raffiche)\s*:?\s*({NUMBER_RE})\s*(km/h|kph|mph|kt|kts|knots?)\b", text)
    press, _, _ = parse_pressure(rf"Pressione\s*:?\s*({NUMBER_RE})\s*(hPa|mb|inHg|in)\b", text)
    rain, _, _ = parse_rain(rf"(?:Pioggia oggi|Precipitazioni oggi)\s*:?\s*({NUMBER_RE})\s*(mm|in|inch|inches)\b", text)

    result.update({
        "updated": updated,
        "temperature_c": temp,
        "humidity_pct": hum,
        "wind_dir": wdir,
        "wind_speed_kmh": wind,
        "wind_gust_kmh": gust,
        "pressure_hpa": press,
        "rain_today_mm": rain,
    })

    warnings = result["quality_warnings"]
    if temp is not None and not (-35 <= temp <= 55):
        warnings.append("Temperatura fuori intervallo plausibile")
        result["temperature_c"] = None
    if hum is not None and not (0 <= hum <= 100):
        warnings.append("Umidità fuori intervallo 0–100%")
        result["humidity_pct"] = None
    if wind is not None and not (0 <= wind <= 250):
        warnings.append("Velocità vento fuori intervallo plausibile")
        result["wind_speed_kmh"] = None
    if gust is not None and not (0 <= gust <= 300):
        warnings.append("Raffica fuori intervallo plausibile")
        result["wind_gust_kmh"] = None
    if press is not None and not (850 <= press <= 1100):
        warnings.append("Pressione fuori intervallo plausibile")
        result["pressure_hpa"] = None
    return result


def has_useful_weather_data(data: Dict[str, Any]) -> bool:
    return any(data.get(k) is not None for k in (
        "temperature_c", "humidity_pct", "wind_speed_kmh", "wind_gust_kmh", "pressure_hpa", "rain_today_mm"
    ))


def scrape_station(browser: Browser, station_code: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "station_code": station_code,
        "station_name": meta["name"],
        "lineameteo_id": meta["lineameteo_id"],
        "wunderground_url": meta["wu_url"],
        "fallback_url": meta["fallback_url"],
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "parser_version": "2.0-safe",
        "data": None,
        "errors": [],
    }

    page = browser.new_page(
        viewport={"width": 1440, "height": 2400},
        locale="it-IT",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
    )

    try:
        try:
            wu_text = fetch_rendered_text(page, meta["wu_url"], wait_text="PWS CURRENT CONDITIONS")
            wu_data = parse_wunderground(wu_text)
            if has_useful_weather_data(wu_data):
                output["data"] = wu_data
                return output
            output["errors"].append("WU: parsing vuoto o incompleto")
        except PlaywrightTimeoutError:
            output["errors"].append("WU: timeout")
        except Exception as exc:
            output["errors"].append(f"WU: {type(exc).__name__}: {exc}")

        try:
            lm_text = fetch_rendered_text(page, meta["fallback_url"], wait_text="Dettagli della stazione")
            lm_data = parse_lineameteo_fallback(lm_text)
            if has_useful_weather_data(lm_data):
                output["data"] = lm_data
                return output
            output["errors"].append("LineaMeteo: nessun valore meteorologico riconosciuto")
        except PlaywrightTimeoutError:
            output["errors"].append("LineaMeteo fallback: timeout")
        except Exception as exc:
            output["errors"].append(f"LineaMeteo fallback: {type(exc).__name__}: {exc}")

        return output
    finally:
        page.close()


def main() -> None:
    all_results: Dict[str, Any] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        try:
            for code, meta in STATIONS.items():
                all_results[code] = scrape_station(browser, code, meta)
        finally:
            browser.close()
    print(json.dumps(all_results, indent=2, ensure_ascii=False, allow_nan=False))




# =========================
# API WEB PER RENDER
# =========================

import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

api = FastAPI(title="Meteo Arbus-Guspini API", version="1.0")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_CACHE_SECONDS = 300
_cache = {"timestamp": 0.0, "data": None}


def collect_all_stations() -> Dict[str, Any]:
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
def api_root() -> Dict[str, str]:
    return {
        "status": "ok",
        "message": "Backend meteo Arbus-Guspini attivo",
        "dati": "/stations",
        "controllo": "/health",
    }


@api.get("/health")
def api_health() -> Dict[str, str]:
    return {"status": "ok"}


@api.get("/stations")
def api_stations(force: bool = False) -> Dict[str, Any]:
    now = time.time()

    if (
        not force
        and _cache["data"] is not None
        and now - _cache["timestamp"] < _CACHE_SECONDS
    ):
        return {
            "cached": True,
            "cache_age_seconds": round(now - _cache["timestamp"]),
            "stations": _cache["data"],
        }

    data = collect_all_stations()
    _cache["timestamp"] = time.time()
    _cache["data"] = data

    return {
        "cached": False,
        "cache_age_seconds": 0,
        "stations": data,
    }
