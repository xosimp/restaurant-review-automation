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
    over = c.compare(c.scan(), baseline, c.fingerprints())
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


def test_a_swapped_statement_fails_though_the_count_held(tmp_path, monkeypatch):
    """Blind re-audit 10/27/26 B1 F14: the baseline holds each statement's
    fingerprint, so removing one unscoped write and adding another in the
    same file fails — a count alone passed it."""
    import check_tenant_scope as c
    src = tmp_path / "x.py"
    monkeypatch.setattr(c, "ROOT", str(tmp_path))
    monkeypatch.setattr(c, "_sources", lambda: ["x.py"])
    src.write_text('conn.execute("UPDATE reviews SET text=? WHERE id=?", (t, i))\n')
    baseline = {"x.py::reviews": {"n": 1, "stmts": c.fingerprints({"reviews"})["x.py::reviews"], "why": "w"}}
    src.write_text('conn.execute("UPDATE  reviews SET text=?\\n WHERE id=?", (t, i))\n')   # reformatted only
    assert c.compare(c.scan({"reviews"}), baseline, c.fingerprints({"reviews"})) == []
    src.write_text('conn.execute("UPDATE reviews SET deleted=1 WHERE id=?", (i,))\n')         # swapped
    assert c.scan({"reviews"}) == {"x.py::reviews": 1}
    assert c.compare(c.scan({"reviews"}), baseline, c.fingerprints({"reviews"})) == [("x.py::reviews", 1, 1)]
    assert c.compare(c.scan({"reviews"}), baseline) == []          # the count alone would have passed it


def test_every_baseline_entry_holds_a_fingerprint_per_statement():
    import check_tenant_scope as c
    for path in (c.BASELINE, c.READS_BASELINE):
        baseline = json.load(open(path, encoding="utf-8"))
        bad = [k for k, v in baseline.items() if len(v.get("stmts") or []) != int(v.get("n") or 0)]
        assert not bad, f"{os.path.basename(path)}: re-baseline (--update / --update-reads): {bad}"


def test_the_tables_are_read_from_the_code():
    import check_tenant_scope as c
    t = c.tenant_tables()
    assert {"reviews", "guest_contacts", "memberships", "jobs"} <= t and "restaurants" not in t


def test_no_new_unscoped_read_on_a_tenant_table():
    """The reads half (blind re-audit 10/20/26 R7-18): a new
    `SELECT ... FROM <tenant table> ... WHERE id=?` without restaurant_id."""
    import check_tenant_scope as c
    baseline = json.load(open(c.READS_BASELINE, encoding="utf-8"))
    over = c.compare(c.scan_reads(), baseline, c.fingerprints(reads=True))
    assert not over, ("new unscoped reads (add AND restaurant_id=?, or raise "
                      f"scripts/tenant_scope_reads_baseline.json naming the ownership check): {over}")


def test_the_read_scan_sees_an_unscoped_select_and_passes_a_scoped_one(tmp_path, monkeypatch):
    import check_tenant_scope as c
    src = tmp_path / "y.py"
    src.write_text('conn.execute("SELECT text FROM reviews WHERE id=?", (i,))\n'
                   'conn.execute("SELECT text FROM reviews WHERE id=? AND restaurant_id=?", (i, r))\n'
                   'conn.execute("SELECT text "\n             "FROM reviews WHERE id=?", (i,))\n'
                   'conn.execute("DELETE FROM reviews WHERE id=?", (i,))\n')
    monkeypatch.setattr(c, "ROOT", str(tmp_path))
    monkeypatch.setattr(c, "_sources", lambda: ["y.py"])
    assert c.scan_reads({"reviews"}) == {"y.py::reviews": 2}


# ── the widened scan (audit #25 A03-06) ──────────────────────────────────────

def _scan_source(tmp_path, monkeypatch, text, tables=frozenset({"reviews"}), reads=False):
    import check_tenant_scope as c
    src = tmp_path / "w.py"
    src.write_text(text)
    monkeypatch.setattr(c, "ROOT", str(tmp_path))
    monkeypatch.setattr(c, "_sources", lambda: ["w.py"])
    return (c.scan_reads if reads else c.scan)(set(tables))


def test_an_id_anywhere_in_the_where_is_counted(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'conn.execute("UPDATE reviews SET x=? WHERE status=? AND id=?", (x, s, i))\n'
                       'conn.execute("DELETE FROM reviews WHERE status=\'draft\' AND id IN (?, ?)", (a, b))\n')
    assert got == {"w.py::reviews": 2}


def test_an_id_in_list_and_a_qualified_id_are_counted(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'conn.execute("UPDATE reviews SET x=? WHERE id IN (" + marks + ")", ids)\n'
                       'conn.execute("UPDATE reviews r SET x=? WHERE r.id = ?", (x, i))\n'
                       'conn.execute("SELECT * FROM reviews t WHERE t.id IN (?, ?)", (a, b))\n')
    assert got == {"w.py::reviews": 2}
    reads = _scan_source(tmp_path, monkeypatch,
                         'conn.execute("SELECT * FROM reviews t WHERE t.id IN (?, ?)", (a, b))\n', reads=True)
    assert reads == {"w.py::reviews": 1}


def test_an_f_string_with_a_hole_in_the_id_list_is_counted(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'ph = ",".join("?" * len(ids))\n'
                       'conn.execute(f"UPDATE reviews SET x=? WHERE id IN ({ph})", (x, *ids))\n'
                       'conn.execute(f"UPDATE reviews SET {col}=? WHERE id=?", (v, i))\n')
    assert got == {"w.py::reviews": 2}


def test_a_long_triple_quoted_statement_is_read_to_its_end(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'conn.execute("""\n'
                       '    UPDATE reviews\n'
                       '    SET draft_response=?,\n'
                       '        response_status=\'drafted\',\n'
                       '        draft_needs_review=?\n'
                       '    WHERE response_status NOT IN (\'posted\', \'approved\')\n'
                       '      AND id=?\n'
                       '""", (d, n, i))\n')
    assert got == {"w.py::reviews": 1}


def test_a_statement_split_over_many_concatenated_lines_is_one_statement(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'conn.execute("UPDATE reviews SET a=?, "\n'
                       '             "b=?, "\n'
                       '             "c=?, "\n'
                       '             "d=? "\n'
                       '             "WHERE id=? "\n'
                       '             "AND restaurant_id=?", (1, 2, 3, 4, i, r))\n'
                       'conn.execute("UPDATE reviews SET a=?, "\n'
                       '             "b=?, "\n'
                       '             "c=? "\n'
                       '             "WHERE id=?", (1, 2, 3, i))\n')
    assert got == {"w.py::reviews": 1}


def test_scoped_statements_other_columns_and_other_tables_are_not_counted(tmp_path, monkeypatch):
    text = ('conn.execute("UPDATE reviews SET x=? WHERE id=? AND restaurant_id=?", (x, i, r))\n'
            'conn.execute("UPDATE reviews SET x=? WHERE review_id=?", (x, i))\n'
            'conn.execute("UPDATE reviews SET x=? WHERE restaurant_id=?", (x, r))\n'
            'conn.execute("UPDATE reviews SET x=? WHERE google_id=?", (x, g))\n'
            'conn.execute("UPDATE app_secrets SET v=? WHERE id=?", (v, i))\n'
            'conn.execute("SELECT * FROM reviews WHERE id=? AND restaurant_id=?", (i, r))\n'
            'conn.execute("SELECT * FROM reviews r JOIN restaurants x ON x.id = r.restaurant_id WHERE r.id=?", (i,))\n'
            'def f():\n'
            '    """UPDATE reviews SET x=? WHERE id=? — a docstring is not a statement."""\n')
    assert _scan_source(tmp_path, monkeypatch, text) == {}
    assert _scan_source(tmp_path, monkeypatch, text, reads=True) == {}


def test_setting_restaurant_id_is_not_scoping_by_it(tmp_path, monkeypatch):
    got = _scan_source(tmp_path, monkeypatch,
                       'conn.execute("UPDATE reviews SET restaurant_id=? WHERE id=?", (r, i))\n')
    assert got == {"w.py::reviews": 1}


def test_every_baseline_entry_says_which_check_covers_it():
    import check_tenant_scope as c
    for path in (c.BASELINE, c.READS_BASELINE):
        baseline = json.load(open(path, encoding="utf-8"))
        assert all(isinstance(v, dict) and int(v.get("n") or 0) > 0 for v in baseline.values()), path
        assert c.unexplained(baseline) == [], (f"{os.path.basename(path)}: give each entry a 'why' naming "
                                               f"its ownership check: {c.unexplained(baseline)}")


def test_unexplained_and_compare_read_the_reasoned_format():
    import check_tenant_scope as c
    baseline = {"a.py::t": {"n": 2, "why": "checked by _owned()"}, "b.py::t": {"n": 1, "why": "TODO"},
                "c.py::t": {"n": 1, "why": " "}}
    assert c.unexplained(baseline) == ["b.py::t", "c.py::t"]
    assert c.compare({"a.py::t": 3, "d.py::t": 1}, baseline) == [("a.py::t", 3, 2), ("d.py::t", 1, 0)]
    assert c.compare({"a.py::t": 2}, baseline) == []


def test_an_update_keeps_each_reason_and_marks_a_new_key_todo(tmp_path):
    import check_tenant_scope as c
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"a.py::t": {"n": 3, "why": "checked by _owned()"},
                                "gone.py::t": {"n": 1, "why": "removed"}}))
    out = c._write(str(path), {"a.py::t": 2, "new.py::t": 1}, {"a.py::t": ["f1", "f2"], "new.py::t": ["f3"]})
    assert out == {"a.py::t": {"n": 2, "stmts": ["f1", "f2"], "why": "checked by _owned()"},
                   "new.py::t": {"n": 1, "stmts": ["f3"], "why": "TODO"}}
    assert json.loads(path.read_text()) == out
