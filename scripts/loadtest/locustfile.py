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

Scenarios (README.md): burst (8am), sustained, stress (ramp to failure),
soak (8 h), failure recovery (kill the queue worker mid-run).

Run:
  LOADTEST_HOST=https://staging.example.up.railway.app \
  LOADTEST_USERS=scripts/loadtest/users.csv \
  locust -f scripts/loadtest/locustfile.py --host "$LOADTEST_HOST"
"""
import csv
import os
import random
import threading
from urllib.parse import urlparse

from locust import HttpUser, between, events, task

_ALLOWED = ("localhost", "127.0.0.1", "staging", ".test", ".localhost")


def _guard(host: str):
    h = (urlparse(host).hostname or "").lower()
    if not any(a in h for a in _ALLOWED):
        raise SystemExit(f"refusing to load-test {h!r}: only staging or local hosts (scripts/loadtest/README.md)")
    if "cavnar.ai" in h and "staging" not in h:
        raise SystemExit("refusing production")


@events.test_start.add_listener
def _on_start(environment, **_):
    _guard(environment.host or "")


_USERS = []
_LOCK = threading.Lock()


def _accounts():
    """users.csv from seed_staging.py: username,password,restaurant_id,role."""
    global _USERS
    with _LOCK:
        if not _USERS:
            path = os.environ.get("LOADTEST_USERS", "scripts/loadtest/users.csv")
            with open(path) as f:
                _USERS = list(csv.DictReader(f))
        return random.choice(_USERS)


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
