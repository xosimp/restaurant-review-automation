"""iOS post-merge sweep (re-audit W1–W16, 10/8/26): the app-wide rules the
eight module rounds left uneven, held from the source.

  - every literal `CavnarWebLinkRow` path lands somewhere on the web: a nav
    handler of its own, or a `data-nav="<module>/<section>"` the router
    scrolls to (W12) — a path with neither opens the module's top;
  - one pair of web-row verbs, "Edit on the web" / "Open on the web" (W6);
  - one owner-side name for the action queue, "Needs you" (W7);
  - one L2 label, "See the evidence" (W5);
  - the motion that moves things is gated on Reduce Motion (W1);
  - the weekday chips on a task sheet are 44pt buttons (W11);
  - Food Cost's AI read carries each line's measured confidence (W14);
  - the web profile's owner phone is read-only for a teammate, like the
    owner's name (W16; the server already refuses the change).
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
FEATURES = os.path.join(APP, "Features")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def _swift(rel):
    return _read(APP, rel)


def _code(src):
    """The source without its comment lines."""
    return "\n".join(l for l in src.split("\n") if not l.lstrip().startswith("//"))


def _swift_files(root):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".swift"):
                yield os.path.join(dirpath, n)


def _html():
    return _read(ROOT, "templates", "dashboard.html")


def _web_rows():
    """(file, path) for every CavnarWebLinkRow with a literal path."""
    out = []
    for p in _swift_files(APP):
        src = _read(p)
        for m in re.finditer(r"CavnarWebLinkRow\(", src):
            chunk = src[m.end():m.end() + 400]
            pm = re.search(r'\bpath:\s*"([^"]+)"', chunk)
            if pm:
                out.append((os.path.relpath(p, APP), pm.group(1)))
    return out


def test_every_web_row_path_lands_on_the_web():
    html = _html()
    data_nav = set(re.findall(r'data-nav="([a-z]+/[a-z0-9-]+)"', html))
    handlers = set(re.findall(r"cavNavRegister\(\s*'([a-z]+)'", html))
    tabs = {"home", "reviews", "labor", "inventory", "food", "marketing", "intel", "competitor", "account"}
    canon = {"food": "inventory", "competitor": "intel"}
    # Heads whose handler routes a sub-path itself (everything else under
    # them falls back to data-nav).
    handled = {"account", "dna", "dsr", "locations", "recs", "admin"}
    special = {("labor", "schedule"), ("labor", "overtime"), ("labor", "waiting"), ("labor", "notes"),
               ("labor", "people"), ("labor", "lineup"), ("marketing", "campaigns")}
    rows = _web_rows()
    assert len(rows) > 40
    bad = []
    for f, path in rows:
        segs = path.split("?")[0].split("/")
        head = segs[0]
        if head in handled:
            continue
        if len(segs) == 1:
            if head not in tabs and head not in handlers:
                bad.append((f, path))
            continue
        if (head, segs[1]) in special:
            continue
        if canon.get(head, head) + "/" + segs[1] not in data_nav:
            bad.append((f, path))
    assert bad == [], f"web rows whose path the dashboard doesn't route: {bad}"


def test_the_sweep_paths_point_at_their_sections():
    html = _html()
    for nav in ("inventory/waste-trend", "inventory/compare", "inventory/stock", "inventory/prices",
                "intel/history", "labor/score"):
        assert f'data-nav="{nav}"' in html, nav
    # Operational Score opens its panel before the router scrolls to it.
    assert "if (p.rest[0] === 'score')" in html and "toggleTeamPanel()" in html
    assert 'path: "labor/score"' in _swift("Features/Labor/TeamStrengthSection.swift")
    food = _swift("Features/FoodCost/FoodCostAnalyticsSection.swift")
    for nav in ("inventory/waste-trend", "inventory/compare", "inventory/stock", "inventory/prices"):
        assert f'path: "{nav}"' in food, nav
    assert 'path: "intel/history"' in _swift("Features/Intel/IntelView.swift")


def test_two_web_row_verbs():
    for p in _swift_files(APP):
        src = _code(_read(p))
        for m in re.finditer(r"CavnarWebLinkRow\(", src):
            chunk = src[m.end():m.end() + 400].split("CavnarWebLinkRow(")[0]
            for label in re.findall(r'actionLabel:\s*"([^"]+)"', chunk):
                assert label in ("Edit on the web", "Open on the web"), (os.path.relpath(p, APP), label)
        for label in re.findall(r'\?\s*"([^"]+ on the web)"\s*:\s*"([^"]+ on the web)"', src):
            assert set(label) <= {"Edit on the web", "Open on the web"}, (os.path.relpath(p, APP), label)


def test_the_owner_queue_is_needs_you():
    for rel in ("Features/Home/HomeActionDeck.swift", "Features/LocationSwitcher/LocationGroupHomeView.swift",
                "Features/Command/CommandSheet.swift", "Features/Command/ProposalReopenSheet.swift",
                "Features/DailyReport/DailyReportView.swift", "Features/FoodCost/InvoiceScanSheet.swift"):
        src = _code(_swift(rel))
        assert '"Needs attention"' not in src and '"Waiting on you"' not in src, rel
        assert "Needs you" in src, rel


def test_one_evidence_label():
    banned = ('"Show the full read"', '"Show the reasoning"', '"Hide the full read"',
              'Text(open ? "Less" : "Details")', 'Label("Details", systemImage')
    for p in _swift_files(FEATURES):
        src = _code(_read(p))
        for b in banned:
            assert b not in src, (os.path.relpath(p, APP), b)


def test_motion_respects_reduce_motion():
    home = _swift("Features/Home/HomeView.swift")
    assert ".offset(y: heroAppeared || reduceMotion ? 0 : 26)" in home
    assert ".offset(y: appeared || reduceMotion ? 0 : 20)" in home
    intel = _swift("Features/Intel/IntelView.swift")
    assert ".offset(y: contentAppeared ? 0 : 20)" not in intel
    for rel, needle in (("Features/FoodCost/FoodCostTrendChart.swift", "barsVisible || reduceMotion"),
                        ("Features/Reviews/ReviewsListView.swift", "filled && !reduceMotion"),
                        ("Features/Reviews/ReviewDetailView.swift", "reduceMotion ? .opacity :"),
                        ("Features/LocationSwitcher/LocationSwitcherView.swift", "reduceMotion ? .opacity :"),
                        ("Features/Auth/LoginView.swift", "reduceMotion ? .opacity :")):
        assert needle in _swift(rel), rel


def test_task_sheet_weekdays_are_buttons():
    src = _swift("Features/Labor/TaskSheetsScreen.swift")
    i = src.index('Text(["M", "T", "W", "T", "F", "S", "S"][i])')
    block = src[i - 400:i + 900]
    assert "Button {" in block and ".onTapGesture" not in block
    assert "minHeight: 44" in block


def test_food_cost_insight_carries_its_confidence():
    src = _swift("Models/FoodCostAnalytics.swift")
    assert 'case insightRecConfidence = "insight_rec_confidence"' in src
    assert "recConfidence: insightRecConfidence" in src


def test_web_owner_phone_is_read_only_for_a_teammate():
    html = _html()
    i = html.index('id="pe-phone"')
    tag = html[i:html.index(">", i)]
    assert "{% if is_principal is defined and not is_principal %} readonly" in tag
