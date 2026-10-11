import json
import threading
import unittest
from model_transport import SourceCache, model_urls, canonical_url, valid_payload

SETTINGS = {"arbus": {"lat": 39.526, "lon": 8.6}, "guspini": {"lat": 39.54, "lon": 8.635}}
URL = next(model_urls(SETTINGS))
# Synthetic payload used ONLY by offline unit tests, never emitted or archived.
GOOD = json.dumps({"hourly": {"time": ["2099-01-01T00:00", "2099-01-01T01:00"], "temperature_2m": [10.0, 11.0]}}).encode()

class TransportTests(unittest.TestCase):
    def test_sources_and_model_settings_are_unchanged(self):
        urls = list(model_urls(SETTINGS))
        self.assertEqual(len(urls), 8)
        self.assertEqual(len(set(map(canonical_url, urls))), 8)
        self.assertTrue(all("forecast_days=15" in canonical_url(u) for u in urls))

    def test_success_is_reused_byte_for_byte(self):
        calls=[]
        cache=SourceCache(lambda url: (calls.append(url) or (200, GOOD, {})))
        first=cache.get(URL)
        again=cache.get(URL)
        self.assertEqual(first[:2], (200, GOOD))
        self.assertEqual(again[1], first[1])
        self.assertEqual(first[2]["X-Meteo-SHA256"], again[2]["X-Meteo-SHA256"])
        self.assertEqual(again[2]["X-Meteo-Cache"], "HIT")
        self.assertEqual(len(calls), 1)

    def test_concurrent_duplicate_requests_are_coalesced(self):
        calls=[]
        cache=SourceCache(lambda url: (calls.append(url) or (200, GOOD, {})))
        threads=[threading.Thread(target=cache.get, args=(URL,)) for _ in range(16)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.hits, 15)

    def test_expired_data_are_not_reused(self):
        now=[1000]
        calls=[]
        cache=SourceCache(lambda url:(calls.append(url) or (200, GOOD, {})), lambda: now[0])
        cache.get(URL)
        now[0]+=1801
        cache.get(URL)
        self.assertEqual(len(calls), 2)

    def test_daily_429_is_never_weather_and_suppresses_retries(self):
        body=b'{"error":true,"reason":"Daily API request limit exceeded. Please try again tomorrow."}'
        calls=[]
        cache=SourceCache(lambda url:(calls.append(url) or (429,body,{})))
        self.assertEqual(cache.get(URL)[0],429)
        self.assertEqual(cache.get(list(model_urls(SETTINGS))[1])[0],429)
        self.assertEqual(cache.status()["fresh_entries"],0)
        self.assertEqual(len(calls),1)
        self.assertTrue(cache.status()["blocked"])

    def test_retry_after_is_respected(self):
        now=[1000]
        calls=[]
        def fetch(url):
            calls.append(url)
            return (429,b'{"error":true,"reason":"Hourly limit"}',{"Retry-After":"60"}) if len(calls)==1 else (200,GOOD,{})
        cache=SourceCache(fetch,lambda:now[0])
        cache.get(URL)
        now[0]=1059
        self.assertEqual(cache.get(URL)[0],429)
        now[0]=1061
        self.assertEqual(cache.get(URL)[0],200)
        self.assertEqual(len(calls),2)

    def test_invalid_payload_is_not_cached(self):
        cache=SourceCache(lambda url:(200,b'{"error":true}',{}))
        with self.assertRaises(ValueError):cache.get(URL)
        self.assertEqual(cache.status()["fresh_entries"],0)

    def test_ssrf_and_model_changes_are_rejected(self):
        for url in [URL.replace("api.open-meteo.com","localhost"),URL.replace("latitude=39.526","latitude=0"),URL+"&apikey=secret", URL.replace("forecast_days=15","forecast_days=16"),URL+"&latitude=39.53",URL.replace("https:","http:")]:
            with self.assertRaises((ValueError,KeyError)):canonical_url(url)

    def test_misaligned_hourly_series_rejected(self):
        with self.assertRaises(ValueError):valid_payload(b'{"hourly":{"time":["a","b"],"temperature_2m":[10]}}')

if __name__ == "__main__":unittest.main()
