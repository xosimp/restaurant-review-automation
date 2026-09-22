"""CSV and file imports — the edges of the owner's self-serve upload.

What this protects: an owner exports shifts or inventory from Excel, POS or a
scheduling tool and uploads it at /client/upload-data (client_api.py), or
imports review history at /api/import-tripadvisor. The upload must either be
refused with a reason, or be saved in a shape the Labor and Food Cost modules
can actually read — never "N rows loaded successfully" followed by a broken
tab and the previous, good dataset gone.

Tests without a marker pin behaviour that works today. Tests marked
xfail(strict=True) assert the CORRECT behaviour for a defect confirmed in the
security/edge audit (SEC-16, SEC-24, SEC-26); they flip to a failure the day
the defect is fixed, so the marker is removed with the fix.

Routes are exercised through the Flask test client with client_bp and
admin_bp registered, a real session cookie from auth.create_session, and the
double-submit CSRF pair sent on every POST (harmless when the hook is not
attached, required when hosted_dashboard has wrapped client_bp).
"""
import io
import json
import math
import sys
import threading

import pytest
from flask import Flask

import admin_routes
import auth
import client_api
import labor
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant


CSRF_TOKEN = "edge-sec-csv-csrf-token"

SHIFTS_HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
GOOD_SHIFTS = (
    SHIFTS_HEADER
    + "2026-09-01,Tuesday,Ann,Server,11:00,17:00,6,6,4000,\n"
    + "2026-09-02,Wednesday,Bob,Cook,10:00,18:00,8,8,4200,\n"
)
INVENTORY_FULL = (
    "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week\n"
    "Chicken Breast,Protein,30,22,5.80,6.0,30,3.5\n"
    "Romaine Lettuce,Produce,20,28,2.50,3.5,25,8.0\n"
)
# Exactly the columns the route documents as required — none of the
# "recommended" ones.
INVENTORY_REQUIRED_ONLY = (
    "item,current_stock,par_level,unit_cost,waste_last_week\n"
    "Chicken Breast,22,30,5.80,3.5\n"
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    """Point every module that bound models.get_conn at import time at this
    test's database (CLAUDE.md "Bound imports"). The upload path reaches
    models, auth, client_api, admin_routes, webhooks, schedule_intel and
    others — rather than list them and miss one, patch every loaded module
    whose get_conn IS the real one."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


class _InertThread:
    """Stands in for threading.Thread: an inventory upload starts a
    background trend/webhook job and the TripAdvisor import starts AI review
    processing. Neither belongs in a deterministic test, and a real thread
    would outlive the test's database redirect."""
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self.target = target

    def start(self):
        _InertThread.started.append(self.target)

    def join(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _no_background_threads(monkeypatch):
    _InertThread.started = []
    monkeypatch.setattr(threading, "Thread", _InertThread)


@pytest.fixture
def world(db_path):
    app = Flask(__name__, template_folder="/Users/simp/review_automation/templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(admin_routes.admin_bp)
    rid = create_restaurant(Restaurant(name="Edge CSV Bistro", owner_email="owner@edge.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@edge.test", "pw123456", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    mgr = create_user(rid, "mgr", "mgr@edge.test", "pw123456", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role='manager' WHERE id=?", (mgr,))
    conn.commit()
    conn.close()
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    client = app.test_client()
    client.set_cookie("csrf_js", CSRF_TOKEN)
    return {"client": client, "rid": rid, "owner": owner, "mgr": mgr, "db_path": db_path}


def _login(world, user_id):
    world["client"].set_cookie("session_token", create_session(user_id, db_path=world["db_path"]))


def _upload(world, data_type, raw, filename="data.csv"):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return world["client"].post(
        "/client/upload-data",
        data={"data_type": data_type, "csv_file": (io.BytesIO(raw), filename)},
        content_type="multipart/form-data",
        headers={"X-CSRF": CSRF_TOKEN},
    )


def _stored(world, data_type="shifts"):
    row = models.get_client_data(world["rid"], db_path=world["db_path"])
    return (row or {}).get(f"{data_type}_csv")


def _ingredient_names(world):
    conn = models.get_conn(world["db_path"])
    rows = conn.execute("SELECT name FROM ingredients WHERE restaurant_id=? ORDER BY name",
                        (world["rid"],)).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def _total_actual_hours(analysis):
    return round(sum(float(v.get("actual") or 0) for v in analysis["employee_hours"].values()), 2)


# ── /client/upload-data: the working path (appendix #12, #14) ──────────────

def test_an_owner_uploading_a_well_formed_shifts_csv_saves_it_and_labor_reads_it(world):
    _login(world, world["owner"])
    r = _upload(world, "shifts", GOOD_SHIFTS)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["rows"] == 2
    assert _stored(world) == GOOD_SHIFTS
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    assert analysis["is_live"] is True
    assert analysis["date_range"]["start"] == "2026-09-01"
    assert analysis["date_range"]["end"] == "2026-09-02"
    assert _total_actual_hours(analysis) == 14.0


def test_an_owner_uploading_a_full_inventory_csv_saves_it_and_creates_ingredients(world):
    _login(world, world["owner"])
    r = _upload(world, "inventory", INVENTORY_FULL)
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert _stored(world, "inventory") == INVENTORY_FULL
    assert _ingredient_names(world) == ["Chicken Breast", "Romaine Lettuce"]


def test_an_unknown_data_type_is_refused_and_nothing_is_saved(world):
    _login(world, world["owner"])
    r = _upload(world, "payroll", GOOD_SHIFTS)
    assert r.get_json()["ok"] is False
    assert _stored(world) is None


def test_an_upload_with_no_file_is_refused(world):
    _login(world, world["owner"])
    r = world["client"].post("/client/upload-data", data={"data_type": "shifts"},
                             content_type="multipart/form-data", headers={"X-CSRF": CSRF_TOKEN})
    assert r.get_json()["ok"] is False
    assert _stored(world) is None


def test_an_empty_file_is_refused(world):
    _login(world, world["owner"])
    r = _upload(world, "shifts", "   \n")
    assert r.get_json()["ok"] is False
    assert _stored(world) is None


def test_a_header_only_file_is_refused_as_having_no_data_rows(world):
    _login(world, world["owner"])
    r = _upload(world, "shifts", SHIFTS_HEADER)
    body = r.get_json()
    assert body["ok"] is False
    assert "no data rows" in body["error"]
    assert _stored(world) is None


def test_a_file_missing_a_required_column_is_refused_and_the_previous_dataset_is_kept(world):
    _login(world, world["owner"])
    assert _upload(world, "shifts", GOOD_SHIFTS).get_json()["ok"] is True
    r = _upload(world, "shifts", "date,employee,role\n2026-09-03,Cy,Cook\n")
    body = r.get_json()
    assert body["ok"] is False
    assert "actual_hours" in body["error"]
    assert _stored(world) == GOOD_SHIFTS


def test_an_upload_is_refused_without_a_session(world):
    r = _upload(world, "shifts", GOOD_SHIFTS)
    assert r.status_code in (302, 401, 403)
    assert _stored(world) is None


# ── Encoding (appendix #4, #5 / SEC-26) ─────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-26: a UTF-8 BOM (Excel 'CSV UTF-8') is decoded as utf-8, so the first header reads '\\ufeffdate' and the file is refused as missing 'date'")
def test_a_bom_prefixed_utf8_shifts_csv_is_accepted_and_readable(world):
    _login(world, world["owner"])
    r = _upload(world, "shifts", b"\xef\xbb\xbf" + GOOD_SHIFTS.encode("utf-8"))
    assert r.get_json()["ok"] is True
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    assert analysis["date_range"]["start"] == "2026-09-01"
    assert _total_actual_hours(analysis) == 14.0


@pytest.mark.xfail(strict=True, reason="SEC-26: a cp1252/Latin-1 file (e.g. 'José' from Excel on Windows) fails utf-8 decoding and is refused as 'Could not read file'")
def test_a_cp1252_shifts_csv_is_accepted_with_its_accented_names_intact(world):
    _login(world, world["owner"])
    csv_text = SHIFTS_HEADER + "2026-09-01,Tuesday,José,Server,11:00,17:00,6,6,4000,\n"
    r = _upload(world, "shifts", csv_text.encode("cp1252"))
    assert r.get_json()["ok"] is True
    assert "José" in (_stored(world) or "")


# ── Headers and dates (appendix #6, #7 / SEC-16) ────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-16: capitalised headers pass the lowercased validation but are saved raw; labor reads lowercase keys and analysis raises KeyError")
def test_capitalised_headers_are_refused_or_analysable_after_upload(world):
    _login(world, world["owner"])
    csv_text = (
        "Date,Day,Employee,Role,Shift_Start,Shift_End,Scheduled_Hours,Actual_Hours,Sales,Notes\n"
        "2026-09-01,Tuesday,Ann,Server,11:00,17:00,6,6,4000,\n"
    )
    body = _upload(world, "shifts", csv_text).get_json()
    if not body["ok"]:
        return  # refused with a reason: acceptable
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    assert analysis["date_range"]["start"] == "2026-09-01"
    assert _total_actual_hours(analysis) == 6.0


@pytest.mark.xfail(strict=True, reason="SEC-16: US M/D/YYYY dates pass validation, then analysis parses dates as %Y-%m-%d and fails")
def test_us_month_day_year_dates_are_refused_or_parsed_after_upload(world):
    _login(world, world["owner"])
    csv_text = (
        SHIFTS_HEADER
        + "9/1/2026,Tuesday,Ann,Server,11:00,17:00,6,6,4000,\n"
        + "9/2/2026,Wednesday,Bob,Cook,10:00,18:00,8,8,4200,\n"
    )
    body = _upload(world, "shifts", csv_text).get_json()
    if not body["ok"]:
        return  # refused with a reason: acceptable
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    assert analysis["date_range"]["start"] == "2026-09-01"
    assert analysis["date_range"]["end"] == "2026-09-02"


# ── Numbers (appendix #8 / SEC-16) ──────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-16: non-finite, negative and absurd numbers pass validation and are saved, poisoning labor analysis")
@pytest.mark.parametrize("column,value", [
    ("actual_hours", "nan"),
    ("actual_hours", "inf"),
    ("actual_hours", "-8"),
    ("actual_hours", "1e308"),
    ("sales", "nan"),
    ("sales", "inf"),
    ("sales", "-4000"),
])
def test_a_shifts_file_with_an_unusable_number_is_refused_and_the_previous_dataset_kept(world, column, value):
    _login(world, world["owner"])
    assert _upload(world, "shifts", GOOD_SHIFTS).get_json()["ok"] is True
    hours = value if column == "actual_hours" else "6"
    sales = value if column == "sales" else "4000"
    bad = SHIFTS_HEADER + f"2026-09-03,Thursday,Cy,Cook,10:00,18:00,8,{hours},{sales},\n"
    body = _upload(world, "shifts", bad).get_json()
    assert body["ok"] is False
    assert _stored(world) == GOOD_SHIFTS


@pytest.mark.xfail(strict=True, reason="SEC-16: a saved NaN/inf reaches analysis and yields labor_pct=inf, which json.dumps writes as 'Infinity' (invalid JSON)")
def test_labor_analysis_of_a_saved_file_never_emits_non_finite_numbers(world):
    bad = (
        SHIFTS_HEADER
        + "2026-09-01,Tuesday,Ann,Server,11:00,17:00,6,nan,4000,\n"
        + "2026-09-01,Tuesday,Bob,Cook,10:00,18:00,8,-8,inf,\n"
        + "2026-09-02,Wednesday,Cy,Cook,10:00,18:00,8,1e308,4000,\n"
    )
    # Written the way a pre-fix upload would have stored it.
    models.save_client_data(world["rid"], "shifts", bad, source="upload", db_path=world["db_path"])
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    json.dumps(analysis, allow_nan=False, default=str)  # raises on NaN/Infinity
    assert math.isfinite(float(analysis["overall_labor_pct"]))


# ── A refused upload keeps the previous dataset (appendix #9 / SEC-16) ──────

def test_a_valid_re_upload_replaces_the_current_dataset(world):
    """Pins today's contract: an accepted upload IS the new dataset."""
    _login(world, world["owner"])
    assert _upload(world, "shifts", GOOD_SHIFTS).get_json()["ok"] is True
    newer = SHIFTS_HEADER + "2026-09-08,Tuesday,Dee,Host,17:00,22:00,5,5,3900,\n"
    assert _upload(world, "shifts", newer).get_json()["ok"] is True
    assert _stored(world) == newer


@pytest.mark.xfail(strict=True, reason="SEC-16: a file whose dates analysis cannot read is accepted and overwrites the previous good dataset with no version kept")
def test_a_shifts_file_whose_dates_cannot_be_read_is_refused_and_the_previous_dataset_kept(world):
    _login(world, world["owner"])
    assert _upload(world, "shifts", GOOD_SHIFTS).get_json()["ok"] is True
    unreadable = (
        SHIFTS_HEADER
        + "notadate,Tuesday,Ann,Server,11:00,17:00,6,6,4000,\n"
        + "sometime,Wednesday,Bob,Cook,10:00,18:00,8,8,4200,\n"
    )
    body = _upload(world, "shifts", unreadable).get_json()
    assert body["ok"] is False
    assert _stored(world) == GOOD_SHIFTS
    analysis = labor.analyse_shifts_for_restaurant(world["rid"])
    assert analysis["date_range"]["start"] == "2026-09-01"


# ── Inventory optional columns (appendix #10 / SEC-26) ──────────────────────

def test_an_inventory_csv_with_only_the_required_columns_is_refused_or_creates_its_ingredients(world):
    _login(world, world["owner"])
    body = _upload(world, "inventory", INVENTORY_REQUIRED_ONLY).get_json()
    if not body["ok"]:
        return  # refused, naming the missing column: acceptable
    assert _ingredient_names(world) == ["Chicken Breast"]


# ── Formula-leading cells (appendix #11 / SEC-16) ───────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-16: a formula-leading cell (=HYPERLINK(...)) is stored verbatim and later exported, a CSV-injection vector")
def test_a_formula_leading_cell_is_refused_or_stored_neutralised(world):
    _login(world, world["owner"])
    csv_text = SHIFTS_HEADER + '2026-09-01,Tuesday,"=HYPERLINK(""http://evil.test"",""x"")",Server,11:00,17:00,6,6,4000,\n'
    body = _upload(world, "shifts", csv_text).get_json()
    if not body["ok"]:
        return  # refused: acceptable
    names = [row["employee"] for row in labor.load_shifts(csv_string=_stored(world))]
    assert names, "the accepted file must have been stored"
    assert not any(n.startswith(("=", "+", "@")) for n in names)


# ── Permission (appendix #14 / SEC-24) ──────────────────────────────────────

def test_a_manager_is_refused_the_food_cost_cogs_route(world):
    """Control for the two SEC-24 tests below: the manager fixture really
    lacks FOOD_COST_VIEW, and the gate refuses it where the path is mapped."""
    _login(world, world["mgr"])
    assert world["client"].get("/api/food-cost/cogs").status_code == 403


@pytest.mark.xfail(strict=True, reason="SEC-24: /client/upload-data is not in the module gate, so a manager without FOOD_COST_VIEW can replace the inventory dataset")
def test_a_manager_without_food_cost_access_cannot_upload_an_inventory_csv(world):
    _login(world, world["owner"])
    assert _upload(world, "inventory", INVENTORY_FULL).get_json()["ok"] is True
    _login(world, world["mgr"])
    r = _upload(world, "inventory", "item,current_stock,par_level,unit_cost,waste_last_week,avg_daily_usage\nX,1,1,1,1,1\n")
    assert r.status_code == 403
    assert _stored(world, "inventory") == INVENTORY_FULL


@pytest.mark.xfail(strict=True, reason="SEC-24: /api/inv-trend is not in the module gate, so a manager without FOOD_COST_VIEW reads the waste series")
def test_a_manager_without_food_cost_access_cannot_read_the_inventory_trend(world):
    _login(world, world["mgr"])
    assert world["client"].get("/api/inv-trend").status_code == 403


def test_a_manager_can_still_upload_shifts(world):
    """Labor is within the manager's access; the inventory refusal above
    must not be implemented by refusing managers the whole upload route."""
    _login(world, world["mgr"])
    body = _upload(world, "shifts", GOOD_SHIFTS).get_json()
    assert body["ok"] is True
    assert _stored(world) == GOOD_SHIFTS


# ── TripAdvisor / third-party review import (appendix #13) ──────────────────
#
# Found while writing these (not in SEC.md): reviews.platform carries
# CHECK(platform IN ('google','yelp','csv','manual')) in init_db()'s schema,
# and the re-key migration copies that CREATE statement verbatim. The
# importer saves every row as 'tripadvisor' (or a 'doordash'/'ubereats'
# override), so save_reviews() rejects them all while the route answers
# ok=True, imported=N, new=0. Separately, the route's background step imports
# analyser.process_new_reviews, which does not exist; the ImportError is
# swallowed, so the "AI drafting in the background" the admin screen promises
# never starts (analyse_pending in the daily pipeline is the only catch-up).

CSV_IMPORT_CHECK = ("NEW (found writing SEC tests, not in SEC.md): reviews.platform CHECK allows only "
                    "google/yelp/csv/manual, so every tripadvisor/doordash/ubereats import row is rejected "
                    "by the database while the route reports ok")

TA_EXPORT = (
    "Rating,Title,Review Text,Reviewer,Date\n"
    "5,Lovely,Great pasta and kind staff,Ann,2026-09-01\n"
    "2,Slow,Waited forty minutes for mains,Bob,2026-09-02\n"
)


def _import_reviews(world, raw, platform=None):
    data = {"file": (io.BytesIO(raw), "reviews.csv")}
    if platform:
        data["platform"] = platform
    return world["client"].post("/api/import-tripadvisor", data=data,
                                content_type="multipart/form-data",
                                headers={"X-CSRF": CSRF_TOKEN})


def _reviews(world):
    conn = models.get_conn(world["db_path"])
    rows = conn.execute("SELECT platform, author, rating, text FROM reviews WHERE restaurant_id=? ORDER BY rating",
                        (world["rid"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_a_bom_prefixed_tripadvisor_export_with_capitalised_headers_is_decoded_and_parsed(world):
    """The utf-8-sig decode and the case-insensitive header lookup — the two
    things the shifts/inventory importer lacks (SEC-26) — work here."""
    _login(world, world["owner"])
    r = _import_reviews(world, b"\xef\xbb\xbf" + TA_EXPORT.encode("utf-8"))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True
    assert body["imported"] == 2


def test_a_review_import_counts_only_rows_with_a_rating_from_one_to_five_and_text(world):
    _login(world, world["owner"])
    csv_text = (
        "rating,text,author\n"
        "4,Good,Ann\n"
        "9,Off the scale,Bob\n"
        ",No rating,Cy\n"
        "abc,Not a number,Dee\n"
        "3,,Eve\n"
    )
    body = _import_reviews(world, csv_text.encode("utf-8")).get_json()
    assert body["ok"] is True
    assert body["imported"] == 1


def test_a_review_import_with_no_usable_rows_is_refused(world):
    _login(world, world["owner"])
    r = _import_reviews(world, b"name,comment\nAnn,\n")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
    assert _reviews(world) == []


def test_a_review_import_that_is_not_utf8_is_refused_with_a_reason(world):
    _login(world, world["owner"])
    r = _import_reviews(world, "rating,text\n5,Caf\xe9 was lovely\n".encode("latin-1"))
    assert r.status_code == 400
    assert "UTF-8" in r.get_json()["error"]


def test_a_review_import_with_no_file_is_refused(world):
    _login(world, world["owner"])
    r = world["client"].post("/api/import-tripadvisor", data={},
                             content_type="multipart/form-data", headers={"X-CSRF": CSRF_TOKEN})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


@pytest.mark.xfail(strict=True, reason=CSV_IMPORT_CHECK)
def test_imported_tripadvisor_reviews_are_stored_with_their_title_and_platform(world):
    _login(world, world["owner"])
    body = _import_reviews(world, b"\xef\xbb\xbf" + TA_EXPORT.encode("utf-8")).get_json()
    assert body["new"] == 2
    rows = _reviews(world)
    assert [(x["platform"], x["author"], x["rating"]) for x in rows] == [
        ("tripadvisor", "Bob", 2), ("tripadvisor", "Ann", 5)]
    assert rows[1]["text"] == "Lovely — Great pasta and kind staff"


@pytest.mark.xfail(strict=True, reason=CSV_IMPORT_CHECK)
def test_a_review_import_takes_an_allowed_platform_override_and_ignores_others(world):
    _login(world, world["owner"])
    first = "rating,text,author\n4,Fast delivery,Ann\n"
    assert _import_reviews(world, first.encode(), platform="DoorDash").get_json()["ok"] is True
    second = "rating,text,author\n3,Cold fries,Bob\n"
    assert _import_reviews(world, second.encode(), platform="google").get_json()["ok"] is True
    assert sorted((x["author"], x["platform"]) for x in _reviews(world)) == [
        ("Ann", "doordash"), ("Bob", "tripadvisor")]


@pytest.mark.xfail(strict=True, reason="NEW (found writing SEC tests, not in SEC.md): the import's background step imports analyser.process_new_reviews, which does not exist; the ImportError is swallowed and no analysis is started")
def test_a_review_import_starts_background_analysis_of_the_new_reviews(world, monkeypatch):
    # Isolated from the CHECK defect above: save_reviews is stubbed to report
    # one new review, so the only thing under test is the background hand-off.
    fake = models.Review(restaurant_id=world["rid"], platform="csv", external_id="x",
                         author="Ann", rating=5, text="Superb")
    monkeypatch.setattr(models, "save_reviews", lambda reviews, *a, **k: (1, [fake]))
    _login(world, world["owner"])
    assert _import_reviews(world, b"rating,text\n5,Superb\n").get_json()["ok"] is True
    assert len(_InertThread.started) == 1
    assert callable(_InertThread.started[0])
