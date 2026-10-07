"""Re-audit 9/29/26 — what the review diagnosis and the links read (R8).

CROSSMODULE-9   the review diagnosis's slice never read what staff actually
                WORKED (shift_facts) or the nightly reports' no-shows, and it
                read no links — Simple EJ's has 1,137 worked shifts and 0
                published-schedule outcomes, so its staffing line was empty.
CROSSMODULE-18  the links told the owner to "check covers" although the
                nightly report records guests every night; neither diagnosis
                read them.
"""
from datetime import date, timedelta

import pytest

import ai_guard
import business_intelligence as bi
import link_memory as lm
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(bi, "get_conn", redirect, raising=False)
    yield


# The restaurant's date (operator time when no zone is set), the edge of
# every window read here - not the machine's (CI is UTC: tomorrow after 7pm).
from time_utils import restaurant_now as _rnow
TODAY = _rnow(None, naive=True).date()
NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _rid(name="Slice R8 Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _last(weekday, weeks_back):
    d = TODAY - timedelta(days=1)
    while NAMES[d.weekday()] != weekday:
        d -= timedelta(days=1)
    return d - timedelta(days=7 * weeks_back)


def _metric(rid, day, metric, value):
    _exec("INSERT OR REPLACE INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
          "VALUES (?,?,?,?,?)", (rid, day.isoformat(), metric, value, "final"))


def _cluster(**kw):
    c = {"category": "service", "mentions": 7, "review_ids": [1, 2], "avg_rating": 2.0, "dish": None, "role": None,
         "daypart": {"value": "dinner", "count": 5, "share": 0.7},
         "weekday": {"value": "Friday", "count": 5, "share": 0.7}, "weekday_pair": None, "complaints": [],
         "worst_severity": "service", "severity_counts": {}, "unclassified": 0, "first_seen": None,
         "last_seen": None, "window_days": 90}
    c.update(kw)
    return c


def _seed_worked(rid):
    # Friday nights: 3 people, 18 hours lately; 4 people, 26 hours before.
    for w in range(4):
        for i, n in enumerate(("ana", "bo", "cy")):
            _exec("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                  "shift_start, actual_hours, source) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, _last("Friday", w).isoformat(), n, n, "server", "17:00", 6.0, "pos"))
        for i, n in enumerate(("ana", "bo", "cy", "di")):
            _exec("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                  "shift_start, actual_hours, source) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, _last("Friday", w + 4).isoformat(), n, n, "server", "17:00", 6.5, "pos"))
        # A Friday lunch shift is not in a dinner slice.
        _exec("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
              "shift_start, actual_hours, source) VALUES (?,?,?,?,?,?,?,?)",
              (rid, _last("Friday", w).isoformat(), "lu", "lu", "server", "10:00", 5.0, "pos"))


def test_the_slice_reads_what_staff_actually_worked_with_no_published_schedule():
    import review_intelligence as ri
    rid = _rid()
    _seed_worked(rid)
    sl = ri.slice_context(rid, _cluster())
    worked = sl["lines"]["worked"]
    assert isinstance(worked, ai_guard.OperationalLine)
    assert "Worked: Friday dinner averaged 18 actual hours and 3 people" in worked
    assert "against 26 hours and 4 people" in worked
    assert worked.fields["worked_hours_now"]["value"] == 18.0
    assert worked.fields["worked_people_before"]["value"] == 4.0
    assert "Worked:" in sl["block"]
    # The model's evidence is checked against the line's own fields.
    kept, dropped = ai_guard.verify_operational_evidence(
        [{"module": "worked", "metric": "hours", "value": "18 actual hours, down from 26"}], {"worked": worked},
        ri.OPERATIONAL_MODULES)
    assert kept and not dropped
    kept, dropped = ai_guard.verify_operational_evidence(
        [{"module": "worked", "metric": "hours", "value": "14 hours"}], {"worked": worked}, ri.OPERATIONAL_MODULES)
    assert not kept and dropped


def test_the_slice_reads_the_nightly_reports_no_shows_and_guests():
    import review_intelligence as ri
    rid = _rid()
    for w in range(4):
        d = _last("Friday", w)
        _metric(rid, d, "labor.no_shows", 1 if w < 2 else 0)
        _metric(rid, d, "labor.late_arrivals", 1)
        _metric(rid, d, "sales.guests", 150)
        _metric(rid, d, "labor.hours", 50)
        _metric(rid, _last("Tuesday", w), "labor.no_shows", 3)       # not the slice's night
    sl = ri.slice_context(rid, _cluster())
    n = sl["lines"]["nightly"]
    assert "2 no-shows over 4 of those nights" in n and "4 late arrivals" in n
    assert "150 guests a night (4 nights), 3 per labor hour" in n
    assert n.fields["no_shows"]["value"] == 2 and n.fields["slice_gplh"]["value"] == 3.0


def test_the_diagnosis_may_cite_the_worked_and_nightly_lines():
    import review_intelligence as ri
    assert {"worked", "nightly", "guests"} <= set(ri.OPERATIONAL_MODULES)
    # The module list is built per cluster from its lines (re-audit P4-12).
    shape = ri.evidence_guide({m: None for m in ri.OPERATIONAL_MODULES})["evidence_shape"]
    assert "labor|food_cost|waste|marketing|shifts|guests|worked|nightly|games" in shape
    # The cluster's own shape sits in the message since the prompt was split
    # for the cache (AI cost audit 10/7/26 #63).
    assert "{evidence_shape}" in ri.DIAGNOSE_USER
    import inspect
    # The evidence is gathered in diagnosis_plan since the 4am batch shares
    # it (AI cost audit 10/7/26 #58).
    assert 'cl_lines.update({k: v for k, v in (sl.get("lines") or {}).items()' in inspect.getsource(ri.diagnosis_plan)


def test_the_review_diagnosis_reads_its_links():
    import memory_context
    rid = _rid()
    lm.observe(rid, [{"kind": "dsr_x_reviews", "day": "Friday", "subject": "service:friday",
                      "modules": ["reviews", "dsr"], "headline": "7 service complaints on Friday, no-shows on 3 of 6",
                      "confirm_by": "Read the Friday reviews."},
                     {"kind": "reviews_x_food_cost", "day": "Friday", "subject": "service:friday",
                      "modules": ["reviews", "food_cost"], "headline": "food"}])
    assert "links" in memory_context.SURFACE_SECTIONS["review_diagnosis"]
    lines = lm.link_lines(memory_context.MemoryRequest(rid, "review_diagnosis"))
    assert [l["text"].split(" (")[0] for l in lines] == ["7 service complaints on Friday, no-shows on 3 of 6"]
    assert lines[0]["module"] == "labor" and lines[0]["trusted"] is False


# ── CROSSMODULE-18 ───────────────────────────────────────────────────────────

LEAN_FRIDAY_LABOR = {"is_live": True, "period_days": 28, "date_range": {"days": 28},
                     "dow_summary": {"Monday": 30, "Tuesday": 31, "Wednesday": 30, "Thursday": 30, "Friday": 25,
                                     "Saturday": 29, "Sunday": 30}}
CLUSTERS = [{"category": "service", "mentions": 7, "window_days": 90, "weekday": {"value": "Friday", "share": 0.6}}]


def test_a_link_states_the_covers_the_nightly_report_measured():
    rid = _rid()
    for w in range(4):
        _metric(rid, _last("Friday", w), "sales.guests", 200)
        _metric(rid, _last("Friday", w), "labor.hours", 40)
        for d in ("Monday", "Tuesday"):
            _metric(rid, _last(d, w), "sales.guests", 90)
            _metric(rid, _last(d, w), "labor.hours", 45)
    data = {"reviews": {"clusters": CLUSTERS}, "labor": LEAN_FRIDAY_LABOR, "dsr": bi._dsr_nights(rid)}
    link = next(l for l in bi.correlations(rid, data=data) if l["kind"] == "reviews_x_labor")
    assert "5 guests per labor hour on Fridays (4 nights) against 2 on the other nights (8)" in link["evidence"][-1]
    assert "Check whether Friday covers rose" not in link["confirm_by"]
    assert "cover-count" not in link["not_a_cause"] and "guests per labor hour" in link["not_a_cause"]
    assert link["covers"]["gplh"] == 5.0


def test_without_measured_covers_the_link_keeps_its_old_sentences():
    rid = _rid()
    data = {"reviews": {"clusters": CLUSTERS}, "labor": LEAN_FRIDAY_LABOR, "dsr": None}
    link = next(l for l in bi.correlations(rid, data=data) if l["kind"] == "reviews_x_labor")
    assert "Check whether Friday covers rose" in link["confirm_by"] and "cover-count" in link["not_a_cause"]


def test_both_diagnoses_read_the_measured_guests():
    import food_cost_intelligence as fci
    import review_intelligence as ri
    rid = _rid()
    for k in range(1, 8):
        _metric(rid, TODAY - timedelta(days=k), "sales.guests", 120)
        _metric(rid, TODAY - timedelta(days=k), "labor.hours", 40)
        _metric(rid, TODAY - timedelta(days=28 + k), "sales.guests", 100)
    g = bi.measured_guests(rid)
    assert g["nights"] == 7 and g["avg_guests"] == 120.0 and g["gplh"] == 3.0 and g["before_avg_guests"] == 100.0
    line = ri._operational_lines({"guests": g})["guests"]
    assert "120 guests a night over the last 28 days (7 nights measured), 3 guests per labor hour, against 100" in line
    assert ri.operational_context(rid)["guests"]["avg_guests"] == 120.0
    assert fci.operational_context(rid)["guests"]["avg_guests"] == 120.0
    assert "guests" in fci._operational_lines({"guests": g}) and "guests" in fci.OPERATIONAL_MODULES
    # Two nights is not a reading.
    rid2 = _rid("Thin Co")
    for k in (1, 2):
        _metric(rid2, TODAY - timedelta(days=k), "sales.guests", 120)
    assert bi.measured_guests(rid2) is None
