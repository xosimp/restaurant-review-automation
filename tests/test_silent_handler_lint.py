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
    assert main() == 0


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
