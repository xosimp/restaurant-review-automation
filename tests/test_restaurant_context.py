"""The Restaurant Context Manager (restaurant_context, AI orchestration design
Phase 2, 10/7/26).

What each test holds:

  versions     a section's version moves with its change markers and only
               with them — an unrelated column, the clock within a day, a
               second packet: the same version, the same fingerprint
  caches       L1 serves within L1_SECONDS without a build; a cold process
               reads L2 (context_sections); a new version is rebuilt and
               replaces the row; a builder that fails is said, never cached
  order        sections render in ORDER whatever order they are asked in
  budget       a budget trims TRIM_ORDER's sections first, never the
               profile, the owner's rules or the data state, and says which
               it left out in a DATA STATE line
  viewers      an owner-only rule never reaches TEAM's packet; a reader
               without a module's view gets no module section; each viewer
               is its own cache scope
  missing      a section with nothing on file says so — never a zero
  registry     every policy's context names a real section; the boot table
               exists with its retention index
"""
import sqlite3
import sys

import pytest

import ai_workflows
import memory_context
import models
import owner_memory
import restaurant_context as rc
from models import Restaurant, create_restaurant, update_restaurant

OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 22, "role": "manager", "is_admin": 0, "username": "dana"}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    rc.invalidate()
    yield
    rc.invalidate()


def _rid(name="Context Grill", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        timezone="America/Chicago", module_reviews=1, module_labor=1,
                                        module_inventory=1, module_marketing=1, **kw))


def _exec(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _week(rid, start, end, pct, sales=10000.0, basis="pos"):
    _exec("INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, "
          "total_sales, basis, days, kind) VALUES (?,?,?,?,?,?,?,7,?)",
          (rid, start, end, pct, sales * pct / 100, sales, basis, models.LABOR_PERIOD_WEEK))


# ── a fake section, for the cache mechanics ─────────────────────────────────

_FAKE = {"marker": 1, "builds": 0, "text": "fake body", "fail": False}


def build_fake(req):
    _FAKE["builds"] += 1
    if _FAKE["fail"]:
        raise RuntimeError("the source is down")
    return {"text": _FAKE["text"], "data": {"n": _FAKE["builds"]}}


def version_fake(req):
    return [_FAKE["marker"]]


@pytest.fixture
def fake_section(monkeypatch):
    _FAKE.update(marker=1, builds=0, text="fake body", fail=False)
    sections = dict(rc.SECTIONS)
    sections["weather"] = rc.Section(f"{__name__}:build_fake", f"{__name__}:version_fake", "THE FORECAST")
    monkeypatch.setattr(rc, "SECTIONS", sections)
    return _FAKE


def _l2_rows(rid, section):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM context_sections WHERE restaurant_id=? AND section=?",
                                           (rid, section)).fetchall()]
    finally:
        c.close()


# ── versions ────────────────────────────────────────────────────────────────

def test_a_version_moves_with_its_markers_and_only_with_them():
    rid = _rid()
    first = rc.packet(rid, ("profile",))
    again = rc.packet(rid, ("profile",))
    assert first.versions == again.versions and first.fingerprint == again.fingerprint
    # A restaurants column the profile does not read: the same version.
    update_restaurant(rid, {"weather_cached_at": "2026-10-07T10:00:00+00:00"})
    models._invalidate_request_cache(rid)
    assert rc.packet(rid, ("profile",)).fingerprint == first.fingerprint
    # One it reads: a new version, a new text, a new fingerprint.
    update_restaurant(rid, {"menu_notes": "Lake perch on Fridays"})
    models._invalidate_request_cache(rid)
    moved = rc.packet(rid, ("profile",))
    assert moved.versions["profile"] != first.versions["profile"]
    assert moved.fingerprint != first.fingerprint
    assert "Lake perch on Fridays" in moved.text


def test_the_labor_trend_version_moves_with_a_new_payroll_week_not_with_a_second_read():
    rid = _rid()
    _week(rid, "2026-09-14", "2026-09-20", 31.0)
    _week(rid, "2026-09-21", "2026-09-27", 28.5)
    first = rc.section(rid, "labor_trend")
    assert rc.section(rid, "labor_trend").version == first.version
    assert "Labor by payroll week" in first.text and "TREND: Labor % is DOWN 2.5 points" in first.text
    assert first.data["has_trend"] is True and first.data["trend_diff"] == -2.5
    rc.invalidate()
    _week(rid, "2026-09-28", "2026-10-04", 30.0)
    second = rc.section(rid, "labor_trend")
    assert second.version != first.version
    assert "UP 1.5 points" in second.text


def test_the_labor_trend_is_the_labor_reads_own_lines():
    """The section is labor.labor_trend_section — the lines the labor read
    always carried, now built once and shared (no second copy of the rule)."""
    import labor
    rid = _rid()
    _week(rid, "2026-09-14", "2026-09-20", 31.0)
    _week(rid, "2026-09-21", "2026-09-27", 28.5)
    assert rc.section(rid, "labor_trend").text == labor.labor_trend_section(rid)["text"]
    assert rc.SECTIONS["labor_trend"].builder == "restaurant_context:build_labor_trend"


# ── caches ──────────────────────────────────────────────────────────────────

def test_l1_then_l2_then_a_rebuild_on_a_new_version(fake_section):
    rid = _rid()
    one = rc.section(rid, "weather")
    assert one.source == "build" and fake_section["builds"] == 1
    two = rc.section(rid, "weather")
    assert two.source == "l1" and fake_section["builds"] == 1
    assert len(_l2_rows(rid, "weather")) == 1
    rc.invalidate(rid)                                  # a new process: L1 is empty
    three = rc.section(rid, "weather")
    assert three.source == "l2" and fake_section["builds"] == 1 and three.text == "fake body"
    fake_section["marker"] = 2
    fake_section["text"] = "fresh body"
    four = rc.section(rid, "weather")
    assert four.source == "build" and fake_section["builds"] == 2 and four.text == "fresh body"
    rows = _l2_rows(rid, "weather")
    assert len(rows) == 1 and rows[0]["text"] == "fresh body"     # replaced, never appended


def test_l1_lapses_after_its_window(fake_section, monkeypatch):
    rid = _rid()
    rc.section(rid, "weather")
    monkeypatch.setattr(rc, "L1_SECONDS", -1)
    assert rc.section(rid, "weather").source == "l2"


def test_a_builder_that_fails_is_said_and_never_cached(fake_section):
    rid = _rid()
    fake_section["fail"] = True
    out = rc.section(rid, "weather")
    assert out.missing and "could not be read" in out.text.lower()
    assert _l2_rows(rid, "weather") == []
    fake_section["fail"] = False
    assert rc.section(rid, "weather").text == "fake body"


# ── order and budget ────────────────────────────────────────────────────────

def test_sections_render_in_the_fixed_order_whatever_order_they_are_asked_in(fake_section):
    rid = _rid()
    asked = ("data_state", "weather", "labor_trend", "profile")
    pk = rc.packet(rid, asked)
    assert list(pk.sections) == [n for n in rc.ORDER if n in asked]
    pos = [pk.text.index(rc.SECTIONS[n].title + ":") for n in ("profile", "labor_trend", "weather", "data_state")]
    assert pos == sorted(pos)
    assert pk.text.rstrip().split("\n\n")[-1].startswith(rc.SECTIONS["data_state"].title)
    assert set(rc.ORDER) == set(rc.SECTIONS) and rc.ORDER[0] == "profile" and rc.ORDER[-1] == "data_state"


def test_a_budget_trims_the_lowest_priority_first_and_says_so(fake_section):
    rid = _rid()
    fake_section["text"] = "x " * 4000                  # ~2000 tokens of forecast
    pk = rc.packet(rid, ("profile", "owner_rules", "weather", "data_state"), budget_tokens=300)
    assert pk.trimmed == ["weather"]
    assert "x x x" not in pk.text
    assert "Left out of this context for length: weather" in pk.text
    assert rc.SECTIONS["data_state"].title in pk.text
    # Never trimmed, whatever the budget.
    tiny = rc.packet(rid, ("profile", "owner_rules", "data_state"), budget_tokens=1)
    assert tiny.trimmed == [] and rc.SECTIONS["profile"].title in tiny.text
    for name in ("profile", "owner_rules", "data_state"):
        assert rc.SECTIONS[name].trim is False and name not in rc.TRIM_ORDER
    # A trimmed packet is its own cache key.
    assert pk.fingerprint != rc.packet(rid, ("profile", "owner_rules", "weather", "data_state")).fingerprint


# ── viewers ─────────────────────────────────────────────────────────────────

PRIVATE_RULE = "Never schedule Dana on weekends, we are letting her go"
TEAM_RULE = "Always keep two servers on Friday nights"


def test_an_owner_only_rule_never_reaches_the_team_and_each_viewer_is_its_own_scope():
    rid = _rid()
    owner_memory.remember(rid, PRIVATE_RULE, kind="constraint", audience="principals", user=OWNER)
    owner_memory.remember(rid, TEAM_RULE, kind="constraint", audience="team", user=OWNER)
    owner_pk = rc.packet(rid, ("owner_rules",))
    team_pk = rc.packet(rid, ("owner_rules",), viewer=memory_context.TEAM)
    assert PRIVATE_RULE in owner_pk.text and TEAM_RULE in owner_pk.text
    assert TEAM_RULE in team_pk.text and PRIVATE_RULE not in team_pk.text
    assert owner_pk.sections["owner_rules"].scope != team_pk.sections["owner_rules"].scope
    scopes = {r["viewer_scope"] for r in _l2_rows(rid, "owner_rules")}
    assert scopes == {"principals", "team"}


def test_a_new_rule_is_a_new_owner_rules_version():
    rid = _rid()
    before = rc.section(rid, "owner_rules")
    assert before.missing and "None on file" in before.text
    owner_memory.remember(rid, TEAM_RULE, kind="constraint", audience="team", user=OWNER)
    after = rc.section(rid, "owner_rules")
    assert after.version != before.version and TEAM_RULE in after.text


def test_a_reader_without_the_modules_view_gets_no_module_section(monkeypatch):
    rid = _rid()
    _week(rid, "2026-09-14", "2026-09-20", 31.0)
    _week(rid, "2026-09-21", "2026-09-27", 28.5)
    import memory_context as mc
    monkeypatch.setattr(mc, "_may_read_module", lambda user, module, cache: module != "labor")
    out = rc.section(rid, "labor_trend", viewer=MANAGER)
    assert out.missing and "Labor by payroll week" not in out.text and "31.0" not in out.text
    # The account holders' own view reads every module.
    assert "Labor by payroll week" in rc.section(rid, "labor_trend", viewer=None).text


def test_the_findings_are_the_owners_view_only():
    rid = _rid()
    out = rc.section(rid, "findings", viewer=memory_context.TEAM)
    assert out.missing and "owner's view" in out.text


def test_viewer_scopes():
    assert rc.viewer_scope(None, "owner_rules") == "principals"
    assert rc.viewer_scope(memory_context.PRINCIPALS, "owner_rules") == "principals"
    assert rc.viewer_scope(memory_context.TEAM, "owner_rules") == "team"
    assert rc.viewer_scope(memory_context.team_viewer("food_read"), "owner_rules").startswith("team:")
    a = rc.viewer_scope(MANAGER, "owner_rules")
    assert a.startswith("login:22:") and a != rc.viewer_scope(dict(MANAGER, role="client"), "owner_rules")
    assert rc.viewer_scope(MANAGER, "profile") == "all"


# ── missing data ────────────────────────────────────────────────────────────

def test_missing_data_is_said_never_a_zero():
    rid = _rid()
    for name in ("labor_trend", "sales_trend", "alerts", "roster"):
        out = rc.section(rid, name)
        assert out.missing, name
        assert out.text and not any(z in out.text for z in ("$0", " 0%", "0.0%")), (name, out.text)
    kpis = rc.section(rid, "kpis")
    assert "no complete payroll week" in kpis.text.lower() and "none on file" in kpis.text.lower()
    # Its labor figure is all-in only where the salaries may be seen, and
    # its cache scope says which (outside a request: a job, the owner's).
    assert kpis.scope == "principals;salaries"


def test_sales_by_week_from_the_nightly_archive_with_partial_weeks_said():
    from datetime import date, timedelta
    rid = _rid()
    today = rc.SectionRequest(rid, "sales_trend").today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    for i in range(7):
        d = (last_monday + timedelta(days=i)).isoformat()
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, sales) VALUES (?,?,?)", (rid, d, 1000.0))
    _exec("INSERT INTO labor_daily_history (restaurant_id, date, sales) VALUES (?,?,?)",
          (rid, (last_monday - timedelta(days=7)).isoformat(), 500.0))
    out = rc.section(rid, "sales_trend")
    assert not out.missing
    assert "$7,000" in out.text and "(1 of 7 days on file)" in out.text
    assert any(f.value == 7000.0 for f in out.facts)
    assert isinstance(date.fromisoformat(out.data["weeks"][-1]["week_start"]), date)


# ── the registry ────────────────────────────────────────────────────────────

def test_every_policy_context_names_a_real_section_and_every_path_resolves():
    for name, pol in ai_workflows.POLICIES.items():
        assert set(pol.context) <= set(rc.SECTIONS), (name, set(pol.context) - set(rc.SECTIONS))
    for name, sec in rc.SECTIONS.items():
        assert callable(rc._resolve(sec.builder)), name
        assert callable(rc._resolve(sec.version)), name
        assert sec.scope in ("all", "viewer", "salaries"), name


def test_the_reads_render_only_sections_their_policy_lists():
    import client_api
    import inventory
    import labor
    for wf, secs in (("labor_insight", labor.LABOR_READ_SECTIONS), ("inventory_insight", inventory.FOOD_READ_SECTIONS),
                     ("review_insight", client_api.REVIEW_READ_SECTIONS),
                     ("marketing_insight", client_api.MARKETING_READ_SECTIONS)):
        assert set(secs) <= set(ai_workflows.POLICIES[wf].context), wf


def test_the_table_is_created_at_boot_with_its_retention_index(db_path):
    import ops
    c = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(context_sections)")}
        idx = {r[1] for r in c.execute("PRAGMA index_list(context_sections)")}
    finally:
        c.close()
    assert {"restaurant_id", "section", "viewer_scope", "version", "built_at", "text", "facts_json",
            "tokens"} <= cols
    assert "idx_context_sections_built" in idx
    assert ops._RETENTION_COLUMN["context_sections"] == "built_at" and ops._RETENTION_DAYS["context_sections"] > 0


def test_owner_memory_invalidate_drops_the_memory_sections_from_l1(fake_section):
    rid = _rid()
    rc.section(rid, "owner_rules")
    rc.section(rid, "weather")
    owner_memory.invalidate(rid)
    keys = {k[1] for k in rc._L1 if k[0] == rid}
    assert "owner_rules" not in keys and "weather" in keys
