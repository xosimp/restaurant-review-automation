"""Every endpoint path the clients name resolves to a registered route
(audit #25 A04-12 / A06-07).

Only iOS WRITES were pinned (tests/test_ios_request_body_parity.py). A route
renamed or removed left a dashboard fetch or an iPhone read calling a 404,
and the screen showed its empty state, not an error. This reads every path
literal in the web templates and static scripts (`'/api/…'`, `'/admin/…'`,
`'/staff/…'`, `'/mobile/api/…'`) and in the iOS app's Swift sources
(`"/mobile/api/…"`, `"/staff/…"`, interpolations as one segment) and matches
each against the url_map of every blueprint the app registers
(`blueprints.url_map_app()`, the list hosted_dashboard registers from):

- an exact literal must match a rule segment for segment (a converter
  matches any one segment);
- a literal that is a prefix — it ends in `/`, carries a `?query`, or is
  followed by `+` (the rest is concatenated at run time) — must be the start
  of some rule.

A dynamic `fetch(url)` with no literal cannot be checked statically; nothing
here pretends to. iOS test fixtures (CavnarAITests) name fake endpoints on
purpose and are not scanned.
"""
import glob
import os
import re

import pytest

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# literal -> why it names no route (it is not a request).
NOT_A_REQUEST = {
    "/staff/api": "APIClient.isStaffAPI compares a path with the staff tier's prefix; nothing is requested",
}

_WEB = re.compile(r"""(['"])((?:/api/|/mobile/api/|/staff/|/admin/)[^'"\s<>{}]*)\1(\s*\+)?""")
_INTERP = r"\\\((?:[^()]|\([^()]*\))*\)"
_SWIFT = re.compile(r'"((?:/mobile/api/|/staff/)(?:[^"\\\n]|' + _INTERP + r'|\\.)*)"(\s*\+)?')


def _segments(rule):
    return [None if s.startswith("<") else s for s in rule.strip("/").split("/")]


@pytest.fixture(scope="module")
def rules():
    import blueprints
    app = blueprints.url_map_app(template_folder=os.path.join(ROOT, "templates"))
    return [_segments(r.rule) for r in app.url_map.iter_rules() if r.endpoint != "static"]


def resolves(literal, prefix, rules):
    """Whether `literal` names a rule (exactly) or starts one (`prefix`).
    "{*}" is an interpolated segment and matches any one segment."""
    trailing = literal.endswith("/")
    parts = [p for p in literal.strip("/").split("/")] if literal.strip("/") else []
    for rs in rules:
        if (len(parts) > len(rs)) if prefix else (len(parts) != len(rs)):
            continue
        for i, part in enumerate(parts):
            r = rs[i]
            if part == "{*}" or r is None:
                continue
            if prefix and not trailing and i == len(parts) - 1:
                if not r.startswith(part):
                    break
            elif part != r:
                break
        else:
            return True
    return False


def _web_literals():
    out = []
    files = sorted(glob.glob(os.path.join(ROOT, "templates", "**", "*.html"), recursive=True)
                   + glob.glob(os.path.join(ROOT, "static", "**", "*.js"), recursive=True))
    for f in files:
        src = open(f, encoding="utf-8", errors="replace").read()
        for m in _WEB.finditer(src):
            raw = m.group(2)
            prefix = bool(m.group(3)) or raw.endswith(("/", "=")) or "?" in raw
            out.append((os.path.relpath(f, ROOT), src.count("\n", 0, m.start()) + 1, raw,
                        raw.split("?")[0].split("#")[0], prefix))
    return out


def _swift_literals():
    out = []
    files = sorted(f for f in glob.glob(os.path.join(ROOT, "ios", "CavnarAI", "**", "*.swift"), recursive=True)
                   if "/CavnarAITests/" not in f)
    for f in files:
        src = open(f, encoding="utf-8", errors="replace").read()
        for m in _SWIFT.finditer(src):
            raw = m.group(1)
            lit = re.sub(_INTERP, "{*}", raw)
            prefix = bool(m.group(2)) or raw.endswith("/") or "?" in lit
            lit = "/".join("{*}" if "{*}" in s else s for s in lit.split("?")[0].split("/"))
            out.append((os.path.relpath(f, ROOT), src.count("\n", 0, m.start()) + 1, raw, lit, prefix))
    return out


def _unresolved(literals, rules):
    return [f"{f}:{line}: {raw}" for f, line, raw, lit, prefix in literals
            if raw not in NOT_A_REQUEST and not resolves(lit, prefix, rules)]


def test_the_scans_find_the_clients_paths():
    """Guards the guard: a regex that matched nothing would pass below."""
    web, swift = _web_literals(), _swift_literals()
    assert len(web) >= 800, len(web)
    assert len(swift) >= 700, len(swift)
    assert any(raw == "/api/staff-brief" for _, _, raw, _, _ in web)
    assert any(lit == "/mobile/api/staff-docs/{*}/remove" for _, _, _, lit, _ in swift)


def test_the_matcher_refuses_a_path_no_route_serves(rules):
    assert resolves("/api/staff-brief", False, rules)
    assert resolves("/mobile/api/staff-docs/{*}/remove", False, rules)
    assert resolves("/api/staff-docs/", True, rules)
    assert not resolves("/api/definitely-not-a-route", False, rules)
    assert not resolves("/mobile/api/staff-docs/{*}/vanish", False, rules)
    assert not resolves("/api/definitely-not", True, rules)


def test_every_dashboard_fetch_reaches_a_route(rules):
    bad = _unresolved(_web_literals(), rules)
    assert not bad, "web paths no route serves:\n" + "\n".join(bad)


def test_every_ios_path_reaches_a_route(rules):
    bad = _unresolved(_swift_literals(), rules)
    assert not bad, "iOS paths no route serves:\n" + "\n".join(bad)


def test_every_not_a_request_entry_is_still_in_the_app():
    raws = {raw for _, _, raw, _, _ in _swift_literals() + _web_literals()}
    assert set(NOT_A_REQUEST) <= raws, set(NOT_A_REQUEST) - raws


def test_the_url_map_app_leaves_the_real_blueprints_unregistered(monkeypatch):
    """url_map_app registers copies (blind re-audit 10/27/26 B4): the shared
    blueprint objects stay as they were, so a later csrf_protect /
    before_request on one still works (Flask refuses a new hook on a
    blueprint any app has registered), and the copies carry every route. No
    blueprint nests another (a copy would share the child)."""
    import blueprints
    from flask import Blueprint
    real = blueprints.all_blueprints()
    assert all(not bp._blueprints for bp in real), "a nested blueprint: url_map_app's copies would share it"
    app = blueprints.url_map_app()
    assert {bp.name for bp in real} <= set(app.blueprints)
    assert all(app.blueprints[bp.name] is not bp for bp in real)
    # On a blueprint no app has registered, so the test does not depend on
    # what an earlier test in this process registered.
    fresh = Blueprint("fresh_probe", __name__)
    fresh.add_url_rule("/fresh-probe", "probe", lambda: "ok")
    monkeypatch.setattr(blueprints, "all_blueprints", lambda: (fresh,))
    probe_app = blueprints.url_map_app()
    assert "/fresh-probe" in {r.rule for r in probe_app.url_map.iter_rules()}
    fresh.before_request(lambda: None)      # raises if url_map_app had registered `fresh` itself
