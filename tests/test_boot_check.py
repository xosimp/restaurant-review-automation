"""scripts/boot_check.py — the deploy gate's boots (audit #25 A05-01; blind
re-audit 10/27/26 B1 F7/F12, B5 "the gate never boots in production mode").

The boots themselves run as gate steps (.github/workflows/ci.yml); this file
holds the parts that decide what a boot may touch:

- a boot child's environment is an allowlist: no credential, no database
  URL but the one given, no Railway marker unless --production asks for
  production's, and the network guard on;
- the guard (scripts/boot_guard/sitecustomize.py) refuses a connection off
  the machine in a child process and logs it, and lets loopback and the
  scratch Postgres through;
- --gunicorn starts exactly railway.json's command, bound to loopback.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import boot_check  # noqa: E402

pytestmark = pytest.mark.contract


def test_a_boot_child_inherits_no_credential_and_no_database(monkeypatch, tmp_path):
    for k, v in {"ANTHROPIC_API_KEY": "sk-live", "PAGERDUTY_ROUTING_KEY": "pd", "STRIPE_SECRET_KEY": "x",
                 "DATABASE_URL": "postgresql://prod", "RAILWAY_PROJECT_ID": "p", "SENTRY_DSN": "d",
                 "TWILIO_AUTH_TOKEN": "t", "SOME_PASSWORD": "pw", "FLASK_APP": "x"}.items():
        monkeypatch.setenv(k, v)
    env = boot_check.child_env(str(tmp_path))
    leaked = {k for k in env if k in ("ANTHROPIC_API_KEY", "PAGERDUTY_ROUTING_KEY", "STRIPE_SECRET_KEY",
                                      "DATABASE_URL", "RAILWAY_PROJECT_ID", "SENTRY_DSN", "TWILIO_AUTH_TOKEN",
                                      "SOME_PASSWORD", "FLASK_APP")}
    assert leaked == set()
    assert env["RAILWAY_VOLUME_MOUNT_PATH"] == str(tmp_path) and env["CAVNAR_NET_GUARD"] == "1"
    assert env["PYTHONPATH"] == boot_check.GUARD_DIR


def test_postgres_and_production_markers_only_when_asked(tmp_path):
    url = "postgresql://postgres@127.0.0.1:55433/cavnar_boot"
    local = boot_check.child_env(str(tmp_path), url)
    assert local["DATABASE_URL"] == url and local["ALLOW_LOCAL_PG"] == "1"
    assert local["CAVNAR_NET_GUARD_ALLOW"] == "127.0.0.1"
    assert not any(k.startswith("RAILWAY_") and k != "RAILWAY_VOLUME_MOUNT_PATH" for k in local)
    prod = boot_check.child_env(str(tmp_path), url, production=True)
    assert prod["RAILWAY_ENVIRONMENT_NAME"] == "production" and prod["RAILWAY_PROJECT_ID"]
    # On Railway's markers the URL is honoured as production's own is; no local override.
    assert "ALLOW_LOCAL_PG" not in prod
    import config
    assert set(config.RAILWAY_MARKERS) <= set(prod)


def _guarded(code, tmp_path, allow=""):
    log = tmp_path / "blocked.log"
    env = boot_check.child_env(str(tmp_path), guard_log=str(log))
    if allow:
        env["CAVNAR_NET_GUARD_ALLOW"] = allow
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    return r, (log.read_text() if log.exists() else "")


def test_the_guard_refuses_a_connection_off_the_machine_in_a_child_and_logs_it(tmp_path):
    r, log = _guarded("import socket\ntry:\n    socket.create_connection(('203.0.113.7', 9), timeout=2)\n"
                      "except ConnectionRefusedError as e:\n    print('REFUSED', e)\n", tmp_path)
    assert "REFUSED boot guard" in r.stdout, r.stdout + r.stderr
    assert "203.0.113.7:9" in log
    assert boot_check._blocked(str(tmp_path / "blocked.log")) == ["203.0.113.7:9"]


def test_the_guard_lets_loopback_through(tmp_path):
    # Nothing listens on port 9: the OS refuses, never the guard, and nothing
    # is logged. (The scratch Postgres's host reaches the guard's allowlist
    # through CAVNAR_NET_GUARD_ALLOW — test_postgres_and_production_markers.)
    code = ("import socket\nfor host in ('127.0.0.1', '::1', 'localhost'):\n"
            "    try:\n        socket.create_connection((host, 9), timeout=2)\n"
            "    except OSError as e:\n        print(host, 'GUARD' if 'boot guard' in str(e) else 'os')\n")
    r, log = _guarded(code, tmp_path)
    assert log == "" and "GUARD" not in r.stdout and r.stdout.count(" os") >= 2, r.stdout + r.stderr


def test_gunicorn_is_started_as_railway_starts_it_on_loopback():
    with open(os.path.join(ROOT, "railway.json"), encoding="utf-8") as fh:
        start = json.load(fh)["deploy"]["startCommand"].split()
    cmd = boot_check.gunicorn_command(8765)
    assert cmd[cmd.index("hosted_dashboard:app")] == start[1]
    for flag in ("--workers", "--threads", "--timeout", "--keep-alive"):
        assert cmd[cmd.index(flag) + 1] == start[start.index(flag) + 1], flag
    assert cmd[cmd.index("--bind") + 1] == "127.0.0.1:8765"


def test_wait_for_returns_the_first_answer_or_gives_up():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s
    answers = iter([False, False, "up"])
    assert boot_check.wait_for(lambda: next(answers), 10, clock=clock, sleep=sleep) == "up"
    t[0] = 0.0
    assert boot_check.wait_for(lambda: False, 5, clock=clock, sleep=sleep) is None and t[0] >= 5


def test_a_production_boot_summary_names_the_tick():
    line = json.dumps({"status": 200, "env": "production", "boot_ms": 4000, "scheduler_alive": True,
                       "tick_completed_at": "2026-10-27 06:35:25", "health": json.dumps({"status": "ok"})})
    got = json.loads(boot_check.summary(line))
    assert got == {"status": 200, "env": "production", "boot_ms": 4000, "scheduler_alive": True,
                   "tick_completed_at": "2026-10-27 06:35:25", "health": "ok"}
