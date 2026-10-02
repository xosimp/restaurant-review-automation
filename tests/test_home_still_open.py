"""Home's "Still open" (renderFollow → renderOpen), 10/2/26: the brief-line
loop in the same function declared its own `var acts` (a button string), and
a var is the whole function's — the last line's string replaced the queue,
and Still open drew 304 blank "Not today" rows (or vanished when the last
line had no buttons)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fn(page, name):
    i = page.index("function " + name + "(")
    j = page.index("\n  function ", i + 10)
    return page[i:j]


def test_render_follow_declares_acts_once():
    page = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    body = _fn(page, "renderFollow")
    assert len(re.findall(r"\bvar\s+acts\s*=", body)) == 1
    assert "renderOpen(acts)" in body


def test_still_open_only_draws_a_list():
    page = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "Object.prototype.toString.call(acts)!=='[object Array]'" in _fn(page, "renderOpen")
