"""Schedule fix round 10/11/26, workstream J — importing the code never loads
a .env (blind re-audit BUGSGEN-3, SCALE-29).

A keyless audit process (env -i) ran the generation pipeline and made four
real Anthropic calls: a stage boundary reached push.native_push_allowed,
which imported scheduler, whose import ran load_dotenv(.env). Only the
process entry points (hosted_dashboard, worker, main, audit_app) load it."""
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY_POINTS = {"hosted_dashboard", "main", "audit_app", "worker", "dashboard"}

_PROBE = textwrap.dedent("""
    import importlib, json, os, pathlib, pkgutil, sys, tempfile
    root = pathlib.Path(sys.argv[1])
    sys.path.insert(0, str(root))
    vol = tempfile.mkdtemp()
    open(os.path.join(vol, "reviews.db"), "a").close()
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = vol
    import dotenv, dotenv.main
    calls = []
    def _trace(*a, **k):
        import traceback
        calls.append("".join(traceback.format_stack(limit=5)))
        return False
    dotenv.load_dotenv = dotenv.main.load_dotenv = _trace
    dotenv.dotenv_values = dotenv.main.dotenv_values = lambda *a, **k: calls.append("dotenv_values") or {}
    before = dict(os.environ)
    skip = set(sys.argv[2].split(","))
    names = [p.stem for p in sorted(root.glob("*.py")) if p.stem not in skip]
    for pkg in ("dsr", "event_intel", "intelligence"):
        if (root / pkg / "__init__.py").exists():
            names.append(pkg)
            names += [pkg + "." + m.name for m in pkgutil.iter_modules([str(root / pkg)])]
    failed = {}
    for n in names:
        try:
            importlib.import_module(n)
        except BaseException as e:
            failed[n] = repr(e)[:160]
    changed = sorted(k for k in set(os.environ) | set(before) if os.environ.get(k) != before.get(k))
    print(json.dumps({"imported": len(names) - len(failed), "failed": failed, "calls": calls,
                      "changed": changed}))
""")


def _clean_env():
    env = {k: v for k, v in os.environ.items()
           if not any(s in k for s in ("API_KEY", "AUTH_TOKEN", "SECRET", "_KEY"))}
    env.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
    return env


def test_importing_every_module_loads_no_env_file_and_leaves_the_environment_as_it_was():
    out = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT), ",".join(sorted(ENTRY_POINTS))],
                         capture_output=True, text=True, timeout=180, env=_clean_env(), cwd=str(ROOT))
    assert out.returncode == 0, out.stderr[-2000:]
    import json
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["imported"] > 250
    assert got["calls"] == [], "a module loads a .env on import:\n" + "\n".join(got["calls"][:2])
    assert got["changed"] == []


def test_only_the_entry_points_load_the_env_file():
    # The entry points are the processes; nothing else may load it.
    import re
    code_call = re.compile(r"^(?!\s*#).*\b(load_dotenv|dotenv_values)\(", re.M)
    for module in sorted(ENTRY_POINTS - {"dashboard"}):
        assert "load_dotenv" in (ROOT / f"{module}.py").read_text(), module
    offenders = [p.name for p in list(ROOT.glob("*.py")) + list(ROOT.glob("*/*.py"))
                 if p.stem not in ENTRY_POINTS and "tests" not in p.parts and "scripts" not in p.parts
                 and code_call.search(p.read_text(errors="replace"))]
    assert offenders == []


def test_the_scheduler_gate_is_read_without_importing_the_scheduler(monkeypatch):
    import config
    import push
    for v in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
              "ALLOW_LOCAL_SCHEDULER", "RESTORE_FROM", "MAINTENANCE_READ_ONLY"):
        monkeypatch.delenv(v, raising=False)
    assert config.scheduling_allowed() is False and push.native_push_allowed() is False
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert config.scheduling_allowed() is True and push.native_push_allowed() is True
    monkeypatch.setenv("MAINTENANCE_READ_ONLY", "1")
    assert config.scheduling_allowed() is False
    import scheduler
    assert scheduler.scheduling_allowed() is False          # one rule, read in one place
    monkeypatch.delenv("MAINTENANCE_READ_ONLY")
    monkeypatch.setenv("RESTORE_FROM", "/tmp/x")
    assert scheduler.scheduling_allowed() is False
    import inspect
    assert "import scheduler" not in inspect.getsource(push.native_push_allowed)
