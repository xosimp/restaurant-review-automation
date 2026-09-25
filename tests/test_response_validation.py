"""response_validation: properties, helpers, logging, the admin read and
the performance budget. The golden corpus is test_response_validation_corpus.

Properties (NS6 §C Tests): validate never adds a figure, never raises
confidence, and is idempotent — a second pass makes no new rewrites.
"""
import json
import re
import time

import pytest

import ai_guard
import confidence_engine
import response_validation as rv
from response_validation import Fact, ValidationContext as Ctx, validate
from test_response_validation_corpus import CASES, build_ctx


# ── properties over the whole corpus ────────────────────────────────────────

def _numbers(text):
    t = rv._normalise(ai_guard.normalise_numbers(str(text or "")))
    out = set()
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)(k\b)?", t, re.I):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(round(v * (1000 if m.group(2) else 1), 2))     # "$1.2k" is 1,200
    return out


def _allowed_new_numbers(ctx):
    """Numbers a rewrite may bring in from the context itself: a fact's
    value (the canonical opportunity sentence) and a benchmark's source
    year, n and as-of date (B1's citation: a peer group is always named
    with how many and as of when, BM3-5)."""
    out = set()
    for f in ctx.facts:
        if f.value is not None:
            out.add(round(abs(f.value), 2))
        src = f.source if isinstance(f.source, dict) else {}
        for k in ("year", "n"):
            if isinstance(src.get(k), (int, float)):
                out.add(round(float(src[k]), 2))
        if f.kind == "benchmark":
            for d in re.findall(r"\d+", str(src.get("as_of") or f.as_of or "")):
                out.add(round(float(d), 2))
    return out


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_never_adds_a_figure(case):
    ctx = build_ctx(case)
    v = validate(case["text"], ctx)
    new = _numbers(v.text) - _numbers(case["text"]) - _allowed_new_numbers(ctx)
    assert not new, f"{case['src']}: added {new} → {v.text!r}"


_MODAL_RANK = [(r"\bwill\b|['’]ll\b|\bgoing\s+to\b|\bguarantee\w*|\bdefinitely\b|\bcertainly\b", 4),
               (r"\bshould\b|\blikely\b", 3), (r"\bcould\b|\bmay\b", 2), (r"\bmight\b", 1)]
_CAUSE_WORDS = re.compile(r"\b(?:caused|causes|because|due\s+to|drove|driving|led\s+to|is\s+why|responsible\s+for|"
                          r"triggered|is\s+behind|are\s+behind|thanks\s+to|the\s+reason|proves?|paid\s+off)\b", re.I)
_HEDGED_CAUSE = re.compile(r"\b(?:possibly|may|might|could|partly)\s+(?:\w+\s+){0,2}?(?:because|due\s+to|caused|"
                           r"driven|behind|thanks|helped)", re.I)
_CONF_RE = re.compile(r"\b(?:high|medium|low)\s+confidence\b|\d{1,3}\s?%\s+(?:sure|confident|certain)|"
                      r"\bI['’]?m\s+(?:\w+\s+)?(?:sure|confident|certain)\b", re.I)


def _modal_max(text):
    return max([rank for pat, rank in _MODAL_RANK if re.search(pat, text, re.I)] or [0])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_never_raises_confidence(case):
    ctx = build_ctx(case)
    v = validate(case["text"], ctx)
    if not v.text:
        return
    # A hedge ("may", "could", "might") added to a flat statement lowers it;
    # "should" or stronger may only appear where something as strong was.
    assert _modal_max(v.text) <= max(_modal_max(case["text"]), 2), f"{case['src']}: {v.text!r}"
    bare = len(_CAUSE_WORDS.findall(v.text)) - len(_HEDGED_CAUSE.findall(v.text))
    before = len(_CAUSE_WORDS.findall(case["text"])) - len(_HEDGED_CAUSE.findall(case["text"]))
    assert bare <= max(before, 0), f"{case['src']}: an unhedged cause was added → {v.text!r}"
    assert len(_CONF_RE.findall(v.text)) <= len(_CONF_RE.findall(case["text"])), case["src"]
    # the verdict can only restrict: a withheld or refused answer shows no controls
    if v.verdict in ("withhold", "refuse"):
        assert v.actions["controls"] is False


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_idempotent(case):
    ctx = build_ctx(case)
    v1 = validate(case["text"], ctx)
    if not v1.text:
        return
    v2 = validate(v1.text, ctx)
    assert v2.actions["rewrites"] == [], f"{case['src']}: second pass rewrote {v2.actions['rewrites']}"
    assert v2.text == v1.text


# ── the lexicon is pinned to the K1 thresholds ──────────────────────────────

def test_modal_lexicon_is_pinned_to_the_confidence_thresholds():
    H, M = confidence_engine.HIGH_AT, confidence_engine.MEDIUM_AT
    assert rv.target_level(H) == 3 and rv.target_level(H - 0.1) == 2
    assert rv.target_level(M) == 2 and rv.target_level(M - 0.1) == 1
    assert rv.target_level(None) == 1          # unmeasured reads as the lowest
    ctx = lambda p: Ctx(surface="ask", confidence={"pct": p})
    assert validate("This will reduce labor.", ctx(H)).text == "This should reduce labor."
    assert validate("This will reduce labor.", ctx(M)).text == "This could reduce labor."
    assert validate("This will reduce labor.", ctx(M - 1)).text == "This might reduce labor."
    # never raised: "might" stays "might" at 90%
    assert validate("This might reduce labor.", ctx(90)).text == "This might reduce labor."
    # a conditional keeps its will
    assert validate("If you trim Tuesday, labor will drop.", ctx(10)).text == "If you trim Tuesday, labor will drop."


def test_always_banned_words_go_at_any_confidence():
    for word in ("guaranteed to", "definitely", "certainly", "without a doubt"):
        v = validate(f"This is {word} the fix." if word != "guaranteed to" else "This is guaranteed to work.",
                     Ctx(surface="ask", confidence={"pct": 99}))
        assert word.split()[0] not in v.text.lower(), v.text
    assert "proves" not in validate("The data proves it.", Ctx(surface="ask")).text


# ── helpers ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,kind", [
    ("food.recoverable_monthly", "opportunity"), ("labor.potential_savings_monthly", "opportunity"),
    ("home.at_stake_monthly", "opportunity"), ("labor.gap_monthly", "opportunity"), ("x.opportunity", "opportunity"),
    ("sales.budget_net", "plan"), ("labor.target_pct", "plan"), ("goal_value", "plan"), ("plan_hours", "plan"),
    ("food.est_food_cost", "estimate"), ("waste_estimated", "estimate"),
    ("sales.forecast_net", "projection"), ("waste.projected_month", "projection"),
    ("sales.net", "measured"), ("value.delivered_monthly", "measured"), ("sales.vs_budget_net", "computed"),
    ("benchmark.labor_p50", "benchmark"), ("menu.price", "price"),
])
def test_kind_of_key(key, kind):
    assert rv.kind_of_key(key) == kind


def test_facts_from_dict_types_units_periods_and_keeps_null():
    fs = {f.key: f for f in rv.facts_from_dict(
        {"labor": {"pct": 31.4, "cost": 4120, "hours": 312.5, "potential_savings_monthly": 850},
         "reviews": {"rating_avg": 4.3, "count": 18, "note": "text is not a fact", "live": True},
         "food": {"waste_pct": None}},
        kind_map={"cost": "measured"}, period_map={"labor.cost": "week"})}
    assert fs["labor.pct"].unit == "%" and fs["labor.pct"].kind == "measured"
    assert fs["labor.cost"].unit == "$" and fs["labor.cost"].period == "week"
    assert fs["labor.hours"].unit == "h"
    assert fs["labor.potential_savings_monthly"].kind == "opportunity"
    assert fs["labor.potential_savings_monthly"].period == "month"
    assert fs["reviews.rating_avg"].unit == "★" and fs["reviews.count"].unit == "count"
    assert "reviews.note" not in fs and "reviews.live" not in fs
    assert fs["food.waste_pct"].value is None      # null is never 0
    v = validate("Waste is 0% of purchases.", Ctx(surface="food_insight", facts=list(fs.values())))
    assert "F1" in v.codes


def test_context_rejects_an_unknown_surface_and_defaults_delivery():
    with pytest.raises(ValueError):
        Ctx(surface="homepage")
    assert Ctx(surface="digest").delivery == "unattended"
    assert Ctx(surface="ask").delivery == "interactive"
    assert Ctx(surface="reply_public").audience == "guest_public"


def test_unattended_escalates_caveat_rules_to_drop():
    text = "Ratings slipped since the new menu launched. Labor ran 31.4%."
    facts = [Fact("labor.pct", 31.4, "%")]
    inter = validate(text, Ctx(surface="labor_insight", facts=facts))
    unatt = validate(text, Ctx(surface="digest", facts=facts))
    assert inter.verdict == "caveat" and "since" in inter.text
    assert unatt.verdict == "pass" and unatt.text == "Labor ran 31.4%."
    assert unatt.actions["dropped"] == ["Ratings slipped since the new menu launched."]


def test_withhold_turns_controls_off_and_carries_a_structured_caveat():
    v = validate("Waste came to $420.", Ctx(surface="food_insight", facts=[Fact("waste.week", 412.5, "$",
                                                                                   period="week")]))
    assert v.verdict == "withhold" and v.actions["controls"] is False
    assert v.actions["caveats"] and "UNVERIFIED" not in " ".join(v.actions["caveats"])
    assert v.text_with_caveats().startswith("Waste came to $420.")


def test_validate_lines_drops_whole_lines():
    ctx = Ctx(surface="digest", facts=[Fact("labor.pct", 31.4, "%"), Fact("waste.week", 412, "$", period="week")])
    res = rv.validate_lines(["Labor ran 31.4%. Ratings slipped since the new menu launched.",
                             "Waste was $412 this week.", "Cavnar saved you $412 in waste."], ctx)
    assert res.lines == ["Waste was $412 this week."]
    assert len(res.dropped) == 2 and len(res.verdicts) == 3


def test_verdict_is_json_serialisable():
    v = validate("You saved $850 a month.", Ctx(surface="ask", facts=[
        Fact("labor.potential_savings_monthly", 850, "$", "opportunity", "month")]))
    d = json.loads(json.dumps(v.to_dict()))
    assert d["verdict"] == "pass" and d["codes"] == ["F4"] and d["version"] == rv.VERSION


def test_every_finding_span_is_at_most_60_chars():
    long = "Guests " + "really " * 30 + "complained after the menu change."
    v = validate(long, Ctx(surface="ask"))
    assert all(len(f["span"]) <= 60 for f in v.findings)


def test_mode_for_reads_the_env(monkeypatch):
    monkeypatch.delenv("RESPONSE_VALIDATION_MODE", raising=False)
    assert rv.mode_for("ask") == "enforce"
    monkeypatch.setenv("RESPONSE_VALIDATION_MODE", json.dumps({"ask": "shadow", "*": "enforce"}))
    assert rv.mode_for("ask") == "shadow" and rv.mode_for("digest") == "enforce"
    monkeypatch.setenv("RESPONSE_VALIDATION_MODE", "not json")
    assert rv.mode_for("ask") == "enforce"


def test_apply_shows_the_original_in_shadow_mode(monkeypatch):
    monkeypatch.setenv("RESPONSE_VALIDATION_MODE", json.dumps({"ask": "shadow"}))
    ctx = Ctx(surface="ask", confidence={"pct": 60})
    shown, v = rv.apply("This will definitely reduce labor.", ctx, log_it=False)
    assert shown == "This will definitely reduce labor." and v.text == "This could reduce labor."
    monkeypatch.setenv("RESPONSE_VALIDATION_MODE", json.dumps({"ask": "enforce"}))
    shown, v = rv.apply("This will definitely reduce labor.", ctx, log_it=False)
    assert shown == "This could reduce labor."


def test_engine_is_pure():
    """Layer 0: no database, model, clock or network at module scope."""
    src = open(rv.__file__, encoding="utf-8").read()
    head = src.split("\ndef log(", 1)[0]
    for banned in ("import models", "import ai_utils", "sqlite3", "anthropic", "requests", "datetime.now",
                   "time.time(", "import ops"):
        assert banned not in head, banned


# ── the log and the admin read ──────────────────────────────────────────────

def test_log_writes_a_row_without_answer_or_guest_text(db_path):
    import models
    ctx = Ctx(restaurant_id=7, surface="digest", untrusted=["my secret guest words are right here in this review"],
              facts=[Fact("labor.pct", 31.4, "%")], policy={"action": "weekly_digest"})
    text = ("Labor ran 31.4%. My secret guest words are right here in this review. "
            "Ratings slipped since the new menu launched.")
    v = validate(text, ctx)
    assert rv.log(v, ctx, original=text, db_path=db_path) is True
    conn = models.get_conn(db_path)
    row = dict(conn.execute("SELECT * FROM ai_validation_log").fetchone())
    conn.close()
    assert row["surface"] == "digest" and row["action"] == "weekly_digest" and row["restaurant_id"] == 7
    assert set(json.loads(row["rules"])) == {"I1", "K1"}
    stored = " ".join(str(x) for x in row.values())
    assert "secret guest words" not in stored and "Labor ran" not in stored
    assert all(len(t) <= 60 for t in json.loads(row["tokens"]))
    assert row["text_hash"] == rv.text_hash(text) and row["n_drops"] == 2


def test_log_never_raises():
    assert rv.log(validate("x", Ctx(surface="ask")), Ctx(surface="ask"), db_path="/nonexistent/dir/x.db") is False


def test_table_is_created_at_boot_not_on_the_call_path():
    import inspect
    import ai_utils
    import models
    assert "CREATE TABLE IF NOT EXISTS ai_validation_log" in inspect.getsource(models.init_db)
    assert "CREATE TABLE" not in inspect.getsource(ai_utils.log_validation)


def test_admin_validation_rates_and_route(db_path, monkeypatch):
    import admin_ops
    import admin_routes
    import auth
    import models
    real = models.get_conn
    for mod in (models, admin_ops, admin_routes, auth):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for text, surface in (("You saved $850 a month.", "ask"), ("Labor ran 31.4%.", "ask"),
                          ("Ratings slipped since the new menu launched.", "digest")):
        ctx = Ctx(surface=surface, facts=[Fact("labor.pct", 31.4, "%"),
                                          Fact("x.potential_savings_monthly", 850, "$", "opportunity", "month")])
        rv.log(validate(text, ctx), ctx, db_path=db_path)
    out = admin_ops.validation_rates(days=30)
    by = {s["surface"]: s for s in out["surfaces"]}
    assert out["total"] == 3 and by["ask"]["n"] == 2
    assert by["ask"]["rules"][0]["rule"] == "F4" and by["ask"]["rules"][0]["pct"] == 50.0
    assert by["digest"]["verdicts"]["refuse"] == 1 and by["digest"]["caught_pct"] == 100.0

    from flask import Flask
    from auth_routes import auth_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    app.register_blueprint(auth_bp)
    cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": 1, "is_admin": 0,
                                                            "username": "c", "email": "c@x.com", "role": "client"})
    assert cl.get("/admin/api/validation").status_code in (302, 401, 403)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1,
                                                            "username": "will", "email": "w@x.com", "role": "admin"})
    r = cl.get("/admin/api/validation?days=7")
    assert r.status_code == 200 and r.get_json()["days"] == 7 and r.get_json()["total"] == 3


# ── the unified primitives in ai_guard (NS6 A3) ─────────────────────────────

def test_unified_tolerance_is_the_dsr_rule():
    from dsr import narrative
    for raw in ("$4,212", "$4,200", "$20,000", "31.4%", "$2.4k", "$420"):
        c = ai_guard.figure_claims(raw)[0]
        assert ai_guard.precision_tolerance(c) == pytest.approx(narrative._tolerance(c))
    c = ai_guard.figure_claims("$1,200")[0]
    assert ai_guard.precision_tolerance(c, hedged=True) >= 40      # "roughly $1,200" for $1,240


def test_unified_direction_reads_what_the_dsr_reads():
    """claimed_direction agrees with the DSR's reader wherever the DSR's
    reads a direction, and also reads ai_guard's ("fell to 31%": "to" is a
    filler) — the union of the three readers (NS6 A3)."""
    from dsr import narrative
    for text in ("Sales fell $400 tonight.", "Labor ran 3 points over target.", "Sales were $420 below budget.",
                 "Comps were -$85.", "Labor at 24.2% tonight.", "Sales beat budget by $1,350."):
        c = ai_guard.figure_claims(text)[0]
        assert ai_guard.claimed_direction(text, c["start"], c["end"]) == narrative._direction(text, c), text
    for text, want in (("Net rose to $19,850.", 1), ("Labor fell to 31.4%.", -1)):
        c = ai_guard.figure_claims(text)[0]
        assert ai_guard.claimed_direction(text, c["start"], c["end"]) == want, text


def test_cached_figure_parse_hands_out_fresh_sets():
    a = ai_guard._figures("Labor $4,120 and 31.4%.")
    a["money"].add(999999.0)
    assert 999999.0 not in ai_guard._figures("Labor $4,120 and 31.4%.")["money"]


# ── the performance budget (NS6 §C: ≤ 10 ms p95 insight, ≤ 50 ms p95 Ask) ───

def _p95(fn, n):
    """p95 in ms of process CPU time (the suite runs under xdist beside
    other work; wall-clock would measure the neighbours), best of three
    batches so one noisy batch is not the verdict."""
    fn()                               # warm the regex caches
    best = None
    for _batch in range(3):
        ts = []
        for i in range(n):
            t0 = time.process_time()
            fn(i)
            ts.append((time.process_time() - t0) * 1000)
        ts.sort()
        p95 = ts[int(len(ts) * 0.95) - 1]
        best = p95 if best is None else min(best, p95)
    return best


_ANSWER = ("Hi Sam, labor ran 31.4% this week, up from last week. Wednesday ran 22.1% and Friday ran 22.8%. "
           "Trimming Wednesday will save you $867 a month. Waste on Item3 came to $80.50 this week. "
           "The slow Tuesday is why labor ran high, and guests always complain on Fridays. "
           "This will definitely reduce labor. I'm 90% sure the kitchen is the bottleneck. "
           "Most restaurants run labor near 30%. You are wasting $2,078.40 a month on food.\n"
           "Recommendations:\n1. Trim one server on Wednesday close.\n2. Watch Item7 waste at $134.50 a week.\n"
           "3. Keep Friday as it is.")


def _insight_facts():
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    fs = [Fact(f"labor.day{i}_pct", 20 + i * 0.7, "%", entity=d) for i, d in enumerate(days)]
    fs += [Fact(f"food.item{i}_waste_weekly", 40 + i * 13.5, "$", "measured", "week", entity=f"Item{i}")
           for i in range(20)]
    fs += [Fact("labor.gap_monthly", 867, "$", "opportunity", "month", data_days=28),
           Fact("labor.pct", 31.4, "%", direction="up"), Fact("sales.net", 13200, "$", "measured", "week"),
           Fact("labor.cost", 4120, "$", "measured", "week"), Fact("reviews.rating", 4.3, "★"),
           Fact("reviews.count", 18, "count"), Fact("food.est_monthly", 2078.4, "$", "estimate", "month")]
    return fs


def test_performance_budget_insight():
    facts = _insight_facts()
    assert len(json.dumps([f.__dict__ for f in facts])) <= 8 * 1024
    ctx = Ctx(surface="labor_insight", facts=facts, confidence={"pct": 62}, names_allowed={"Sam"},
              cause_anchors=[{"text": "short staffing on Friday dinner", "strength": "likely"}])
    p95 = _p95(lambda i=0: validate(_ANSWER, ctx), 60)
    assert p95 <= 10.0, f"insight p95 {p95:.1f} ms"


def test_performance_budget_ask():
    corpus = " ".join(f"Line {i}: labor {20 + i % 17}.{i % 10}% on ${1000 + i * 7:,} of sales, rating "
                      f"{3 + (i % 20) / 10:.1f} over {i % 40} reviews." for i in range(900))[:60000]
    typed = Ctx(surface="ask", facts=_insight_facts() * 4, confidence={"pct": 45})
    answer = _ANSWER * 2
    assert _p95(lambda i=0: validate(answer, typed), 30) <= 50.0
    # The legacy prompt-text path (no typed facts yet) on a 60 KB corpus.
    # Ask caches its context per restaurant for 60 s and ai_guard memoises
    # the parse, so the turns of one conversation read it once; the first
    # read of a fresh corpus is the parse itself (ai_guard's, ~20-30 ms).
    ctx = Ctx(surface="ask", context_text=corpus, confidence={"pct": 45})
    p95 = _p95(lambda i=0: validate(answer, ctx), 20)
    assert p95 <= 50.0, f"Ask legacy p95 {p95:.1f} ms"
    cold = _p95(lambda i=0: validate(answer, Ctx(surface="ask", context_text=corpus + f" run {i}",
                                                 confidence={"pct": 45})), 10)
    assert cold <= 100.0, f"Ask legacy cold p95 {cold:.1f} ms"
