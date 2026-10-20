"""Scale fix round 10/19/26, agent F1 (multi-tenant security): the small
findings that have no better home (A7_security.md)."""
from datetime import datetime, timedelta


def test_sec19_the_tenant_caches_are_bounded():
    import client_api
    cache = {}
    for i in range(10):
        client_api._bounded_put(cache, i, (datetime.utcnow(), i), ttl_secs=3600, max_entries=4)
    assert list(cache) == [6, 7, 8, 9], "the oldest go first"
    old = {"a": (datetime.utcnow() - timedelta(hours=2), 1)}
    client_api._bounded_put(old, "b", (datetime.utcnow(), 2), ttl_secs=3600)
    assert list(old) == ["b"], "an expired entry is swept on the next write"
    import inspect
    src = inspect.getsource(client_api)
    assert "_aivis_cache[rid] =" not in src and "_city_cache[place_id] =" not in src


def _email_rows(db_path, rid, statuses, email_type="guest_newsletter"):
    import models
    conn = models.get_conn(db_path)
    conn.executemany("INSERT INTO email_log (restaurant_id, email_type, to_email, status) VALUES (?,?,?,?)",
                     [(rid, email_type, f"g{i}@x.test", s) for i, s in enumerate(statuses)])
    conn.commit()
    conn.close()


def test_sec02_a_tenant_whose_guest_mail_draws_complaints_is_paused(db_path, monkeypatch):
    import guest_email as ge
    for v in ("GUEST_EMAIL_COMPLAINT_PAUSE_PCT", "GUEST_EMAIL_BOUNCE_PAUSE_PCT"):
        monkeypatch.delenv(v, raising=False)
    _email_rows(db_path, 1, ["delivered"] * 40 + ["complained"])
    assert ge.guest_mail_paused(1, db_path=db_path) is None, "below the sample floor nothing is judged"
    _email_rows(db_path, 1, ["delivered"] * 60)
    assert "marked as spam" in ge.guest_mail_paused(1, db_path=db_path)
    _email_rows(db_path, 2, ["delivered"] * 900 + ["bounced"] * 60)
    assert "bounced" in ge.guest_mail_paused(2, db_path=db_path)
    _email_rows(db_path, 3, ["delivered"] * 900 + ["complained"] * 30, email_type="owner_digest")
    assert ge.guest_mail_paused(3, db_path=db_path) is None, "owner mail is not the tenant's guest list"
    import inspect
    assert "guest_mail_paused(" in inspect.getsource(ge.send_newsletter)
    assert "guest_mail_paused(" in inspect.getsource(ge._send_batch)


def test_sec02_guest_mail_can_leave_from_its_own_domain(monkeypatch):
    import config
    import guest_email as ge
    import types
    monkeypatch.setenv("GUEST_FROM_EMAIL", "news@guests.cavnar.ai")
    r = types.SimpleNamespace(name="EJ's", mailing_address="1 Main", owner_email="o@x.test")
    payload = ge._render(r, {"email": "g@x.test", "name": "", "email_token": "t"}, "Hi", "<p>x</p>",
                         "https://dashboard.cavnar.ai")
    assert payload["from"].endswith("<news@guests.cavnar.ai>")
    monkeypatch.delenv("GUEST_FROM_EMAIL")
    assert config.guest_from_email() == ""


def test_sec09_every_meta_connect_start_is_bound_to_its_login():
    import inspect
    import mobile_api
    import social_routes
    web = inspect.getsource(social_routes.instagram_connect)
    assert "set_cookie(IG_NONCE_COOKIE" in web and "sign_bound_state(" in web and "sign_mobile_state(" not in web
    app = inspect.getsource(mobile_api.mobile_instagram_authorize)
    assert "sign_bound_state(" in app and "f\"u{int(current_user['id'])}\"" in app
    confirm = inspect.getsource(mobile_api.mobile_instagram_confirm)
    assert "principal_only" in confirm and "confirm_mobile_connection" in confirm
    swift = open(inspect.getfile(mobile_api).rsplit("/", 1)[0]
                 + "/ios/CavnarAI/CavnarAI/Features/Account/AccountViewModel.swift").read()
    assert "/mobile/api/connections/instagram/confirm" in swift
