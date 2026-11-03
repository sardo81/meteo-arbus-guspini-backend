#!/usr/bin/env python3
import os
from urllib.request import Request, urlopen

backend = os.environ["BACKEND_URL"].rstrip("/")
secret = os.environ["AUTOMATION_SECRET"]
request = Request(
    backend + "/automation/rain-events",
    headers={"X-Automation-Secret": secret},
    method="GET",
)
with urlopen(request, timeout=120) as response:
    print(response.read().decode("utf-8"))
