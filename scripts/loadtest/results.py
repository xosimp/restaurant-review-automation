"""
The load test's results file (release-safety audit #25 WS10): one JSON document per run, written by
locustfile.py when the run stops, read by a person comparing runs and by nothing else. No Locust import here,
so the format and the verdict are tested without it (tests/test_ws10_loadtest_kit.py).

  scripts/loadtest/results/<scenario>-<YYYYMMDD-HHMMSS>Z.json

  {
    "format": 1,
    "scenario": "morning_burst",            # SCENARIOS
    "mode": "rolling" | "synchronized",     # the morning burst's two shapes (below)
    "host": "staging-....up.railway.app",   # the hostname only, never a URL with credentials
    "build": "<sha>" | null,                # /health's build when the run read it
    "started_at": "2026-10-26T13:00:00Z", "ended_at": "...", "seconds": 2100,
    "world": {"stores": 100, "logins": 600, "by_timezone": {...}, "by_kind": {...}},   # from users.csv
    "users": {"peak": 240, "spawn_rate": 0.8},
    "routes": [ {"name": "/api/home/brief", "method": "GET", "requests": 1234, "failures": 0,
                 "server_errors": 0, "p50_ms": 180, "p95_ms": 900, "p99_ms": 1500, "max_ms": 2400,
                 "avg_ms": 260, "rps": 1.9, "limit_p95_ms": 2000, "ok": true}, ... ],
    "health": {"p95_ms": 120, "limit_p95_ms": 300, "ok": true},
    "totals": {"requests": 54321, "failures": 3, "server_errors": 0, "rps": 25.9},
    "verdict": {"ok": false, "failed": ["/api/morning-brief p95 6200 ms > 5000 ms", ...]},
    "notes": "free text from LOADTEST_NOTES"
  }

THE VERDICT is the README's pass line for the scenario: every route's p95 under its limit (2 s; the four
measured slow routes 5 s), no 5xx at all, /health's p95 under 300 ms. The stress scenario has no pass line —
its verdict is always ok and its numbers are the capacity record. A route with fewer than MIN_REQUESTS
requests is reported but not judged (one slow call is not a p95).
"""
import json
import os
import re
from datetime import datetime, timezone

FORMAT = 1
SCENARIOS = ("morning_burst", "sustained", "stress", "soak")
DEFAULT_P95_MS = 2000
# The four routes production measured slow (7 days to 10/19/26) — judged at 5 s until fixed, not hidden.
SLOW_ROUTES = {"/api/morning-brief": 5000, "/api/good-news": 5000, "/api/labor/publish-review": 5000,
               "/api/cross-module": 5000}
HEALTH_P95_MS = 300
MIN_REQUESTS = 20
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def limit_for(name) -> int:
    """A route's p95 limit in ms (its name as the locustfile names it: the path, sometimes with a [tag])."""
    path = str(name or "").split(" ")[0].split("?")[0]
    return SLOW_ROUTES.get(path, DEFAULT_P95_MS)


def _route(row) -> dict:
    out = {"name": str(row.get("name") or ""), "method": str(row.get("method") or "GET"),
           "requests": int(row.get("requests") or 0), "failures": int(row.get("failures") or 0),
           "server_errors": int(row.get("server_errors") or 0)}
    for k in ("p50_ms", "p95_ms", "p99_ms", "max_ms", "avg_ms"):
        v = row.get(k)
        out[k] = None if v is None else round(float(v), 1)
    out["rps"] = round(float(row.get("rps") or 0.0), 3)
    out["limit_p95_ms"] = limit_for(out["name"])
    return out


def evaluate(rows, scenario, meta=None) -> dict:
    """The results document for one run: `rows` are per-route dicts (name, method, requests, failures,
    server_errors, p50_ms, p95_ms, p99_ms, max_ms, avg_ms, rps); `meta` carries mode, host, build,
    started_at, ended_at, seconds, world, users, notes."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r} (one of {', '.join(SCENARIOS)})")
    meta = dict(meta or {})
    routes, failed = [], []
    health = {"p95_ms": None, "limit_p95_ms": HEALTH_P95_MS, "ok": True}
    totals = {"requests": 0, "failures": 0, "server_errors": 0, "rps": 0.0}
    judged = scenario != "stress"
    for row in rows or ():
        r = _route(row)
        totals["requests"] += r["requests"]
        totals["failures"] += r["failures"]
        totals["server_errors"] += r["server_errors"]
        totals["rps"] = round(totals["rps"] + r["rps"], 3)
        if r["name"].startswith("/health"):
            health["p95_ms"] = r["p95_ms"]
            health["ok"] = (not judged) or r["p95_ms"] is None or r["p95_ms"] <= HEALTH_P95_MS
            if not health["ok"]:
                failed.append(f"/health p95 {r['p95_ms']:g} ms > {HEALTH_P95_MS} ms")
        ok = True
        if judged and r["requests"] >= MIN_REQUESTS and r["p95_ms"] is not None and r["p95_ms"] > r["limit_p95_ms"]:
            ok = False
            failed.append(f"{r['name']} p95 {r['p95_ms']:g} ms > {r['limit_p95_ms']} ms")
        if judged and r["server_errors"]:
            ok = False
            failed.append(f"{r['name']} {r['server_errors']} server error(s)")
        r["ok"] = ok
        routes.append(r)
    routes.sort(key=lambda r: (-(r["p95_ms"] or 0), r["name"]))
    host = str(meta.get("host") or "")
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split("@")[-1]
    return {
        "format": FORMAT, "scenario": scenario, "mode": meta.get("mode"), "host": host,
        "build": meta.get("build"), "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"),
        "seconds": meta.get("seconds"), "world": meta.get("world") or {}, "users": meta.get("users") or {},
        "routes": routes, "health": health, "totals": totals,
        "verdict": {"ok": not failed, "failed": failed}, "notes": meta.get("notes") or "",
    }


def world_of(accounts) -> dict:
    """users.csv rows -> {stores, logins, by_timezone (stores), by_kind (stores)}."""
    stores = {}
    for a in accounts or ():
        stores.setdefault(str(a.get("restaurant_id")), (a.get("timezone") or "?", a.get("kind") or "?"))
    by_tz, by_kind = {}, {}
    for tz, kind in stores.values():
        by_tz[tz] = by_tz.get(tz, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"stores": len(stores), "logins": len(accounts or ()), "by_timezone": by_tz, "by_kind": by_kind}


def file_name(result) -> str:
    stamp = str(result.get("started_at") or "").replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
    stamp = stamp.split(".")[0] or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{result['scenario']}-{stamp}Z.json"


def write(result, directory=None) -> str:
    """Write the document; its path."""
    directory = directory or RESULTS_DIR
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, file_name(result))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


# ── the morning burst's shape ───────────────────────────────────────────────

BURST_RAMP_SECONDS = 300
BURST_HOLD_SECONDS = 1800
BURST_ACTIVE_SHARE = 0.40
# The rolling burst: each zone's morning, east to west, compressed. Real mornings are an hour or more apart
# (New York 7am is Chicago 6am, Denver 5am, Los Angeles 4am, Honolulu 1-2am); a run plays each zone's
# wave WAVE_GAP_SECONDS after the one east of it, so the waves overlap the way the real ones do at their
# edges, and the run fits in an hour.
WAVE_ORDER = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Pacific/Honolulu")
WAVE_GAP_SECONDS = 600


def burst_users(t, total_logins, mode="synchronized", zone_logins=None) -> int:
    """Users the morning burst wants `t` seconds in: 40% of every seeded login, reached in 5 minutes and held
    30 (synchronized — every zone's morning at once, the worst case); or each zone's 40% ramped in its own
    wave, east to west, WAVE_GAP_SECONDS apart, each held BURST_HOLD_SECONDS (rolling). 0 when the run is
    over."""
    if mode == "rolling":
        zone_logins = zone_logins or {}
        zones = [z for z in WAVE_ORDER if zone_logins.get(z)] + \
            sorted(z for z in zone_logins if z not in WAVE_ORDER and zone_logins.get(z))
        total = 0
        last_end = 0
        for i, z in enumerate(zones):
            start = i * WAVE_GAP_SECONDS
            end = start + BURST_RAMP_SECONDS + BURST_HOLD_SECONDS
            last_end = max(last_end, end)
            want = zone_logins[z] * BURST_ACTIVE_SHARE
            if start <= t < start + BURST_RAMP_SECONDS:
                total += want * (t - start) / BURST_RAMP_SECONDS
            elif start + BURST_RAMP_SECONDS <= t < end:
                total += want
        if t >= last_end:
            return 0
        return max(1, int(round(total))) if total else 0
    if t >= BURST_RAMP_SECONDS + BURST_HOLD_SECONDS:
        return 0
    want = total_logins * BURST_ACTIVE_SHARE
    if t < BURST_RAMP_SECONDS:
        return max(1, int(round(want * t / BURST_RAMP_SECONDS)))
    return max(1, int(round(want)))


def active_zones(t, zone_logins) -> list:
    """The zones whose rolling wave is running `t` seconds in (who new users log in as)."""
    zones = [z for z in WAVE_ORDER if zone_logins.get(z)] + \
        sorted(z for z in zone_logins if z not in WAVE_ORDER and zone_logins.get(z))
    out = []
    for i, z in enumerate(zones):
        start = i * WAVE_GAP_SECONDS
        if start <= t < start + BURST_RAMP_SECONDS + BURST_HOLD_SECONDS:
            out.append(z)
    return out
