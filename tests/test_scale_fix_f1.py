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


def test_sec07_one_login_cannot_hold_every_request_thread():
    import request_limits as rl
    rl.reset()
    t = 1000.0
    # A dashboard open (~23 reads at once) passes.
    assert all(rl.check("u1", 5, "/api/x", now=t) == 0 for _ in range(30))
    # The slow routes: three at once, then one every five seconds per login.
    heavy = [rl.check("u2", 5, "/api/morning-brief", now=t) for _ in range(4)]
    assert heavy[:3] == [0, 0, 0] and heavy[3] > 0
    assert rl.check("u3", 5, "/api/morning-brief", now=t) == 0, "another login has its own share"
    # A script hammering as one login is refused before the restaurant is.
    rl.reset()
    refused = sum(1 for _ in range(200) if rl.check("u4", 9, "/api/x", now=t) > 0)
    assert refused >= 100
    assert rl.check("u5", 9, "/api/x", now=t) == 0, "its teammates still get through"
    rl.reset()


def test_sec07_the_decorators_answer_429_with_retry_after(monkeypatch):
    import inspect
    import auth
    for dec in (auth.login_required, auth.mobile_login_required):
        assert "session_refusal(user)" in inspect.getsource(dec)
    import request_limits as rl
    from flask import Flask
    monkeypatch.setenv("REQUEST_LIMITS", "1")
    rl.reset()
    app = Flask(__name__)
    user = {"id": 31, "restaurant_id": 4, "role": "owner"}
    with app.test_request_context("/api/good-news"):
        got = [rl.session_refusal(user) for _ in range(4)]
    assert got[:3] == [None, None, None]
    assert got[3].status_code == 429 and int(got[3].headers["Retry-After"]) >= 1
    rl.reset()


def test_sec08_public_limits_do_not_write():
    import inspect
    import client_api
    import marketing_links
    import staff_routes
    assert "ai_rate_limited(" not in inspect.getsource(marketing_links._tap_write_allowed)
    assert 'memory_limited(f"guestoptin:{ip}"' in inspect.getsource(client_api.guest_optin_submit)
    assert "memory_limited(f\"portal:{ip}\"" in inspect.getsource(staff_routes._throttled)
    import request_limits as rl
    rl.reset()
    assert [rl.memory_limited("k", 2, 60, now=1.0) for _ in range(3)] == [False, False, True]
    assert rl.memory_limited("k", 2, 60, now=62.0) is False, "the window slides"
    rl.reset()


def test_sec08_a_bad_signature_flood_stops_writing_the_ledger(db_path):
    import models
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/webhooks/resend", environ_base={"REMOTE_ADDR": "198.51.100.40"}):
        for _ in range(models.REFUSED_WEBHOOK_WRITES_PER_MINUTE + 25):
            models.record_inbound_webhook("resend", False, reason="bad signature", db_path=db_path)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT * FROM inbound_webhook_health WHERE provider='resend'").fetchone()
    conn.close()
    counts = [v for k, v in dict(row).items() if "fail" in k and isinstance(v, int)]
    assert counts and max(counts) == models.REFUSED_WEBHOOK_WRITES_PER_MINUTE


def test_sec10_the_tenant_name_scan_does_not_grow_with_the_fleet():
    import re
    import time
    from intelligence import privacy
    names = [f"Spot{i} Tavern Co" for i in range(20000)] + ["Simple EJ's", "Halsted & Rye", "Lou's Place"]
    m = privacy._names_regex(names)
    old = re.compile(r"(?<![\w'])(?:" + "|".join(re.escape(n) for n in sorted(
        {n for n in names}, key=len, reverse=True)) + r")(?![\w'])", re.I)
    cases = ["Simple EJ's runs 31%", "simple ej's", "Simple EJ'sX", "XSimple EJ's", "at halsted & rye tonight",
             "Lou's Place.", "Lou's Places", "spot19999 tavern co", "pizza peers in Chicago", "", "Spot7 Tavern"]
    for c in cases:
        assert bool(m.search(c)) == bool(old.search(c)), c
    text = "Pizza places in Lakeview, median labor 31%, the band moved this week. " * 6
    t0 = time.perf_counter()
    for _ in range(200):
        m.search(text)
    per = (time.perf_counter() - t0) / 200
    assert per < 0.002, f"{per * 1000:.2f} ms a string with 20,000 names"


def test_sec14_a_key_rotation_keeps_every_token(db_path, monkeypatch):
    """CREDENTIAL_KEY rotated with the old one in CREDENTIAL_KEY_PREVIOUS:
    everything still reads, the boot pass re-seals it under the new key,
    and then the old key can go."""
    from cryptography.fernet import Fernet
    import credentials
    import models
    import webhooks
    from models import Restaurant, create_restaurant
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_KEY", old)
    monkeypatch.delenv("CREDENTIAL_KEY_PREVIOUS", raising=False)
    credentials._fernet_cache.clear()
    rid = create_restaurant(Restaurant(name="Rot Co", owner_email="r@x.test"), db_path=db_path)
    models.update_restaurant(rid, {"rpower_token": "rp-secret"}, db_path=db_path)
    webhooks.init_webhooks(db_path)
    monkeypatch.setattr(webhooks, "_validate_webhook_url", lambda url: None)    # no DNS in a test
    secret = webhooks.save_webhook(rid, "https://hooks.example.com/x", ["review.new"], db_path=db_path)
    conn = models.get_conn(db_path)
    raw_hook = conn.execute("SELECT secret FROM webhooks WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert raw_hook.startswith(credentials.PREFIX) and secret not in raw_hook, "the signing secret is sealed"
    # Rotate: new key first, the old one kept for reading.
    monkeypatch.setenv("CREDENTIAL_KEY", new)
    monkeypatch.setenv("CREDENTIAL_KEY_PREVIOUS", old)
    credentials._fernet_cache.clear()
    assert models.get_restaurant(rid, db_path).rpower_token == "rp-secret"
    assert webhooks.get_webhook(rid, db_path)["secret"] == secret
    out = credentials.rotate_existing(db_path)
    assert out["rotated"] == 2 and out["unreadable"] == 0
    assert credentials.rotate_existing(db_path)["rotated"] == 0, "idempotent"
    monkeypatch.delenv("CREDENTIAL_KEY_PREVIOUS")
    credentials._fernet_cache.clear()
    assert models.get_restaurant(rid, db_path).rpower_token == "rp-secret", "readable with the new key alone"
    assert webhooks.get_webhook(rid, db_path)["secret"] == secret
    credentials._fernet_cache.clear()


def test_sec14_plain_webhook_secrets_are_sealed_at_boot(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    import credentials
    import models
    import webhooks
    monkeypatch.delenv("CREDENTIAL_KEY", raising=False)
    credentials._fernet_cache.clear()
    webhooks.init_webhooks(db_path)
    monkeypatch.setattr(webhooks, "_validate_webhook_url", lambda url: None)    # no DNS in a test
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Hook Co", owner_email="h@x.test"), db_path=db_path)
    secret = webhooks.save_webhook(rid, "https://hooks.example.com/y", ["review.new"], db_path=db_path)
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    credentials._fernet_cache.clear()
    assert webhooks.get_webhook(rid, db_path)["secret"] == secret, "a plain row from before still reads"
    assert credentials.encrypt_webhook_secrets(db_path) == 1
    conn = models.get_conn(db_path)
    raw = conn.execute("SELECT secret FROM webhooks WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert raw.startswith(credentials.PREFIX) and webhooks.get_webhook(rid, db_path)["secret"] == secret
    credentials._fernet_cache.clear()


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
