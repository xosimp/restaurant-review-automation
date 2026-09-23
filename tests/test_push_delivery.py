"""push.py — APNs delivery: retry-with-backoff, per-delivery logging, and
the three different things a failure can mean.

A failed send is classified before it is acted on (push._classify):

  dead     — Apple says the token will never work: deleted on the spot.
  provider — our signing key, our apns-topic, our network, or Apple's own
             trouble. Says nothing about the device, so it must never
             advance the failure counter. Ten alerts sent during an expired
             .p8 used to delete EVERY device at EVERY restaurant.
  token    — an unclassified 4xx. Counted, and at the threshold the token is
             PARKED (disabled_reason), not deleted, so the next app launch
             re-registers it."""
import os

import pytest

import push
from push import (
    init_push, register_device_token, get_device_tokens,
    remove_device_token, _deliver, _AUTO_DISABLE_AFTER,
)
from auth import init_auth, create_user
from models import get_conn, create_restaurant, Restaurant


@pytest.fixture
def rid(db_path):
    init_auth(db_path=db_path)
    return create_restaurant(Restaurant(name="Push Co", owner_email="p@x.com"), db_path=db_path)


@pytest.fixture
def uid(db_path, rid):
    return create_user(rid, "owner1", "owner1@x.com", "correct-horse", db_path=db_path)


def _no_sleep(monkeypatch):
    monkeypatch.setattr(push.time, "sleep", lambda s: None)


def _no_real_jwt(monkeypatch):
    """_provider_jwt() signs with a real APNs .p8 key from env vars — tests
    have none, so stub it out; delivery tests care about the HTTP behavior,
    not the auth token itself."""
    monkeypatch.setattr(push, "_provider_jwt", lambda: "fake-jwt-token")


def _fake_httpx_client(status_code=200, reason=None, raises=None):
    """A stand-in for push._client()'s shared HTTP/2 client. One client is
    reused across deliveries now (a TLS handshake per notification was the
    first thing to break at scale), so tests patch the accessor rather than
    httpx.Client itself."""
    calls = []

    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {"reason": reason} if reason else {}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append((url, content, headers))
            if raises:
                raise raises
            return _Resp()

    client = _Client()
    return (lambda: client), calls


def _use(monkeypatch, fake_client):
    monkeypatch.setattr(push, "_client", fake_client)


def _register(db_path, uid, rid, token="a" * 64, environment="production"):
    register_device_token(uid, rid, token, environment, db_path=db_path)
    return get_device_tokens(rid, db_path=db_path)[0]


def test_successful_delivery_logs_one_row(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=200)
    _use(monkeypatch, fake_client)

    result = _deliver(token_row, "1star", "1-star review", "Ann left a 1-star review", None, db_path=db_path)

    assert len(calls) == 1  # succeeded first try, no retries
    assert result == {"ok": True, "status": 200, "attempts": 1, "error": None}

    conn = get_conn(db_path)
    row = conn.execute("SELECT ok, attempts, status FROM push_deliveries WHERE device_token_id=?", (token_row["id"],)).fetchone()
    conn.close()
    assert row["ok"] == 1
    assert row["attempts"] == 1
    assert row["status"] == 200


def test_failed_delivery_retries_three_times_with_backoff(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=500)
    _use(monkeypatch, fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert len(calls) == 3  # exhausted all 3 attempts — 500 isn't a permanent-failure reason
    assert result["ok"] is False
    assert result["attempts"] == 3


def test_network_exception_is_recorded_as_error(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(raises=ConnectionError("refused"))
    _use(monkeypatch, fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)
    assert result["ok"] is False
    assert result["status"] == 0
    assert "refused" in result["error"]


def test_consecutive_failures_accumulate_across_calls(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=400, reason="SomethingNew")
    _use(monkeypatch, fake_client)

    for _ in range(3):
        token_row = get_device_tokens(rid, db_path=db_path)[0]  # re-fetch — failures accumulate
        _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures FROM device_tokens WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 3


def test_a_provider_failure_never_advances_the_token_counter(db_path, rid, uid, monkeypatch):
    """THE P0. An expired signing key, a missing apns-topic or an APNs
    outage is our problem, not the device's. Counting those was what
    deleted every registered device after ten alerts — a silent, permanent
    unsubscribe nobody asked for and nothing reported."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)

    for status, reason in ((403, "ExpiredProviderToken"), (400, "MissingTopic"),
                           (500, ""), (429, "TooManyRequests")):
        fake_client, _ = _fake_httpx_client(status_code=status, reason=reason)
        _use(monkeypatch, fake_client)
        for _ in range(_AUTO_DISABLE_AFTER + 2):
            rows = get_device_tokens(rid, db_path=db_path)
            assert rows, f"token deleted by {status} {reason!r}"
            _deliver(rows[0], "1star", "title", "body", None, db_path=db_path)
        row = get_device_tokens(rid, db_path=db_path)[0]
        assert row["consecutive_failures"] == 0, f"{status} {reason!r} advanced the counter"
        assert row["disabled_reason"] is None


def test_an_expired_provider_token_forces_a_fresh_jwt(db_path, rid, uid, monkeypatch):
    """403 ExpiredProviderToken with a 50-minute JWT cache meant all three
    retries re-sent the same dead token, and so did every push for the rest
    of the window."""
    _no_sleep(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)
    minted = []

    def _mint():
        minted.append(1)
        return f"jwt-{len(minted)}"

    monkeypatch.setattr(push, "_provider_jwt", _mint)
    fake_client, calls = _fake_httpx_client(status_code=403, reason="ExpiredProviderToken")
    _use(monkeypatch, fake_client)

    _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert len(calls) == 3
    assert [c[2]["authorization"] for c in calls] == ["bearer jwt-1", "bearer jwt-2", "bearer jwt-3"]


def test_unconfigured_apns_names_itself_instead_of_killing_tokens(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)
    monkeypatch.delenv("APNS_KEY_ID", raising=False)
    monkeypatch.delenv("APNS_TEAM_ID", raising=False)
    monkeypatch.delenv("APNS_PRIVATE_KEY", raising=False)
    push.invalidate_provider_jwt()
    fake_client, calls = _fake_httpx_client(status_code=200)
    _use(monkeypatch, fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert result["ok"] is False
    assert calls == []                      # never even attempted
    assert "APNS_KEY_ID" in result["error"]
    assert get_device_tokens(rid, db_path=db_path)  # and the device survives


def test_token_is_parked_not_deleted_at_the_threshold(db_path, rid, uid, monkeypatch):
    """Deleting it made push permanently dead until the owner happened to
    relaunch. Parking keeps the row, so a fix plus one launch restores it —
    and get_device_tokens(for_delivery=True) stops sending meanwhile."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=400, reason="SomethingNew")
    _use(monkeypatch, fake_client)

    for _ in range(_AUTO_DISABLE_AFTER):
        rows = get_device_tokens(rid, db_path=db_path)
        _deliver(rows[0], "1star", "title", "body", None, db_path=db_path)

    rows = get_device_tokens(rid, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["disabled_reason"]
    assert get_device_tokens(rid, db_path=db_path, for_delivery=True) == []

    # A relaunch re-registers and clears it.
    register_device_token(uid, rid, rows[0]["apns_token"], "production", db_path=db_path)
    assert len(get_device_tokens(rid, db_path=db_path, for_delivery=True)) == 1


def test_permanent_failure_reason_deletes_token_on_first_failure(db_path, rid, uid, monkeypatch):
    """APNs' BadDeviceToken/Unregistered means the token will never work
    again — this should delete it on the very first such response, not wait
    out the full _AUTO_DISABLE_AFTER counter like a transient 500 would."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=410, reason="Unregistered")
    _use(monkeypatch, fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert result["ok"] is False
    assert len(calls) == 1  # no point retrying a permanently dead token
    assert get_device_tokens(rid, db_path=db_path) == []


def test_successful_delivery_resets_consecutive_failures(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)
    conn = get_conn(db_path)
    conn.execute("UPDATE device_tokens SET consecutive_failures=5 WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    token_row = get_device_tokens(rid, db_path=db_path)[0]

    fake_client, calls = _fake_httpx_client(status_code=200)
    _use(monkeypatch, fake_client)

    _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures, last_success_at FROM device_tokens WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_register_device_token_upserts_by_token(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "b" * 64
    register_device_token(uid, rid, token, "sandbox", db_path=db_path)
    register_device_token(uid, rid, token, "production", db_path=db_path)  # re-register (e.g. reinstall)

    rows = get_device_tokens(rid, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["environment"] == "production"


def test_register_device_token_clears_prior_failures(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "c" * 64
    register_device_token(uid, rid, token, "production", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE device_tokens SET consecutive_failures=8 WHERE apns_token=?", (token,))
    conn.commit()
    conn.close()

    register_device_token(uid, rid, token, "production", db_path=db_path)

    row = get_device_tokens(rid, db_path=db_path)[0]
    assert row["consecutive_failures"] == 0


def test_remove_device_token_deletes_it(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "d" * 64
    register_device_token(uid, rid, token, "production", db_path=db_path)
    remove_device_token(token, db_path=db_path)
    assert get_device_tokens(rid, db_path=db_path) == []


def test_fire_push_is_a_noop_with_no_registered_devices(db_path, rid):
    """No device registered — the alert-firing code in notify.py calls this
    unconditionally when al_*_push is on; it must not raise."""
    init_push(db_path=db_path)
    push.fire_push(rid, "1star", "title", "body", db_path=db_path)  # should not raise


# ── delivery concurrency is bounded ─────────────────────────────────────────
#
# fire_push used to start one thread per device. _deliver retries with
# backoff, so each thread lives for seconds, and the scheduler fires push for
# every restaurant in one pass — fifty restaurants with a couple of devices
# each meant a hundred-plus concurrent threads on a small Railway container,
# every one holding an HTTP/2 connection and writing to the same SQLite file.

def test_deliveries_run_on_a_bounded_pool_not_a_thread_per_device(db_path, rid, uid, monkeypatch):
    import threading as _t
    init_push(db_path=db_path)
    for i in range(12):
        register_device_token(uid, rid, f"tok{i:02d}" + "0" * 58, "production", db_path=db_path)

    peak = {"n": 0}
    live = {"n": 0}
    lock = _t.Lock()
    release = _t.Event()

    def slow_deliver(*a, **k):
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        release.wait(2)
        with lock:
            live["n"] -= 1

    monkeypatch.setattr(push, "_deliver", slow_deliver)
    monkeypatch.setattr(push, "_MAX_PUSH_WORKERS", 4)
    monkeypatch.setattr(push, "_executor", None)  # rebuild the pool at the patched size

    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    _t.Event().wait(0.3)
    observed_peak = peak["n"]
    release.set()
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)

    assert observed_peak <= 4, f"{observed_peak} deliveries ran at once for 12 devices"
    assert observed_peak >= 2, "the pool should still deliver in parallel, not one at a time"


def test_every_device_is_still_delivered_to(db_path, rid, uid, monkeypatch):
    """Bounding concurrency must not drop anyone."""
    import threading as _t
    seen = []
    lock = _t.Lock()
    init_push(db_path=db_path)
    for i in range(7):
        register_device_token(uid, rid, f"dev{i:02d}" + "0" * 58, "production", db_path=db_path)

    def record(token_row, *a, **k):
        with lock:
            seen.append(token_row["apns_token"])

    monkeypatch.setattr(push, "_deliver", record)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)

    assert len(seen) == 7
    assert len(set(seen)) == 7


def test_a_full_queue_drops_rather_than_exhausting_the_container(db_path, rid, uid, monkeypatch):
    init_push(db_path=db_path)
    register_device_token(uid, rid, "z" * 64, "production", db_path=db_path)
    calls = []
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(push, "_MAX_PUSH_QUEUED", 0)
    # Past the pool ceiling a delivery waits for a slot (MOD-NOT-12); it is
    # dropped only once the waiting line is full as well.
    monkeypatch.setattr(push, "_MAX_PUSH_OVERFLOW", 0)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)
    assert calls == [], "past the ceiling, a delivery is dropped rather than queued forever"


def test_the_dropped_alert_is_logged_against_this_test_s_own_db_not_the_default_one(db_path, rid, uid, monkeypatch, tmp_path):
    """fire_push's ops.capture() call used to omit db_path, so it always fell
    through to models.DB_PATH regardless of which database the caller was
    actually using — every isolated test run of the queue-full path above
    was quietly writing a real 'push queue full at 0' row into whatever the
    process's default database happened to be (the developer's own local
    reviews.db when running pytest without RAILWAY_VOLUME_MOUNT_PATH set).
    Proven here by pointing models.DB_PATH somewhere else entirely and
    confirming that decoy file stays empty."""
    import ops
    import models
    decoy = str(tmp_path / "decoy.db")
    monkeypatch.setattr(models, "DB_PATH", decoy)
    init_push(db_path=db_path)
    register_device_token(uid, rid, "z" * 64, "production", db_path=db_path)
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: None)
    monkeypatch.setattr(push, "_MAX_PUSH_QUEUED", 0)
    # Past the pool ceiling a delivery waits for a slot (MOD-NOT-12); it is
    # dropped only once the waiting line is full as well.
    monkeypatch.setattr(push, "_MAX_PUSH_OVERFLOW", 0)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)

    assert not os.path.exists(decoy), "the failure must not leak into the default database"
    conn = get_conn(db_path)
    rows = conn.execute("SELECT job, error FROM job_failures").fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["job"] == "fire_push"


def test_the_collapse_id_is_stable_across_one_deliverys_retries(db_path, rid, uid, monkeypatch):
    """Its entire job. A client-side timeout often means Apple took the push
    and the response was lost, so the retries must carry the same key — a key
    recomputed per attempt turns one event into three banners."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=500)
    _use(monkeypatch, fake_client)

    _deliver(token_row, "staff_signin", "t", "b", None, db_path=db_path)

    keys = [c[2]["apns-collapse-id"] for c in calls]
    assert len(keys) == 3 and len(set(keys)) == 1


def test_two_sign_ins_in_one_day_are_two_notifications(db_path, rid, uid, monkeypatch):
    """Keyed on the date, the second sign-in REPLACED the first on the lock
    screen — and the security value of the feature went with it."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=200)
    _use(monkeypatch, fake_client)

    _deliver(token_row, "login", "t", "b", None, db_path=db_path)
    _deliver(token_row, "login", "t", "b", None, db_path=db_path)

    assert calls[0][2]["apns-collapse-id"] != calls[1][2]["apns-collapse-id"]


def test_a_masked_key_names_itself(monkeypatch):
    """Production stored a MASKED RENDERING of the .p8 as the key: PEM
    headers intact, 200 bullet characters where the base64 body should be.
    Push had therefore never worked, and cryptography's raw complaint —
    "Unable to load PEM file ... Invalid symbol 226, offset 0" — named
    neither the variable nor the cause."""
    monkeypatch.setenv("APNS_KEY_ID", "5NN4WK66VN")
    monkeypatch.setenv("APNS_TEAM_ID", "8DW8XL63K6")
    monkeypatch.setenv("APNS_PRIVATE_KEY",
                       "-----BEGIN PRIVATE KEY-----\n" + ("•" * 64 + "\n") * 3
                       + "•" * 8 + "\n-----END PRIVATE KEY-----")
    push.invalidate_provider_jwt()

    with pytest.raises(push.PushNotConfigured, match="masked placeholder"):
        push._provider_jwt()


def test_an_unset_key_still_names_the_variable(monkeypatch):
    monkeypatch.delenv("APNS_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("APNS_KEY_ID", "k")
    monkeypatch.setenv("APNS_TEAM_ID", "t")
    push.invalidate_provider_jwt()

    with pytest.raises(push.PushNotConfigured, match="APNS_PRIVATE_KEY"):
        push._provider_jwt()


def test_bad_device_token_tries_the_other_host_before_believing_it(db_path, rid, uid, monkeypatch):
    """BadDeviceToken does not only mean "dead" — Apple returns it just as
    readily for a LIVE token sent to the wrong host. The client decides
    sandbox vs production from its own build, and that guess is wrong
    whenever a Release build is signed with a development profile (Xcode's
    default when you Run to a device). Deleting on the first one made it a
    loop: register, fail, delete, re-register, fail."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid, environment="production")
    token_row = get_device_tokens(rid, db_path=db_path)[0]

    seen = []

    class _Resp:
        def __init__(self, code): self.status_code = code
        def json(self): return {"reason": "BadDeviceToken"} if self.status_code != 200 else {}

    class _Client:
        def post(self, url, content=None, headers=None):
            seen.append(url)
            # Production host rejects it; sandbox accepts.
            return _Resp(200 if "sandbox" in url else 400)

    monkeypatch.setattr(push, "_client", lambda: _Client())

    result = _deliver(token_row, "1star", "t", "b", None, db_path=db_path)

    assert result["ok"] is True
    assert any("api.push.apple.com" in u for u in seen)
    assert any("api.sandbox.push.apple.com" in u for u in seen)

    rows = get_device_tokens(rid, db_path=db_path)
    assert len(rows) == 1, "a live token must not be deleted"
    assert rows[0]["environment"] == "sandbox", "the stored environment is corrected"


def test_a_genuinely_dead_token_is_still_deleted(db_path, rid, uid, monkeypatch):
    """The self-healing retry must not resurrect a token Apple has really
    finished with — both hosts refusing it means gone."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid, environment="production")
    token_row = get_device_tokens(rid, db_path=db_path)[0]

    fake_client, calls = _fake_httpx_client(status_code=410, reason="BadDeviceToken")
    _use(monkeypatch, fake_client)

    _deliver(token_row, "1star", "t", "b", None, db_path=db_path)

    assert len(calls) == 2, "one attempt per host"
    assert get_device_tokens(rid, db_path=db_path) == []
