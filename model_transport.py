"""Shared transport for existing model requests; never writes forecast archives."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

log = logging.getLogger("meteo.model_transport")
ALLOWED = {("api.open-meteo.com", "/v1/gfs"), ("api.open-meteo.com", "/v1/ecmwf"), ("ensemble-api.open-meteo.com", "/v1/ensemble")}
COMMON = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "pressure_msl", "cloud_cover", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "temperature_925hPa", "geopotential_height_925hPa", "temperature_850hPa", "geopotential_height_850hPa"]
EXTRA = ["rain", "showers", "cape", "lifted_index", "convective_inhibition", "weather_code"]
ENSEMBLE = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "pressure_msl", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"]
MAX_BYTES = 16 * 1024 * 1024
CACHE_SECONDS = 1800

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def canonical_url(url):
    if not isinstance(url, str) or len(url) > 3000:
        raise ValueError("Invalid model URL")
    u = urlsplit(url)
    if (u.scheme != "https" or u.username or u.password or u.fragment or u.port not in (None, 443) or (u.hostname, u.path) not in ALLOWED):
        raise ValueError("Model source is not allowed")
    pairs = parse_qsl(u.query, keep_blank_values=True)
    q = dict(pairs)
    allowed_keys = {"latitude", "longitude", "elevation", "hourly", "models", "wind_speed_unit", "timezone", "forecast_days"}
    if len(q) != len(pairs) or set(q) - allowed_keys:
        raise ValueError("Unsupported model parameters")
    if not (39.40 <= float(q["latitude"]) <= 39.70 and 8.40 <= float(q["longitude"]) <= 8.80):
        raise ValueError("Coordinates outside Arbus-Guspini")
    if (q.get("elevation") != "nan" or q.get("wind_speed_unit") != "kmh" or q.get("timezone") != "Europe/Rome" or q.get("forecast_days") != "15"):
        raise ValueError("Model settings must remain unchanged")
    variables = q.get("hourly", "").split(",")
    if not variables or len(variables) != len(set(variables)):
        raise ValueError("Invalid variables")
    if u.path == "/v1/ensemble":
        if q.get("models") not in {"gfs025", "ecmwf_ifs025"} or set(variables) != set(ENSEMBLE):
            raise ValueError("Unsupported ensemble query")
    else:
        diagnostic = set(EXTRA) - ({"lifted_index"} if u.path == "/v1/ecmwf" else set())
        if "models" in q or set(variables) not in (set(COMMON), set(COMMON) | diagnostic):
            raise ValueError("Unsupported deterministic query")
    return "https://" + u.hostname + u.path + "?" + urlencode(sorted(q.items()))

def request_bytes(url, method="GET", body=None, headers=None):
    request = Request(url, data=body, headers=headers or {}, method=method)
    opener = build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=60)
    except HTTPError as exc:
        response = exc
    with response:
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("Model response exceeds the size limit")
        return response.code, payload, dict(response.headers)

def valid_payload(body):
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Source returned an error, not weather data")
    hourly = data.get("hourly")
    times = hourly.get("time") if isinstance(hourly, dict) else None
    if not isinstance(times, list) or not times or any(not isinstance(t, str) for t in times):
        raise ValueError("Missing hourly time axis")
    if times != sorted(set(times)):
        raise ValueError("Invalid hourly time axis")
    temp_keys = [k for k in hourly if k == "temperature_2m" or k.startswith("temperature_2m_member")]
    if not temp_keys or not any(any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in hourly[k]) for k in temp_keys if isinstance(hourly[k], list)):
        raise ValueError("Missing real temperature values")
    for key, values in hourly.items():
        if key != "time" and (not isinstance(values, list) or len(values) != len(times)):
            raise ValueError("Misaligned hourly values")
    return data

class SourceCache:
    def __init__(self, fetch=request_bytes, clock=time.time):
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}
        self.blocked_until = 0.0
        self.block_reason = None
        self.upstream = 0
        self.hits = 0

    def get(self, url):
        key = canonical_url(url)
        with self.lock:
            now = self.clock()
            cached = self.entries.get(key)
            if cached and 0 <= now - cached[0] < CACHE_SECONDS:
                self.hits += 1
                return self._reply(cached, "HIT")
            if now < self.blocked_until:
                return 429, json.dumps({"error": True, "reason": self.block_reason}).encode(), {"Retry-After": str(max(1, int(self.blocked_until - now))), "X-Meteo-Cache": "BLOCKED"}
            self.upstream += 1
            status, body, headers = self.fetch(key)
            if status != 200:
                try:
                    reason = json.loads(body).get("reason") or "Model source unavailable"
                except (ValueError, AttributeError):
                    reason = "Model source unavailable"
                if status == 429:
                    retry = headers.get("Retry-After", headers.get("retry-after", ""))
                    try:
                        delay = float(retry)
                    except (TypeError, ValueError):
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - now
                        except (TypeError, ValueError, IndexError):
                            delay = 300
                    if "daily" in reason.lower():
                        next_day = (datetime.fromtimestamp(now, timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                        delay = max(delay, next_day.timestamp() - now)
                    self.blocked_until = now + max(1, delay)
                    self.block_reason = reason
                log.warning("MODEL_UPSTREAM status=%s path=%s reason=%s", status, urlsplit(key).path, reason[:200])
                return status, body, {"X-Meteo-Cache": "ERROR", "Retry-After": str(max(1, int(self.blocked_until - now)))}
            valid_payload(body)
            fetched = self.clock()
            entry = (fetched, body, hashlib.sha256(body).hexdigest())
            self.entries = {k: v for k, v in self.entries.items() if 0 <= fetched-v[0] < CACHE_SECONDS}
            if len(self.entries) >= 16:
                del self.entries[min(self.entries, key=lambda k: self.entries[k][0])]
            self.entries[key] = entry
            log.warning("MODEL_UPSTREAM status=200 path=%s sha256=%s bytes=%s", urlsplit(key).path, entry[2], len(body))
            return self._reply(entry, "MISS")

    def _reply(self, entry, kind):
        return 200, entry[1], {"X-Meteo-Cache": kind, "X-Meteo-Fetched-At": datetime.fromtimestamp(entry[0], timezone.utc).isoformat(), "X-Meteo-SHA256": entry[2], "Cache-Control": "no-store"}

    def status(self):
        with self.lock:
            now = self.clock()
            return {"version": 1, "ttl_seconds": CACHE_SECONDS, "fresh_entries": sum(0 <= now-v[0] < CACHE_SECONDS for v in self.entries.values()), "upstream_requests": self.upstream, "cache_hits": self.hits, "blocked": now < self.blocked_until, "reason": self.block_reason if now < self.blocked_until else None}

def model_urls(settings):
    for place in ("arbus", "guspini"):
        p = settings[place]
        for model in ("gfs", "ecmwf", "gfs025", "ecmwf_ifs025"):
            ensemble = model in ("gfs025", "ecmwf_ifs025")
            variables = ENSEMBLE if ensemble else COMMON + [v for v in EXTRA if model == "gfs" or v != "lifted_index"]
            params = {"latitude": p["lat"], "longitude": p["lon"], "elevation": "nan", "hourly": ",".join(variables), "wind_speed_unit": "kmh", "timezone": "Europe/Rome", "forecast_days": 15}
            if ensemble:
                params["models"] = model
            base = "https://ensemble-api.open-meteo.com/v1/ensemble" if ensemble else "https://api.open-meteo.com/v1/" + model
            yield base + "?" + urlencode(params)

def install_routes(api, cloud_state_get):
    from fastapi import Header, HTTPException
    from fastapi.responses import Response
    if os.getenv("METEO_MODEL_SOURCE", "") == "github-snapshot":
        from snapshot_source import SnapshotSource
        cache = SnapshotSource()
    else:
        cache = SourceCache()

    @api.post("/automation/models/fetch")
    def fetch_model(payload: dict, x_automation_secret: str | None = Header(default=None)):
        expected = os.getenv("AUTOMATION_SECRET", "").strip()
        if not expected or not x_automation_secret or not secrets.compare_digest(expected, x_automation_secret):
            raise HTTPException(status_code=401, detail="Invalid automation key")
        try:
            code, body, headers = cache.get(payload.get("url"))
            return Response(body, status_code=code, headers=headers, media_type="application/json")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            log.warning("MODEL_TRANSPORT_ERROR %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Model source temporarily unavailable") from exc

    @api.get("/automation/models/status")
    def model_status():
        return cache.status()

    def warm():
        try:
            settings = cloud_state_get()["state"]["settings"]
            for url in model_urls(settings):
                code, _, _ = cache.get(url)
                if code == 429:
                    break
            log.warning("MODEL_PREFLIGHT %s", json.dumps(cache.status(), sort_keys=True))
        except Exception as exc:
            log.warning("MODEL_PREFLIGHT_FAILED %s: %s", type(exc).__name__, str(exc)[:180])

    @api.on_event("startup")
    async def warm_models():
        threading.Thread(target=warm, name="meteo-model-preflight", daemon=True).start()

def fulfil_cron_model(route, backend, secret):
    url = route.request.url
    parsed = urlsplit(url)
    if (parsed.hostname, parsed.path) not in ALLOWED:
        return False
    canonical_url(url)
    code, body, headers = request_bytes(backend.rstrip("/") + "/automation/models/fetch", method="POST", body=json.dumps({"url": url}).encode(), headers={"Content-Type": "application/json", "X-Automation-Secret": secret})
    h = {k.lower(): v for k, v in headers.items()}
    proof = {k: h.get(k) for k in ("x-meteo-cache", "x-meteo-fetched-at", "x-meteo-sha256")}
    print("MODEL_TRANSPORT", parsed.path, "status=" + str(code), json.dumps(proof), flush=True)
    route.fulfill(status=code, body=body, headers={"Content-Type": "application/json", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
    return True
