"""The shifts CSV upload (/client/upload-data, data_type=shifts) as the MOD
audit found it: an upload must either be refused with a message the owner
can act on, or be saved in a shape the Labor module can read. Never "N rows
loaded successfully" followed by a Labor tab that raises. Also the side
effects of a successful upload: the overtime alert email and the
labor.updated webhook.

Driven through the Flask test client with client_bp registered, a real
session cookie from auth.create_session and the double-submit CSRF pair.
Every outbound email and webhook is stubbed. Confirmed defects are strict
xfails naming the finding."""
import io
import os
import sys
import threading
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "edge-mod-a-labor-csrf"
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"



# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, delayed, demand, food_cost_intelligence, intraday, inventory, inventory_ledger  # noqa: E401,F401
import invoices, issues, marketing_signals, notify, ops, pos, push, recipes, reporter  # noqa: E401,F401
import staff_settings, strategy_jobs, toast, square, clover, webhooks  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    # Every repo module holding a get_conn: the real one by identity, and any
    # stale redirect an earlier test's lazy import bound (CLAUDE.md "Bound imports").
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)


class _InertThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self.target = target

    def start(self):
        return None

    def join(self, *a, **k):
        return None


@pytest.fixture
def sent(monkeypatch):
    """Every Resend SDK send and every fired webhook, captured."""
    import resend
    import webhooks
    out = {"email": [], "webhook": []}
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda p: out["email"].append(p)), raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook",
                        lambda rid, event, payload=None, *a, **k: out["webhook"].append((event, payload)))
    monkeypatch.setattr(threading, "Thread", _InertThread)
    return out


@pytest.fixture
def world(db_path):
    app = Flask(__name__, template_folder="/Users/simp/review_automation/templates")
    app.register_blueprint(client_api.client_bp)
    rid = create_restaurant(Restaurant(name="Upload Grill", owner_email="owner@upload.test",
                                       module_labor=1), db_path=db_path)
    uid = create_user(rid, "owner", "owner@upload.test", "pw123456", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    client = app.test_client()
    client.set_cookie("csrf_js", CSRF)
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    return {"client": client, "rid": rid, "db_path": db_path}


def _upload(world, raw, data_type="shifts"):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return world["client"].post("/client/upload-data",
                                data={"data_type": data_type, "csv_file": (io.BytesIO(raw), "shifts.csv")},
                                content_type="multipart/form-data", headers={"X-CSRF": CSRF})


def _stored(world):
    row = models.get_client_data(world["rid"], db_path=world["db_path"])
    return (row or {}).get("shifts_csv")


def _accepted_then_readable(world, resp):
    """The invariant every spreadsheet shape must meet: refused with a
    message, or saved and analysable."""
    body = resp.get_json()
    if not body.get("ok"):
        assert body.get("error"), body
        return
    a = labor.analyse_shifts_for_restaurant(world["rid"])
    assert a["is_live"] is True


# ── A3 #23-#26: real spreadsheet shapes through the route ───────────────────

def test_an_excel_date_upload_is_refused_or_readable(world, sent):
    _accepted_then_readable(world, _upload(world, "date,employee,role,actual_hours,sales\n9/14/2026,A,Server,8,100\n"))


def test_a_title_case_header_upload_is_refused_or_readable(world, sent):
    _accepted_then_readable(world, _upload(world, "Date,Employee,Role,Actual_Hours,Sales\n2026-09-14,A,Server,8,1000\n"))


def test_a_bom_prefixed_upload_is_accepted_and_readable(world, sent):
    r = _upload(world, "﻿date,employee,role,actual_hours,sales\n2026-09-14,A,Server,8,1000\n")
    assert r.get_json()["ok"] is True
    assert labor.analyse_shifts_for_restaurant(world["rid"])["total_sales"] == 1000.0


def test_a_totals_row_upload_is_refused_or_readable(world, sent):
    _accepted_then_readable(world, _upload(world, "date,employee,role,actual_hours,sales\n"
                                                  "2026-09-14,A,Server,8,1000\n,Total,,8,\n"))


def test_a_revenue_column_upload_shows_its_sales(world, sent):
    r = _upload(world, "date,employee,role,actual_hours,revenue\n2026-09-14,A,Server,8,4200\n")
    assert r.get_json()["ok"] is True
    assert labor.analyse_shifts_for_restaurant(world["rid"])["total_sales"] == 4200.0


# ── A3 #29-#31: size, missing columns, encoding ─────────────────────────────

def test_a_file_over_the_row_cap_is_refused_with_413_and_nothing_is_saved(world, sent, monkeypatch):
    monkeypatch.setattr(client_api, "MAX_CSV_ROWS", 5)
    rows = "".join(f"2026-09-{d:02d},Monday,A,Server,11:00,17:00,6,6,1000,\n" for d in range(1, 8))
    r = _upload(world, HEADER + rows)
    assert r.status_code == 413
    assert r.get_json()["ok"] is False and "5" in r.get_json()["error"]
    assert _stored(world) is None


def test_a_file_missing_a_required_column_names_the_column(world, sent):
    r = _upload(world, "date,employee,role,sales\n2026-09-14,A,Server,1000\n")
    body = r.get_json()
    assert body["ok"] is False and "actual_hours" in body["error"]
    assert _stored(world) is None


def test_a_windows_1252_file_is_refused_with_a_reason_or_kept_intact(world, sent):
    raw = "date,employee,role,actual_hours,sales\n2026-09-14,José,Server,8,1000\n".encode("cp1252")
    body = _upload(world, raw).get_json()
    if body["ok"]:
        assert "José" in _stored(world)
    else:
        assert body["error"] and _stored(world) is None


def test_a_valid_upload_saves_and_is_analysed(world, sent):
    r = _upload(world, HEADER + "2026-09-14,Monday,A,Server,11:00,17:00,6,6,1000,\n")
    assert r.get_json()["ok"] is True
    a = labor.analyse_shifts_for_restaurant(world["rid"])
    assert a["is_live"] is True and a["total_sales"] == 1000.0


# ── A3 #33 / MOD-LAB-17: the overtime alert email ───────────────────────────

def _overtime_week(start):
    return HEADER + "".join(
        f"{(start + timedelta(days=i)).isoformat()},X,Marcus T.,Server,10:00,19:00,9,9,3000,\n" for i in range(5))


def _overtime_emails(sent):
    return [e for e in sent["email"] if "Overtime" in (e.get("subject") or "")]


def test_an_upload_with_a_current_overtime_week_emails_the_owner(world, sent):
    monday = date.today() - timedelta(days=date.today().weekday())
    _upload(world, _overtime_week(monday - timedelta(days=7)))
    assert len(_overtime_emails(sent)) == 1


def test_uploading_the_same_file_twice_sends_one_overtime_email(world, sent):
    monday = date.today() - timedelta(days=date.today().weekday())
    csv_text = _overtime_week(monday - timedelta(days=7))
    _upload(world, csv_text)
    _upload(world, csv_text)
    assert len(_overtime_emails(sent)) == 1


def test_overtime_weeks_long_past_are_not_emailed_as_this_week(world, sent):
    _upload(world, _overtime_week(date(2026, 6, 1)))
    assert _overtime_emails(sent) == []


def test_the_overtime_email_escapes_employee_names(world, sent):
    monday = date.today() - timedelta(days=date.today().weekday())
    csv_text = _overtime_week(monday - timedelta(days=7)).replace("Marcus T.", "<a href=x>Marcus</a>")
    _upload(world, csv_text)
    html = _overtime_emails(sent)[0]["html"]
    assert "<a href=x>" not in html


# ── A3 #34 / MOD-LAB-19: the labor.updated webhook ──────────────────────────

def test_the_labor_updated_webhook_carries_the_labor_percentage(world, sent):
    _upload(world, HEADER + "2026-09-14,Monday,A,Server,11:00,17:00,6,6,1000,\n")
    payloads = [p for e, p in sent["webhook"] if e == "labor.updated"]
    assert payloads, sent["webhook"]
    assert payloads[0].get("labor_pct") is not None
    assert payloads[0].get("total_hours") is not None


def test_the_labor_updated_webhook_fires_once_per_shifts_upload(world, sent):
    _upload(world, HEADER + "2026-09-14,Monday,A,Server,11:00,17:00,6,6,1000,\n")
    assert [e for e, _ in sent["webhook"]].count("labor.updated") == 1
