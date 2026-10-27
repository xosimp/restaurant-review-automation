"""Release-safety fix round 10/26/26, WS2 — scripts/smoke.py, /health's build
and the X-Cavnar-Build header (A05-11, A05-13, A06-13, A07-01; R15).

The checks run in-process against fakes, then once against the real app's
Flask test client — in a subprocess, because importing hosted_dashboard boots
the app (scratch volume, no provider keys)."""
import json
import os
import subprocess
import sys
import tempfile

from scripts import smoke

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = "abc4d51f0123456789abcdef0123456789abcdef"


def _fake(routes):
    def fetch(path, headers=None):
        hit = routes.get(path)
        if hit is None:
            return 404, {}, b""
        return hit(headers or {}) if callable(hit) else hit
    return fetch


def _health(build, status=200, state="ok"):
    return status, {"Content-Type": "application/json"}, json.dumps({"status": state, "build": build}).encode()


LOGIN = (200, {"Content-Type": "text/html"},
         b'<form><input name="password"></form><script src="/static/a.js?v=1"></script>'
         b'<link rel="stylesheet" href="/b/abc.css">')


def test_a_good_deploy_passes():
    out = []
    fetch = _fake({"/health": _health(SHA[:12]), "/login": LOGIN, "/static/a.js?v=1": (200, {}, b"x"),
                   "/b/abc.css": (200, {}, b"x")})
    assert smoke.run(fetch, expect_sha=SHA, out=out.append, sleep=lambda s: None) == 0
    assert all(line.startswith("PASS") for line in out)


def test_the_wrong_build_fails_with_the_rollback_step():
    out = []
    fetch = _fake({"/health": _health("000000000000"), "/login": LOGIN, "/static/a.js?v=1": (200, {}, b""),
                   "/b/abc.css": (200, {}, b"")})
    assert smoke.run(fetch, expect_sha=SHA, out=out.append, sleep=lambda s: None, retries=2) == 1
    assert any("FAIL health" in line and "expected abc4d51f0123" in line for line in out)
    assert out[-1] == smoke.ROLLBACK_HINT


def test_production_must_say_it_is_production():
    def h(env):
        return 200, {}, json.dumps({"status": "ok", "build": SHA[:12], "env": env}).encode()
    ok, detail = smoke.check_health(_fake({"/health": h("staging")}), SHA, retries=1, expect_env="production")
    assert not ok and "send" in detail
    assert smoke.check_health(_fake({"/health": h("production")}), SHA, retries=1, expect_env="production")[0]


def test_an_unknown_build_fails():
    ok, detail = smoke.check_health(_fake({"/health": _health("unknown")}), None, retries=1)
    assert not ok and "no build" in detail


def test_a_500_health_or_a_missing_static_file_fails():
    ok, detail = smoke.check_health(_fake({"/health": _health(SHA[:12], status=500, state="error")}), SHA,
                                    retries=1)
    assert not ok and "500" in detail
    ok, detail = smoke.check_assets(_fake({"/static/a.js?v=1": (200, {}, b"")}), ["/static/a.js?v=1", "/b/gone.css"])
    assert not ok and "/b/gone.css 404" in detail


def test_reads_send_the_token_as_cookie_and_bearer():
    seen = {}

    def web(h):
        seen["cookie"] = h.get("Cookie")
        return 200, {"Content-Type": "application/json"}, b"{}"

    def mobile(h):
        seen["auth"] = h.get("Authorization")
        return 200, {"Content-Type": "application/json"}, b'{"ok": true}'
    ok, detail = smoke.check_reads(_fake({"/api/home/brief": web, "/mobile/api/me": mobile}), "tok")
    assert ok and seen == {"cookie": "session_token=tok", "auth": "Bearer tok"}


def test_the_mobile_read_names_an_app_build_so_a_cutoff_never_fails_the_smoke():
    """Once MOBILE_HEADERLESS_CUTOFF is set, a headerless /mobile/api/me is
    426: the smoke would fail every deploy and say "roll back" (blind
    re-audit 10/27/26). It names itself as a build above any minimum."""
    from datetime import date

    import client_compat as cc
    env = {"MOBILE_HEADERLESS_CUTOFF": "2000-01-01", "MOBILE_MIN_BUILD": "5000"}

    def mobile(h):
        if cc.update_refusal("/mobile/api/me", "GET", h, today=date(2026, 11, 2), environ=env):
            return 426, {"Content-Type": "application/json"}, b'{"ok": false, "update_required": true}'
        return 200, {"Content-Type": "application/json"}, b'{"ok": true}'

    web = (200, {"Content-Type": "application/json"}, b"{}")
    ok, detail = smoke.check_reads(_fake({"/api/home/brief": web, "/mobile/api/me": mobile}), "tok")
    assert ok, detail
    # ...and a headerless read really is refused under that setting.
    assert mobile({"Authorization": "Bearer tok"})[0] == 426
    # The smoke's build is never kept as an installed one.
    app = cc.from_headers(smoke.SMOKE_APP_HEADERS)
    assert app is not None and app.is_smoke and app.build > 5000


SCRIPT = r'''
import json, os, sqlite3, sys
vol, sha = sys.argv[1], sys.argv[2]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                  QUEUE_WORKER="0")
for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "DATABASE_URL", "GIT_COMMIT"):
    os.environ.pop(k, None)
if sha:
    os.environ["RAILWAY_GIT_COMMIT_SHA"] = sha
else:
    os.environ.pop("RAILWAY_GIT_COMMIT_SHA", None)
import dotenv; dotenv.load_dotenv = lambda *a, **k: False  # never production's .env (audit #25)
import hosted_dashboard as h
import auth, models
from scripts import smoke
rid = models.create_restaurant(models.Restaurant(name="Smoke Co", owner_email="smoke@x.test", module_reviews=1))
uid = auth.create_user(rid, "smoke", "smoke@x.test", "correct-horse-battery", is_admin=False)
token = auth.create_session(uid, restaurant_id=rid)
c = h.app.test_client()
def fetch(path, headers=None):
    headers = dict(headers or {})
    cookie = headers.pop("Cookie", None)
    if cookie:                     # the test client keeps its own cookie jar
        name, _, value = cookie.partition("=")
        c.set_cookie(name, value)
    r = c.get(path, headers=headers)
    if cookie:
        c.delete_cookie(name)
    return r.status_code, dict(r.headers), r.data
lines = []
rc = smoke.run(fetch, expect_sha=sha or None, token=token, out=lines.append, sleep=lambda s: None, retries=1)
_s, hdrs, _b = fetch("/health")
print(json.dumps({"rc": rc, "lines": lines, "header": hdrs.get("X-Cavnar-Build"),
                  "html_header": fetch("/login")[1].get("X-Cavnar-Build")}))
'''


def _boot(sha):
    vol = tempfile.mkdtemp(prefix="cavnar-smoke-")
    out = subprocess.run([sys.executable, "-c", SCRIPT, vol, sha], cwd=ROOT, capture_output=True, text=True,
                         timeout=240)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_smoke_against_the_real_app():
    res = _boot(SHA)
    assert res["rc"] == 0, res["lines"]
    assert any(line.startswith("PASS reads") and "web and mobile" in line for line in res["lines"]), res["lines"]
    assert res["header"] == SHA[:12], "every JSON response names its build"
    assert res["html_header"] is None
    res = _boot("")
    assert res["rc"] == 1 and any("FAIL health" in line for line in res["lines"])
    assert res["header"] == "unknown"


# ── the release workflow and the SHA-lag check ───────────────────────────────

def test_release_lag_verdicts():
    from scripts import release_lag
    head_time = 1_800_000_000.0
    assert release_lag.verdict(SHA[:12], SHA, head_time, head_time + 5 * 3600)[0] is True
    ok, msg = release_lag.verdict("000000000000", SHA, head_time, head_time + 30 * 60)
    assert ok and "within" in msg
    ok, msg = release_lag.verdict("000000000000", SHA, head_time, head_time + 90 * 60)
    assert not ok and "stalled" in msg
    assert release_lag.verdict("unknown", SHA, head_time, head_time)[0] is False


def test_release_lag_measures_from_the_push_not_the_commit(monkeypatch):
    """Blind re-audit 10/27/26 (B5 P3): a commit made two hours before its
    push is not two hours late the moment it is pushed."""
    from scripts import release_lag
    assert release_lag.pushed_at(None, "main", SHA, "") == (None, "no --repo or GITHUB_TOKEN")
    committed, pushed = 1_800_000_000.0, 1_800_000_000.0 + 2 * 3600
    acts = [{"after": "f" * 40, "timestamp": "2027-01-15T08:00:00Z", "activity_type": "push"},
            {"after": SHA, "timestamp": "2027-01-15T10:00:00+00:00", "activity_type": "push"}]
    t = release_lag.push_time_from_activities(acts, SHA)
    assert t is not None and release_lag.push_time_from_activities(acts, "0" * 40) is None
    deps = [{"sha": SHA, "created_at": "2027-01-15T10:00:05Z"}, {"sha": SHA, "created_at": "2027-01-15T11:00:00Z"}]
    assert release_lag.push_time_from_deployments(deps, SHA) - t == 5
    # Thirty minutes after the push, two and a half hours after the commit: within the lag.
    assert release_lag.verdict("000000000000", SHA, pushed, pushed + 30 * 60)[0] is True
    assert release_lag.verdict("000000000000", SHA, committed, pushed + 30 * 60)[0] is False
    # main() reads the push time when it can, and says when it fell back.
    monkeypatch.setattr(release_lag, "deployed_build", lambda url: "000000000000")
    monkeypatch.setattr(release_lag, "pushed_at", lambda repo, branch, sha, token: (pushed, "the push"))
    monkeypatch.setattr(release_lag.time, "time", lambda: pushed + 30 * 60)
    assert release_lag.main(["https://x.test", "--head-sha", SHA, "--head-time", str(committed),
                             "--repo", "o/r"]) == 0
    monkeypatch.setattr(release_lag, "pushed_at", lambda repo, branch, sha, token: (None, "no --repo"))
    assert release_lag.main(["https://x.test", "--head-sha", SHA, "--head-time", str(committed)]) == 1


def test_release_workflow_smokes_each_production_deploy_and_checks_lag_hourly():
    import yaml
    with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    on = wf.get("on") or wf.get(True)
    assert "deployment_status" in on and on["schedule"][0]["cron"].split()[1] == "*"
    smoke_job, lag_job = wf["jobs"]["smoke"], wf["jobs"]["lag"]
    assert "deployment_status.state == 'success'" in smoke_job["if"]
    assert smoke_job["timeout-minutes"] <= 15 and lag_job["timeout-minutes"] <= 15
    runs = " ".join(s.get("run", "") for s in smoke_job["steps"])
    assert "scripts/smoke.py" in runs and "--expect-sha" in runs and "github.event.deployment.sha" in runs
    assert "--expect-env production" in runs
    assert any(s.get("env", {}).get("SMOKE_TOKEN") for s in smoke_job["steps"])
    lag_runs = " ".join(s.get("run", "") for s in lag_job["steps"])
    assert "scripts/release_lag.py" in lag_runs and "--repo" in lag_runs
    assert any(s.get("env", {}).get("GITHUB_TOKEN") for s in lag_job["steps"])
    assert wf["permissions"] == {"contents": "read", "deployments": "read"}
