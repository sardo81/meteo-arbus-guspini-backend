import base64
import gzip
import hashlib
import json
import unittest
from datetime import datetime, timezone
from model_transport import model_urls, canonical_url
from snapshot_source import SnapshotSource, decode_snapshot, MAX_AGE

NOW = datetime(2026, 10, 11, 20, 0, tzinfo=timezone.utc).timestamp()
URLS = list(model_urls({'arbus': {'lat': 39.526, 'lon': 8.598}, 'guspini': {'lat': 39.54, 'lon': 8.632}}))
BODY = b'{"hourly":{"time":["2026-10-12T08:00","2026-10-12T09:00"],"temperature_2m":[19.1,20.2]}}'

def document(age=10):
    stamp = datetime.fromtimestamp(NOW-age, timezone.utc).isoformat()
    return {'version': 1, 'created_at': stamp, 'responses': [{'url': u, 'fetched_at': stamp, 'sha256': hashlib.sha256(BODY).hexdigest(), 'body_base64': base64.b64encode(BODY).decode()} for u in URLS]}

def pack(doc):
    return gzip.compress(json.dumps(doc).encode())

class SnapshotTests(unittest.TestCase):
    def test_exact_bytes_and_all_eight_requests(self):
        _, entries = decode_snapshot(pack(document()), NOW)
        self.assertEqual(len(entries), 8)
        self.assertTrue(all(v[1] == BODY for v in entries.values()))
    def test_expired_or_future_data_rejected(self):
        for age in (MAX_AGE+1, -60):
            with self.assertRaises(ValueError):
                decode_snapshot(pack(document(age)), NOW)
    def test_tampering_rejected(self):
        doc = document(); doc['responses'][0]['sha256'] = '0'*64
        with self.assertRaises(ValueError): decode_snapshot(pack(doc), NOW)
    def test_missing_model_rejected(self):
        doc = document(); doc['responses'].pop()
        with self.assertRaises(ValueError): decode_snapshot(pack(doc), NOW)
    def test_duplicate_model_rejected(self):
        doc = document(); doc['responses'][1] = doc['responses'][0]
        with self.assertRaises(ValueError): decode_snapshot(pack(doc), NOW)
    def test_unsafe_host_rejected(self):
        doc = document(); doc['responses'][0]['url'] = 'http://127.0.0.1/private'
        with self.assertRaises(ValueError): decode_snapshot(pack(doc), NOW)
    def test_one_download_serves_all_slots_without_upstream_calls(self):
        calls = []
        def fetch(url): calls.append(url); return 200, pack(document()), {}
        cache = SnapshotSource(fetch=fetch, clock=lambda: NOW)
        for slot in range(3):
            for url in URLS:
                code, body, headers = cache.get(url)
                self.assertEqual(code, 200); self.assertEqual(body, BODY)
                self.assertEqual(headers['X-Meteo-Cache'], 'SNAPSHOT')
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.status()['upstream_requests'], 0)
        self.assertEqual(cache.status()['cache_hits'], 24)
    def test_unavailable_snapshot_never_falls_back_to_open_meteo(self):
        calls = []
        def fetch(url): calls.append(url); return 404, b'not found', {}
        cache = SnapshotSource(fetch=fetch, clock=lambda: NOW)
        self.assertEqual(cache.get(URLS[0])[0], 503)
        self.assertEqual(cache.get(URLS[1])[0], 503)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('open-meteo.com', calls[0])
    def test_no_stale_success_after_source_expires(self):
        clock = [NOW]
        cache = SnapshotSource(fetch=lambda u: (200, pack(document()), {}), clock=lambda: clock[0])
        self.assertEqual(cache.get(URLS[0])[0], 200)
        clock[0] += MAX_AGE+1
        self.assertEqual(cache.get(URLS[0])[0], 503)
    def test_original_capture_timestamp_is_preserved(self):
        cache = SnapshotSource(fetch=lambda u: (200, pack(document(900)), {}), clock=lambda: NOW)
        headers = cache.get(URLS[0])[2]
        self.assertEqual(datetime.fromisoformat(headers['X-Meteo-Fetched-At']).timestamp(), NOW-900)

if __name__ == '__main__': unittest.main()
