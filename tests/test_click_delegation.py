"""Delegated click handlers on the dashboard walk up from the click to a
marked element. The walk stops at <body>, which also has getAttribute, so a
handler that only checks `t.getAttribute` after the loop runs on EVERY click
anywhere on the page. The ratings match handler did exactly that: every
click posted an empty match and toasted "Pick a name from the roster."

Every handler that walks to document.body must, before acting, refuse when
the walk ended at body without finding its attribute."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()

WALK = re.compile(r"while\s*\(\s*(\w+)\s*&&\s*\1\s*!==\s*document\.body\s*&&\s*!\((.*?)\)\)\s*\1\s*=\s*\1\.parentNode;", re.S)


def test_the_ratings_match_handler_ignores_clicks_elsewhere():
    i = SRC.index("t.getAttribute('data-rt-match') !== null)) t = t.parentNode;")
    guard = SRC[i:i + 600]
    assert "t === document.body" in guard or "getAttribute('data-rt-match') === null" in guard


def test_every_walk_to_body_is_followed_by_a_found_check():
    """A walk that stops at body must be followed, before any request or
    call, by a check that rejects body or re-reads the attribute it wanted
    (so a null from body ends the handler)."""
    bad = []
    for m in WALK.finditer(SRC):
        var = m.group(1)
        after = SRC[m.end():m.end() + 700]
        # The statements up to the first network call or handler call.
        stop = min([x for x in (after.find("fetch("), after.find("jsend("), after.find("matchRating(")) if x >= 0]
                   or [len(after)])
        head = after[:stop]
        rejects_body = f"{var} === document.body" in head or f"{var}===document.body" in head
        # The generic `if (!t || !t.getAttribute) return;` passes for body,
        # so it does not count; a second early return (on the value the
        # handler read, the row it looked for, a container check) does.
        early_returns = len(re.findall(r"\)\s*return\s*;", head))
        if not (rejects_body or early_returns >= 2):
            line = SRC[:m.start()].count("\n") + 1
            bad.append(f"dashboard.html:{line}")
    assert not bad, "handlers that act on a click anywhere on the page: " + ", ".join(bad)
