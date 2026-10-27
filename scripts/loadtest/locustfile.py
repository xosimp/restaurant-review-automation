"""
Cavnar AI load test — realistic traffic for N restaurants (scale audit 10/19/26).

NEVER point this at production or at RPOWER. It refuses a host that is not a
staging host or localhost (see _guard) and expects restaurants seeded by
scripts/loadtest/seed_staging.py, every one on the simulated RPOWER store
(demo_store), so no POS, Google, Twilio, Resend or model is ever called.

Traffic model (shares of virtual users, each one a person at a restaurant):
  OwnerMorning   35%  opens the morning brief, last night's report, Labor; 7-9am burst
  ManagerShift   35%  Labor, the Studio's week, Reviews, Food Cost, a few edits
  StaffApp       25%  the iPhone app: home, schedule, reviews (Bearer token)
  HeavyReader     5%  Bar, Intel, Reports history, Ask (Ask is read-only here)
Think times come from a log-normal (median 20 s, long tail), not a fixed wait.

Scenarios (README.md): morning burst, sustained, stress (ramp to failure),
soak (8 h), failure recovery (kill the queue worker mid-run).

LOADTEST_SCENARIO=morning_burst drives the user count itself (MorningBurst, a
LoadTestShape — no --users): 40% of every seeded login in 5 minutes, held 30
(LOADTEST_BURST_MODE=synchronized, the default — every zone's morning at
once), or each time zone's 40% in its own wave, east to west, 10 minutes
apart (rolling — new users log in as the zones whose wave is running). One
Health user probes /health every 5 s throughout. When the run stops the
results file is written (results.py: scripts/loadtest/results/
<scenario>-<stamp>Z.json, with the verdict against the README's pass line).

Run:
  LOADTEST_HOST=https://staging.example.up.railway.app \
  LOADTEST_USERS=scripts/loadtest/users.csv LOADTEST_SCENARIO=morning_burst \
  locust -f scripts/loadtest/locustfile.py --host "$LOADTEST_HOST" --headless
"""
import csv
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from locust import HttpUser, LoadTestShape, between, constant, events, task

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import results as _results  # noqa: E402  (scripts/loadtest/results.py — no Locust in it)

_ALLOWED = ("localhost", "127.0.0.1", "staging", ".test", ".localhost")


def _guard(host: str):
    h = (urlparse(host).hostname or "").lower()
    if not any(a in h for a in _ALLOWED):
        raise SystemExit(f"refusing to load-test {h!r}: only staging or local hosts (scripts/loadtest/README.md)")
    if "cavnar.ai" in h and "staging" not in h:
        raise SystemExit("refusing production")


SCENARIO = (os.environ.get("LOADTEST_SCENARIO") or "sustained").strip().lower()
BURST_MODE = (os.environ.get("LOADTEST_BURST_MODE") or "synchronized").strip().lower()
_RUN = {"started": None, "started_at": None, "build": None, "peak_users": 0}


@events.test_start.add_listener
def _on_start(environment, **_):
    _guard(environment.host or "")
    if SCENARIO not in _results.SCENARIOS:
        raise SystemExit(f"LOADTEST_SCENARIO={SCENARIO!r}: one of {', '.join(_results.SCENARIOS)}")
    _RUN["started"] = time.monotonic()
    _RUN["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_USERS = []
_LOCK = threading.Lock()


def _all_accounts():
    """users.csv from seed_staging.py: username,password,restaurant_id,role,timezone,kind."""
    global _USERS
    with _LOCK:
        if not _USERS:
            path = os.environ.get("LOADTEST_USERS", "scripts/loadtest/users.csv")
            with open(path) as f:
                _USERS = list(csv.DictReader(f))
        return _USERS


def _zone_logins():
    out = {}
    for a in _all_accounts():
        z = a.get("timezone") or "?"
        out[z] = out.get(z, 0) + 1
    return out


def _accounts():
    """One login to act as: any, or in a rolling burst one from a zone whose morning wave is running."""
    pool = _all_accounts()
    if SCENARIO == "morning_burst" and BURST_MODE == "rolling" and _RUN["started"] is not None:
        zones = set(_results.active_zones(time.monotonic() - _RUN["started"], _zone_logins()))
        live = [a for a in pool if (a.get("timezone") or "?") in zones]
        pool = live or pool
    return random.choice(pool)


if SCENARIO == "morning_burst":
    class MorningBurst(LoadTestShape):
        """The morning burst (README): the user count over time, from users.csv's logins."""

        def tick(self):
            t = self.get_run_time()
            want = _results.burst_users(t, len(_all_accounts()), BURST_MODE, _zone_logins())
            if want <= 0:
                return None
            _RUN["peak_users"] = max(_RUN["peak_users"], want)
            return want, max(1.0, want / 60.0)


def _server_errors(environment):
    """{(name, method): 5xx count} from Locust's error table (aggregated across workers)."""
    out = {}
    for err in environment.stats.errors.values():
        if re.search(r"\b5\d\d\b", str(err.error)):
            key = (err.name, err.method)
            out[key] = out.get(key, 0) + int(err.occurrences)
    return out


def _rows(environment):
    errs = _server_errors(environment)
    rows = []
    for (name, method), e in environment.stats.entries.items():
        if not e.num_requests:
            continue
        rows.append({"name": name, "method": method, "requests": e.num_requests, "failures": e.num_failures,
                     "server_errors": errs.get((name, method), 0),
                     "p50_ms": e.get_response_time_percentile(0.5), "p95_ms": e.get_response_time_percentile(0.95),
                     "p99_ms": e.get_response_time_percentile(0.99), "max_ms": e.max_response_time,
                     "avg_ms": e.avg_response_time, "rps": e.total_rps})
    return rows


@events.test_stop.add_listener
def _on_stop(environment, **_):
    """The results file (results.py) — on the master in a distributed run, where the stats are whole."""
    from locust.runners import WorkerRunner
    if isinstance(environment.runner, WorkerRunner) or _RUN["started"] is None:
        return
    seconds = round(time.monotonic() - _RUN["started"])
    result = _results.evaluate(_rows(environment), SCENARIO, {
        "mode": BURST_MODE if SCENARIO == "morning_burst" else None, "host": environment.host or "",
        "build": _RUN["build"], "started_at": _RUN["started_at"],
        "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "seconds": seconds,
        "world": _results.world_of(_all_accounts()),
        "users": {"peak": _RUN["peak_users"] or getattr(environment.runner, "user_count", 0)},
        "notes": os.environ.get("LOADTEST_NOTES", "")})
    path = _results.write(result, os.environ.get("LOADTEST_RESULTS_DIR") or None)
    verdict = "PASS" if result["verdict"]["ok"] else "FAIL: " + "; ".join(result["verdict"]["failed"][:5])
    print(f"[loadtest] {SCENARIO}: {verdict} -> {path}", flush=True)


class Health(HttpUser):
    """One probe of /health every 5 s for the whole run (the README's /health p95 line), and the build it
    names for the results file."""
    fixed_count = 1
    weight = 1
    wait_time = constant(5)

    @task
    def health(self):
        r = self.client.get("/health", name="/health")
        if _RUN["build"] is None and r.ok:
            try:
                _RUN["build"] = (r.json() or {}).get("build")
            except ValueError:
                pass


def _think():
    # Log-normal think time: median ~20 s, 95th ~2 min - people read before clicking.
    return min(300.0, random.lognormvariate(3.0, 0.9))


class _Web(HttpUser):
    abstract = True
    wait_time = between(1, 2)          # replaced per task by _think via self._pause

    def on_start(self):
        a = _accounts()
        self.rid = a["restaurant_id"]
        self.client.get("/login", name="/login [GET]")
        csrf = self.client.cookies.get("csrf_js", "")
        r = self.client.post("/login", data={"username": a["username"], "password": a["password"], "csrf_token": csrf},
                             headers={"X-CSRF": csrf}, name="/login [POST]", allow_redirects=False)
        if r.status_code not in (200, 302):
            raise RuntimeError(f"login failed {r.status_code}")
        self.csrf = self.client.cookies.get("csrf_js", csrf)

    def _get(self, path, name=None):
        return self.client.get(path, name=name or path)

    def _post(self, path, json=None, name=None):
        return self.client.post(path, json=json or {}, headers={"X-CSRF": self.csrf}, name=name or path)

    def _pause(self):
        import gevent
        gevent.sleep(_think())


# What one dashboard open fires, MEASURED from production's request rollups (7 days to 10/19/26): the page
# shell, then ~35 API reads at once. The slow ones are the point: /api/morning-brief averaged 13.4 s,
# /api/good-news 11.1 s, /api/labor/publish-review 10.1 s, /api/cross-module 3.3 s, /api/home/brief 1.35 s.
DASHBOARD_OPEN = ("/api/activity", "/api/notifications/unread-count", "/api/status", "/api/review-stats",
                  "/api/benchmarks/card", "/api/dsr?limit=1", "/api/home/brief", "/api/morning-brief",
                  "/api/passkeys", "/api/goals", "/api/people", "/api/team/inbox", "/api/data-health",
                  "/api/changelog/unread-count", "/api/review-request-stats", "/api/value", "/api/actions",
                  "/api/guest-qr", "/api/closeout", "/api/cross-module", "/api/dna", "/api/good-news", "/api/issues")
LABOR_OPEN = ("/api/labor/schedule-history", "/api/labor/publish-review", "/api/labor/bench", "/api/labor/compliance",
              "/api/labor/auto-draft")


class OwnerMorning(_Web):
    weight = 35

    @task(6)
    def morning(self):
        self._get("/", "/ (dashboard shell)")
        for p in DASHBOARD_OPEN:
            self._get(p, p.split("?")[0])
        self._pause()

    @task(2)
    def labor(self):
        for p in LABOR_OPEN:
            self._get(p)
        self._pause()

    @task(1)
    def report_history(self):
        self._get("/api/dsr?limit=30", "/api/dsr [history]")
        self._pause()


class ManagerShift(_Web):
    weight = 35

    @task(4)
    def labor(self):
        for p in LABOR_OPEN:
            self._get(p)
        self._pause()

    @task(2)
    def poll(self):
        # What an open tab does between clicks: the unread counters and the status ping.
        self._get("/api/notifications/unread-count")
        self._get("/api/status")
        self._pause()

    @task(2)
    def food_cost(self):
        self._get("/api/food-cost/cfo")
        self._get("/api/food-cost/plates")
        self._pause()

    @task(1)
    def small_write(self):
        # A small write every so often: activity logging is the commonest POST measured (388 in 7 days).
        self._post("/api/log-activity", {"event": "loadtest", "module": "labor"}, "/api/log-activity [POST]")
        self._pause()


class HeavyReader(_Web):
    weight = 5

    @task(3)
    def bar(self):
        self._get("/api/food-cost/bar")
        self._pause()

    @task(2)
    def intel(self):
        self._get("/api/ai-visibility")
        self._get("/api/intel/movement")
        self._pause()

    @task(1)
    def ask_history(self):
        self._get("/api/ask-cavnar/conversations")
        self._pause()


class StaffApp(HttpUser):
    weight = 25
    wait_time = between(1, 2)

    def on_start(self):
        a = _accounts()
        r = self.client.post("/mobile/api/login", json={"username": a["username"], "password": a["password"]},
                             name="/mobile/api/login")
        tok = (r.json() or {}).get("token") if r.ok else None
        if not tok:
            raise RuntimeError(f"mobile login failed {r.status_code}")
        self.client.headers.update({"Authorization": f"Bearer {tok}"})

    @task(5)
    def home(self):
        self.client.get("/mobile/api/home", name="/mobile/api/home")
        import gevent
        gevent.sleep(_think())

    @task(3)
    def reviews(self):
        self.client.get("/mobile/api/reviews", name="/mobile/api/reviews")
        import gevent
        gevent.sleep(_think())
