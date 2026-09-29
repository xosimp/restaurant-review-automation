"""Memory fix round, integration wave (9/29/26): every wire between the
memory providers and the model calls that read them.

One restaurant with a little of every store — the owner's constraint and
goal, a claim Cavnar AI made on each surface, a decline, a measured result,
a kept cross-module link, a measured event, a person, marketing results, a
chat — and memory_context called for every surface, as the owner, a manager
and the team: each section appears exactly where SURFACE_SECTIONS and its
provider say it should, and nowhere else. Then a source test lists every
generator that reads memory_context, so a wire that is cut shows up here.
"""
import inspect
import json
import sys
from datetime import date, datetime, timedelta

import pytest

import memory_context
import models
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import ai_reads  # noqa: E402
import event_memory  # noqa: E402
import goals  # noqa: E402
import link_memory  # noqa: E402
import owner_memory  # noqa: E402
import people  # noqa: E402
import rec_ledger  # noqa: E402
import shift_facts  # noqa: E402
import staff_settings  # noqa: E402

OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 22, "role": "manager", "is_admin": 0, "username": "dana"}
TODAY = date.today()


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
    from intelligence import jobs
    jobs.invalidate_excluded()
    yield


def _x(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _ago(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


_n = [0]


def _episode(rid, key, module, verdict=None, status="completed", days_ago=40):
    _n[0] += 1
    rec_id = f"w{_n[0]}"
    created = _ago(days_ago)
    tid = None
    if verdict:
        start = TODAY - timedelta(days=days_ago + 20 * _n[0])
        tid = _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                 "baseline_value, started_on, evaluate_on, status, verdict, after_start, after_end, concurrent) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "recommendation", key, "t", "weekday_sales:Monday", 10.0, start.isoformat(),
                  (start + timedelta(days=7)).isoformat(), "evaluated", verdict, start.isoformat(),
                  (start + timedelta(days=6)).isoformat(), "[]"))
    kind = rec_ledger.kind_of(key)
    _x("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, tracker_id, "
       "created_at, last_event_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       (rec_id, rid, key, module, kind, key, status, json.dumps(rec_ledger.tags_for(key, module, kind)), tid,
        created, created, created))
    _x("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, at) VALUES (?,?,?,?,?,?,?)",
       (rec_id, rid, key, "shown", "home", f"shown:{rec_id}", created))


def _seed(monkeypatch):
    rid = create_restaurant(Restaurant(name="Harbor Grill", owner_email="harbor@x.test", timezone="America/Chicago",
                                       module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1))
    # constraints: the owner's standing note, about the whole business.
    owner_memory.remember(rid, "The patio stays closed on weekdays", kind="constraint", user=OWNER)
    # goals: the owner's labor goal.
    goals.set_goal(rid, "labor_pct", 27, user_id=11, authority="principal")
    goals.set_goal(rid, "food_cost_pct", 29, user_id=11, authority="principal")
    goals.set_goal(rid, "avg_rating", 4.6, user_id=11, authority="principal")
    goals.set_goal(rid, "sales", 9000, user_id=11, authority="principal")
    # last_claim: one claim Cavnar AI made on each surface that reads claims.
    for surface in ai_reads.SURFACE_MODULE:
        _x("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text) VALUES (?,?,?,?,?)",
           (rid, surface, f"{surface}:s", "cause", f"Claim on {surface}."))
    # decisions: a reasoned decline in each module.
    for key, module in (("trim_day:Tuesday", "labor"), ("reprice:Wings", "food"), ("reply_rate:faster", "reviews"),
                        ("post_this_week", "marketing"), ("competitor_gap:lunch", "intel")):
        rec_ledger.present(rid, key, module, "home", title=f"Advice {key}")
        rec_ledger.record(rid, key, "dismissed", surface="home", meta={"reason": f"no, because of {module}"},
                          authority="principal")
    # what_worked: five measured labor results and ignored marketing.
    for v in ("improved", "improved", "improved", "improved", "no_clear_change"):
        _episode(rid, "trim_day:Monday", "labor", verdict=v)
    for _ in range(4):
        _episode(rid, "post_this_week", "marketing", status="expired")
    for v in ("improved", "improved", "improved", "worsened", "improved"):
        _episode(rid, "reprice:Salmon", "food", verdict=v)
        _episode(rid, "reply_rate:faster", "reviews", verdict=v)
    # links: a food link and a marketing link, kept.
    import business_intelligence as bi
    link_memory.observe(rid, [
        {"kind": "reviews_x_food_cost", "day": "Friday", "subject": bi._link_subject("service", "Friday"),
         "modules": ["reviews", "food_cost"], "headline": "Friday carries the service complaints and 40% of waste",
         "category": "service", "mentions": 6},
        {"kind": "marketing_x_reviews", "subject": "reviews_up", "modules": ["marketing", "reviews"],
         "headline": "6 posts went out and reviews rose 40%"}])
    # events: a recurring measured effect, and one on a date in every window.
    for k, lift in enumerate((22.0, 20.0, 25.0)):
        d = TODAY - timedelta(weeks=k + 2)
        _x("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, lift_pct) "
           "VALUES (?,?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), "event", "cubs", "Cubs home game", lift))
    event_memory.refresh_effects(rid, {"cubs"})
    _x("INSERT INTO demand_signals (restaurant_id, date, kind, label, source) VALUES (?,?,?,?,?)",
       (rid, TODAY.isoformat(), "event", "Cubs home game", "owner"))
    # people: a server trained on bar, with shifts on file.
    rows = [{"date": (TODAY - timedelta(days=d)).isoformat(), "day": "x", "employee": n, "role": role,
             "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6",
             "sales": "1000"} for d in range(1, 60, 3) for n, role in (("Ana B.", "Server"), ("Maria G.", "Bartender"))]
    shift_facts.ingest(rid, rows, "rpower")
    people.add_role(rid, "Ana B.", "Bartender")
    import attendance
    for w in (1, 2, 3):
        attendance.record(rid, "Maria G.", (TODAY - timedelta(weeks=w)).isoformat(), "no_show", "coverage_check")
    # A claim the labor read made about Fridays: what the schedule, asked
    # about Fridays, reads back.
    _x("INSERT INTO ai_claims (restaurant_id, surface, subject, signature, claim_type, text) VALUES (?,?,?,?,?,?)",
       (rid, "labor_read", "day:friday", "labor:day:friday", "cause", "Fridays run a server heavy."))
    # marketing: measured post results (the deep reader, stubbed at its source).
    import marketing_signals
    monkeypatch.setattr(marketing_signals, "measured_lines", lambda r, **k: ["wing night posts: +12% sales, 3 posts"])
    # conversation: the questions this login keeps asking.
    import ask_conversations
    monkeypatch.setattr(ask_conversations, "often_asks_line", lambda r, uid, db_path=None: {
        "text": "Often asks about labor on Fridays", "source": "system", "trusted": True, "weight": 1.0})
    return rid


# The subjects each generator passes (the call sites below), where a section
# is found by subject rather than by surface.
SUBJECTS = {"schedule": ["labor", "schedule", "labor:day:friday"],
            "ask_conversation": ["conversation:0"]}

# A section a surface lists whose provider says nothing there, on purpose.
SILENT = {
    # The nightly report's own last actions and how they held are its
    # YESTERDAY'S PRIORITIES block (dsr.narrative.own_record); other
    # surfaces' claims reach it only by subject, and it asks by date.
    ("dsr_narrative", "last_claim"),
}


def _sections(rid, viewer):
    out = {}
    for surface in memory_context.SURFACE_SECTIONS:
        block = memory_context.memory_context(rid, surface, viewer=viewer, budget_chars=50000,
                                              subjects=SUBJECTS.get(surface, ()))
        assert not block.errors, (surface, block.errors)
        out[surface] = set(block.sections)
    return out


def test_every_provider_path_resolves_to_a_real_function():
    import importlib
    for name, (path, _prio) in memory_context.PROVIDERS.items():
        mod, fn = path.split(":")
        assert callable(getattr(importlib.import_module(mod), fn)), name
    for surface, wanted in memory_context.SURFACE_SECTIONS.items():
        assert set(wanted) <= set(memory_context.PROVIDERS), surface


@pytest.mark.parametrize("viewer", [OWNER, MANAGER, None], ids=["owner", "manager", "internal"])
def test_every_section_reaches_every_surface_that_reads_it_and_no_other(monkeypatch, viewer):
    rid = _seed(monkeypatch)
    got = _sections(rid, viewer)
    for surface, wanted in memory_context.SURFACE_SECTIONS.items():
        expected = {sec for sec in wanted if (surface, sec) not in SILENT}
        if surface == "ask_conversation" and viewer is None:
            expected = set()            # per login: an internal caller has no chat and no habits
        assert got[surface] == expected, (surface, sorted(got[surface]), sorted(expected))


def test_the_silent_pairs_are_silent_by_the_providers_own_rule(monkeypatch):
    rid = _seed(monkeypatch)
    import marketing
    for surface in ("marketing", "reply_drafter"):
        assert "marketing" not in memory_context.SURFACE_SECTIONS[surface]
        assert marketing.memory_lines(memory_context.MemoryRequest(rid, surface)) == []
    assert marketing.memory_lines(memory_context.MemoryRequest(rid, "ask")), "Ask still hears marketing"
    assert "surface != 'dsr_narrative'" in inspect.getsource(ai_reads.claim_lines)


def test_a_manager_is_not_left_without_claims_by_the_owners_newest():
    rid = create_restaurant(Restaurant(name="Claims Grill", owner_email="claims@x.test", module_reviews=1))
    _x("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text) VALUES (?,?,?,?,?)",
       (rid, "review_read", "category:service", "cause", "Service is slow on Fridays."))
    for i in range(6):                  # six newer owner-level claims
        _x("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text) VALUES (?,?,?,?,?)",
           (rid, "digest", f"week:{i}", "cause", f"Owner-level claim {i}."))
    manager = memory_context.memory_context(rid, "ask", viewer=MANAGER).text
    assert "Service is slow on Fridays." in manager and "Owner-level claim" not in manager
    owner = memory_context.memory_context(rid, "ask", viewer=OWNER).text
    assert "Owner-level claim 5." in owner


def test_the_reviews_read_judges_against_the_owners_rating_goal():
    rid = create_restaurant(Restaurant(name="Rating Grill", owner_email="rating@x.test", module_reviews=1))
    goals.set_goal(rid, "avg_rating", 4.6, user_id=11, authority="principal")
    block = memory_context.memory_context(rid, "review_read")
    assert "goals" in block.sections and "4.6" in block.text


# ── every generator that reads memory ───────────────────────────────────────

# (module, function, surface): the model calls the workstreams report wired
# to memory_context. A wire cut in a refactor fails here.
GENERATORS = [
    ("ask_cavnar", "_memory_context", "ask"),                       # M2: Ask's snapshot
    ("ask_cavnar", "ask_with_tools", "ask_conversation"),          # M2: the per-turn block
    ("schedule_engine", "_build_schedule_result", "schedule"),             # M3: the schedule draft
    ("labor", "labor_memory_block", None),                          # M3: the labor read (surface passed in)
    ("review_intelligence", "diagnosis_memory", None),              # M4: review + food diagnoses
    ("client_api", "review_read_memory", "review_read"),            # M4: the Reviews read
    ("inventory", "food_read_memory", "food_read"),                 # M4: the Food read
    ("strategy_jobs", "plan_memory", "weekly_plan"),                # M4: the Monday plan
    ("dsr.narrative", "_memory", "dsr_narrative"),                  # M5: the nightly report
    ("reporter", None, "digest"),                                   # M5: the weekly digest
    ("morning_brief", None, "brief"),                               # M5: the morning brief
    ("marketing", "marketing_memory_block", "marketing"),           # M6: the four marketing generators
    ("drafter", None, "reply_drafter"),                             # M6: the reply drafter
    ("competitor", None, "competitor_read"),                        # M6: the competitor read
]


def test_every_generator_the_reports_name_reads_memory_context():
    import importlib
    for mod_name, fn, surface in GENERATORS:
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(getattr(mod, fn)) if fn else inspect.getsource(mod)
        assert "memory_context(" in src, (mod_name, fn)
        if surface:
            assert f'"{surface}"' in src, (mod_name, fn, surface)
    # Each surface a generator reads is one SURFACE_SECTIONS knows.
    assert {s for _m, _f, s in GENERATORS if s} <= set(memory_context.SURFACE_SECTIONS)
    import review_intelligence
    import food_cost_intelligence
    assert 'diagnosis_memory(restaurant_id, "review_diagnosis"' in inspect.getsource(review_intelligence)
    assert 'diagnosis_memory(restaurant_id, "food_diagnosis"' in inspect.getsource(food_cost_intelligence)
    import labor
    assert 'labor_memory_block(restaurant_id, analysis)' in inspect.getsource(labor.get_claude_insights)
    # The four marketing generators all take the block: the post and the
    # calendar (marketing), the Studio text (guest_marketing), the newsletter
    # (guest_email).
    import marketing
    import guest_marketing
    import guest_email
    assert inspect.getsource(marketing).count("marketing_memory_block(") >= 3
    assert "marketing_memory_block(restaurant.id)" in inspect.getsource(guest_marketing)
    assert "marketing_memory_block(restaurant.id)" in inspect.getsource(guest_email)


def test_every_memory_context_call_site_is_a_listed_generator():
    """A new model call that reads memory must be added to GENERATORS (and
    its surface classified shared or per login in memory_context)."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    sites = set()
    for p in root.glob("**/*.py"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("tests/", ".claude/", "venv/", ".venv/")) or rel == "memory_context.py":
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if re.search(r"\.memory_context\(", text):
            sites.add(rel[:-3].replace("/", "."))
    assert sites == {m for m, _f, _s in GENERATORS}, sites ^ {m for m, _f, _s in GENERATORS}


def test_every_registered_memory_table_has_a_floor_and_readers_and_answers_are_kept():
    import ops
    memory_tables = ("rec_silences", "rec_rank_builds", "shift_facts", "attendance_events", "person_signals",
                     "ai_reads", "ai_claims", "reply_draft_rejections", "marketing_model_drafts",
                     "ask_memory_archive")
    for t in memory_tables:
        assert t in ops._RETENTION_DAYS, t
        assert ops._RETENTION_FLOOR_DAYS.get(t), t
        assert ops._RETENTION_READERS.get(t), t
    assert set(ops._RETENTION_DAYS) <= set(ops._RETENTION_FLOOR_DAYS)
    keep = ops._RETENTION_ONLY["rec_events"]
    for answer in ("dismissed", "completed", "accepted", "implemented", "snoozed"):
        assert f"'{answer}'" not in keep, answer
