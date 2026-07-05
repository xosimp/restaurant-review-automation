"""Frontend invariants that have no other enforcement point.

The dashboard's JS deliberately uses only ES5 syntax (var/function — no
backticks, const/let, arrow functions, or async/await) for maximum client
compatibility. That rule lived only in people's heads until now; this test is
its enforcement. Also parses every Jinja template so a stray brace can't take
down a page at request time."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")

BANNED = [
    (r"`", "backtick template literal"),
    (r"\bconst\s", "const declaration"),
    (r"\blet\s", "let declaration"),
    (r"=>", "arrow function"),
    (r"\basync\s+function\b", "async function"),
    (r"\bawait\s", "await"),
]


def _inline_scripts(path):
    html = open(path, encoding="utf-8").read()
    return re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.DOTALL)


@pytest.mark.parametrize("pattern,label", BANNED)
def test_dashboard_js_is_es5_only(pattern, label):
    offenders = []
    for block in _inline_scripts(DASHBOARD):
        for i, line in enumerate(block.split("\n"), 1):
            if re.search(pattern, line):
                offenders.append(f"line ~{i}: {line.strip()[:100]}")
    assert not offenders, f"{label} found in dashboard.html inline JS:\n" + "\n".join(offenders[:10])


def test_all_templates_parse():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
    failures = []
    for name in os.listdir(os.path.join(ROOT, "templates")):
        if not name.endswith(".html"):
            continue
        try:
            env.parse(open(os.path.join(ROOT, "templates", name), encoding="utf-8").read())
        except Exception as e:
            failures.append(f"{name}: {e}")
    assert not failures, "Jinja parse errors:\n" + "\n".join(failures)
