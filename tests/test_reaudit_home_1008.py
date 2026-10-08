"""Blind re-audit 10/8/26 of the Home / performance work (F_home #1, #2, #3,
#6, #7).

#1  The stored Reviews read is scoped like the process cache: the read a
    login shown its other locations gets (their complaint themes) is its own
    insight_store kind ("reviews:locations"), and the stale-while-refresh
    answer, the refused-read fallback and the synchronous "a stale read
    beats no read" fallback each read the kind that matches
    _review_sees_locations. A row under the plain kind that still carries
    location themes (written before the split) is never served to a login
    that may not see them. Labor and Marketing reads are not viewer-scoped
    (one read per restaurant, no viewer in their prompts), so one kind each
    is right.
#2  Home's ActionItem decodes `count`: the fixture the XCTest decodes is a
    real /actions item, held to action_queue.items here.
#3  iOS InsightRefresh stops on a failed read and shows the server's words.
#6  The web insightSwr waits out a dropped poll and never hands a read still
    being written over as the final (cached) answer.
#7  build_home_brief(reads=...) is the same Home as computing them in it.
"""
import json
import shutil
import subprocess

import pytest
from flask import Flask

import ai_async
import ai_utils
import auth
import client_api
import home_brief
import insight_refresh
import insight_store
import mobile_api
import models
from models import Restaurant, create_restaurant, get_conn

ROOT = "ios/CavnarAI/"


class _Held:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a, **k):
        self.jobs.append((fn, a, k))


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(client_api, "_insight_cache", {})
    home_brief.invalidate()
    yield db_path


@pytest.fixture
def held(monkeypatch):
    h = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: h)
    return h


OWNER = {"id": 1, "role": "client", "sees": True}
MANAGER = {"id": 2, "role": "manager", "sees": False}

OWNER_READ = {"insight": "Wait times at Lakeview run 3x this location's.",
              "locations": {"available": True, "locations": [{"id": 9, "label": "Lakeview"}]}}
TEAM_READ = {"insight": "Wait times came up in 4 of 30 reviews.",
             "locations": {"available": False, "reason": "shown only to logins that manage locations"}}


@pytest.fixture
def reviews_read(monkeypatch):
    """The Reviews read's render: every miss reaches the model (refused under
    stored_reads_only), and who sees the other locations is the viewer's
    `sees` flag."""
    monkeypatch.setattr(client_api, "_review_sees_locations", lambda rid, viewer: bool((viewer or {}).get("sees")))

    def render(rid, viewer=None):
        ai_utils.refuse_if_stored_only("review_insight")
        return {"insight": "fresh"}, 200

    monkeypatch.setattr(client_api, "_do_review_insight", render)
    monkeypatch.setattr(client_api, "_review_insight_recs", lambda rid, out: out)


def _answer(rid, viewer):
    with Flask(__name__).test_request_context("/api/review-insight?async=1"):
        return client_api.review_insight_answer(rid, viewer)


def _rid(db):
    return create_restaurant(Restaurant(name="Scope Co", owner_email="o@x.test", module_reviews=1), db_path=db)


# ── #1: the stored Reviews read is scoped like the process cache ────────────

def test_the_two_reviews_reads_are_stored_under_their_own_kinds():
    assert client_api.review_store_kind(True) == "reviews:locations"
    assert client_api.review_store_kind(False) == "reviews"
    assert insight_store._KIND_ACTIONS["reviews:locations"] == "review_insight"


def test_a_stored_owner_read_is_never_served_to_a_login_that_cannot_see_locations(db, held, reviews_read):
    rid = _rid(db)
    insight_store.put(rid, "reviews:locations", "fp-owner", OWNER_READ)
    manager, _ = _answer(rid, MANAGER)
    assert "Lakeview" not in json.dumps(manager)
    assert manager["pending"] is True and manager["insight"] == insight_refresh.PENDING_MESSAGE
    owner, _ = _answer(rid, OWNER)
    assert owner["insight"] == OWNER_READ["insight"] and owner["stale"] is True and owner["refreshing"] is True
    # Each login's read is its own job, too.
    assert owner["refresh_job"] != manager["refresh_job"]


def test_each_login_is_served_its_own_stored_read(db, held, reviews_read):
    rid = _rid(db)
    insight_store.put(rid, "reviews:locations", "fp-owner", OWNER_READ)
    insight_store.put(rid, "reviews", "fp-team", TEAM_READ)
    assert _answer(rid, MANAGER)[0]["insight"] == TEAM_READ["insight"]
    assert _answer(rid, OWNER)[0]["insight"] == OWNER_READ["insight"]


def test_a_cross_location_row_from_before_the_split_is_never_the_teams(db, held, reviews_read):
    """Production's plain "reviews" row may be an owner's read written before
    the kinds were split: it carries its location themes, so it is refused."""
    rid = _rid(db)
    insight_store.put(rid, "reviews", "fp-legacy", OWNER_READ)
    manager, _ = _answer(rid, MANAGER)
    assert "Lakeview" not in json.dumps(manager) and manager["pending"] is True
    assert client_api.review_stored_latest(rid, False) == (None, None)
    body, _at = client_api.review_stored_latest(rid, True)
    assert body is None, "the owner's kind is its own row"


def test_the_synchronous_fallback_reads_the_kind_that_matches_the_login(db, monkeypatch):
    """_do_review_insight's "a stale read beats no read" fallback, reached
    for real: the client cannot be had, so the read fails and the last
    stored read for THIS login answers — never the owner's."""
    rid = _rid(db)
    monkeypatch.setattr(client_api, "_review_sees_locations", lambda rid, viewer: bool((viewer or {}).get("sees")))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no client")))
    monkeypatch.setattr(client_api, "_review_insight_recs", lambda rid, out: out)
    monkeypatch.setattr(client_api, "_record_insight_fallback", lambda *a, **k: None)
    insight_store.put(rid, "reviews:locations", "fp-owner", OWNER_READ)
    manager, _ = client_api._do_review_insight(rid, viewer=MANAGER)
    assert "Lakeview" not in json.dumps(manager)
    owner, _ = client_api._do_review_insight(rid, viewer=OWNER)
    assert owner["insight"] == OWNER_READ["insight"] and owner["stale"] is True
    insight_store.put(rid, "reviews", "fp-team", TEAM_READ)
    client_api._insight_cache.clear()
    manager, _ = client_api._do_review_insight(rid, viewer=MANAGER)
    assert manager["insight"] == TEAM_READ["insight"] and manager["stale"] is True


def test_no_reviews_read_path_names_the_plain_kind_any_more():
    """Every store read and write of the Reviews read goes through the
    login's kind — the cache-hit get, the refused-read fallback, the put
    and both stale fallbacks."""
    src = open("client_api.py", encoding="utf-8").read()
    body = src[src.index("def review_insight_answer("):src.index("def _verify_named_entities(")]
    start = src.index("def _do_review_insight(")
    body += src[start:src.index("\ndef ", start + 1)]
    for call in ('.latest(rid, "reviews")', '.get(rid, "reviews"', '.put(rid, "reviews"'):
        assert call not in body, call
    assert "_ist_ri.get(rid, _sk_ri, _fp_ri" in src and "_ist_ri.put(rid, _sk_ri, _fp_ri" in src


def test_the_cross_location_read_is_kept_as_an_owner_level_read():
    """Its history (ai_reads) is filed under its own surface, which Ask and
    the memory lines serve only to the account holder."""
    import ai_reads
    assert ai_reads.STORE_SURFACE["reviews:locations"] == "review_read_locations"
    assert ai_reads.SURFACE_MODULE["review_read_locations"] == ai_reads.OWNER_ONLY
    assert ai_reads.line_scope("review_read_locations") == {"audience": "principals"}


def test_ask_never_reads_a_cross_location_row_as_the_teams_reviews_read(db):
    import ask_cavnar_tools as tools
    rid = _rid(db)
    # The row as production may hold it (written before the split; its kept
    # history is another table).
    c = get_conn(db)
    c.execute("INSERT INTO insight_cache (restaurant_id, kind, fingerprint, payload) VALUES (?,?,?,?)",
              (rid, "reviews", "fp-legacy", json.dumps(OWNER_READ)))
    c.commit()
    c.close()
    reads = json.loads(tools.run_read_tool("read_recent_reads", rid, {"days": 30}))["reads"]
    assert not any("Lakeview" in (r.get("text") or "") for r in reads)


def test_labor_and_marketing_reads_are_one_per_restaurant_by_construction():
    """Neither read takes a viewer: the prompt, the process cache and the
    stored row are the restaurant's, so one kind each cannot cross logins."""
    import inspect
    for fn in (client_api.labor_refresh, client_api._do_mkt_insight, client_api.labor_stale_read):
        assert "viewer" not in inspect.signature(fn).parameters, fn.__name__
    src = open("client_api.py", encoding="utf-8").read()
    mkt = src[src.index("def mkt_insight_answer("):src.index("def _mkt_insight_out(")]
    assert "viewer" not in mkt


# ── #2: Home's ActionItem decodes a real /actions item's count ──────────────

FIXTURE = ROOT + "CavnarAITests/Fixtures/actions_no_response.json"


def test_the_ios_actions_fixture_is_a_real_action_queue_item(db):
    import action_queue
    import uuid
    rid = create_restaurant(Restaurant(name="Queue Co", owner_email="q@x.test", module_reviews=1), db_path=db)
    c = get_conn(db)
    for rating in (1, 4, 5):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, draft_response, "
                  "review_date, fetched_at, processed, response_status) "
                  "VALUES (?,'google',?,'A',?,'ok','Thank you.',date('now'),datetime('now'),1,'drafted')",
                  (rid, uuid.uuid4().hex, rating))
    c.commit()
    c.close()
    real = {i["key"]: i for i in action_queue.items(rid, db_path=db, present=False)["items"]}["no_response"]
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    item = fixture["items"][0]
    assert item == json.loads(json.dumps(real)), "the fixture is the item the server writes"
    assert item["count"] == 3


def test_action_item_decodes_count():
    src = open(ROOT + "CavnarAI/Features/Home/HomeFollowThrough.swift", encoding="utf-8").read()
    assert "case key, kind, title, detail, severity, action, nav, count }" in src
    assert "count = (try? c.decodeIfPresent(Int.self, forKey: .count)) ?? nil" in src
    tests = open(ROOT + "CavnarAITests/ReauditHomeTests.swift", encoding="utf-8").read()
    assert 'forResource: "actions_no_response"' in tests


# ── #3: InsightRefresh stops on a failed read ───────────────────────────────

def test_the_phone_stops_following_a_failed_read_and_says_why():
    src = open(ROOT + "CavnarAI/Core/InsightRefresh.swift", encoding="utf-8").read()
    assert "if let message = failureMessage(error) {" in src
    assert "failureMessage(body: answer.body)" in src
    for f in ("Features/Labor/LaborAnalyticsViewModel.swift", "Features/Reviews/ReviewsAnalyticsViewModel.swift",
              "Features/Marketing/MarketingAnalyticsViewModel.swift"):
        vm = open(ROOT + "CavnarAI/" + f, encoding="utf-8").read()
        assert "failed: { self.insightError = $0 }" in vm, f
    for f in ("Features/Labor/LaborView.swift", "Features/Reviews/ReviewsAnalyticsSection.swift",
              "Features/Marketing/MarketingAnalyticsSection.swift"):
        view = open(ROOT + "CavnarAI/" + f, encoding="utf-8").read()
        assert "CavnarCaveat.readUnavailable(" in view, f


def test_a_failed_refresh_answers_with_the_routes_error_the_phone_reads(db, held):
    """What the phone stops on: serve()'s answer to a poll of a job that
    wrote nothing, with no older read — the route's error and status."""
    rid = _rid(db)
    job, _ = insight_refresh.start("reviews", rid, {"locations": False},
                                   lambda: ({"insight": "Cavnar AI is paused", "error": "Cavnar AI is paused"}, 429))
    for fn, a, k in held.jobs:
        fn(*a, **k)

    def render():
        ai_utils.refuse_if_stored_only("x")

    payload, status = insight_refresh.serve(
        "reviews", rid, render=render, refresh=lambda: None, stale=lambda note: None,
        pending=lambda j: ({}, 200), failed=lambda err, st: ({"insight": err, "error": err}, st),
        refresh_job=job)
    assert status == 429 and payload["error"] == "Cavnar AI is paused" and "refresh_job" not in payload


# ── #6: the web poll ────────────────────────────────────────────────────────

DASH = open("templates/dashboard.html", encoding="utf-8").read()


def _swr_js(answers, setup=""):
    i = DASH.index("var INSIGHT_SWR_LIMIT_MS")
    fn = DASH[i:DASH.index("window.insightSwr = insightSwr;", i)]
    return ("var AI_POLL_STEPS=[1500,3000,5000], calls = [], fails = [], window = {}, now = 0;\n"
            "Date.now = function(){ return now; };\n"
            "function setTimeout(f, ms) { now += ms; f(); }\n"
            "function apiJson(r) { return Promise.resolve(r); }\n"
            "var answers = " + answers + ";\n"
            "function fetch(url) { var a = answers.shift(); now += a.t || 0;"
            " return a.drop ? Promise.reject(new Error('dropped')) : Promise.resolve(a.d); }\n"
            + setup + fn +
            "\ninsightSwr('/api/review-insight', function(d, interim) { calls.push([d.insight, interim]); },"
            " function(d, kept) { fails.push([d && d.error || null, !!kept]); });\n"
            "setTimeout(function(){}, 0);\n"
            "process.on('exit', function(){ console.log(JSON.stringify({calls: calls, fails: fails})); });\n")


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_dropped_poll_is_waited_out_and_never_blanks_the_older_read():
    got = _node(_swr_js("[{d: {ok: true, stale: true, refreshing: true, refresh_job: 'j1', insight: 'Monday'}},"
                        " {drop: true}, {drop: true},"
                        " {d: {ok: true, insight: 'Today'}}]"))
    assert got["calls"] == [["Monday", True], ["Today", False]]
    assert got["fails"] == []


def test_a_poll_that_keeps_dropping_leaves_the_older_read_up():
    got = _node(_swr_js("[{d: {ok: true, stale: true, refreshing: true, refresh_job: 'j1', insight: 'Monday'}},"
                        " {drop: true}, {drop: true}, {drop: true}, {drop: true}, {drop: true}, {drop: true}]"))
    assert got["calls"] == [["Monday", True]]
    assert got["fails"] == [[None, True]], "kept: the older read stays on screen"


def test_a_read_still_being_written_after_three_minutes_is_never_the_final_answer():
    got = _node(_swr_js("[{d: {ok: true, stale: true, refreshing: true, refresh_job: 'j1', insight: 'Monday'}},"
                        " {t: 200000, d: {ok: true, stale: true, refreshing: true, refresh_job: 'j1',"
                        " insight: 'Monday'}}]"))
    assert ["Monday", False] not in got["calls"], "never handed over (and cached) as final"
    assert got["fails"] == [[None, True]]


def test_a_read_still_pending_after_three_minutes_says_so():
    got = _node(_swr_js("[{d: {ok: true, pending: true, refreshing: true, refresh_job: 'j1', insight: 'x'}},"
                        " {t: 200000, d: {ok: true, pending: true, refreshing: true, refresh_job: 'j1',"
                        " insight: 'x'}}]"))
    assert got["calls"] == []
    assert got["fails"][0][1] is False and "still writing" in got["fails"][0][0]


def test_every_web_caller_keeps_an_older_read_on_a_failure():
    for route in ("/api/review-insight", "/api/mkt-insight", "/api/labor-insight"):
        i = DASH.index(f"insightSwr('{route}', function(d, interim){{")
        call = DASH[i:DASH.index("});", DASH.index("}, function(f, kept){", i)) + 3]
        assert "if(kept)return;" in call, route


# ── #7: Home with the caller's reads is the Home it computes itself ─────────

def _strip(payload):
    """The brief less what moves between two builds a moment apart: the
    clock stamps the build writes (generated_at, local_now, changes.since)."""
    import re
    text = json.dumps(payload, sort_keys=True, default=str)
    return re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[-+]\d\d:\d\d|Z)?", "<now>", text)


def test_build_home_brief_with_reads_matches_computing_them(db):
    from inventory import analysis_for
    from labor import analyse_shifts_for_restaurant
    rid = create_restaurant(Restaurant(name="Corner Bar", owner_email="o@x.com", module_reviews=1, module_labor=1,
                                       module_inventory=1, module_marketing=1), db_path=db)
    user = {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "client",
            "is_admin": 0, "email": "o@x.com"}
    plain, st = home_brief.build_home_brief(user, fresh=True, present=False)
    assert st == 200
    reads = {"labor": analyse_shifts_for_restaurant(rid), "inventory": analysis_for(rid)}
    lent, st = home_brief.build_home_brief(user, fresh=True, present=False, reads=reads)
    assert st == 200
    assert _strip(lent) == _strip(plain)


def test_the_mobile_home_lends_exactly_what_its_tiles_ran(db):
    """/mobile/api/home's tiles hand home_brief the analyses they ran, in
    the shapes _build reads — labor's dict, inventory's (items, is_live,
    analysis)."""
    rid = create_restaurant(Restaurant(name="Corner Bar", owner_email="o@x.com", module_reviews=1, module_labor=1,
                                       module_inventory=1), db_path=db)
    restaurant = models.get_restaurant(rid)
    tiles = mobile_api._home_module_tiles(rid, restaurant, user={"id": 1, "restaurant_id": rid, "role": "client"})
    reads = tiles["reads"]
    assert set(reads) == {"labor", "inventory"}
    assert isinstance(reads["labor"], dict) and len(reads["inventory"]) == 3
    user = {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "client",
            "is_admin": 0, "email": "o@x.com"}
    plain, _ = home_brief.build_home_brief(user, fresh=True, present=False)
    lent, _ = home_brief.build_home_brief(user, fresh=True, present=False, reads=reads)
    assert _strip(lent) == _strip(plain)
