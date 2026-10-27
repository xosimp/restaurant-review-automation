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
called), so a bare call follows a patched DB_PATH too.
"""
import ast
import os
import subprocess

import pytest

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
