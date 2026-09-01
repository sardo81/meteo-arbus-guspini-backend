#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteo Arbus-Guspini backend v15.2 — daily temperature extremes.

Estende il backend v14 senza modificare:
- raccolta stazioni;
- archivio Supabase esistente;
- monitor pioggia;
- notifiche push;
- previsioni;
- pesi/correzioni GFS ed ECMWF;
- storico delle emissioni.

Aggiunge soltanto endpoint READ ONLY che calcolano Tmin/Tmax giornaliere
dalle vere osservazioni archiviate in station_observations.

IMPORTANTE:
le Tmin/Tmax osservate NON vengono ricostruite dagli slot previsionali
08:00 / 14:00 / 20:00. Sono ricavate esclusivamente dai campioni realmente
raccolti dal backend durante la giornata.

FIX v15.2:
- paginazione Supabase/PostgREST;
- timestamp del filtro inviato in UTC con suffisso "Z", senza carattere "+"
  nella query URL, così PostgREST non lo trasforma in uno spazio.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app_feed_v14_push_notifications import (
    api,
    STATIONS,
    STATION_OBSERVATIONS_TABLE,
    ROME_TZ,
    _cloud_configured,
    _supabase_request,
    safe_float,
)

DAILY_EXTREMES_DEFAULT_DAYS = 14
DAILY_EXTREMES_MAX_DAYS = 90
DAILY_EXTREMES_MAX_ROWS = 5000
DAILY_EXTREMES_PAGE_SIZE = 1000


def _parse_datetime_utc(value: Any) -> Optional[datetime]:
    """Converte un timestamp ISO in datetime UTC timezone-aware."""
    if value is None:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _station_label(code: str, fallback: Any = None) -> str:
    """Nome breve usato dal frontend."""
    if code == "IARBUS7":
        return "Arbus"
    if code == "IGUSPI1":
        return "Guspini"

    configured = None
    if isinstance(STATIONS, dict):
        configured = STATIONS.get(code, {}).get("name")

    text = str(fallback or configured or code)
    return text.split(" (")[0].strip() or code


def _fetch_station_observations(days: int) -> List[Dict[str, Any]]:
    """
    Legge soltanto i campi necessari dall'archivio osservazioni.

    Supabase/PostgREST può imporre un massimo server-side di 1000 righe
    per singola risposta. Per coprire davvero l'intera finestra richiesta,
    leggiamo quindi l'archivio a pagine successive usando offset.
    """
    now_local = datetime.now(ROME_TZ)
    first_local_day = now_local.date() - timedelta(days=days - 1)

    start_local = datetime.combine(
        first_local_day,
        datetime.min.time(),
        tzinfo=ROME_TZ,
    )

    # FIX IMPORTANTE:
    # nella query URL NON usiamo isoformat() con "+00:00".
    # Il carattere "+" può essere decodificato come spazio.
    # Usiamo sempre il formato UTC RFC3339 con suffisso Z.
    start_utc = (
        start_local
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    all_rows: List[Dict[str, Any]] = []
    offset = 0

    while len(all_rows) < DAILY_EXTREMES_MAX_ROWS:
        page_limit = min(
            DAILY_EXTREMES_PAGE_SIZE,
            DAILY_EXTREMES_MAX_ROWS - len(all_rows),
        )

        endpoint = (
            f"{STATION_OBSERVATIONS_TABLE}"
            "?select=station_code,station_name,observed_at_utc,temperature_c"
            f"&observed_at_utc=gte.{start_utc}"
            "&order=observed_at_utc.desc"
            f"&limit={page_limit}"
            f"&offset={offset}"
        )

        page = _supabase_request(
            endpoint,
            method="GET",
        )

        if not isinstance(page, list) or not page:
            break

        valid_rows = [row for row in page if isinstance(row, dict)]
        all_rows.extend(valid_rows)

        if len(page) < page_limit:
            break

        offset += len(page)

    return all_rows


def daily_station_extremes(
    days: int = DAILY_EXTREMES_DEFAULT_DAYS,
) -> Dict[str, Any]:
    """
    Calcola Tmin/Tmax osservate per giorno locale Europe/Rome.

    Regole metodologiche:
    - solo dati realmente presenti in station_observations;
    - nessuna ricostruzione dai forecast 08/14/20;
    - nessuna modifica dello storico;
    - nessuna modifica di forecast, probabilità, correzioni o pesi;
    - il giorno corrente è sempre marcato come non finale;
    - per i giorni conclusi vengono riportati anche indicatori di copertura.
    """
    if not _cloud_configured():
        raise RuntimeError("Archivio cloud non configurato")

    days = max(1, min(int(days), DAILY_EXTREMES_MAX_DAYS))
    rows = _fetch_station_observations(days)

    now_local = datetime.now(ROME_TZ)
    today_local = now_local.date()

    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    local_times: Dict[tuple[str, str], List[datetime]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        station_code = str(row.get("station_code") or "").strip()
        temperature_c = safe_float(row.get("temperature_c"))
        observed_utc = _parse_datetime_utc(row.get("observed_at_utc"))

        if not station_code or temperature_c is None or observed_utc is None:
            continue

        observed_local = observed_utc.astimezone(ROME_TZ)
        day_key = observed_local.date().isoformat()

        local_times.setdefault((station_code, day_key), []).append(observed_local)

        station_days = grouped.setdefault(station_code, {})
        item = station_days.get(day_key)

        if item is None:
            item = {
                "station_code": station_code,
                "station": _station_label(
                    station_code,
                    row.get("station_name"),
                ),
                "date": day_key,
                "tmin_c": round(float(temperature_c), 1),
                "tmax_c": round(float(temperature_c), 1),
                "tmin_observed_at": observed_local.isoformat(),
                "tmax_observed_at": observed_local.isoformat(),
                "first_observed_at": observed_local.isoformat(),
                "last_observed_at": observed_local.isoformat(),
                "samples": 0,
            }
            station_days[day_key] = item

        item["samples"] += 1
        item["last_observed_at"] = observed_local.isoformat()

        if float(temperature_c) < float(item["tmin_c"]):
            item["tmin_c"] = round(float(temperature_c), 1)
            item["tmin_observed_at"] = observed_local.isoformat()

        if float(temperature_c) > float(item["tmax_c"]):
            item["tmax_c"] = round(float(temperature_c), 1)
            item["tmax_observed_at"] = observed_local.isoformat()

    stations: Dict[str, List[Dict[str, Any]]] = {}
    flat: List[Dict[str, Any]] = []

    for station_code, station_days in grouped.items():
        output: List[Dict[str, Any]] = []

        for day_key in sorted(station_days.keys(), reverse=True):
            item = station_days[day_key]
            times = sorted(local_times.get((station_code, day_key), []))

            first_dt = times[0] if times else None
            last_dt = times[-1] if times else None
            day_date = datetime.fromisoformat(day_key).date()

            final = day_date < today_local
            early_morning_covered = any(dt.hour < 8 for dt in times)
            afternoon_covered = any(12 <= dt.hour <= 18 for dt in times)

            coverage_hours = 0.0
            if first_dt is not None and last_dt is not None:
                coverage_hours = max(
                    0.0,
                    (last_dt - first_dt).total_seconds() / 3600.0,
                )

            tmin_complete = bool(final and early_morning_covered)
            tmax_complete = bool(final and afternoon_covered)

            item.update(
                {
                    "final": final,
                    "coverage_hours": round(coverage_hours, 2),
                    "early_morning_covered": early_morning_covered,
                    "afternoon_covered": afternoon_covered,
                    "tmin_complete": tmin_complete,
                    "tmax_complete": tmax_complete,
                    "complete_day_coverage": bool(
                        tmin_complete and tmax_complete
                    ),
                    "source": STATION_OBSERVATIONS_TABLE,
                    "method": "prospective_station_archive_daily_min_max",
                }
            )

            output.append(item)
            flat.append(item)

        stations[_station_label(station_code)] = output

    return {
        "status": "ok",
        "source": STATION_OBSERVATIONS_TABLE,
        "timezone": "Europe/Rome",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "days_requested": days,
        "rows_used": len(rows),
        "page_size": DAILY_EXTREMES_PAGE_SIZE,
        "max_rows": DAILY_EXTREMES_MAX_ROWS,
        "method": "prospective_station_archive_daily_min_max",
        "retroactive_forecast_reconstruction": False,
        "forecast_slots_used_as_observation": False,
        "stations": stations,
        "daily_extremes": flat,
    }


@api.get("/automation/observations/daily-extremes")
def automation_observations_daily_extremes(
    days: int = DAILY_EXTREMES_DEFAULT_DAYS,
) -> Dict[str, Any]:
    """Endpoint tecnico usato dal frontend per l'audit degli estremi."""
    if not _cloud_configured():
        raise HTTPException(
            status_code=503,
            detail="Archivio cloud non configurato",
        )

    try:
        return daily_station_extremes(days=days)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Estremi giornalieri non disponibili: {exc}",
        ) from exc


@api.get("/daily_extremes")
def daily_extremes_alias(
    days: int = DAILY_EXTREMES_DEFAULT_DAYS,
) -> Dict[str, Any]:
    """
    Alias semplice per il test diretto da browser.

    Esempio:
    /daily_extremes?days=14
    """
    return automation_observations_daily_extremes(days=days)


api.version = "15.2-daily-extremes-pagination-z"
api.title = "Meteo Arbus-Guspini API"
