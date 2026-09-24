"""Workstream A infrastructure: what every call site uses to adopt the
Response Validation Layer.

- the engine's typed + prompt-text ("hybrid") mode: typed facts carry the
  kinds that matter; a figure only the prompt states is a measured figure
  with the old presence semantics, never less strict than the old check;
- response_validation.enforce / Validated / payload / legacy_note /
  entity_facts;
- models.other_tenant_names (T1's list), cached and dropped on a change;
- insight_store's versioned reads: a stored read is re-validated from the
  model's own text when the engine version changes, with no model call.
"""
import json

import models
import response_validation as rv
from response_validation import Fact, ValidationContext as Ctx

PROMPT = """Data:
- Overall labor cost: $12,400 on $40,000 in sales (31.0% labor ratio)
- Overstaffed days: [{"day": "Tuesday", "labor_pct": 38.2, "cost": 1450}]
- Opportunity (gap above target, not money saved): $867 a month
- Window: 30 days of shifts; rating 4.3 from 212 reviews
"""
GAP = Fact("labor.gap_monthly", 867, "$", "opportunity", "month", data_days=30)


def _ctx(**kw):
    base = dict(restaurant_id=1, surface="labor_insight", facts=[GAP], context_text=PROMPT)
    base.update(kw)
    return Ctx(**base)


# ── hybrid mode ─────────────────────────────────────────────────────────────

def test_a_figure_only_the_prompt_states_still_backs_the_claim():
    # Typed facts alone would call 38.2% (a JSON number) and $40,000 invented.
    v = rv.validate("Labor ran 31.0% on $40,000 in sales; Tuesday ran 38.2%.", _ctx())
    assert v.verdict == "pass", v.findings


def test_the_typed_kind_wins_over_the_prompt_text():
    # The prompt says "$867 a month" too; the typed opportunity decides it.
    v = rv.validate("You saved $867 a month.", _ctx())
    assert "not money saved" in v.text and "You saved" not in v.text


def test_an_invented_figure_is_still_caught_in_hybrid_mode():
    v = rv.validate("Tuesday ran 44% labor.", _ctx())
    assert v.verdict == "withhold" and "F1" in v.codes


def test_a_percent_never_rests_on_a_bare_count_of_days():
    v = rv.validate("Labor ran 30% over target.", _ctx())
    assert "F1" in v.codes or "F8" in v.codes


def test_a_prompt_figure_keeps_the_period_the_prompt_gave_it():
    ctx = _ctx(context_text=PROMPT + "- Waste: $300 a week\n")
    assert "F3" in rv.validate("Waste ran $300 a month.", ctx).codes
    assert rv.validate("Waste ran $300 a week.", ctx).verdict == "pass"


def test_points_can_be_a_move_between_two_prompt_percentages():
    assert rv.validate("Tuesday ran 7.2 points over the period's labor ratio.", _ctx()).verdict == "pass"


def test_typed_only_policy_turns_the_prompt_fallback_off():
    v = rv.validate("Labor ran 31.0%.", _ctx(policy={"typed_only": True}))
    assert "F1" in v.codes


# ── enforce / Validated / payload ───────────────────────────────────────────

def test_enforce_returns_a_str_carrying_the_structured_verdict():
    out = rv.enforce("Tuesday ran 44% labor.", _ctx(), log_it=False)
    assert isinstance(out, str) and isinstance(out, rv.Validated)
    assert out.validation == {"verdict": "withhold", "caveats": out.verdict.actions["caveats"],
                              "controls": False, "codes": ["F1"], "version": rv.VERSION}
    # The legacy marker the clients still read, worded as the caveats.
    assert out.endswith("UNVERIFIED: Some figures here aren't in your data: 44%.")
    assert json.dumps({"t": out}) == json.dumps({"t": str(out)})


def test_a_clean_text_carries_no_marker_and_a_pass():
    out = rv.enforce("Labor ran 31.0% this period.", _ctx(), log_it=False)
    assert "UNVERIFIED" not in out and out.validation["verdict"] == "pass" and out.validation["controls"]


def test_a_refused_text_is_empty():
    out = rv.enforce("Unlike Gia Mia down the street, you run lean.",
                     _ctx(tenant_names_denied={"Gia Mia"}, surface="digest"), log_it=False)
    assert out == "" and out.validation["verdict"] == "refuse"


def test_shadow_mode_shows_the_original_without_a_marker(monkeypatch):
    monkeypatch.setenv("RESPONSE_VALIDATION_MODE", '{"labor_insight": "shadow"}')
    out = rv.enforce("Tuesday ran 44% labor.", _ctx(), log_it=False)
    assert out == "Tuesday ran 44% labor." and out.validation["verdict"] == "withhold"


def test_strip_marker_removes_a_stored_reads_old_line():
    assert rv.strip_marker("Labor ran 31%.\n\nUNVERIFIED: $170") == "Labor ran 31%."


def test_entity_facts_bind_a_days_figure_to_that_day():
    facts = rv.entity_facts({"Tuesday": [38.2], "Wednesday": [29.1]}, [31.0])
    ok = rv.validate("Tuesday ran 38.2% labor.", Ctx(surface="labor_insight", facts=facts))
    bad = rv.validate("Wednesday ran 38.2% labor.", Ctx(surface="labor_insight", facts=facts))
    assert ok.verdict == "pass" and "F2" in bad.codes


def test_anchor_helper():
    assert rv.anchor("  short   staffing ", "association") == [{"text": "short staffing", "strength": "association"}]
    assert rv.anchor(None) == []


# ── other_tenant_names ──────────────────────────────────────────────────────

def test_other_tenant_names_excludes_itself_and_its_own_group(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    models.init_db(db)
    conn = models.get_conn(db)
    rows = [("Gia Mia", None, None, "a@x.com"), ("Simple EJ's", "Simple EJ's Downtown", "EJ", "e@x.com"),
            ("Simple EJ's Uptown", None, "EJ", "e@x.com"), ("Pizza", None, None, "p@x.com"),
            ("Lula Cafe", None, None, "l@x.com")]
    ids = []
    for name, loc, group, email in rows:
        cur = conn.execute("INSERT INTO restaurants (name, location_name, location_group, owner_email) "
                           "VALUES (?,?,?,?)", (name, loc, group, email))
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    names = models.other_tenant_names(ids[1], db_path=db)
    assert names == {"Gia Mia", "Lula Cafe"}          # not its sibling, itself, or a generic name
    assert "Simple EJ's" in models.other_tenant_names(ids[0], db_path=db)


def test_other_tenant_names_are_dropped_on_a_restaurant_change(tmp_path):
    db = str(tmp_path / "t.db")
    models.init_db(db)
    conn = models.get_conn(db)
    a = conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Alpha Diner','a@x.com')").lastrowid
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Beta Bistro','b@x.com')")
    conn.commit()
    assert models.other_tenant_names(a, db_path=db) == {"Beta Bistro"}
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Gamma Grill House','g@x.com')")
    conn.commit()
    conn.close()
    assert "Gamma Grill House" not in models.other_tenant_names(a, db_path=db)     # cached
    models._notify_restaurant_change(a)
    assert "Gamma Grill House" in models.other_tenant_names(a, db_path=db)


# ── insight_store versioned reads ───────────────────────────────────────────

def test_a_stored_read_is_revalidated_when_the_engine_version_changes(tmp_path, monkeypatch):
    import insight_store
    db = str(tmp_path / "s.db")
    models.init_db(db)
    ctx = _ctx()
    raw = "You saved $867 a month."
    out = rv.enforce(raw, ctx, log_it=False)
    assert insight_store.put(5, "labor", "fp", out, db_path=db, raw=raw)
    calls = []

    def reval(r):
        calls.append(r)
        return rv.enforce(r, ctx, log_it=False)

    same = insight_store.get(5, "labor", "fp", db_path=db, revalidate=reval)
    assert same == out and same.validation == out.validation and not calls
    monkeypatch.setattr(rv, "VERSION", "rv-next")
    again = insight_store.get(5, "labor", "fp", db_path=db, revalidate=reval)
    assert calls == [raw] and "not money saved" in again
    stored = json.loads(models.get_conn(db).execute("SELECT payload FROM insight_cache").fetchone()[0])
    assert stored["_rv"] == "rv-next" and stored["raw"] == raw


def test_a_pre_engine_stored_read_is_revalidated_from_its_own_text(tmp_path):
    import insight_store
    db = str(tmp_path / "s.db")
    models.init_db(db)
    insight_store.put(5, "food", "fp", "You saved $867 a month.\n\nUNVERIFIED: $170", db_path=db)
    got = insight_store.get(5, "food", "fp", db_path=db, revalidate=lambda r: rv.enforce(r, _ctx(), log_it=False))
    assert "UNVERIFIED: $170" not in got and "not money saved" in got
    # A caller that has not adopted the engine still gets the stored text.
    assert insight_store.get(5, "food", "fp", db_path=db).startswith("About $867")
