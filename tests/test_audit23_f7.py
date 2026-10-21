"""Audit #23 fix round, group F7 — security, credentials and tenant isolation
of the RPOWER integration (10/21/26).

What these protect (finding ids from /Users/simp/rpower-audit-1021/A7_security.md):
  * A7-01 / A1-14: the partner token is held once (credentials.pos_credentials),
    stores bind its id, a rotation is one write, the budget is keyed by it.
  * A1-22: one store's unit of work resolves its credential once.
  * A7-02: a bulk-onboarded store is held for its owner's store check.
  * A7-03 / A3-07 / A1-20: kept lists scoped per credential, employee rows
    projected, orphans purged, a day's max age, one unknown hire read alone.
  * A7-04 / A7-05: no one's pay outside the pay view (live moves, pushes, DSR).
  * A7-06 / A7-15: one store, one live restaurant (in the database), and a demo
    reads a live store only with the override.
  * A7-07 / A1-06: the guest match is opt-in, per id, bounded.
  * A7-08 / A7-18: credentials fail closed on Railway; join links never sign
    with CREDENTIAL_KEY.
  * A7-09, A7-10, A7-11, A7-12, A7-13, A7-14, A7-17, A5-01.
"""
import json
import os
import sys
import time

import pytest
from flask import Flask

import auth
import billing_jobs
import bulk_provisioning
import credentials
import models
import rpower
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, update_restaurant

CSRF = "f7-csrf"


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    for k in ("CREDENTIAL_KEY", "CREDENTIAL_KEY_PREVIOUS", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
              "RAILWAY_SERVICE_ID", "ALLOW_LOCAL_SCHEDULER", "PRINCIPAL_2FA_REQUIRED",
              "PRINCIPAL_2FA_REQUIRED_SINCE", "RPOWER_MANIFEST_HMAC_KEY", "RPOWER_MAX_STORES_PER_CREDENTIAL",
              "POS_PURGE_AFTER_DISCONNECT_DAYS"):
        monkeypatch.delenv(k, raising=False)
    import config
    monkeypatch.setattr(config, "on_railway", lambda: False)
    monkeypatch.setattr(config, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "enqueue_pos_onboard", lambda *a, **k: None)


def _rest(db_path, name="Store", **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.lower().replace(' ', '')}@x.test"),
                            db_path=db_path)
    if fields:
        update_restaurant(rid, fields, db_path=db_path)
    return rid


def _q(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ── A7-01 / A1-14: the partner credential, held once ────────────────────────

def test_a_store_reads_its_token_through_the_credential_it_binds(db_path):
    cid = credentials.credential_for_token("rpower", "partner-tok", actor="will")
    assert credentials.credential_for_token("rpower", "partner-tok") == cid, "one row per token"
    rid = _rest(db_path, rpower_credential_id=cid, rpower_cg=1280, rpower_store_mid="9001")
    c = rpower._creds(rid)
    assert c["token"] == "partner-tok" and c["credential_id"] == cid
    assert rpower.is_connected(rid)
    # Never the token in what the console lists.
    listed = credentials.list_credentials("rpower")
    assert listed[0]["id"] == cid and "token" not in listed[0] and listed[0]["stores"] == 1


def test_a_rotation_is_one_write_for_every_store_and_clears_the_old_copies(db_path):
    cid = credentials.credential_for_token("rpower", "old-tok")
    a = _rest(db_path, "A", rpower_credential_id=cid, rpower_token="old-tok", rpower_cg=1, rpower_store_mid="1")
    b = _rest(db_path, "B", rpower_credential_id=cid, rpower_cg=1, rpower_store_mid="2")
    out = credentials.rotate(cid, "new-tok", actor="will")
    assert out["ok"] and out["version"] == 2 and out["stores"] == 2 and out["legacy_cleared"] == 1
    assert rpower._creds(a)["token"] == rpower._creds(b)["token"] == "new-tok"
    assert models.get_restaurant(a).rpower_token is None


def test_a_revoked_credential_never_falls_back_to_the_rows_old_copy(db_path):
    cid = credentials.credential_for_token("rpower", "leaked")
    rid = _rest(db_path, rpower_credential_id=cid, rpower_token="leaked", rpower_cg=1, rpower_store_mid="1")
    assert credentials.revoke(cid, actor="will")["ok"]
    assert rpower._creds(rid)["token"] == "" and not rpower.is_connected(rid)


def test_boot_binds_every_legacy_row_to_one_credential_and_leaves_the_simulated_store(db_path):
    ej = _rest(db_path, "EJs", rpower_token="ej-tok", rpower_cg=1280, rpower_store_mid="9001")
    demo = _rest(db_path, "EJs Demo", rpower_token="ej-tok", rpower_cg=1280, rpower_store_mid="9001", is_demo=1)
    sim = _rest(db_path, "Halsted", rpower_token="demo-store:halsted", rpower_cg=77, rpower_store_mid="1", is_demo=1)
    out = credentials.bind_legacy_pos_tokens()
    assert out["bound"] == 2 and out["credentials"] == 1 and out["skipped"] == 1
    a, b = models.get_restaurant(ej), models.get_restaurant(demo)
    assert a.rpower_credential_id and a.rpower_credential_id == b.rpower_credential_id
    assert models.get_restaurant(sim).rpower_credential_id is None
    # Nothing a store reads changed, and the copies stay through the rollback window.
    assert rpower._creds(ej)["token"] == "ej-tok" and a.rpower_token == "ej-tok"
    assert credentials.clear_legacy_pos_tokens() == 0
    assert credentials.clear_legacy_pos_tokens(keep_days=0) == 2
    assert models.get_restaurant(ej).rpower_token is None and rpower._creds(ej)["token"] == "ej-tok"
    assert credentials.bind_legacy_pos_tokens()["bound"] == 0, "idempotent"


def test_the_budget_bucket_is_the_credentials_and_survives_a_rotation(db_path):
    cid = credentials.credential_for_token("rpower", "tok-1")
    rid = _rest(db_path, rpower_credential_id=cid, rpower_cg=1, rpower_store_mid="1")
    token, _base = rpower._ctx(rid)
    assert rpower._bucket_key(token) == f"rpower:cred:{cid}"
    credentials.rotate(cid, "tok-2")
    token2, _ = rpower._ctx(rid)
    assert token2 == "tok-2" and rpower._bucket_key(token2) == f"rpower:cred:{cid}"
    # A token named without its store's credential keeps the digest bucket.
    assert rpower._bucket_key("someone-else").startswith("rpower:") and "cred" not in rpower._bucket_key("x")


def test_one_units_helpers_resolve_the_credential_once(db_path, monkeypatch):
    cid = credentials.credential_for_token("rpower", "tok")
    rid = _rest(db_path, rpower_credential_id=cid, rpower_cg=1, rpower_store_mid="1")
    calls = []
    real = rpower._read_creds
    monkeypatch.setattr(rpower, "_read_creds", lambda r: calls.append(r) or real(r))
    with models.read_scope():
        for _ in range(5):
            rpower._ctx(rid)
    assert len(calls) == 1
    rpower._ctx(rid)
    rpower._ctx(rid)
    assert len(calls) == 3, "outside a unit nothing is kept"
    # A write inside the unit drops it.
    with models.read_scope():
        rpower._ctx(rid)
        update_restaurant(rid, {"rpower_store_mid": "2"}, db_path=db_path)
        assert rpower._creds(rid)["store_mid"] == "2"


def test_the_admin_save_binds_the_credential_and_keeps_no_copy(db_path, monkeypatch):
    import rpower_routes
    monkeypatch.setattr(rpower, "test_token", lambda t: {"ok": True, "stores": [
        {"cg": 5, "store_mid": "50", "name": "Five", "serial_number": "SN-5"}]})
    app = Flask(__name__)
    rid = _rest(db_path)
    with app.test_request_context("/admin/rpower/save/1", method="POST", json={"token": "tok-save"}):
        resp = rpower_routes._save_rpower_token(rid, {"username": "will"})
    body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    assert body["ok"] and body["credential_id"]
    r = models.get_restaurant(rid)
    assert r.rpower_token is None and r.rpower_credential_id == body["credential_id"]
    assert rpower._creds(rid)["token"] == "tok-save" and r.rpower_store_mid == "50"


# ── A7-08: credentials fail closed on Railway; A7-18 join links ─────────────

def test_on_railway_a_credential_save_without_a_key_is_refused(db_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "on_railway", lambda: True)
    paged = []
    monkeypatch.setattr(credentials, "_alert", lambda kind, subject, lines: paged.append(kind))
    assert credentials.saves_refused()
    with pytest.raises(credentials.CredentialKeyError):
        credentials.encrypt("a-token")
    with pytest.raises(credentials.CredentialKeyError):
        update_restaurant(_rest(db_path), {"rpower_token": "plain"}, db_path=db_path)
    assert "key" in paged
    monkeypatch.setenv("CREDENTIAL_KEY", _key())
    assert credentials.saves_refused() is None
    assert credentials.encrypt("a-token").startswith(credentials.PREFIX)


def test_off_railway_a_missing_key_still_stores_plaintext(db_path):
    assert credentials.saves_refused() is None
    assert credentials.encrypt("dev-token") == "dev-token"


def test_a_credential_that_no_longer_opens_pages_and_the_probe_fails(db_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", _key())
    cid = credentials.credential_for_token("rpower", "sealed-tok")
    rid = _rest(db_path, rpower_credential_id=cid, rpower_cg=1, rpower_store_mid="1")
    credentials._plain_cache.clear()
    monkeypatch.setenv("CREDENTIAL_KEY", _key())           # the key changed under it
    credentials._fernet_cache.clear()
    seen = []
    monkeypatch.setattr(credentials, "undecryptable", lambda where="": seen.append(where))
    assert rpower._creds(rid)["token"] == ""
    import provider_health
    out = provider_health.probe_rpower()
    assert out["state"] == "failing" and "does not open" in (out.get("detail") or out.get("error") or json.dumps(out))


def test_join_links_never_sign_with_the_credential_key(monkeypatch):
    import guest_links
    import config
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("CREDENTIAL_KEY", "the-encryption-key")
    assert guest_links._legacy_secret() == b"cavnar-dev-only"
    monkeypatch.setattr(config, "on_railway", lambda: True)
    assert guest_links._legacy_secret() is None and guest_links._legacy_sig(5) is None
    assert guest_links.verify_join("5-abcdef0123456789") is None
    monkeypatch.setenv("SECRET_KEY", "s3cret")
    assert guest_links._legacy_secret() == b"s3cret"


# ── A7-03 / A3-07 / A1-20: kept lists ───────────────────────────────────────

def test_employee_records_are_kept_without_their_phones(db_path):
    rows = [{"mid": "11", "fname": "Maria", "lname": "Lopez", "payroll_id": "P1", "term_date": "2000-01-01",
             "phone": "(312) 555-0101", "mobile": "312 555 0102", "passcode": "1234", "reg_rate": 18}]
    kept = rpower._project("employee/getbycg", rows)
    assert set(kept[0]) == {"mid", "fname", "lname", "payroll_id", "term_date", "phone_keys"}
    assert len(kept[0]["phone_keys"]) == 2 and "3125550101" not in json.dumps(kept)
    assert rpower.phone_key("+1 312-555-0101") in kept[0]["phone_keys"]
    other = [{"mid": "1", "name": "Burger", "price": 9}]
    assert rpower._project("menuitem/getbycg", other) == other


def test_two_customers_with_one_cg_never_share_a_list(db_path, monkeypatch):
    c1 = credentials.credential_for_token("rpower", "tok-a")
    c2 = credentials.credential_for_token("rpower", "tok-b")
    a = _rest(db_path, "A", rpower_credential_id=c1, rpower_cg=9999, rpower_store_mid="1")
    b = _rest(db_path, "B", rpower_credential_id=c2, rpower_cg=9999, rpower_store_mid="2")
    reads = []
    monkeypatch.setattr(rpower, "_paged", lambda token, path, params, **k: reads.append(token) or [
        {"mid": "1", "fname": token, "lname": "X", "phone": "3125550199"}])
    assert rpower._list(a, "employee/getbycg")[0]["fname"] == "tok-a"
    assert rpower._list(b, "employee/getbycg")[0]["fname"] == "tok-b"
    assert reads == ["tok-a", "tok-b"]
    scopes = {r["scope"] for r in _q(db_path, "SELECT scope FROM pos_catalogs WHERE provider='rpower'")}
    assert scopes == {f"c{c1}:9999", f"c{c2}:9999"}
    assert "3125550199" not in json.dumps(_q(db_path, "SELECT payload FROM pos_catalogs"))


def test_old_cg_only_lists_move_projected_and_orphans_are_purged(db_path):
    import pos_io
    cid = credentials.credential_for_token("rpower", "tok")
    _rest(db_path, rpower_credential_id=cid, rpower_cg=1280, rpower_store_mid="9001")
    pos_io.catalog_put("rpower", "1280", "employee/getbycg",
                       [{"mid": "1", "fname": "A", "lname": "B", "mobile": "3125550111"}])
    pos_io.catalog_put("rpower", "4444", "job/getbycg", [{"mid": "1", "name": "Server"}])
    out = rpower.migrate_catalog_scopes()
    # Every cg-only row goes (each held raw records); the bound group's is
    # copied to its new scope first, projected.
    assert out["moved"] == 1 and out["deleted"] == 2
    moved = pos_io.catalog_get("rpower", f"c{cid}:1280", "employee/getbycg")[1]
    assert "mobile" not in moved[0] and moved[0]["phone_keys"]
    assert not _q(db_path, "SELECT 1 FROM pos_catalogs WHERE scope IN ('4444', '1280')")
    pos_io.catalog_put("rpower", "5555", "job/getbycg", [{"mid": "1", "name": "Cook"}])
    assert rpower.purge_catalogs() == 1, "a cg-only list kept later goes at once too"
    # A disconnected store's lists wait out the orphan window, then go.
    update_restaurant(models.get_restaurant(1).id, {"rpower_store_mid": None, "rpower_credential_id": None},
                      db_path=db_path)
    assert rpower.purge_catalogs() == 0
    assert rpower.purge_catalogs(orphan_days=0) == 1


def test_a_kept_list_lives_a_day_and_one_new_hire_is_read_alone(db_path, monkeypatch):
    assert rpower.CATALOG_MAX_AGE_SECONDS == 86400 or os.getenv("RPOWER_CATALOG_MAX_AGE_SECONDS")
    cid = credentials.credential_for_token("rpower", "tok")
    rid = _rest(db_path, rpower_credential_id=cid, rpower_cg=1, rpower_store_mid="1")
    lists = {"job/getbycg": [{"mid": "7", "name": "Server"}],
             "employee/getbycg": [{"mid": "1", "fname": "Old", "lname": "Hand"}]}
    monkeypatch.setattr(rpower, "_paged", lambda token, path, params, **k: list(lists.get(path, [])))
    calls = []

    def req(token, path, params=None):
        calls.append(path)
        return [{"mid": params["mid"], "fname": "New", "lname": "Hire", "phone": "3125550000"}]
    monkeypatch.setattr(rpower, "_request", req)
    rpower._people_cached(rid)
    people = rpower._people_for(rid, [{"emp_mid": "2", "job_mid": "7", "in_dttm": "2026-10-20T10:00:00"}])
    assert calls == ["employee/getbymid"]
    assert people["employees"]["2"] == "New Hire" and people["employees"]["1"] == "Old Hand"


# ── A7-02: the bulk owner check; A5-01 the cap per credential ───────────────

CSV = ("restaurant_name,owner_email,rpower_cg,rpower_store_mid\n"
       "Wave One,one@wave.test,1280,9001\n")


def _stores(serial="SN-9001"):
    return {"ok": True, "stores": [{"cg": 1280, "store_mid": "9001", "name": "Wave One POS",
                                    "serial_number": serial},
                                   {"cg": 1280, "store_mid": "9002", "name": "Wave Two POS",
                                    "serial_number": "SN-9002"}]}


def test_a_listed_store_is_held_until_its_owner_confirms_it(db_path, monkeypatch):
    monkeypatch.setattr(rpower, "test_token", lambda t: _stores())
    rows, _ = bulk_provisioning.parse_csv(CSV)
    plan = bulk_provisioning.plan(rows)
    assert plan["rows"][0]["owner_check"] == "held" and plan["owner_check"]["held"] == 1
    out = bulk_provisioning.apply(rows, "will", rpower_token="partner")
    rid = out["rows"][0]["restaurant_id"]
    assert models.get_restaurant(rid).pos_owner_hold
    st = bulk_provisioning.owner_check_state(rid)
    assert st["held"] and st["can_check"]
    assert not bulk_provisioning.verify_owner(rid, "Wave One POS", "SN-0000", "owner")["ok"]
    assert not bulk_provisioning.verify_owner(rid, "Somewhere Else", "SN-9001", "owner")["ok"]
    assert bulk_provisioning.verify_owner(rid, "wave one pos", "sn 9001", "owner")["released"]
    assert models.get_restaurant(rid).pos_owner_hold is None
    # The serial is never kept as typed.
    assert "SN-9001" not in json.dumps(_q(db_path, "SELECT * FROM onboarding_confirmations"))


def test_five_misses_lock_the_check_for_the_hour(db_path, monkeypatch):
    monkeypatch.setattr(rpower, "test_token", lambda t: _stores())
    rows, _ = bulk_provisioning.parse_csv(CSV)
    rid = bulk_provisioning.apply(rows, "will", rpower_token="partner")["rows"][0]["restaurant_id"]
    for _ in range(bulk_provisioning.OWNER_CHECK_MAX_FAILS):
        bulk_provisioning.verify_owner(rid, "nope", "nope", "x")
    out = bulk_provisioning.verify_owner(rid, "Wave One POS", "SN-9001", "owner")
    assert out.get("locked") and models.get_restaurant(rid).pos_owner_hold


def test_an_rpower_signed_list_holds_nothing(db_path, monkeypatch):
    import hashlib
    import hmac
    monkeypatch.setattr(rpower, "test_token", lambda t: _stores())
    monkeypatch.setenv("RPOWER_MANIFEST_HMAC_KEY", "rpower-key")
    sig = hmac.new(b"rpower-key", CSV.strip().encode(), hashlib.sha256).hexdigest()
    assert bulk_provisioning.manifest_verified(CSV, sig) and not bulk_provisioning.manifest_verified(CSV, "00")
    rows, _ = bulk_provisioning.parse_csv(CSV)
    out = bulk_provisioning.apply(rows, "will", rpower_token="partner", csv_text=CSV, manifest_signature=sig)
    assert out["manifest"] and models.get_restaurant(out["rows"][0]["restaurant_id"]).pos_owner_hold is None


def test_an_existing_owner_joins_only_with_the_rows_merge_flag(db_path, monkeypatch):
    home = _rest(db_path, "Home", owner_email="one@wave.test")
    update_restaurant(home, {"owner_email": "one@wave.test"}, db_path=db_path)
    create_user(home, "one", "one@wave.test", "pw-long-enough-1", db_path=db_path)
    rows, _ = bulk_provisioning.parse_csv(CSV)
    item = bulk_provisioning.plan(rows)["rows"][0]
    assert item["action"] == "error" and "merge_owner=yes" in item["error"] and item["joins_owner"]
    rows2, _ = bulk_provisioning.parse_csv(CSV.replace("rpower_store_mid\n", "rpower_store_mid,merge_owner\n")
                                           .replace("9001\n", "9001,yes\n"))
    assert bulk_provisioning.plan(rows2)["rows"][0]["action"] == "create"


def test_one_partner_credential_admits_at_most_the_cap(db_path, monkeypatch):
    monkeypatch.setattr(rpower, "test_token", lambda t: _stores())
    monkeypatch.setenv("RPOWER_MAX_STORES_PER_CREDENTIAL", "1")
    csv = CSV + "Wave Two,two@wave.test,1280,9002\n"
    rows, _ = bulk_provisioning.parse_csv(csv)
    out = bulk_provisioning.apply(rows, "will", rpower_token="partner")
    acts = {r["restaurant_name"]: r for r in out["rows"]}
    assert acts["Wave One"]["action"] == "created"
    assert acts["Wave Two"]["action"] == "error" and "RPOWER_MAX_STORES_PER_CREDENTIAL" in acts["Wave Two"]["error"]


def test_a_held_store_sees_only_the_store_check(db_path):
    rid = _rest(db_path, "Held", pos_owner_hold="2026-10-21 10:00:00")
    uid = create_user(rid, "held", "held@x.test", "pw-long-enough-1", db_path=db_path)
    app = Flask(__name__, template_folder="../templates")

    @app.route("/api/labor/anything")
    @auth.login_required
    def anything(current_user):
        return {"ok": True}

    @app.route("/api/pos/store-check")
    @auth.login_required
    def check(current_user):
        return {"ok": True, "check": True}

    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    r = c.get("/api/labor/anything")
    assert r.status_code == 403 and r.get_json()["pos_store_check"] is True
    assert c.get("/api/pos/store-check").get_json()["check"] is True
    update_restaurant(rid, {"pos_owner_hold": None}, db_path=db_path)
    assert c.get("/api/labor/anything").get_json()["ok"] is True


# ── A7-04 / A7-05: pay only in the pay view ─────────────────────────────────

def test_a_moves_sentence_and_figures_lose_their_pay():
    import labor_live
    m = {"kind": "cut", "dollars": 36.0, "net": 30.0, "premium": 6.0,
         "text": "Let Maria (Server) go at 8:00pm instead of 10:00pm: saves 2h (~$36), less $6 predictability pay "
                 "under Chicago. Server stays at or above what tonight's pace needs until close."}
    out = labor_live.redact_move(m)
    assert out["dollars"] is None and out["net"] is None and out["premium"] is None and "$" not in out["text"]
    assert "saves 2h" in out["text"] and out["pay_hidden"]
    assert "$" not in labor_live.strip_pay_text("Call in Ann 5-9pm: 4h, about $72.")
    assert "$" not in labor_live.strip_pay_text("Some new wording costing $40 somewhere")


def test_a_push_carries_no_pay_unless_every_phone_may_see_it():
    import labor_live
    owner = {"id": 1, "role": "owner", "restaurant_id": 1}
    member = {"id": 2, "role": "member", "restaurant_id": 1}
    assert labor_live._audience_sees_pay([owner], {1})
    assert not labor_live._audience_sees_pay([owner, member], {1, 2})
    assert not labor_live._audience_sees_pay([owner], {1, 3}), "a recipient not known is no"


def test_the_live_read_drops_pay_for_a_login_that_does_not_see_it(db_path, monkeypatch):
    import floor_api
    import labor_live
    rid = _rest(db_path, "Live")
    move = {"id": 1, "kind": "cut", "employee": "Maria", "text": "Let Maria go: saves 2h (~$36).", "dollars": 36.0,
            "premium": None, "status": "suggested"}
    monkeypatch.setattr(labor_live, "kept_read", lambda *a, **k: ({"available": True, "moves": [dict(move)]}, 1))
    monkeypatch.setattr(labor_live, "todays_moves", lambda *a, **k: [dict(move)])
    app = Flask(__name__)
    with app.test_request_context("/api/labor/live"):
        body, status = floor_api.live_read({"id": 9, "role": "member", "restaurant_id": rid})
        assert status == 200
        assert body["moves_today"][0]["dollars"] is None and "$" not in body["moves_today"][0]["text"]
        assert body["read"]["moves"][0]["dollars"] is None
        body, _ = floor_api.live_read({"id": 1, "role": "owner", "restaurant_id": rid})
        assert body["moves_today"][0]["dollars"] == 36.0


def _labor_facts():
    return {"blocks": {"labor": {"status": "ready", "metrics": {"cost": 500, "pct": 25},
                                 "detail": {"overtime_drift": {"available": True, "minutes": 45, "dollars": 14.25,
                                                               "people": [{"employee": "Alex", "early": 0,
                                                                           "late": 30, "dollars": 9.5}]},
                                            "departments": [{"department": "Bar", "hours": 6, "cost": 108,
                                                             "pct_of_sales": 5.4, "roles": ["Bartender"],
                                                             "people": 1},
                                                            {"department": "Kitchen", "hours": 40, "cost": 700,
                                                             "pct_of_sales": 30.0, "roles": ["Cook"],
                                                             "people": 4}],
                                            "observations": [{"key": "overtime_drift",
                                                              "text": "45 minutes were punched outside scheduled "
                                                                      "shifts, about $14 — most by Alex."}]}}}}


def test_the_manager_report_drops_one_persons_pay():
    from dsr import access
    member = {"id": 2, "role": "member", "restaurant_id": 1}
    facts, hidden = access.redact(_labor_facts(), member)
    det = facts["blocks"]["labor"]["detail"]
    assert "dollars" not in det["overtime_drift"] and "dollars" not in det["overtime_drift"]["people"][0]
    bar, kitchen = det["departments"]
    assert bar["cost"] is None and bar["pct_of_sales"] is None and bar["pay_hidden"]
    assert kitchen["cost"] == 700
    assert "$" not in det["observations"][0]["text"] and "labor.drift_dollars" in hidden
    owner = {"id": 1, "role": "owner", "restaurant_id": 1}
    facts, _ = access.redact(_labor_facts(), owner)
    assert facts["blocks"]["labor"]["detail"]["overtime_drift"]["people"][0]["dollars"] == 9.5
    manager = {"id": 3, "role": "manager", "restaurant_id": 1}
    facts, _ = access.redact(_labor_facts(), manager)
    assert facts["blocks"]["labor"]["detail"]["departments"][0]["cost"] == 108, "who sends the schedule sees pay"


# ── A7-06 / A7-15: one live restaurant per store; demos ─────────────────────

def test_the_database_refuses_a_second_live_restaurant_on_one_store(db_path):
    models.ensure_pos_binding_indexes(db_path=db_path)
    _rest(db_path, "Live A", rpower_cg=1280, rpower_store_mid="9001")
    b = _rest(db_path, "Live B")
    with pytest.raises(Exception):
        update_restaurant(b, {"rpower_cg": 1280, "rpower_store_mid": "9001"}, db_path=db_path)
    demo = _rest(db_path, "Demo", is_demo=1)
    update_restaurant(demo, {"rpower_cg": 1280, "rpower_store_mid": "9001"}, db_path=db_path)  # demos are exempt
    assert "Live A" in models.demo_binding_refusal(demo, db_path=db_path)
    assert models.pos_binding_duplicates("rpower", db_path=db_path) == []


def test_a_demo_reads_a_live_store_only_with_the_override(db_path):
    demo = _rest(db_path, "Demo", is_demo=1)
    r = models.get_restaurant(demo)
    assert rpower.demo_live_store_refusal(r, "real-token")
    assert rpower.demo_live_store_refusal(r, "demo-store:halsted") is None
    update_restaurant(demo, {"pos_live_mirror": 1}, db_path=db_path)
    assert rpower.demo_live_store_refusal(models.get_restaurant(demo), "real-token") is None
    assert rpower.demo_live_store_refusal(models.get_restaurant(_rest(db_path, "Real")), "real-token") is None


def test_the_grandfathers_keep_wills_demo_mirror_and_the_guest_match_where_it_ran(db_path):
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM data_migrations WHERE name IN (?, ?)",
                 (models.POS_LIVE_MIRROR_MIGRATION, models.POS_GUEST_MATCH_MIGRATION))
    conn.commit()
    conn.close()
    ej = _rest(db_path, "EJs", rpower_token="ej-tok", rpower_cg=1280, rpower_store_mid="9001")
    demo = _rest(db_path, "EJs Demo", rpower_token="ej-tok", rpower_cg=1280, rpower_store_mid="9001", is_demo=1)
    sim = _rest(db_path, "Halsted", rpower_token="demo-store:h", rpower_cg=77, rpower_store_mid="1", is_demo=1)
    gone = _rest(db_path, "Gone", rpower_token="x", rpower_store_mid="5", billing_status="cancelled")
    assert models.grandfather_pos_live_mirror(db_path) == 1
    assert models.grandfather_pos_guest_match(db_path) == 2
    get = lambda rid: models.get_restaurant(rid)
    assert get(demo).pos_live_mirror == 1 and get(ej).pos_live_mirror == 0 and get(sim).pos_live_mirror == 0
    assert get(ej).pos_guest_match == 1 and get(sim).pos_guest_match == 1
    assert get(demo).pos_guest_match == 0 and get(gone).pos_guest_match == 0
    assert models.grandfather_pos_guest_match(db_path) == 0, "once"


# ── A7-07 / A1-06: the guest match ──────────────────────────────────────────

def test_the_guest_match_runs_only_where_opted_in_and_reads_only_the_nights_customers(db_path, monkeypatch):
    import guest_hub
    a = _rest(db_path, "In", rpower_token="t", rpower_cg=1, rpower_store_mid="1", pos_guest_match=1)
    _rest(db_path, "Out", rpower_token="t2", rpower_cg=2, rpower_store_mid="2")
    seen = []
    monkeypatch.setattr(guest_hub, "match_pos_visits", lambda rid, day, **k: seen.append(rid) or {"tickets": 0})
    guest_hub.run_pos_guest_match(db_path=db_path, day="2026-10-20")
    assert seen == [a]
    asked = []
    monkeypatch.setattr(rpower, "_ctx", lambda rid: ("t", {"cg": 1, "storemid": "1"}))
    monkeypatch.setattr(rpower, "_request", lambda token, path, params=None: asked.append((path, params["mid"])) or [
        {"mid": params["mid"], "name": "Pat", "phone": "3125550123"}])
    out = rpower.fetch_customers_by_mid(a, {"42", "43", "42"})
    assert sorted(asked) == [("customer/getbymid", "42"), ("customer/getbymid", "43")]
    rpower.fetch_customers_by_mid(a, {"42"})
    assert len(asked) == 2, "kept for the group"
    assert out["42"]["phone"] == "3125550123"


def test_the_guest_match_is_bounded_and_resumes_where_it_stopped(db_path, monkeypatch):
    import guest_hub
    ids = [_rest(db_path, f"S{i}", rpower_token="t", rpower_cg=i, rpower_store_mid=str(i), pos_guest_match=1)
           for i in range(1, 4)]
    seen = []

    def slow(rid, day, **k):
        seen.append(rid)
        return {"tickets": 0}
    monkeypatch.setattr(guest_hub, "match_pos_visits", slow)
    out = guest_hub.run_pos_guest_match(db_path=db_path, day="2026-10-20", max_seconds=-1)
    assert out["hit_bound"] and seen == []
    guest_hub._match_cursor(db_path, ids[0])
    guest_hub.run_pos_guest_match(db_path=db_path, day="2026-10-20")
    assert seen == [ids[1], ids[2], ids[0]] and guest_hub._match_cursor(db_path) == 0


# ── A7-09: organisations by cg and partner ──────────────────────────────────

def test_one_operators_stores_in_one_cg_are_one_organisation():
    from intelligence import privacy
    rows = [{"id": 1, "owner_email": "gm1@x", "rpower_cg": 500, "is_demo": 0},
            {"id": 2, "owner_email": "gm2@x", "rpower_cg": 500, "is_demo": 0},
            {"id": 3, "owner_email": "gm3@x", "rpower_cg": 600, "is_demo": 0},
            {"id": 4, "owner_email": "will@x", "rpower_cg": 500, "is_demo": 1}]
    out = privacy._union_rows(rows, [])
    assert out[1] == out[2] and out[3] != out[1] and out[4] != out[1]


def test_a_partners_stores_are_one_organisation(db_path):
    import onboarding
    from intelligence import privacy
    a, b = _rest(db_path, "PA"), _rest(db_path, "PB")
    pid = onboarding.create_partner("Group", billing_mode="direct")
    onboarding.attach_store(pid, a)
    onboarding.attach_store(pid, b)
    orgs = privacy.org_map(db_path)
    assert orgs[a] == orgs[b]


# ── A7-10: the staff name claim ─────────────────────────────────────────────

def test_a_claimed_names_pos_pay_waits_unless_the_pos_phone_vouches(db_path, monkeypatch):
    rid = _rest(db_path, "Claim")
    monkeypatch.setattr(rpower, "employee_phone_keys", lambda r, name, keyfn=None: {rpower.phone_key("3125550142")})
    assert auth._pos_phone_verdict(rid, "Jordan P.", "+13125550142") == "match"
    assert auth._pos_phone_verdict(rid, "Jordan P.", "+13125550999") == "mismatch"
    monkeypatch.setattr(rpower, "employee_phone_keys", lambda r, name, keyfn=None: set())
    assert auth._pos_phone_verdict(rid, "Jordan P.", "+13125550142") == "no_phone"
    uid = create_user(rid, "jordan", "j@staff.invalid", "pw-long-enough-1", db_path=db_path, generated=True)
    conn = models.get_conn(db_path)
    held = auth._write_claim(conn, uid, rid, "+13125550999", "Jordan P.", None, "h", "tok", pay_ok=False)
    conn.commit()
    conn.close()
    assert auth.staff_pos_pay_held(held, rid)
    assert auth.approve_staff_pos_pay(held, rid, "erik") and not auth.staff_pos_pay_held(held, rid)


def test_a_held_login_gets_no_punches(db_path):
    import staff_insights
    app = Flask(__name__)
    with app.test_request_context("/staff/api/earnings"):
        from flask import g
        g.staff_pos_held = True
        rows, reason = staff_insights.my_punch_rows(1, "Jordan P.", "2026-10-01", "2026-10-20")
        assert rows == [] and reason == "pending_approval"
        assert "manager confirms" in staff_insights._EARNINGS_REASONS["pending_approval"]


# ── A7-11: "Send to POS" is the account holder's ────────────────────────────

def test_only_the_account_holder_switches_send_to_pos(db_path):
    import floor_api
    rid = _rest(db_path, "Push")
    app = Flask(__name__)
    with app.test_request_context("/api/labor/floor-settings", method="POST", json={"pos_push": False}):
        body, status = floor_api.floor_settings_set({"id": 2, "role": "manager", "restaurant_id": rid})
        assert status == 403 and body["owner_only"]


# ── A7-12: the owner's disconnect ───────────────────────────────────────────

def test_the_owners_disconnect_needs_the_step_up_and_leaves_a_trace(db_path, monkeypatch):
    import rpower_routes
    import client_api
    rid = _rest(db_path, "Disc", rpower_token="t", rpower_cg=1, rpower_store_mid="1")
    app = Flask(__name__)
    with app.test_request_context("/api/rpower/disconnect", method="POST"):
        payload, status = rpower_routes.disconnect_refusal({"id": 1, "role": "owner", "restaurant_id": rid})
        assert status == 403 and payload["reauth_required"]
        payload, status = rpower_routes.disconnect_refusal({"id": 2, "role": "manager", "restaurant_id": rid})
        assert status == 403 and payload["owner_only"]
    logged = []
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: logged.append(a[1]))
    rpower_routes.do_disconnect({"id": 1, "role": "owner", "restaurant_id": rid})
    r = models.get_restaurant(rid)
    assert logged == ["pos_disconnected"] and r.rpower_store_mid is None and r.pos_disconnected_at


# ── A7-13: POS data after a disconnect; the export ──────────────────────────

def _punch(db_path, rid, day="2026-01-05"):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date) VALUES (?,?,?)",
                 (rid, "rpower", day))
    conn.commit()
    conn.close()


def test_a_disconnected_stores_archive_is_purged_after_the_window(db_path, monkeypatch):
    import offboarding
    rid = _rest(db_path, "Left")
    _punch(db_path, rid)
    out = offboarding.purge_disconnected_pos(db_path=db_path)
    assert out["stamped"] == 1 and models.get_restaurant(rid).pos_disconnected_at
    assert offboarding.purge_disconnected_pos(db_path=db_path)["purged"] == {}, "inside the window"
    update_restaurant(rid, {"pos_disconnected_at": "2025-01-01 00:00:00"}, db_path=db_path)
    out = offboarding.purge_disconnected_pos(db_path=db_path)
    assert out["purged"][rid] >= 1 and not _q(db_path, "SELECT 1 FROM pos_archive_days WHERE restaurant_id=?", (rid,))
    assert models.get_restaurant(rid).pos_disconnected_at is None
    # A connected store is never touched, and reconnecting stops the clock.
    live = _rest(db_path, "Live", rpower_token="t", rpower_cg=3, rpower_store_mid="3",
                 pos_disconnected_at="2025-01-01 00:00:00")
    _punch(db_path, live)
    out = offboarding.purge_disconnected_pos(db_path=db_path)
    assert live not in out["purged"] and models.get_restaurant(live).pos_disconnected_at is None
    monkeypatch.setenv("POS_PURGE_AFTER_DISCONNECT_DAYS", "0")
    assert offboarding.purge_after_days() == 0


def test_the_pos_archive_exports_as_one_csv_per_table(db_path):
    import zipfile
    import offboarding
    rid = _rest(db_path, "Export")
    _punch(db_path, rid)
    out = offboarding.pos_export_to_file(rid, db_path=db_path)
    assert out["tables"]["pos_archive_days"] == 1
    with zipfile.ZipFile(out["path"]) as zf:
        names = set(zf.namelist())
        assert "pos_archive_days.csv" in names and "pos_punches.csv" in names
        assert "2026-01-05" in zf.read("pos_archive_days.csv").decode()
    assert offboarding.export_path(out["file"]) == out["path"]
    assert offboarding.export_path("../etc/passwd") is None
    os.remove(out["path"])


# ── A7-14: the owner's error sentence ───────────────────────────────────────

def test_an_owner_sees_a_fixed_sentence_never_rpowers_raw_text(db_path):
    raw = "HTTP 400 from ticketsales/getbybusinessdate: {'error':'bad'} url=?cg=1280&storemid=9001"
    rid = _rest(db_path, "Err", rpower_token="t", rpower_cg=1280, rpower_store_mid="9001", rpower_sync_error=raw)
    st = rpower.get_connection_status(rid)
    assert st["error"] == raw and st["owner_error"] == rpower.OWNER_ERROR_SENTENCES["other"]
    assert "9001" not in st["owner_error"]
    assert rpower.owner_error_class("RPOWER rejected the token (401). Check…") == "auth"
    assert rpower.owner_error_class("ticket failed after 3 attempts: HTTP 503") == "unavailable"
    assert rpower.owner_error_class(None) is None
    import pos_health
    ps = pos_health.provider_state(models.get_restaurant(rid), "rpower")
    assert ps["error"] == rpower.OWNER_ERROR_SENTENCES["other"] and ps["error_raw"] == raw


# ── A7-17: principal 2FA for new tenants ────────────────────────────────────

def test_principal_two_factor_since_a_day_covers_restaurants_made_from_it(db_path, monkeypatch):
    monkeypatch.setenv("PRINCIPAL_2FA_REQUIRED_SINCE", "2026-10-21")
    monkeypatch.setattr(auth, "two_fa_destination", lambda u, r, method=None: {"kind": "email"})
    owner = {"id": 1, "role": "owner"}
    new = Restaurant(name="New", owner_email="n@x.test")
    new.created_at = "2026-10-22 09:00:00"
    old = Restaurant(name="Old", owner_email="o@x.test")
    old.created_at = "2026-09-01 09:00:00"
    assert auth.principal_two_factor_required(owner, new) is True
    assert auth.principal_two_factor_required(owner, old) is False
    assert auth.principal_two_factor_required({"id": 2, "role": "member"}, new) is False


def test_the_offsite_copy_carries_the_partner_credential_sealed(db_path):
    import sqlite3
    import offsite_backup
    conn = sqlite3.connect(db_path)
    try:
        plan = offsite_backup.seal_plan(conn)
        assert ("pos_credentials", "token", "credential") in plan
        _wipe, null, unclassified = offsite_backup.scrub_plan(conn, sealed={(t, c) for t, c, _m in plan})
        assert ("pos_credentials", "token") not in null and not unclassified
        # Without a key the copy scrubs it, as every credential.
        _wipe, null, _u = offsite_backup.scrub_plan(conn, sealed=set())
        assert ("pos_credentials", "token") in null
    finally:
        conn.close()
    assert ("pos_credentials", "token") in credentials.SEALED_COLUMNS
