"""A process that boots hosted_dashboard in a subprocess must stub dotenv
first (release-safety audit #25, WS9 finding; blind re-audit 10/27/26 B1
F17). The in-process suite is covered by conftest; a subprocess is not, and
hosted_dashboard loads the .env beside it — in the main checkout,
production's keys.

The check covers tests/ and scripts/, and an import of hosted_dashboard
however it is written — indented, inside the string a child runs
(`python -c "...import hosted_dashboard..."`), or through importlib — and
asks for the stub itself (`load_dotenv = lambda …`), not merely the word
load_dotenv somewhere in the file (scripts/repo_inventory.py's child loaded
the .env until this check read scripts/ too)."""
import glob
import os
import re

import pytest

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# An import statement (after a newline, any indentation, `;`, a quote, or a
# `\n` inside a string literal), or importlib by name.
BOOTS = re.compile(r"(?:(?:^|[\s;\"'(]|\\n)(?:import\s+hosted_dashboard\b|from\s+hosted_dashboard\s+import)"
                   r"|import_module\(\s*['\"]hosted_dashboard['\"])", re.M)
STUB = re.compile(r"load_dotenv\s*=\s*lambda")


def offenders(sources):
    """{path: source} -> the paths that start a subprocess, boot
    hosted_dashboard somewhere in the file, and never stub load_dotenv."""
    return sorted(p for p, src in sources.items()
                  if "subprocess" in src and BOOTS.search(src) and not STUB.search(src))


def _sources():
    out = {}
    for base in ("tests", "scripts"):
        for path in glob.glob(os.path.join(ROOT, base, "**", "*.py"), recursive=True):
            if os.path.basename(path) == os.path.basename(__file__):
                continue
            with open(path, encoding="utf-8") as fh:
                out[os.path.relpath(path, ROOT)] = fh.read()
    return out


def test_every_subprocess_app_stubs_load_dotenv():
    missing = offenders(_sources())
    assert missing == [], ("stub dotenv before importing hosted_dashboard in the subprocess script "
                           f"(`import dotenv; dotenv.load_dotenv = lambda *a, **k: False`): {missing}")


@pytest.mark.parametrize("src, flagged", [
    ("import subprocess\nimport hosted_dashboard\n", True),
    ("import subprocess\nCODE = \"import os\\nimport hosted_dashboard as h\\n\"\n", True),
    ("import subprocess\ndef f():\n    import hosted_dashboard\n", True),
    ("import subprocess, importlib\nimportlib.import_module('hosted_dashboard')\n", True),
    # The word alone is not the stub.
    ("import subprocess\n# load_dotenv is dangerous\nimport hosted_dashboard\n", True),
    ("import subprocess\nimport dotenv\ndotenv.load_dotenv = lambda *a, **k: False\nimport hosted_dashboard\n", False),
    ("import hosted_dashboard\n", False),                      # in-process: conftest stubs it
    ("import subprocess\nimport hosted_dashboards_helper\n", False),
])
def test_the_scan(src, flagged):
    assert bool(offenders({"x.py": src})) is flagged
