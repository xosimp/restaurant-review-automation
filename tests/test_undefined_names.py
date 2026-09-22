"""No module references a name that is never defined.

A NameError inside a try/except that swallows it is invisible until the
path runs — the Sep 21 re-audit found two (a `_config` alias that never
existed, a `DB_PATH` used in a job without its import). pyflakes catches
exactly this class statically; only its "undefined name" finding is
enforced here, since unused imports and the like are handled elsewhere.
Skips, rather than passes, when pyflakes is not installed (it is in
requirements-dev.txt, so CI runs it).
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_no_module_uses_an_undefined_name():
    pytest.importorskip("pyflakes")
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    files = [f for f in files if not f.startswith("tests/")]
    out = subprocess.run([sys.executable, "-m", "pyflakes", *files], cwd=ROOT, capture_output=True, text=True).stdout
    undefined = [line for line in out.splitlines() if "undefined name" in line]
    assert undefined == [], "\n".join(undefined)
