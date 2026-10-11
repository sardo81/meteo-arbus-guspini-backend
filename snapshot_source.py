"""Read exact, fresh Open-Meteo responses captured by our bounded GitHub job.

This transport never computes weather values, changes models or writes an archive.
There is deliberately no fallback to the rate-limited Render egress connection.
"""
from __future__ import annotations
import base64
import gzip
import hashlib
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from model_transport import canonical_url, request_bytes, valid_payload

log = logging.getLogger("meteo.snapshot_source")
SNAPSHOT_URL = "https://raw.githubusercontent.com/sardo81/meteo-arbus-guspini-backend/meteo-model-data/model-snapshot.json.gz"
MAX_AGE = 3600
MAX_DECOMPRESSED = 40 * 1024 * 1024


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing source timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Source timestamp must include timezone")
    return dt.timestamp()


def decode_snapshot(blob, now):
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        raw = stream.read(MAX_DECOMPRESSED + 1)
    if len(raw) > MAX_DECOMPRESSED:
        raise ValueError("Snapshot size limit exceeded")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError("Unsupported snapshot format")
    created = timestamp(doc.get("created_at"))
    if not -30 <= now-created <= MAX_AGE:
        raise ValueError("Snapshot is expired or future-dated")
    records = doc.get("responses")
    if not isinstance(records, list) or len(records) != 8:
        raise ValueError("An atomic snapshot must contain all eight model requests")
    entries = {}
    for record in records:
        key = canonical_url(record["url"])
        captured = timestamp(record.get("fetched_at"))
        if not -30 <= now-captured <= MAX_AGE or captured > created+30:
            raise ValueError("Model response is expired or future-dated")
        body = base64.b64decode(record["body_base64"], validate=True)
        if hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise ValueError("Model response hash mismatch")
        valid_payload(body)
        if key in entries:
            raise ValueError("Duplicate model response")
        entries[key] = (captured, body, record["sha256"])
    return created, entries


class SnapshotSource:
    def __init__(self, fetch=request_bytes, clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.lock = threading.Lock()
        self.entries = {}
        self.created = 0.0
        self.next_check = 0.0
        self.downloads = 0
        self.hits = 0
        self.error = None

    def get(self, url):
        key = canonical_url(url)
        with self.lock:
            now = self.clock()
            if now >= self.next_check:
                self.next_check = now + 60
                try:
                    self.downloads += 1
                    code, blob, _ = self.fetch(SNAPSHOT_URL)
                    if code != 200:
                        raise ValueError("Snapshot download HTTP " + str(code))
                    created, entries = decode_snapshot(blob, self.clock())
                    if created < self.created:
                        raise ValueError("Snapshot rollback rejected")
                    self.created, self.entries = created, entries
                    self.error = None
                    log.warning("MODEL_SNAPSHOT_ACCEPTED responses=%s captured_at=%s age_seconds=%s", len(entries), datetime.fromtimestamp(created, timezone.utc).isoformat(), round(self.clock()-created))
                except Exception as exc:
                    self.error = str(exc)[:180]
                    log.warning("MODEL_SNAPSHOT_UNAVAILABLE %s", self.error)
            now = self.clock()
            entry = self.entries.get(key)
            if entry and -30 <= now-entry[0] <= MAX_AGE:
                self.hits += 1
                return 200, entry[1], {"X-Meteo-Cache": "SNAPSHOT", "X-Meteo-Fetched-At": datetime.fromtimestamp(entry[0], timezone.utc).isoformat(), "X-Meteo-SHA256": entry[2], "Cache-Control": "no-store"}
            reason = self.error or "No fresh snapshot for the exact requested model"
            return 503, json.dumps({"error": True, "reason": reason}).encode(), {"X-Meteo-Cache": "UNAVAILABLE", "Retry-After": "60", "Cache-Control": "no-store"}

    def status(self):
        with self.lock:
            now = self.clock()
            fresh = sum(-30 <= now-v[0] <= MAX_AGE for v in self.entries.values())
            return {"version": 2, "mode": "github-exact-source-snapshot", "ready": fresh == 8, "ttl_seconds": MAX_AGE, "fresh_entries": fresh, "upstream_requests": 0, "snapshot_downloads": self.downloads, "cache_hits": self.hits, "blocked": False, "snapshot_age_seconds": round(now-self.created) if self.created else None, "reason": self.error}
