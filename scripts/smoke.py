"""Read-only smoke test for a deployed relay.

    python scripts/smoke.py http://127.0.0.1:8080

Checks liveness and readiness, that the served dashboard has the same <h1> as
the dashboard.html in this checkout (i.e. the deployed build is this
version), and that the API rejects unauthenticated calls with the documented
error envelope.  It writes nothing, so it is safe against a long-lived
cluster.  Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard.html"
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def heading(html: str) -> str:
    match = H1.search(html)
    if match is None:
        raise SystemExit("FAIL: no <h1> found")
    return " ".join(match.group(1).split())


def main() -> None:
    base_url = sys.argv[1].rstrip("/")

    deadline = time.monotonic() + 60
    while True:
        try:
            if get(f"{base_url}/ready")[0] == 200:
                break
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit("FAIL: /ready did not return 200 within 60 seconds")
        time.sleep(1)
    print("ok   /ready is 200")

    status, _ = get(f"{base_url}/health")
    if status != 200:
        raise SystemExit(f"FAIL: /health returned {status}")
    print("ok   /health is 200")

    expected = heading(DASHBOARD.read_text(encoding="utf-8"))
    status, page = get(f"{base_url}/")
    if status != 200:
        raise SystemExit(f"FAIL: dashboard returned {status}")
    served = heading(page)
    if served != expected:
        raise SystemExit(f"FAIL: dashboard heading is {served!r}, expected {expected!r}")
    print(f"ok   dashboard heading is {served!r}")

    status, body = get(f"{base_url}/api/v1/agents")
    if status != 401 or json.loads(body).get("error", {}).get("code") != "missing_credentials":
        raise SystemExit(f"FAIL: unauthenticated /api/v1/agents returned {status} {body!r}")
    print("ok   unauthenticated API call is rejected with 401")


if __name__ == "__main__":
    main()
