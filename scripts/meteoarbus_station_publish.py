#!/usr/bin/env python3
"""Publish genuine fresh PWS readings to the existing MeteoArbus JSON feed."""
import datetime as dt
import ftplib
import io
import json
import netrc
import os
import pathlib
import urllib.request

HOME = pathlib.Path.home()
KEY_FILE = HOME / ".cache/cloudrun-meteo-secrets/WU_API_KEY"
REMOTE = "test-v2/data/live.json"
HOST = "ftp.arbusmeteo.altervista.org"
LOCAL_BACKUP = HOME / ".cache/meteoarbus-live-backup.json"
STATIONS = {"arbus": ("IARBUS7", "2620"), "guspini": ("IGUSPI1", "160")}

def fetch(station_id):
    key = os.environ["METEO_WU_API_KEY"].strip()
    url = ("https://api.weather.com/v2/pws/observations/current?stationId="
           + station_id + "&numericPrecision=decimal&format=json&units=m&apiKey=" + key)
    req = urllib.request.Request(url, headers={"User-Agent": "MeteoArbus/14.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=22) as response:
        obs = json.load(response).get("observations", [])
    if not obs:
        raise RuntimeError(station_id + " no observations")
    item = obs[0]
    observed = dt.datetime.fromisoformat(item["obsTimeUtc"].replace("Z", "+00:00"))
    age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
    if age < -120 or age > 1800:
        raise RuntimeError(station_id + " observation not fresh")
    return item

def main():
    readings = {}
    for place, (sid, lm) in STATIONS.items():
        try:
            o = fetch(sid)
            m = o.get("metric") or {}
            readings[place] = {
                "id_wu": sid, "id_lineameteo": lm,
                "updated_at": o["obsTimeUtc"],
                "temperature_c": m.get("temp"), "humidity_pct": o.get("humidity"),
                "wind_kmh": m.get("windSpeed"), "wind_dir": o.get("winddir"),
                "gust_kmh": m.get("windGust"), "pressure_hpa": m.get("pressure"),
                "rain_today_mm": m.get("precipTotal"), "rain_rate_mm_h": m.get("precipRate"),
            }
            print(place, "fresh", o["obsTimeUtc"])
        except Exception as exc:
            print(place, "unavailable", type(exc).__name__, str(exc)[:90])
    if not readings:
        raise SystemExit("No fresh observations; existing feed unchanged")
    username = os.environ["METEO_FTP_USER"]
    password = os.environ["METEO_FTP_PASSWORD"]
    with ftplib.FTP(HOST, timeout=25) as ftp:
        ftp.login(username, password)
        buf = io.BytesIO()
        ftp.retrbinary("RETR " + REMOTE, buf.write)
        original = buf.getvalue()
        doc = json.loads(original)
        LOCAL_BACKUP.parent.mkdir(exist_ok=True)
        LOCAL_BACKUP.write_bytes(original)
        for place, value in readings.items():
            doc.setdefault("stations", {}).setdefault(place, {}).update(value)
        arbus = readings.get("arbus")
        if arbus:
            doc["current"].update({k: arbus.get(k) for k in
                ("temperature_c", "humidity_pct", "wind_kmh", "wind_dir",
                 "gust_kmh", "pressure_hpa", "rain_today_mm")})
        doc["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        content = json.dumps(doc, ensure_ascii=False, indent=2).encode()
        ftp.storbinary("STOR " + REMOTE, io.BytesIO(content))
        # A successful FTP STOR does not prove the public feed was updated.
        # Read back the remote file and fail this job on mismatched contents.
        verify_buf = io.BytesIO()
        ftp.retrbinary("RETR " + REMOTE, verify_buf.write)
        verified = json.loads(verify_buf.getvalue())
        if verified.get("updated_at") != doc["updated_at"]:
            raise RuntimeError("FTP verification failed: publication timestamp differs")
        for place, value in readings.items():
            published = verified.get("stations", {}).get(place, {})
            if published.get("updated_at") != value["updated_at"]:
                raise RuntimeError("FTP verification failed: " + place + " observation differs")
    print("PUBLISHED_AND_VERIFIED", ",".join(readings), "bytes", len(content))

if __name__ == "__main__":
    main()
