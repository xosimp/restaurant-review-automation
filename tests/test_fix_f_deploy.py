"""Workstream F — reproducible builds and the deploy shape (#112, #38, #134).

Read from the files themselves: what Railway builds and starts, and what CI
installs, are configuration, and a wrong line is a failed deploy.
"""
import json
import os
import re
import shlex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


def _pins(name):
    """{name: version} for every requirement line; a line that is not an
    exact pin fails."""
    out = {}
    for raw in _read(name).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[a-z0-9,\-]+\])?==([0-9][A-Za-z0-9.\-+]*)$", line)
        assert m, f"{name}: not an exact pin: {raw!r}"
        out[m.group(1).lower().replace("_", "-")] = m.group(3)
    return out


# ── #112: Python and every dependency pinned ─────────────────────────────────

def test_python_is_pinned_to_what_production_runs():
    assert _read(".python-version").strip() == "3.12.7"


def test_the_lock_pins_every_package_exactly():
    lock = _pins("requirements.txt")
    assert len(lock) >= 40, "the lock carries the transitive packages too"
    for pkg in ("pydantic", "httpcore", "certifi", "urllib3", "jinja2", "cffi"):
        assert pkg in lock, f"transitive {pkg} is missing from the lock"


def test_production_versions_are_the_ones_pinned():
    lock = _pins("requirements.txt")
    assert lock["anthropic"] == "0.125.0"
    assert lock["stripe"] == "11.6.0"
    assert lock["resend"] == "2.48.0"
    assert lock["gunicorn"] == "23.0.0"
    assert lock["httpx"] == "0.28.1"
    assert lock["flask"] == "3.1.3"
    # the pins that were already exact are unchanged
    assert lock["werkzeug"] == "3.1.8" and lock["sentry-sdk"] == "2.8.0"
    # 9/29/26: the three pip-audit findings, bumped on purpose (every other pin held)
    assert lock["cryptography"] == "50.0.1" and lock["pillow"] == "12.3.0" and lock["pypdf"] == "6.19.0"
    assert lock["requests"] == "2.33.1" and lock["python-dotenv"] == "1.2.2" and lock["schedule"] == "1.2.2"


def test_every_direct_pin_is_the_same_in_the_lock():
    direct, lock = _pins("requirements.in"), _pins("requirements.txt")
    assert direct, "requirements.in lists the direct dependencies"
    for name, ver in direct.items():
        assert lock.get(name) == ver, f"{name}: requirements.in says {ver}, the lock {lock.get(name)}"


def test_ci_installs_the_lock_on_the_pinned_python_and_proves_it_complete():
    ci = _read(".github/workflows/ci.yml")
    assert "python-version-file: .python-version" in ci
    assert "pip install --no-deps -r requirements-dev.txt" in ci
    assert re.search(r"run: pip check\b", ci), "pip check proves the lock is complete and consistent"
    assert "pip-audit -r requirements.txt" in ci
    dev = _read("requirements-dev.txt")
    assert "-r requirements.txt" in dev
    _pins("requirements-dev.txt")        # every dev line is an exact pin too


# ── #38 / #134: the start command ────────────────────────────────────────────

def _start():
    return json.loads(_read("railway.json"))["deploy"]["startCommand"]


def test_the_start_command_keeps_one_worker_and_four_threads():
    args = shlex.split(_start())
    assert args[0] == "gunicorn" and "hosted_dashboard:app" in args
    assert args[args.index("--workers") + 1] == "1", "CLAUDE.md: do not raise --workers past 1"
    assert args[args.index("--threads") + 1] == "4"


class _SafeAtoms(dict):
    """gunicorn's access-log atoms: a missing key renders as '-'."""
    def __getitem__(self, k):
        return super().__getitem__(k) if k in self else "-"


def test_gunicorn_writes_an_access_log_with_the_route_rule_and_request_id_never_the_path():
    args = shlex.split(_start())
    assert args[args.index("--access-logfile") + 1] == "-"
    fmt = args[args.index("--access-logformat") + 1]
    # gunicorn reads WSGI environ keys as %({key}e)s; http_layer puts the
    # route RULE there. The raw path (%(U)s) and request line (%(r)s) carry
    # tokens (/reset-password/<token>, /i/<token>), the query string codes.
    for atom in ("%(U)s", "%(r)s", "%(q)s", "%(f)s"):
        assert atom not in fmt, atom
    assert "%({cavnar.route}e)s" in fmt and "%({cavnar.request_id}e)s" in fmt and "%(M)s" in fmt
    line = fmt % _SafeAtoms({"m": "GET", "{cavnar.route}e": "/reset-password/<token>", "s": "200", "M": 12,
                             "b": "5120", "{cavnar.class}e": "web", "{cavnar.request_id}e": "0f3a9c"})
    assert line == "GET /reset-password/<token> 200 12ms bytes=5120 class=web rid=0f3a9c"
    # A request gunicorn answered itself never reached the app: no route, no id.
    assert fmt % _SafeAtoms({"m": "GET", "s": "400", "M": 0, "b": "-"}) == "GET - 400 0ms bytes=- class=- rid=-"


def test_the_app_puts_the_route_rule_and_request_id_where_gunicorn_reads_them():
    from flask import Flask, request
    import http_layer
    app = Flask(__name__)
    http_layer.register(app)
    seen = {}

    @app.route("/reset-password/<token>")
    def reset(token):
        seen.update({k: request.environ.get(k) for k in ("cavnar.route", "cavnar.request_id", "cavnar.class")})
        return "ok"
    resp = app.test_client().get("/reset-password/secret-token-123")
    assert seen["cavnar.route"] == "/reset-password/<token>"
    assert seen["cavnar.request_id"] == resp.headers["X-Request-ID"] and seen["cavnar.class"] == "web"


def test_the_deploy_healthcheck_is_health_with_room_for_a_real_boot():
    deploy = json.loads(_read("railway.json"))["deploy"]
    assert deploy["healthcheckPath"] == "/health"
    assert deploy["healthcheckTimeout"] >= 60


def test_the_procfile_starts_the_same_server_not_the_dev_one():
    proc = _read("Procfile").strip()
    assert proc.startswith("web: ")
    assert proc[len("web: "):] == _start(), "one start command, not two that disagree"
