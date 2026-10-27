"""No production module binds models.get_conn at import (audit #25 A04-07).

`from models import get_conn` at module top level copies the function object
into the importing module, so a test's monkeypatch of `models.get_conn` never
reaches that module's bare get_conn() calls: they read and write whatever
database the copy was bound to, and the test certifies behaviour against the
wrong file. CLAUDE.md ("Bound imports") prescribes a module-level `get_conn`
that resolves through `models` at call time; this ratchet holds every tracked
production module to it. An import inside a function body is resolved when
the function runs and is not a bound copy, so it is allowed.

`models.get_conn`'s own default is None (it maps None to models.DB_PATH when
called), so a bare `get_conn()` follows a patched DB_PATH too. A FUNCTION
whose own parameter defaults to the path (`def f(rid, db_path=DB_PATH)` —
~1,900 of them, in 113 modules) does not: the default was bound when the
module was imported, so a test that patches models.DB_PATH and calls `f(rid)`
reads the session's default database (blind re-audit 10/27/26 B4 "bound
DB_PATH defaults"). This file does not cure that; it counts those defaults
per module against tests/ratchets/db_path_defaults.json, which may only go
down — new code takes `db_path=None` (get_conn maps None to DB_PATH when it
runs), and a test that needs another file passes db_path explicitly.
"""
import ast
import os
import subprocess

import pytest

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bound_get_conn(source):
    """The line numbers of module-level `from models import get_conn` (under
    any alias) in `source`, or [] when the module also defines a top-level
    get_conn (the call-time shim) or imports none."""
    tree = ast.parse(source)
    lines = [n.lineno for n in tree.body
             if isinstance(n, ast.ImportFrom) and n.module == "models" and n.level == 0
             and any(a.name == "get_conn" for a in n.names)]
    if not lines:
        return []
    defines = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "get_conn"
                  for n in tree.body)
    return [] if defines else lines


DB_PATH_DEFAULTS = os.path.join(ROOT, "tests", "ratchets", "db_path_defaults.json")


def db_path_defaults(source):
    """How many parameters in `source` default to DB_PATH (a bare name or
    any `<module>.DB_PATH`) — bound when the module is imported."""
    n = 0
    for fn in ast.walk(ast.parse(source)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for d in list(fn.args.defaults) + [k for k in fn.args.kw_defaults if k is not None]:
                if (isinstance(d, ast.Name) and d.id == "DB_PATH") or \
                        (isinstance(d, ast.Attribute) and d.attr == "DB_PATH"):
                    n += 1
    return n


def _db_path_counts():
    out = {}
    for path in _production_sources():
        full = os.path.join(ROOT, path)
        if os.path.exists(full):
            with open(full, encoding="utf-8") as fh:
                n = db_path_defaults(fh.read())
            if n:
                out[path] = n
    return out


def _production_sources():
    # Tracked files plus new, not-yet-committed ones (so a new module is
    # held to the rule before its first commit, not after).
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
                                    cwd=ROOT, text=True).split()
    return sorted({f for f in files if not f.startswith(("tests/", "scripts/"))})


def test_no_production_module_binds_models_get_conn_at_import():
    offenders = []
    for path in _production_sources():
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        offenders += [f"{path}:{n}" for n in bound_get_conn(src)]
    assert not offenders, (
        "module-level `from models import get_conn` binds the function at import, so a patch of "
        "models.get_conn never reaches it — define the call-time shim instead "
        "(`import models as _models_mod` + `def get_conn(db_path=None): ...`, see client_api.py): "
        + ", ".join(offenders))


def test_no_module_gains_a_db_path_default_bound_at_import():
    import json
    with open(DB_PATH_DEFAULTS, encoding="utf-8") as fh:
        base = json.load(fh)["counts"]
    grown = sorted((f, base.get(f, 0), n) for f, n in _db_path_counts().items() if n > base.get(f, 0))
    assert not grown, ("these modules gained a `db_path=DB_PATH` default (file, baseline, now) — it is bound at "
                       "import, so a patched models.DB_PATH never reaches it; take `db_path=None`: %s" % grown)


def test_the_db_path_default_scanner():
    src = ("from models import DB_PATH\nimport models\n"
           "def a(rid, db_path=DB_PATH):\n    pass\n"
           "def b(rid, *, db_path=models.DB_PATH):\n    pass\n"
           "def c(rid, db_path=None):\n    pass\n")
    assert db_path_defaults(src) == 2


def test_the_scan_has_files_to_read():
    assert len(_production_sources()) > 200


@pytest.mark.parametrize("src, flagged", [
    ("from models import get_conn, DB_PATH\n", True),
    ("from models import DB_PATH, get_conn as gc\n", True),
    ("from models import DB_PATH\nimport models as _m\n\ndef get_conn(db_path=None):\n    return _m.get_conn(db_path)\n",
     False),
    ("from models import get_conn\n\ndef get_conn(db_path=None):\n    pass\n", False),
    ("def f():\n    from models import get_conn\n    return get_conn()\n", False),
    ("from .models import get_conn\n", False),
    ("import models\n", False),
])
def test_the_scanner_flags_a_bound_import_and_passes_the_shim(src, flagged):
    assert bool(bound_get_conn(src)) is flagged


def test_a_bare_get_conn_follows_a_patched_db_path(tmp_path, monkeypatch):
    import models
    path = str(tmp_path / "patched.db")
    monkeypatch.setattr(models, "DB_PATH", path)
    conn = models.get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ratchet_probe (x INTEGER)")
        conn.execute("INSERT INTO ratchet_probe (x) VALUES (7)")
        conn.commit()
    finally:
        conn.close()
    import sqlite3
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT x FROM ratchet_probe").fetchall() == [(7,)]
    finally:
        raw.close()


def test_a_converted_module_follows_a_patch_of_models_get_conn(monkeypatch):
    """The point of the shim: patching models.get_conn reaches the module."""
    import auth
    import models
    seen = []
    monkeypatch.setattr(models, "get_conn", lambda db_path=None: seen.append(db_path) or "conn")
    assert auth.get_conn() == "conn" and auth.get_conn("/x.db") == "conn"
    assert seen == [None, "/x.db"]


if __name__ == "__main__":
    # python tests/test_no_bound_get_conn.py --update: lower the baseline
    # after converting defaults to db_path=None (it never goes up).
    import json
    import sys
    if "--update" in sys.argv:
        counts = _db_path_counts()
        with open(DB_PATH_DEFAULTS, "w", encoding="utf-8") as fh:
            json.dump({"about": "parameters defaulting to DB_PATH per production module "
                                "(tests/test_no_bound_get_conn.py; only goes down)",
                       "total": sum(counts.values()), "files": len(counts), "counts": dict(sorted(counts.items()))},
                      fh, indent=1)
            fh.write("\n")
        print(f"{sum(counts.values())} in {len(counts)} files")
