"""Web desk-first capabilities (parity audit, web desk findings 1-5).

1. One generic sortable-table helper (cavSortTables), opted in per table.
2. Multi-select in the Reviews inbox; a bulk approve goes through
   /api/reviews/approve-all with pinned review_ids, which never posts a
   flagged or urgent draft.
3. A print stylesheet and Print buttons for the daily report, the monthly
   review and the schedule.
4. The header bell acts in place: Undo on a queued send, Approve / Deny on a
   staff request — the rows now carry what they are about (alert_log
   ref_kind / ref_id) and the server's can_* rule.
5. Wide layouts at >= 1440px for Reviews, Labor and Food Cost only.

Template rules are asserted against the source (tests read the template),
since a rendered fixture would only cover its own branches.
"""
import re

import pytest

import auth
import client_api
import models
import notify
import strategy_jobs
from models import Restaurant, create_restaurant, get_conn

SRC = open("templates/dashboard.html", encoding="utf-8").read()
CARD = open("templates/_review_card.html", encoding="utf-8").read()
DS = open("DESIGN_SYSTEM.md", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _restaurant(db_path, name="Desk Co"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _block(start, end):
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


OWNER = {"id": 7, "role": "owner", "is_admin": 0}


def _viewer(rid, **kw):
    v = dict(OWNER, restaurant_id=rid, base_restaurant_id=rid)
    v.update(kw)
    return v


def _delayed(db_path, rid, kind="order_send", status="pending"):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO delayed_actions (restaurant_id, kind, label, payload_json, execute_at, status) "
                       "VALUES (?,?,?,?,datetime('now','+1 hour'),?)", (rid, kind, "Order", "{}", status))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def _time_off(db_path, rid, status="pending"):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                       "VALUES (?,?,date('now','+3 days'),date('now','+4 days'),?)", (rid, "Dana", status))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def _items(rid, viewer):
    payload, status = client_api._do_get_notifications(rid, viewer=viewer)
    assert status == 200 and payload["ok"], payload
    return {i["id"]: i for i in payload["notifications"]}


# ── 4. The bell acts in place ────────────────────────────────────────────────

def test_reach_keeps_what_the_notification_is_about(db_path):
    assert strategy_jobs._notification_ref({"delayed_action_id": 9, "collapse_key": "x"}) == \
        {"ref_kind": "delayed_action", "ref_id": 9}
    assert strategy_jobs._notification_ref({"request_id": 4, "request_kind": "time_off"}) == \
        {"ref_kind": "time_off", "ref_id": 4}
    assert strategy_jobs._notification_ref({"request_id": 5, "request_kind": "shift"}) == \
        {"ref_kind": "shift", "ref_id": 5}
    assert strategy_jobs._notification_ref({"tab": "labor"}) == {}
    assert strategy_jobs._notification_ref(None) == {}
    # _reach passes it to the history row.
    src = open("strategy_jobs.py", encoding="utf-8").read()
    body = src[src.index("def _reach("):src.index("pushed = {u[\"id\"]", src.index("def _reach("))]
    assert "**_notification_ref(data)" in body


def test_a_queued_send_row_offers_undo_only_while_pending_and_permitted(db_path):
    rid = _restaurant(db_path)
    live = _delayed(db_path, rid)
    gone = _delayed(db_path, rid, status="cancelled")
    a_live = notify.record_notification(rid, "order_send_pending", db_path=db_path,
                                        ref_kind="delayed_action", ref_id=live)
    a_gone = notify.record_notification(rid, "order_send_pending", db_path=db_path,
                                        ref_kind="delayed_action", ref_id=gone)
    plain = notify.record_notification(rid, "labor_over", db_path=db_path)
    items = _items(rid, _viewer(rid))
    assert items[a_live]["delayed_action_id"] == live and items[a_live]["can_undo"] is True
    assert items[a_gone]["delayed_action_id"] == gone and items[a_gone]["can_undo"] is False
    assert "can_undo" not in items[plain] and "delayed_action_id" not in items[plain]


def test_undo_follows_the_routes_own_permission(db_path, monkeypatch):
    import strategy_routes
    rid = _restaurant(db_path)
    live = _delayed(db_path, rid)
    aid = notify.record_notification(rid, "order_send_pending", db_path=db_path,
                                     ref_kind="delayed_action", ref_id=live)
    seen = []
    monkeypatch.setattr(strategy_routes, "_may_undo", lambda u, kind: seen.append(kind) or False)
    assert _items(rid, _viewer(rid))[aid]["can_undo"] is False
    assert seen == ["order_send"]


def test_a_staff_request_row_offers_approve_deny_only_to_a_decider(db_path, monkeypatch):
    import strategy_routes
    rid = _restaurant(db_path)
    open_req = _time_off(db_path, rid)
    done_req = _time_off(db_path, rid, status="approved")
    a_open = notify.record_notification(rid, "shift_request", db_path=db_path, ref_kind="time_off", ref_id=open_req)
    a_done = notify.record_notification(rid, "shift_request", db_path=db_path, ref_kind="time_off", ref_id=done_req)
    items = _items(rid, _viewer(rid))
    assert items[a_open]["request_id"] == open_req and items[a_open]["request_kind"] == "time_off"
    assert items[a_open]["can_decide"] is True
    assert items[a_done]["can_decide"] is False
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: False)
    assert _items(rid, _viewer(rid))[a_open]["can_decide"] is False


def test_another_locations_request_is_never_decided_from_here(db_path):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    req = _time_off(db_path, other)
    # A row at this location naming another location's request id.
    aid = notify.record_notification(rid, "shift_request", db_path=db_path, ref_kind="time_off", ref_id=req)
    assert _items(rid, _viewer(rid))[aid]["can_decide"] is False


def test_a_shift_request_row_reads_shift_change_requests(db_path):
    models.init_shift_requests(db_path=db_path)
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id) VALUES (?)", (rid,)).lastrowid
    req = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start) "
                       "VALUES (?,?,?,date('now','+2 days'),'5:00pm')", (rid, hid, "Dana")).lastrowid
    conn.commit()
    conn.close()
    aid = notify.record_notification(rid, "shift_request", db_path=db_path, ref_kind="shift", ref_id=req)
    item = _items(rid, _viewer(rid))[aid]
    assert item["request_kind"] == "shift" and item["can_decide"] is True


def test_the_bell_calls_the_same_routes_the_phone_does():
    bell = _block("  function renderList() {", "  // A row is read when it is opened")
    js = _block("  // Act from the bell (web desk #4)", "  // A row is read when it is opened")
    # Buttons only where the server said the login may act.
    assert "n.can_undo" in bell and "n.can_decide" in bell
    assert "'/api/actions/' + encodeURIComponent(n.delayed_action_id) + '/cancel'" in js
    assert "'/api/labor/time-off/'" in js and "'/api/labor/shift-requests/'" in js
    assert "'/decide'" in js and "decision: decision" in js
    # Irreversible: each one asks first.
    assert js.count("window.confirm(") >= 2
    # The row updates in place.
    assert "renderList()" in js


def test_the_bell_delegates_the_new_actions():
    handler = _block("  document.addEventListener('click', function(e) {\n    if (!e.target || !e.target.closest) return;\n    var filter",
                     "  window._openHdrPanel")
    assert "data-notif-undo" in handler and "data-notif-decide" in handler


# ── 1. Sortable tables ───────────────────────────────────────────────────────

def _sort_js():
    return _block("/* ── Sortable tables (web desk #1)", "/* ── end sortable tables */")


def test_the_sort_helper_is_accessible_and_generic():
    js = _sort_js()
    assert "aria-sort" in js and "'ascending'" in js and "'descending'" in js
    assert "table[data-sortable]" in js
    # Keyboard: a real button in each header, so Enter and Space work.
    assert "<button" in js or "createElement('button')" in js
    # Money, percent and a dash that sorts last.
    assert "\\u2014" in js or "—" in js
    assert "MutationObserver" in js and "takeRecords" in js
    # Stable: ties keep their original order.
    assert "_csortIdx" in js


def test_the_sort_helper_is_es5():
    js = _sort_js()
    for bad in (r"\blet\s", r"\bconst\s", "=>", "`"):
        assert not re.search(bad, js), bad


@pytest.mark.parametrize("marker", [
    '<table class="hb-tbl" data-sortable="group-locations"',          # Group Home
    '<table class="dr-grid" data-sortable="dsr-week"',                  # DSR week
    '<table id="sched-table" data-sortable="schedule"',                # schedule
    'data-sortable="menu-margins"',                                     # menu margins
    'data-sortable="dish-scorecard"',                                   # dish scorecard
])
def test_the_tables_an_owner_sorts_opt_in(marker):
    assert marker in SRC


def test_the_sort_chevron_uses_tokens():
    css = _block("/* Sortable tables (web desk #1) */", "/* end sortable tables css */")
    assert "var(--ember)" in css and "var(--ink3)" in css
    assert not re.search(r"color:\s*#", css)


# ── 2. Bulk select in the Reviews inbox ─────────────────────────────────────

def test_each_actionable_card_has_a_select_box():
    assert 'class="rv2-sel-cb"' in CARD and 'data-rv-sel="{{ r.id }}"' in CARD
    # Not on a reply that already went out or was approved.
    i = CARD.index('class="rv2-sel-cb"')
    guard = CARD[CARD.rfind("{%", 0, i):i]
    assert "posted" in guard and "approved" in guard


def test_bulk_approve_never_includes_a_flagged_or_urgent_draft():
    js = _block("/* ── Reviews multi-select (web desk #2)", "/* ── end reviews multi-select */")
    elig = js[js.index("function rvSelApprovable("):js.index("\n}", js.index("function rvSelApprovable("))]
    assert ".draft-flag" in elig and "urgent" in elig and "'drafted'" in elig
    # The server's own bulk path, pinned to the ids — which re-checks
    # BULK_PUBLISHABLE_SQL (not flagged, not urgent) on its side.
    assert "/api/reviews/approve-all" in js and "review_ids:" in js
    assert "window.confirm(" in js
    # Skip loops the single route; shift-click selects a range.
    assert "'/skip/'" in js and "shiftKey" in js
    assert "toast(" in js


def test_pinned_approve_all_skips_flagged_drafts(db_path, monkeypatch):
    rid = _restaurant(db_path)

    def _rv(flagged=0, urgency="normal"):
        import uuid
        conn = get_conn(db_path)
        cur = conn.execute(
            "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
            "processed, urgency, draft_response, response_status, draft_needs_review) "
            "VALUES (?,?,?,?,?,?,date('now'),datetime('now'),1,?,?,?,?)",
            (rid, "yelp", uuid.uuid4().hex, "Dana", 4, "Nice.", urgency, "Thanks, Dana.", "drafted", flagged))
        conn.commit()
        i = cur.lastrowid
        conn.close()
        return i

    ok, flagged, urgent = _rv(), _rv(flagged=1), _rv(urgency="high")
    approved = []
    monkeypatch.setattr(client_api, "_do_approve",
                        lambda i, r, g=None, bulk=False: (approved.append(i) or ({"ok": True}, 200)))
    import drafter
    monkeypatch.setattr(drafter, "check_reply", lambda text, *a, **k: (None, text))
    payload, status = client_api._do_approve_all(rid, 25, review_ids=[ok, flagged, urgent])
    assert status == 200 and approved == [ok] and payload["approved"] == 1


# ── 3. Print ────────────────────────────────────────────────────────────────

def _print_css():
    return _block("/* ── Print (web desk #3)", "/* ── end print */")


def test_a_print_stylesheet_hides_the_chrome():
    css = _print_css()
    assert "@media print" in css
    for chrome in (".hdr", ".tabs", "#toast", ".cbtn", "#ask-cavnar-fab", "#notif-panel", ".cav-print-hide"):
        assert chrome in css, chrome
    assert "display:none" in css.replace(" ", "")
    # Light paper, ink text, from its own tokens.
    assert "--paper:" in css and "--ink:" in css and "background:var(--paper)" in css
    assert "break-inside:avoid" in css and "print-color-adjust:exact" in css


def test_print_buttons_where_an_owner_prints():
    js = _block("/* ── Print a section (web desk #3)", "/* ── end print a section */")
    assert "window.print()" in js and "afterprint" in js
    buttons = re.findall(r'data-print="([a-z-]+)"', SRC)
    for target in ("dr-root", "hb-month", "schedule-preview-panel", "sched-history-detail"):
        assert target in buttons, target
    # Every Print button is a .cbtn and says the PDF is in the print dialog.
    for m in re.finditer(r"<button[^>]*data-print=[^>]*>", SRC):
        assert "cbtn" in m.group(0) and "PDF" in m.group(0), m.group(0)


# ── 5. Wide layouts ─────────────────────────────────────────────────────────

def test_wide_screens_widen_only_the_dense_panels():
    css = _block("/* ── Wide screens (web desk #5)", "/* ── end wide screens */")
    assert "@media (min-width:1440px)" in css
    assert "#panel-labor" in css and "#panel-inventory" in css
    # Reviews stays one column: Trends opens below the inbox (owner, 9/26/26).
    assert "#panel-reviews" not in css and "grid-template-columns" not in css
    # Home's column is spec'd (DS §11b) and stays as it is.
    assert "#panel-home" not in css and "hb-root" not in css


def test_design_system_documents_the_new_patterns():
    for needle in ("data-sortable", "aria-sort", "@media print", "data-print", "1440px", "rv2-sel"):
        assert needle in DS, needle
