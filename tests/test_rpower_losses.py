"""Comps and voids from RPOWER, as Simple EJ's actually rings them (RPower
endpoint audit, 9/29/26, Critical #3).

A week of live lines showed: comp lines carry their value in `sales` with
both prices 0 (every comp was recorded as $0.00); voids are "Error Correct"
lines with `voided` 0 (no void was ever counted); and the reason on each line
was a bare id nobody read. voidreason/getbycg now names it.
"""
import json
from datetime import date

import pytest

import loss_detection
import models
import pos
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(loss_detection, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    rpower._people_cache.clear()
    rpower._void_reason_cache.clear()
    yield


def _connected(db_path, rid=1):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid) "
                 "VALUES (?,?,?,?,?,?)", (rid, "Loss Co", "l@x.test", "tok", 1280, "1123"))
    conn.commit()
    conn.close()
    return rid


TYPES = [
    {"mid": "S", "name": "Sale", "impacts_sales": 1, "impacts_costs": 1, "type_sale": 1},
    {"mid": "C", "name": "Comp", "impacts_sales": 1, "impacts_costs": 1, "type_comp": 1},
    {"mid": "E", "name": "Error Correct", "impacts_sales": 0, "impacts_costs": 0, "type_error": 1},
    {"mid": "N", "name": "Nonsale", "impacts_sales": 0, "impacts_costs": 0, "type_nonsale": 1},
]
REASONS = [{"mid": "R1", "name": "Error Made"}, {"mid": "R2", "name": "*Manager Test*"},
           {"mid": "R3", "name": "TRANSFERRED"}, {"mid": "R4", "name": "Unhappy Customer"}]


def _line(day, stype, sales, reason="1", mgr="M1", voidmgr="0", **kw):
    row = {"date": f"{day}T00:00:00", "slstype_mid": stype, "sales": sales, "qty": 1.0, "price": 0.0,
           "regular_price": 0.0, "voided": 0.0, "voidrsn_mid": reason, "mgr_mid": mgr,
           "voidmgr_mid": voidmgr, "shift": 1}
    row.update(kw)
    return row


def _stub(monkeypatch, lines):
    routes = {"salestype/getbycg": TYPES, "voidreason/getbycg": REASONS,
              "ticketsales/getbybusinessdate": lines,
              "job/getbycg": [], "employee/getbycg": [{"mid": "M1", "fname": "Erik", "lname": "Stone"},
                                                       {"mid": "M2", "fname": "Dana", "lname": "Reyes"}]}
    def fake(token, path, params=None):
        if path == "ticketsales/getbybusinessdate":      # RPOWER answers only the dates asked for
            lo, hi = params["startdate"], params["enddate"]
            return [ln for ln in lines if lo <= ln["date"][:10] <= hi]
        return routes[path]
    monkeypatch.setattr(rpower, "_request", fake)


def test_a_comp_is_worth_its_sales_when_rpower_leaves_the_prices_at_zero(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch, [_line("2026-09-28", "C", 5.75, reason="R1")])
    (ln,) = rpower.fetch_loss_lines(rid, date(2026, 9, 28), date(2026, 9, 28))
    assert (ln["kind"], ln["amount"], ln["reason"], ln["approver_name"]) == ("comp", 5.75, "Error Made", "Erik Stone")
    # A real regular price still wins (what the item would have sold for).
    _stub(monkeypatch, [_line("2026-09-28", "C", 2.0, reason="R1", regular_price=8.0, qty=2)])
    rpower._void_reason_cache.clear()
    assert rpower.fetch_loss_lines(rid, date(2026, 9, 28), date(2026, 9, 28))[0]["amount"] == 16.0


def test_an_error_correct_line_is_a_void_and_a_transfer_is_no_loss(db_path, monkeypatch):
    rid = _connected(db_path)
    _stub(monkeypatch, [_line("2026-09-28", "E", 12.0, reason="R2", voidmgr="M2"),
                        _line("2026-09-28", "N", 9.0, reason="R3"),
                        _line("2026-09-28", "S", 20.0)])
    got = rpower.fetch_loss_lines(rid, date(2026, 9, 28), date(2026, 9, 28))
    assert [(g["kind"], g["amount"], g["reason"], g["approver"]) for g in got] == [("void", 12.0, "*Manager Test*", "M2")]


def test_the_week_names_its_reasons_and_the_manager(db_path, monkeypatch):
    rid = _connected(db_path)
    lines = [_line(f"2026-09-{d}", "C", 20.0, reason="R1", mgr="M1") for d in range(22, 29)]
    lines += [_line("2026-09-27", "C", 5.0, reason="R4", mgr="M2")]
    _stub(monkeypatch, lines)
    # sync reads back from yesterday: pinned, or 9/22 fell out of its 14 days on 10/7/26.
    monkeypatch.setattr(loss_detection, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 9, 29))}))
    out = loss_detection.sync(rid, days=14)
    assert out["ok"] and out["provider"] == "rpower"
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT by_reason, by_approver FROM pos_loss_daily WHERE restaurant_id=? AND "
                       "business_date='2026-09-27' AND kind='comp'", (rid,)).fetchone()
    conn.close()
    assert json.loads(row["by_reason"]) == {"Error Made": {"amount": 20.0, "events": 1},
                                            "Unhappy Customer": {"amount": 5.0, "events": 1}}
    assert json.loads(row["by_approver"])["M1"]["name"] == "Erik Stone"
    sig = loss_detection.signals(rid, today=date(2026, 9, 29))
    comp = next(k for k in sig["kinds"] if k["kind"] == "comp")
    assert comp["week_amount"] == 145.0
    assert comp["reasons"][0] == {"reason": "Error Made", "amount": 140.0, "events": 7}
    flag = next(f for f in comp["flags"] if f["type"] == "concentration")
    assert "Erik Stone" in flag["headline"] and "POS id" not in flag["headline"]
    assert flag["key"] == "loss:2026-09-22:comp:M1"          # issue keys stay on the stable id


def test_rows_from_before_the_fix_are_re_read_so_no_false_spike(db_path, monkeypatch):
    """Weeks stored under the old rules (comps $0, no voids) would make this
    week's real voids a spike; the first sync re-reads the whole baseline."""
    rid = _connected(db_path)
    conn = models.get_conn(db_path)
    for d in range(1, 29):                                   # old rows: NULL by_reason, $0
        conn.execute("INSERT INTO pos_loss_daily (restaurant_id, business_date, kind, amount, events, by_approver) "
                     "VALUES (?,?,?,?,?,?)", (rid, f"2026-09-{d:02d}", "void", 0, 0, "{}"))
    conn.commit()
    conn.close()
    lines = [_line(f"2026-{m}-{d:02d}", "E", 30.0, reason="R2", voidmgr="M2")
             for m, rng in (("08", range(1, 32)), ("09", range(1, 29))) for d in rng]
    _stub(monkeypatch, lines)
    monkeypatch.setattr(loss_detection, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 9, 29))}))
    out = loss_detection.sync(rid)
    assert out["days"] == 7 * (loss_detection.BASELINE_WEEKS + 1)
    sig = loss_detection.signals(rid, today=date(2026, 9, 29))
    void = next(k for k in sig["kinds"] if k["kind"] == "void")
    # The baseline now holds the re-read weeks (the fixture starts 8/1, so a
    # few July days in the window are real zeros), not the stored $0 weeks.
    assert void["week_amount"] == 210.0 and void["weekly_baseline"] >= 190.0 and not void["flags"]
    assert loss_detection.sync(rid)["days"] == 14                      # once only
