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


def test_sec03_a_shared_bucket_paces_every_process(db_path):
    import rate_bucket
    assert rate_bucket.take("t:1", 2.0, db_path=db_path) == 0.0, "a quiet bucket gives its first slot"
    w = rate_bucket.take("t:1", 2.0, db_path=db_path)
    assert 0 < w <= 0.5, "the next one waits for the refill"
    assert rate_bucket.take("t:2", 2.0, db_path=db_path) == 0.0, "another restaurant's bucket is its own"
    assert rate_bucket.take("t:3", 0, db_path=db_path) == 0.0, "a rate of 0 is off"


def test_sec03_a_tenant_whose_texts_draw_stops_cannot_start_a_campaign(db_path, monkeypatch):
    import guest_marketing as gm
    import models
    monkeypatch.delenv("GUEST_SMS_STOP_PAUSE_PCT", raising=False)
    gm.init_guest_marketing(db_path)
    conn = models.get_conn(db_path)
    rows = [(1, 1, i, f"+1312555{i:04d}", "sent") for i in range(gm.STOP_PAUSE_MIN_SENT)]
    conn.executemany("INSERT INTO guest_campaign_queue (campaign_id, restaurant_id, contact_id, phone, status, sent_at) "
                     "VALUES (?,?,?,?,?, datetime('now','-1 hour'))", rows)
    conn.commit()
    assert gm.stop_rate_pause(1, db_path=db_path) is None
    conn.executemany("INSERT INTO guest_consent_events (restaurant_id, phone, event, source) "
                     "VALUES (0, ?, 'opted_out', 'sms_reply')", [(f"+1312555{i:04d}",) for i in range(10)])
    conn.commit()
    conn.close()
    reason = gm.stop_rate_pause(1, db_path=db_path)
    assert reason and "replied STOP" in reason
    assert gm.stop_rate_pause(2, db_path=db_path) is None, "another restaurant's list is not judged by it"
    seg, refused = gm.prepare_campaign(1, "Half-price wings tonight", "all", db_path=db_path)
    assert refused and refused["blocked"] == "stop_rate"


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
