"""Schedule fix round 10/10/26, workstream Z2 (final integration fixer).

labor_live._record wrote a live cut's answer to the recommendation ledger
with no db_path, so an answer given against an explicit database (a script,
a test, anything not on the default file) landed in the default database -
or nowhere - while the move itself was decided in the right one."""
import labor_live
import models


def _moves(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO live_labor_moves (restaurant_id, business_date, kind, move_key, employee, role, "
            "start_label, end_label, hours, dollars, premium, text, payload_json) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "2026-10-09", "cut", "cut:z2", "Ana", "Server", "6:30pm", "10:00pm", 3.5, 52.5, None,
             "Send Ana home at 6:30pm", '{"date": "2026-10-09", "employee": "Ana", "role": "Server", "kind": "cut"}'))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_a_dismissed_live_cut_is_recorded_in_the_database_it_was_answered_in(db_path):
    rid = models.create_restaurant(models.Restaurant(name="Z2 Co", owner_email="z2@x.test"), db_path=db_path)
    mid = _moves(db_path, rid)
    out = labor_live.dismiss(rid, mid, user={"id": 3, "username": "gm"}, db_path=db_path)
    assert out == {"ok": True, "status": "dismissed"}
    conn = models.get_conn(db_path)
    try:
        ev = conn.execute("SELECT event, key FROM rec_events "
                          "WHERE restaurant_id=? AND key LIKE ?", (rid, "pulse_cut:2026-10-09:%")).fetchall()
    finally:
        conn.close()
    assert [r["event"] for r in ev] == ["dismissed"]
