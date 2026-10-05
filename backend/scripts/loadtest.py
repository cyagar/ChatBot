"""Load test against a deployed instance, for the "Load test against the
real single-host/Neon path" gate in docs/PRODUCTION_READINESS.md.

Exercises four paths at rising concurrency: two cheap endpoints with no DB
write (/readyz, /api/config), one real DB query with no LLM call
(/api/machines/recent), the in-process retrieval path with no LLM call
(/api/admin/query-test -- needs an administrator account), and a small,
bounded number of full end-to-end asks (retrieval + a real LLM call, so
this is the only phase with real API cost -- kept deliberately small).

Usage:
    LOADTEST_BASE_URL=https://bibchatbot.com \
    LOADTEST_EMAIL=someone@example.com LOADTEST_PASSWORD=... \
    LOADTEST_ADMIN_EMAIL=admin@example.com LOADTEST_ADMIN_PASSWORD=... \
    python scripts/loadtest.py

The admin account only needs query-test access; a throwaway administrator
created for the run and deleted afterward is the intended pattern -- never
commit real credentials here or pass them as a literal on the command line
where shell history would keep them.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.environ.get("LOADTEST_BASE_URL", "http://127.0.0.1:8000")


def login(email: str, password: str) -> str:
    req = urllib.request.Request(
        f"{BASE}/api/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.headers.get("Set-Cookie", "").split(";")[0]


def timed_request(method: str, path: str, cookie: str, body: dict | None = None, timeout: int = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method=method,
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return time.monotonic() - t0, resp.status, None
    except urllib.error.HTTPError as e:
        e.read()
        return time.monotonic() - t0, e.code, None
    except Exception as e:
        return time.monotonic() - t0, None, str(e)


def run_phase(name: str, fn, concurrency: int, total: int) -> dict:
    print(f"\n=== {name}: concurrency={concurrency} total={total} ===")
    latencies, errors = [], []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(fn) for _ in range(total)]
        for f in as_completed(futures):
            lat, status, err = f.result()
            latencies.append(lat)
            if err or (status and status >= 400):
                errors.append((status, err))
    latencies.sort()
    n = len(latencies)
    p50 = latencies[n // 2]
    p95 = latencies[min(n - 1, int(n * 0.95))]
    print(f"  n={n} errors={len(errors)} min={latencies[0]:.3f}s p50={p50:.3f}s "
          f"p95={p95:.3f}s max={latencies[-1]:.3f}s mean={statistics.mean(latencies):.3f}s")
    if errors:
        print(f"  error samples: {errors[:5]}")
    return {"n": n, "errors": len(errors), "p50": p50, "p95": p95, "max": latencies[-1]}


def main() -> None:
    email = os.environ["LOADTEST_EMAIL"]
    password = os.environ["LOADTEST_PASSWORD"]
    admin_email = os.environ.get("LOADTEST_ADMIN_EMAIL")
    admin_password = os.environ.get("LOADTEST_ADMIN_PASSWORD")

    cookie = login(email, password)
    admin_cookie = login(admin_email, admin_password) if admin_email else None
    print(f"logged in against {BASE}" + (" (with admin)" if admin_cookie else " (no admin -- skipping retrieval-only phase)"))

    results: dict[str, dict] = {}

    for c in (5, 20, 50):
        results[f"readyz_c{c}"] = run_phase(
            "GET /readyz", lambda: timed_request("GET", "/readyz", cookie), c, c * 3,
        )
    for c in (5, 20, 50):
        results[f"config_c{c}"] = run_phase(
            "GET /api/config", lambda: timed_request("GET", "/api/config", cookie), c, c * 3,
        )
    for c in (5, 20, 40):
        results[f"machines_recent_c{c}"] = run_phase(
            "GET /api/machines/recent", lambda: timed_request("GET", "/api/machines/recent", cookie), c, c * 3,
        )

    if admin_cookie:
        queries = [
            {"question": "water temperature not reaching ready", "machine_id": 1, "top_k": 6},
            {"question": "electrical safety requirements installation", "machine_id": 6, "top_k": 6},
            {"question": "control board failure troubleshooting", "machine_id": 5, "top_k": 6},
            {"question": "final rinse temperature", "machine_id": 6, "top_k": 6},
        ]
        counter = {"i": 0}

        def query_test_call():
            q = queries[counter["i"] % len(queries)]
            counter["i"] += 1
            return timed_request("POST", "/api/admin/query-test", admin_cookie, body=q)

        for c in (5, 15, 30, 60):
            results[f"query_test_c{c}"] = run_phase(
                "POST /api/admin/query-test (retrieval only, no LLM)", query_test_call, c, c * 3,
            )

    # Bounded total count to control real LLM API spend -- one conversation
    # per request, since the server's one-question-at-a-time-per-conversation
    # lock would otherwise serialize "concurrent" requests on the same one.
    def full_ask_call():
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                f"{BASE}/api/conversations",
                data=json.dumps({"machine_id": 1}).encode(),
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                conv = json.loads(resp.read())
            _, status, err = timed_request(
                "POST", f"/api/conversations/{conv['id']}/messages", cookie,
                body={"content": "What should I check if the water temperature is not reaching ready?"},
                timeout=90,
            )
            return time.monotonic() - t0, status, err
        except Exception as e:
            return time.monotonic() - t0, None, str(e)

    for c in (3, 6):
        results[f"full_ask_c{c}"] = run_phase(
            "POST full ask (retrieval + LLM, bounded)", full_ask_call, c, c * 2,
        )

    print("\n\n=== SUMMARY (json) ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
