"""Fix round for the Food Cost + Bar & Alcohol audit of 10/16/26, workstream
C2: invoice intake, scanned invoices, security and data-safety.

Findings (registers in docs/audits/2026-10-16-food-cost-bar-audit.md):
ING-05 / SEC-02 (forged unsigned mail), ING-09 / AI-10 (scanned-invoice
matching and auto-apply), ING-11 (same-number credit memo), ING-14
(near-duplicates), ING-18 (the invoice-number key), BAR-CALC-11 (the keg
deposit), SEC-01 / SCALE-05 (the food tracker's blob), SEC-03 (admin
inventory writes), SEC-04 (log-activity's tab), SCALE-11 (degraded paths).
"""
import json

import pytest

import email_intake
import fintech_invoices as fi
import job_queue
import models

HEAD = ("Vendor Name,Invoice Number,Invoice Date,Invoice Total,Location ID,Product Number,Product Description,"
        "Unit Of Measure,Item Quantity,Item Cost,Extended Price\n")
ADJ_HEAD = ("Vendor Name,Invoice Number,Invoice Date,Invoice Total,Location ID,Product Number,Product Description,"
            "Unit Of Measure,Item Quantity,Item Cost,Item Adjustment,Extended Price\n")


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None: real(p or db_path))
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return db_path


def _rid(db_path, name="EJ's"):
    return models.create_restaurant(models.Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _row(dist, num, day, total, code, desc, uom, qty, price, ext, loc="000001"):
    return f"{dist},{num},{day},{total},{loc},{code},{desc},{uom},{qty},{price},{ext}\n"


def _real(db, rid):
    body = HEAD + _row("Breakthru Beverage Illinois LLC", "233150629", "10/01/2026", "420.00", "9031",
                       "TITOS VDK 1L", "CA", 2, "210.00", "420.00")
    assert fi.ingest_file(rid, "real.csv", body.encode(), db_path=db)["status"] == "stored"


# ── ING-05 / SEC-02: forged unsigned mail ─────────────────────────────────

@pytest.mark.parametrize("auth,unsigned", [
    ({"spf": "none", "dkim": "none", "dmarc": "fail"}, False),
    ({"spf": "gray", "dkim": "gray", "dmarc": "fail"}, False),
    ({"spf": "fail", "dkim": "gray", "dmarc": "gray"}, False),
    ({"spf": "gray", "dkim": "gray", "dmarc": "gray"}, True),
    ({"spf": "none", "dkim": "none", "dmarc": "none"}, True),
])
def test_a_dmarc_fail_is_a_forgery_verdict(auth, unsigned):
    assert email_intake._unsigned_not_forged(auth) is unsigned


def test_a_file_from_a_known_distributor_and_store_vouches(db):
    rid = _rid(db)
    _real(db, rid)
    body = HEAD + _row("Breakthru Beverage Illinois LLC", "128850777", "10/08/2026", "440.00", "9031",
                       "TITOS VDK 1L", "CA", 2, "220.00", "440.00")
    ok, why = fi.vouch(rid, "next.csv", body.encode(), db_path=db)
    assert ok is True, why


def test_an_invoice_naming_no_store_never_vouches(db):
    """A file mixing a store-less invoice with a real one: the store-less
    one would be routed to the route's own restaurant."""
    rid = _rid(db)
    _real(db, rid)
    body = HEAD + _row("Breakthru Beverage Illinois LLC", "128850777", "10/08/2026", "440.00", "9031",
                       "TITOS VDK 1L", "CA", 2, "220.00", "440.00") \
        + _row("Breakthru Beverage Illinois LLC", "128850778", "10/08/2026", "220.00", "9031",
               "TITOS VDK 1L", "CA", 1, "220.00", "220.00", loc="")
    ok, why = fi.vouch(rid, "mixed.csv", body.encode(), db_path=db)
    assert ok is False and "no store" in why


def test_a_new_column_set_never_vouches(db):
    """Fintech's columns plus one: not the layout this restaurant receives."""
    rid = _rid(db)
    _real(db, rid)
    head = HEAD.replace("Extended Price", "Extended Price,PO Number")
    body = head + _row("Breakthru Beverage Illinois LLC", "128850777", "10/08/2026", "440.00", "9031",
                       "TITOS VDK 1L", "CA", 2, "220.00", "440.00").rstrip("\n") + ",PO1\n"
    ok, why = fi.vouch(rid, "other.csv", body.encode(), db_path=db)
    assert ok is False and "layout" in why


@pytest.mark.parametrize("qty,price,ext,total,word", [
    (40, "245.00", "9800.00", "9800.00", "largest"),      # 23x the largest Breakthru invoice
    (1, "1000.00", "1000.00", "1000.00", "outside"),       # ~5x the item's price on file, under 3x the total
])
def test_a_vouched_file_outside_the_distributors_history_is_held(db, qty, price, ext, total, word):
    rid = _rid(db)
    _real(db, rid)
    body = HEAD + _row("Breakthru Beverage Illinois LLC", "999000111", "10/14/2026", total, "9031",
                       "TITOS VDK 1L", "CA", qty, price, ext)
    ok, why = fi.vouch(rid, "big.csv", body.encode(), db_path=db)
    assert ok is False and word in why, why


def test_a_vouched_file_never_changes_an_invoice_on_file(db):
    rid = _rid(db)
    _real(db, rid)
    body = HEAD + _row("Breakthru Beverage Illinois LLC", "233150629", "10/01/2026", "440.00", "9031",
                       "TITOS VDK 1L", "CA", 2, "220.00", "440.00")
    ok, why = fi.vouch(rid, "again.csv", body.encode(), db_path=db)
    assert ok is False and "already on file" in why


def test_mail_read_on_its_files_word_says_so_on_the_admin_card(db, monkeypatch):
    rid = _rid(db)
    _real(db, rid)
    route = "fintech-c2c2c2c2c2@intake.resend.app"
    conn = models.get_conn(db)
    conn.execute("INSERT INTO email_intake_routes (address, restaurant_id, kind, active, created_at, allowed_senders) "
                 "VALUES (?,?, 'fintech', 1, '2026-10-14 12:00:00', 'processor@fintech.net')", (route, rid))
    conn.commit()
    conn.close()
    body = (HEAD + _row("Breakthru Beverage Illinois LLC", "128850777", "10/08/2026", "440.00", "9031",
                        "TITOS VDK 1L", "CA", 2, "220.00", "440.00")).encode()
    email_intake.receive({"email_id": "v1", "to": [route], "from": "processor@fintech.net"}, db_path=db)

    def api_get(path):
        if path.endswith("/attachments"):
            return {"data": [{"id": "a", "filename": "d.csv", "download_url": "u", "size": len(body)}]}
        return {"from": "processor@fintech.net", "authentication": {"spf": "gray", "dkim": "gray", "dmarc": "gray"}}
    monkeypatch.setattr(email_intake, "_api_get", api_get)
    monkeypatch.setattr(email_intake, "_download", lambda url, limit=None: body)
    out = email_intake.process("v1", db_path=db)
    assert out["status"] == "done" and out["files"][0]["vouched"] is True and out["files"][0]["vouch"]
    card = email_intake.admin_view(rid, db_path=db)
    assert [e["vouched"] for e in card["emails"]] == [True]


# ── ING-11: a same-number credit memo ─────────────────────────────────────

def test_a_credit_memo_on_its_invoices_number_is_its_own_document_and_reads_again_unchanged(db):
    rid = _rid(db)
    inv = HEAD + _row("Southern Glazers Wine and Spirits of IL", "4471", "10/01/2026", "600.00", "55",
                      "JAMESON 1L", "CA", 2, "300.00", "600.00")
    cm = HEAD + _row("Southern Glazers Wine and Spirits of IL", "4471", "10/03/2026", "-300.00", "55",
                     "JAMESON 1L", "CA", -1, "300.00", "-300.00")
    fi.ingest_file(rid, "inv.csv", inv.encode(), db_path=db)
    res = fi.ingest_file(rid, "cm.csv", cm.encode(), db_path=db)
    assert res["status"] == "stored" and not res["conflicts"]
    rows = _rows(db, "SELECT invoice_number, number_key, total FROM fintech_invoices ORDER BY id")
    assert [r["total"] for r in rows] == [600.0, -300.0]
    assert rows[1]["invoice_number"] == "4471 credit 10/3/26" and rows[1]["number_key"] == "4471|CR|2026-10-03"
    # Read again: the same two documents, nothing new.
    again = fi.reread_all(rid, apply=True, db_path=db)
    assert again["after"]["invoices"] == 2
    # Purging the credit's file takes the credit out, never the invoice.
    fid = res["file_id"]
    assert fi.purge_file(rid, fid, db_path=db)["deleted"] == 1
    assert [r["total"] for r in _rows(db, "SELECT total FROM fintech_invoices")] == [600.0]


def test_a_same_sign_copy_with_another_total_is_still_a_conflict(db):
    rid = _rid(db)
    a = HEAD + _row("Southern Glazers Wine and Spirits of IL", "4471", "10/01/2026", "600.00", "55",
                    "JAMESON 1L", "CA", 2, "300.00", "600.00")
    b = HEAD + _row("Southern Glazers Wine and Spirits of IL", "4471", "10/01/2026", "900.00", "55",
                    "JAMESON 1L", "CA", 3, "300.00", "900.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    assert fi.ingest_file(rid, "b.csv", b.encode(), db_path=db)["status"] == "conflict"


# ── ING-14: near-duplicates are kept, never merged ────────────────────────

def test_a_suffix_number_from_the_same_distributor_is_kept_beside_the_other(db):
    """Lakeshore 112 ($150, Miller Lite) and Lakeshore 12 ($150, Coors
    Light), same day: two invoices - '1' is not a prefix pattern."""
    rid = _rid(db)
    a = HEAD + _row("Lakeshore Beverage", "112", "10/01/2026", "150.00", "1", "MILLER LITE 1/2BBL", "KE", 1,
                    "150.00", "150.00")
    b = HEAD + _row("Lakeshore Beverage", "12", "10/01/2026", "150.00", "2", "COORS LIGHT 1/2BBL", "KE", 1,
                    "150.00", "150.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    res = fi.ingest_file(rid, "b.csv", b.encode(), db_path=db)
    assert res["status"] == "stored" and not res["near_duplicates"]
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 2
    assert len(_rows(db, "SELECT id FROM fintech_invoice_lines")) == 2


def test_the_same_number_from_another_distributor_is_another_invoice(db):
    rid = _rid(db)
    a = HEAD + _row("Lakeshore Beverage", "5001", "10/01/2026", "150.00", "1", "MILLER LITE", "CA", 1,
                    "150.00", "150.00")
    b = HEAD + _row("Winebow", "5001", "10/01/2026", "150.00", "2", "MALBEC 750", "CA", 1, "150.00", "150.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    fi.ingest_file(rid, "b.csv", b.encode(), db_path=db)
    assert sorted(r["distributor"] for r in _rows(db, "SELECT distributor FROM fintech_invoices")) == \
        ["Lakeshore Beverage", "Winebow"]


def test_a_letter_prefix_variant_is_kept_and_flagged_never_replaced(db):
    rid = _rid(db)
    a = HEAD + _row("Lakeshore Beverage", "12345", "10/01/2026", "150.00", "1", "MILLER LITE", "CA", 1,
                    "150.00", "150.00")
    b = HEAD + _row("Lakeshore Beverage", "INV12345", "10/01/2026", "150.00", "1", "MILLER LITE", "CA", 1,
                    "150.00", "150.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    res = fi.ingest_file(rid, "b.csv", b.encode(), db_path=db)
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 2
    assert res["near_duplicates"] == ["INV12345 (kept beside #12345)"]


# ── ING-18: separators are significant ────────────────────────────────────

def test_the_invoice_key_keeps_separators():
    assert fi.invoice_key("1-2") != fi.invoice_key("12")
    assert fi.invoice_key("0012") == fi.invoice_key("12") == "12"
    assert fi.invoice_key(" 0233150629 ") == fi.invoice_key("233150629")
    assert fi.loose_invoice_key("1-2") == fi.loose_invoice_key("12")


def test_two_numbers_that_differ_by_a_separator_are_two_invoices(db):
    rid = _rid(db)
    a = HEAD + _row("Lakeshore Beverage", "1-2", "10/01/2026", "150.00", "1", "MILLER LITE", "CA", 1,
                    "150.00", "150.00")
    b = HEAD + _row("Lakeshore Beverage", "12", "10/02/2026", "90.00", "1", "MILLER LITE", "CA", 1,
                    "90.00", "90.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    fi.ingest_file(rid, "b.csv", b.encode(), db_path=db)
    assert len(_rows(db, "SELECT id FROM fintech_invoices")) == 2


def test_number_keys_written_before_are_recomputed_at_boot(db):
    rid = _rid(db)
    a = HEAD + _row("Lakeshore Beverage", "1-2", "10/01/2026", "150.00", "1", "MILLER LITE", "CA", 1,
                    "150.00", "150.00")
    fi.ingest_file(rid, "a.csv", a.encode(), db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE fintech_invoices SET number_key='12'")       # the old key
    conn.commit()
    conn.close()
    fi.init_fintech_invoices(db)
    assert _rows(db, "SELECT number_key FROM fintech_invoices") == [{"number_key": "1-2"}]
    # The same file again still finds it.
    b = a.replace("150.00", "150.00")
    assert fi.ingest_file(rid, "a2.csv", (b + "\n").encode(), db_path=db)["status"] == "unchanged"


def test_back_office_keeps_the_loose_key_its_rows_were_written_with():
    import backoffice_ap
    assert backoffice_ap.invoice_key("1-2") == "12"


# ── BAR-CALC-11: the keg deposit is the distributor's own ────────────────

def test_a_distributors_deposit_is_learned_from_its_empty_lines(db):
    """$50 a half barrel: two kegs carry $100 of deposit inside their cost,
    and the empties come back at $50 each."""
    rid = _rid(db)
    first = HEAD + _row("Lakeshore Beverage", "700", "09/01/2026", "-100.00", "9", "EMPTY KEG 1/2", "DS", -2,
                        "50.00", "-100.00")
    fi.ingest_file(rid, "empties.csv", first.encode(), db_path=db)
    body = ADJ_HEAD + ("Lakeshore Beverage,701,10/01/2026,500.00,000001,1,MILLER LITE 1/2BBL,KE,2,200.00,100.00,"
                       "500.00\n")
    fi.ingest_file(rid, "kegs.csv", body.encode(), db_path=db)
    ln = _rows(db, "SELECT deposit, adjustment FROM fintech_invoice_lines WHERE description LIKE 'MILLER%'")[0]
    assert ln == {"deposit": 100.0, "adjustment": None}


def test_a_uniform_per_keg_step_is_a_deposit_without_empties(db):
    rid = _rid(db)
    body = ADJ_HEAD + ("Lakeshore Beverage,702,10/01/2026,450.00,000001,1,ALLAGASH WHITE 1/6BL,KE,2,200.00,"
                       "50.00,450.00\n")
    fi.ingest_file(rid, "kegs.csv", body.encode(), db_path=db)
    ln = _rows(db, "SELECT deposit, adjustment FROM fintech_invoice_lines")[0]
    assert ln == {"deposit": 50.0, "adjustment": None}


def test_the_illinois_deposit_is_still_the_default():
    ln = fi._settle(fi._line("1", "MILLER LITE 1/2BBL", 2, 200.0, 460.0))
    assert ln["deposit"] == 60.0 and ln["adjustment"] is None
    assert fi._learned_deposit([]) is None


@pytest.mark.parametrize("desc", ["ALLAGASH WHITE 1/6BL", "MILLER LITE 1/2 BL", "HALF ACRE 1/4BBL"])
def test_bl_kegs_are_kegs(desc):
    assert fi._is_keg({"description": desc, "uom": "CA"})


# ── ING-09 / AI-10: scanned-invoice matching and the unattended apply ─────

def _ingredient(db, rid, name, unit, cost):
    conn = models.get_conn(db)
    try:
        iid = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, case_size, is_active) "
                           "VALUES (?,?,?,?,1,1)", (rid, name, unit, cost)).lastrowid
        conn.commit()
        return iid
    finally:
        conn.close()


def _line(desc, price, unit="qt", qty=1):
    return {"description": desc, "quantity": qty, "unit": unit, "unit_price": price,
            "line_total": round(qty * price, 2)}


@pytest.mark.parametrize("ingredient,desc", [
    ("Cream", "SOUR CREAM 1QT"), ("Cream", "ICE CREAM VANILLA"), ("Butter", "PEANUT BUTTER"),
    ("Lemon", "LEMON JUICE"),
])
def test_a_one_word_ingredient_inside_another_product_is_offered_never_preselected(db, ingredient, desc):
    import invoices
    rid = _rid(db)
    _ingredient(db, rid, ingredient, "qt", 3.10)
    line = invoices.propose(rid, {"supplier": "Sysco", "lines": [_line(desc, 4.05)]}, db_path=db)["lines"][0]
    assert line["ingredient_name"] == ingredient and line["partial"] is True
    assert line["selected"] is False and line["verified"] is False and line["note"]


def test_a_descriptor_beside_a_one_word_ingredient_is_still_that_ingredient(db):
    import invoices
    rid = _rid(db)
    _ingredient(db, rid, "Butter", "qt", 3.10)
    line = invoices.propose(rid, {"supplier": "Sysco", "lines": [_line("BUTTER SALTED 36/1LB", 3.40)]},
                            db_path=db)["lines"][0]
    assert line["partial"] is False and line["selected"] is True and line["verified"] is True


def test_a_price_far_from_the_ingredients_own_history_is_not_preselected(db):
    """+29% (inside the 40% big-change guard) on an ingredient whose price
    has sat at $3.00-$3.10 on every applied invoice."""
    import invoices
    rid = _rid(db)
    iid = _ingredient(db, rid, "Heavy Cream", "qt", 3.05)
    conn = models.get_conn(db)
    for i, (old, new) in enumerate(((3.00, 3.05), (3.05, 3.10), (3.10, 3.00), (3.00, 3.05))):
        conn.execute("INSERT INTO invoice_imports (restaurant_id, supplier, image_sha, lines_json, applied_json, "
                     "applied_at) VALUES (?,?,?,?,?,datetime('now'))",
                     (rid, "Sysco", f"h{i}", "{}", json.dumps([{"index": 0, "ingredient_id": iid, "old_cost": old,
                                                                 "new_cost": new, "by": "owner"}])))
    conn.commit()
    conn.close()
    line = invoices.propose(rid, {"supplier": "Sysco", "lines": [_line("HEAVY CREAM 36% 1QT", 3.95)]},
                            db_path=db)["lines"][0]
    assert line["price_anomaly"]["median"] == 3.05
    assert line["selected"] is False and line["verified"] is False and "usually $3.05" in line["note"]
    # A price inside its usual band is still preselected.
    ok = invoices.propose(rid, {"supplier": "Sysco", "lines": [_line("HEAVY CREAM 36% 1QT", 3.10)]},
                          db_path=db)["lines"][0]
    assert ok["selected"] is True and "price_anomaly" not in ok


def test_price_anomaly_needs_a_history():
    import invoices
    assert invoices.price_anomaly(9.0, [3.0, 3.1]) is None
    assert invoices.price_anomaly(3.2, [3.0, 3.0, 3.0]) is None          # a 7% move on a flat price
    assert invoices.price_anomaly(4.0, [3.0, 3.0, 3.0]) is not None


def test_a_trusted_supplier_never_auto_applies_a_name_match(db, monkeypatch):
    """CHICKEN BREAST BREADED FZN matched "Chicken Breast" by its words: a
    different product, 20-35% dearer. It waits for the owner; the same line
    on the owner's own alias is applied."""
    import invoices
    import ordering
    rid = _rid(db)
    iid = _ingredient(db, rid, "Chicken Breast", "lb", 3.00)
    monkeypatch.setattr(ordering, "invoice_trust",
                        lambda *a, **k: {"trusted": True, "applied": 3, "full_accepts": 3})
    prop = invoices.propose(rid, {"supplier": "Sysco", "lines": [_line("CHICKEN BREAST BREADED FZN", 3.60, "lb")]},
                            db_path=db)
    assert prop["lines"][0]["matched_by"] == "name" and prop["lines"][0]["verified"] is True
    conn = models.get_conn(db)
    prop["id"] = conn.execute("INSERT INTO invoice_imports (restaurant_id, supplier, image_sha, lines_json) "
                              "VALUES (?,?,?,?)", (rid, "Sysco", "x1", json.dumps(prop))).lastrowid
    conn.commit()
    conn.close()
    assert ordering.auto_apply_if_trusted(rid, prop, db_path=db)["auto_applied"] == []
    prop["lines"][0]["matched_by"] = "your_match"
    applied = ordering.auto_apply_if_trusted(rid, prop, db_path=db)["auto_applied"]
    assert [a["ingredient_id"] for a in applied] == [iid]


# ── SEC-01 / SCALE-05: the food tracker's blob ────────────────────────────

def _blob(db, rid):
    rows = _rows(db, "SELECT food_cost_json FROM client_data WHERE restaurant_id=?", (rid,))
    return json.loads(rows[0]["food_cost_json"]) if rows and rows[0]["food_cost_json"] else {}


def test_a_custom_item_is_bounded(db, monkeypatch):
    import client_api
    monkeypatch.setattr(client_api, "FOOD_TRACKER_WRITES_PER_MINUTE", 10000)
    rid = _rid(db)
    assert client_api._do_save_food_cost_custom_item(rid, "x" * 10000, "lb")[1] == 400
    assert client_api._do_save_food_cost_custom_item(rid, "Saffron", "u" * 21)[1] == 400
    for i in range(client_api.FOOD_TRACKER_CUSTOM_MAX):
        assert client_api._do_save_food_cost_custom_item(rid, f"Item {i}", "lb")[1] == 200
    payload, status = client_api._do_save_food_cost_custom_item(rid, "One more", "lb")
    assert status == 400 and "200" in payload["error"]
    assert len(_blob(db, rid)["custom_items"]) == client_api.FOOD_TRACKER_CUSTOM_MAX
    # The same name again is not a new item, and a delete makes room.
    assert client_api._do_save_food_cost_custom_item(rid, "item 3", "lb")[1] == 200
    assert client_api._do_delete_food_cost_custom_item(rid, "Item 3")[1] == 200
    assert client_api._do_save_food_cost_custom_item(rid, "One more", "lb")[1] == 200


def test_a_quick_count_row_past_the_caps_is_named_back_never_stored(db):
    import client_api
    rid = _rid(db)
    payload, status = client_api._do_food_cost_quickcount(rid, [
        {"name": "Chicken Breast", "unit": "lb", "price": 3.1, "usage": 40},
        {"name": "y" * 81, "unit": "lb", "price": 2.0},
        {"name": "Shrimp", "unit": "z" * 21, "price": 9.0}])
    assert status == 200 and len(payload["rejected"]) == 2
    assert [i["name"] for i in _blob(db, rid)["current"]["items"]] == ["Chicken Breast"]
    assert client_api._do_food_cost_quickcount(rid, [{"name": "A", "price": 1}] * 201)[1] == 400


def test_the_tracker_writes_are_rate_limited_per_restaurant(db, monkeypatch):
    import client_api
    rid = _rid(db)
    monkeypatch.setattr(client_api, "FOOD_TRACKER_WRITES_PER_MINUTE", 2)
    assert client_api._do_save_food_cost_custom_item(rid, "A", "lb")[1] == 200
    assert client_api._do_save_food_cost_custom_item(rid, "B", "lb")[1] == 200
    assert client_api._do_save_food_cost_custom_item(rid, "C", "lb")[1] == 429
    other = _rid(db, "Other")
    assert client_api._do_save_food_cost_custom_item(other, "C", "lb")[1] == 200


def test_a_quick_count_and_a_custom_item_in_the_same_moment_both_land(db):
    """The phone's quick count lands while the web's custom-item add is
    between its read and its write: both edits are on file after."""
    import threading
    import time
    import client_api
    rid = _rid(db)
    client_api._do_save_food_cost_custom_item(rid, "Saffron", "oz")
    read, errors = threading.Event(), []

    def slow_add(fc):
        read.set()
        time.sleep(0.4)            # the quick count arrives now
        fc.setdefault("custom_items", []).append({"name": "Truffle", "unit": "oz"})
        return "added", True

    def web():
        try:
            client_api._mutate_food_cost_blob(rid, slow_add)
        except Exception as e:     # pragma: no cover - reported below
            errors.append(e)
        finally:
            models.close_thread_connections()
    t = threading.Thread(target=web)
    t.start()
    assert read.wait(5)
    payload, status = client_api._do_food_cost_quickcount(rid, [{"name": "Butter", "unit": "lb", "price": 4.0}])
    t.join(10)
    assert not errors and status == 200, (errors, payload)
    fc = _blob(db, rid)
    assert [c["name"] for c in fc["custom_items"]] == ["Saffron", "Truffle"]
    assert [i["name"] for i in fc["current"]["items"]] == ["Butter"]


# ── SEC-03: the admin inventory writes ────────────────────────────────────

def test_every_admin_inventory_write_audits_itself_and_the_pos_ones_run_on_the_pool():
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin_routes.py"),
               encoding="utf-8").read()
    routes = re.findall(r'@admin_bp\.route\("(/admin/inventory/[^"]+)", methods=\["POST"\]\)\n@admin_required\n'
                        r'def (\w+)\(', src)
    assert len(routes) == 11, routes
    for path, fn in routes:
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n@admin_bp.route(")]
        assert "_inventory_audit(" in body or "_inventory_job(" in body, path
    for fn in ("discover_menu_items_route", "resync_depletion_route"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n@admin_bp.route(")]
        assert "_inventory_job(" in body and "jsonify(ok=True, **inventory_ledger" not in body, fn
    helper = src[src.index("def _inventory_audit("):]
    helper = helper[:helper.index("\ndef ")]
    assert "admin_events.record_admin_action(" in helper


@pytest.fixture
def admin_client(db_path, monkeypatch):
    import sys
    from flask import Flask
    import admin_routes
    import auth
    from auth import create_session, create_user, init_auth
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "c2"
    app.register_blueprint(admin_routes.admin_bp)
    hq = models.create_restaurant(models.Restaurant(name="Cavnar AI Admin", owner_email="w@c.test"), db_path=db_path)
    uid = create_user(hq, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", "c2-csrf")
    return c


def _post(c, url, body=None):
    return c.post(url, json=body if body is not None else {}, headers={"X-CSRF": "c2-csrf"})


def test_an_ingredient_change_lands_in_the_typed_audit(admin_client, db_path):
    rid = _rid(db_path, "Client Grill")
    r = _post(admin_client, f"/admin/inventory/ingredients/{rid}", {"name": "Romaine", "unit": "case",
                                                                  "unit_cost": 20})
    iid = r.get_json()["id"]
    assert _post(admin_client, f"/admin/inventory/ingredients/{rid}/{iid}", {"unit_cost": 24}).get_json()["ok"]
    rows = _rows(db_path, "SELECT event_type, actor, target, before_json, after_json, result FROM admin_events "
                          "WHERE event_type LIKE 'inventory.%' ORDER BY id")
    assert [r["event_type"] for r in rows] == ["inventory.ingredient.created", "inventory.ingredient.updated"]
    assert rows[1]["actor"] == "will" and rows[1]["result"] == "ok"
    assert json.loads(rows[1]["before_json"]) == {"unit_cost": 20.0}
    assert json.loads(rows[1]["after_json"]) == {"unit_cost": 24.0}


def test_discovering_menu_items_is_an_admin_job_and_is_audited(admin_client, db_path, monkeypatch):
    import inventory_ledger
    rid = _rid(db_path, "Client Grill")
    monkeypatch.setattr(inventory_ledger, "discover_menu_items",
                        lambda r, days=7: {"discovered": 3, "updated": 1, "total_seen": 9, "hidden": 0})
    r = _post(admin_client, f"/admin/inventory/discover-menu-items/{rid}")
    assert r.status_code == 202 and r.get_json()["job_id"]
    job = admin_client.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert job["ok"] is True and job["discovered"] == 3
    (ev,) = _rows(db_path, "SELECT after_json, result FROM admin_events WHERE event_type='inventory.menu_items.discovered'")
    assert ev["result"] == "ok" and json.loads(ev["after_json"])["discovered"] == 3


# ── SEC-04: the activity ping's tab ───────────────────────────────────────

def test_the_activity_tab_is_a_short_name_or_other():
    import admin_routes
    assert admin_routes._activity_tab("food-cost") == "food-cost"
    assert admin_routes._activity_tab("Labor") == "labor"
    assert admin_routes._activity_tab("x" * 1000000) == "other"
    assert admin_routes._activity_tab("<script>") == "other"
    assert admin_routes._activity_tab(None) == "other"


# ── SCALE-11: a degraded read is heard ────────────────────────────────────

def test_an_unreadable_alias_table_is_captured_and_degrades(db, monkeypatch):
    import invoices
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: seen.append(k.get("job")))

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("database is locked")
    assert invoices._aliases(Broken(), 1, "Sysco") == {}
    assert seen == ["invoice_aliases"]
