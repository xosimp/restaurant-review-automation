"""Silent-failure lint — two shapes of swallowed exception that are never
best-effort: a bare `except:` (which also eats KeyboardInterrupt and
SystemExit), and `except Exception: pass` wrapped around a database write.

From the pre-launch audit, which counted 179 silent handlers. Most are
genuinely optional work and stay as they are; these two shapes hide a
failure that surfaces somewhere else entirely, much later — a failure
counter that never advances so a dead push token is retried forever, an
activity trail with a hole in it, a Google account link that has to be
redone on every sign-in.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from check_silent_handlers import ALLOWED_WRITE_SITES, check_file, main


def test_the_codebase_is_clean():
    assert main([]) == 0


def _write(tmp_path, name, body):
    """The lint reads paths relative to ROOT, so point it at a temp tree."""
    import check_silent_handlers as lint
    p = tmp_path / name
    p.write_text(body)
    original = lint.ROOT
    lint.ROOT = str(tmp_path)
    try:
        return check_file(name)
    finally:
        lint.ROOT = original


def test_a_bare_except_is_rejected(tmp_path):
    problems = _write(tmp_path, "sample.py", "def f():\n    try:\n        g()\n    except:\n        pass\n")
    assert len(problems) == 1
    assert "KeyboardInterrupt" in problems[0]


def test_a_swallowed_write_is_rejected(tmp_path):
    problems = _write(tmp_path, "sample.py",
                      'def save():\n'
                      '    try:\n'
                      '        conn.execute("INSERT INTO t (a) VALUES (1)")\n'
                      '        conn.commit()\n'
                      '    except Exception:\n'
                      '        pass\n')
    assert len(problems) == 1
    assert "database write" in problems[0] and "save()" in problems[0]


def test_a_swallowed_read_is_fine(tmp_path):
    """Best-effort reads are exactly what a silent handler is for."""
    problems = _write(tmp_path, "sample.py",
                      'def load():\n'
                      '    try:\n'
                      '        return conn.execute("SELECT 1").fetchone()\n'
                      '    except Exception:\n'
                      '        pass\n')
    assert problems == []


def test_a_recorded_write_failure_is_fine(tmp_path):
    """The fix the lint is asking for: don't drop it, record it."""
    problems = _write(tmp_path, "sample.py",
                      'def save():\n'
                      '    try:\n'
                      '        conn.execute("INSERT INTO t (a) VALUES (1)")\n'
                      '        conn.commit()\n'
                      '    except Exception as e:\n'
                      '        ops.capture(e, job="save")\n')
    assert problems == []


def test_the_allowlist_is_keyed_by_function_not_line_number():
    """So an ordinary edit above one of these doesn't silently un-allow it."""
    for entry in ALLOWED_WRITE_SITES:
        assert isinstance(entry, tuple) and len(entry) == 2
        module, func = entry
        assert module.endswith(".py")
        assert not func.isdigit()


@pytest.mark.parametrize("module,func", sorted(ALLOWED_WRITE_SITES))
def test_every_allowlisted_site_still_exists(module, func):
    """An allowlist entry for code that's gone is an entry nobody will
    notice has stopped meaning anything."""
    path = os.path.join(ROOT, module)
    assert os.path.exists(path), module
    if func == "<module>":
        return
    assert f"def {func}(" in open(path).read(), f"{module}:{func}"


# ── Audit #25 A07-08: every package, wider shapes, a per-file ratchet ───────

def _tree(tmp_path, files, baseline=None):
    """A temp app tree for the lint: {relative path: source}, and its
    baseline file when given."""
    import json
    import check_silent_handlers as lint
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    if baseline is not None:
        (tmp_path / "scripts" / "silent_handlers_baseline.json").write_text(json.dumps({"files": baseline}))
    return lint


@pytest.fixture
def lint_root(tmp_path, monkeypatch):
    import check_silent_handlers as lint
    monkeypatch.setattr(lint, "ROOT", str(tmp_path))
    monkeypatch.setattr(lint, "BASELINE", str(tmp_path / "scripts" / "silent_handlers_baseline.json"))
    return tmp_path


_WRITE_PASS = ('def mark():\n'
               '    try:\n'
               '        conn.execute("INSERT INTO runs (a) VALUES (1)")\n'
               '        conn.commit()\n'
               '    except Exception:\n'
               '        pass\n')


def test_a_swallowed_write_inside_a_package_is_found(lint_root):
    lint = _tree(lint_root, {"dsr/store.py": _WRITE_PASS, "dsr/__init__.py": ""})
    assert "dsr/store.py" in list(lint._python_files())
    problems, counts = lint.scan()
    assert any(p.startswith("dsr/store.py:5:") and "database write" in p for p in problems), problems
    assert counts["dsr/store.py"] == {"silent": 1, "silent_writes": 1}


def test_a_tuple_handler_is_broad(lint_root):
    lint = _tree(lint_root, {"pkg/mod.py": _WRITE_PASS.replace("except Exception:", "except (KeyError, Exception):")})
    assert lint.scan()[0], "except (KeyError, Exception) swallowed a write unseen"


def test_return_constants_and_continue_leave_no_trace_but_a_log_does(lint_root):
    lint = _tree(lint_root, {"pkg/mod.py": (
        "def a():\n    try:\n        x()\n    except Exception:\n        return None\n"
        "def b():\n    for i in y:\n        try:\n            x()\n        except Exception:\n            continue\n"
        "def c():\n    try:\n        x()\n    except Exception:\n        return []\n"
        "def d():\n    try:\n        x()\n    except Exception as e:\n        log.warning(e)\n        return None\n"
        "def e():\n    try:\n        x()\n    except ValueError:\n        return None\n")})
    problems, counts = lint.scan()
    assert problems == [] and counts["pkg/mod.py"] == {"silent": 3, "silent_writes": 0}


def test_the_ratchet_fails_a_new_silent_handler_and_reports_a_fall(lint_root, capsys):
    body = "def a():\n    try:\n        x()\n    except Exception:\n        return None\n"
    lint = _tree(lint_root, {"pkg/mod.py": body}, baseline={"pkg/mod.py": {"silent": 1, "silent_writes": 0}})
    assert lint.main([]) == 0
    (lint_root / "pkg" / "new.py").write_text(body)
    assert lint.main([]) == 1, "a new file's silent handler passed"
    assert "pkg/new.py: 1 silent broad handlers (the baseline allows 0)" in capsys.readouterr().out
    (lint_root / "pkg" / "new.py").unlink()
    (lint_root / "pkg" / "mod.py").write_text("def a():\n    return x()\n")
    assert lint.main([]) == 0 and "fell below the baseline" in capsys.readouterr().out


def test_update_only_lowers_the_baseline(lint_root, capsys):
    import json
    body = "def a():\n    try:\n        x()\n    except Exception:\n        return None\n"
    lint = _tree(lint_root, {"pkg/mod.py": body + body.replace("def a", "def b")},
                 baseline={"pkg/mod.py": {"silent": 1, "silent_writes": 0}})
    assert lint.main(["--update"]) == 1 and "would RISE" in capsys.readouterr().out
    assert lint.main(["--update", "--allow-increase"]) == 0
    saved = json.loads((lint_root / "scripts" / "silent_handlers_baseline.json").read_text())
    assert saved["files"]["pkg/mod.py"] == {"silent": 2, "silent_writes": 0} and saved["total"]["silent"] == 2
