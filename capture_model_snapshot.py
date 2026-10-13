"""Capture eight current model responses once, atomically, with exact provenance.

Run only in the single scheduled GitHub workflow. A source data error stops the job;
transient network/TLS failures may be retried a small fixed number of times against
the exact same URL. No IP rotation, no partial publication and no forecast-state writes.
"""
from __future__ import annotations
import base64
import gzip
import hashlib
import json
import pathlib
import sys
import time
from urllib.error import URLError
from datetime import datetime, timezone
from urllib.request import urlopen
from model_transport import model_urls, canonical_url, request_bytes, valid_payload


def capture(destination):
    with urlopen("https://meteo-arbus-guspini-api.onrender.com/automation/state", timeout=45) as response:
        state = json.load(response)["state"]
    urls = list(model_urls(state["settings"]))
    if len(urls) != 8 or len(set(map(canonical_url, urls))) != 8:
        raise RuntimeError("Unexpected model request set")
    records = []
    for url in urls:
        canonical = canonical_url(url)
        last_error = None
        for attempt in range(1, 4):
            try:
                code, body, _ = request_bytes(canonical)
                last_error = None
                break
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                print(
                    "CAPTURE_TRANSPORT_RETRY",
                    len(records) + 1,
                    f"attempt={attempt}",
                    type(exc).__name__,
                    flush=True,
                )
                if attempt < 3:
                    time.sleep(3)
        if last_error is not None:
            raise RuntimeError(
                "Capture stopped after transient transport failures; "
                "existing snapshot not replaced"
            ) from last_error
        if code != 200:
            print("CAPTURE_SOURCE_UNAVAILABLE", code, body[:250].decode(errors="replace"), flush=True)
            raise RuntimeError("Capture stopped; existing snapshot not replaced")
        data = valid_payload(body)
        captured = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(body).hexdigest()
        records.append({"url": canonical, "fetched_at": captured, "sha256": digest, "body_base64": base64.b64encode(body).decode("ascii")})
        print("CAPTURE_SOURCE_PASS", len(records), "http=200", "sha256="+digest, "first="+data["hourly"]["time"][0], "last="+data["hourly"]["time"][-1], flush=True)
    created = datetime.now(timezone.utc).isoformat()
    bundle = {"version": 1, "created_at": created, "responses": records}
    compressed = gzip.compress(json.dumps(bundle, separators=(",", ":")).encode(), mtime=0)
    from snapshot_source import decode_snapshot
    _, verified = decode_snapshot(compressed, datetime.now(timezone.utc).timestamp())
    if len(verified) != 8:
        raise RuntimeError("Snapshot round-trip validation failed")
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "model-snapshot.json.gz").write_bytes(compressed)
    manifest = {"version": 1, "created_at": created, "requests": 8, "sha256": hashlib.sha256(compressed).hexdigest(), "responses": [{k:v for k,v in row.items() if k != "body_base64"} for row in records]}
    (destination / "model-snapshot-manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print("CAPTURE_PASS responses=8 source_bytes_unchanged=True forecast_archive_writes=0 bytes="+str(len(compressed)), flush=True)

if __name__ == "__main__":
    capture(sys.argv[1] if len(sys.argv)>1 else "snapshot-output")
