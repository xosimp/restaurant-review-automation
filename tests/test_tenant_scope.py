"""Tenant isolation has a structural guard (scale audit 10/19/26, SEC-16):
a new `UPDATE/DELETE ... WHERE id=?` on a table with a restaurant_id that
does not name restaurant_id fails here (scripts/check_tenant_scope.py)."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "scripts"))


def test_no_new_unscoped_write_on_a_tenant_table():
    import check_tenant_scope as c
    baseline = json.load(open(c.BASELINE, encoding="utf-8"))
    over = c.compare(c.scan(), baseline)
    assert not over, ("new unscoped writes (add AND restaurant_id=?, or raise "
                      f"scripts/tenant_scope_baseline.json naming the ownership check): {over}")


def test_the_scan_sees_an_unscoped_write_and_passes_a_scoped_one(tmp_path, monkeypatch):
    import check_tenant_scope as c
    src = tmp_path / "x.py"
    src.write_text('conn.execute("UPDATE reviews SET text=? WHERE id=?", (t, i))\n'
                   'conn.execute("UPDATE reviews SET text=? WHERE id=? AND restaurant_id=?", (t, i, r))\n'
                   'conn.execute("DELETE FROM reviews "\n             "WHERE id=?", (i,))\n'
                   'conn.execute("UPDATE app_secrets SET v=? WHERE id=?", (v, i))\n')
    monkeypatch.setattr(c, "ROOT", str(tmp_path))
    monkeypatch.setattr(c, "_sources", lambda: ["x.py"])
    assert c.scan({"reviews"}) == {"x.py::reviews": 2}
    assert c.compare({"x.py::reviews": 2}, {"x.py::reviews": 1}) == [("x.py::reviews", 2, 1)]
    assert c.compare({"x.py::reviews": 1}, {"x.py::reviews": 2}) == []


def test_the_tables_are_read_from_the_code():
    import check_tenant_scope as c
    t = c.tenant_tables()
    assert {"reviews", "guest_contacts", "memberships", "jobs"} <= t and "restaurants" not in t
