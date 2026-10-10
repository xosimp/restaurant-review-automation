"""Schedule fix round 10/10/26, workstream J3 — MARKET-18: the staff portal
as an installable app (manifest, a service worker that never stores private
data), Web Push when the VAPID keys are set (RFC 8291/8292, no new
dependency), and offer answers measured by platform."""
import base64
import json
import os

import pytest

import models
import staff_pwa
import web_push


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    priv = k.private_numbers().private_value.to_bytes(32, "big")
    return k, _b64u(pub), _b64u(priv)


def _decrypt(body, ua_key, auth):
    """RFC 8291 from the browser's side, to prove the round trip."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21:21 + idlen]
    ct = body[21 + idlen:]
    ua_public = ua_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ct, None)
    assert rs == 4096 and plain.endswith(b"\x02")
    return plain[:-1]


def test_a_payload_encrypts_so_the_browser_can_read_it():
    ua_key, ua_pub, _p = _keys()
    auth = os.urandom(16)
    body = web_push.encrypt(b'{"title":"Hi"}', ua_pub, _b64u(auth))
    assert _decrypt(body, ua_key, auth) == b'{"title":"Hi"}'


def test_vapid_is_off_without_keys_and_signs_with_them(monkeypatch):
    monkeypatch.delenv("WEB_PUSH_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("WEB_PUSH_VAPID_PRIVATE_KEY", raising=False)
    assert web_push.enabled() is False
    assert web_push.send({"endpoint": "https://fcm.googleapis.com/fcm/send/x"}, {"title": "t"})["ok"] is False
    key, pub, priv = _keys()
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", pub)
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", priv)
    monkeypatch.setenv("WEB_PUSH_SUBJECT", "mailto:ops@cavnar.ai")
    assert web_push.enabled()
    h = web_push.vapid_header("https://fcm.googleapis.com/fcm/send/abc")
    assert h.startswith("vapid t=") and h.endswith(", k=" + pub)
    import jwt
    claims = jwt.decode(h.split("t=")[1].split(",")[0], key.public_key(), algorithms=["ES256"],
                        audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:ops@cavnar.ai"
    # A mismatched pair is off, never half-on.
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", _keys()[1])
    assert web_push.enabled() is False


def test_only_browser_push_services_are_ever_called():
    assert web_push.endpoint_allowed("https://fcm.googleapis.com/fcm/send/x")
    assert web_push.endpoint_allowed("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert web_push.endpoint_allowed("https://web.push.apple.com/abc")
    assert not web_push.endpoint_allowed("http://fcm.googleapis.com/x")
    assert not web_push.endpoint_allowed("https://evil.example.com/fcm.googleapis.com")
    assert not web_push.endpoint_allowed("https://169.254.169.254/latest")


def test_the_service_worker_never_caches_a_persons_data():
    sw = staff_pwa.service_worker()
    assert "/staff/api/" not in sw.replace("Nothing under /staff/api/ and", "")
    assert "url.pathname.indexOf('/static/') === 0" in sw
    assert "caches.match(OFFLINE)" in sw and "showNotification" in sw
    # ES5 like every script the portal ships.
    for banned in ("=>", "`", "const ", "let ", "async ", "await "):
        assert banned not in sw
    m = staff_pwa.manifest()
    assert m["scope"] == "/staff/" and m["start_url"] == "/staff/home" and m["display"] == "standalone"
    for icon in m["icons"]:
        assert os.path.exists(os.path.join(os.path.dirname(os.path.dirname(__file__)), icon["src"].lstrip("/")))


def test_the_platform_of_an_answer():
    assert staff_pwa.platform_of({}, True) == "ios_app"
    android = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/129"}
    assert staff_pwa.platform_of(android, False) == "android_browser"
    assert staff_pwa.platform_of(dict(android, **{"X-Cavnar-Display": "standalone"}), False) == "android_pwa"
    assert staff_pwa.platform_of({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)"}, False) == "ios_browser"


@pytest.fixture
def client(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    from flask import Flask
    import staff_routes
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(staff_routes.staff_bp)
    return app.test_client()


def test_the_shell_routes_are_public_and_carry_nothing_personal(client):
    r = client.get("/staff/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    assert json.loads(r.data)["scope"] == "/staff/"
    sw = client.get("/staff/sw.js")
    assert sw.status_code == 200 and sw.headers["Cache-Control"] == "no-cache"
    assert sw.headers["Service-Worker-Allowed"] == "/staff/"
    off = client.get("/staff/offline")
    assert off.status_code == 200 and b"Nothing about you is stored" in off.data
    assert client.get("/staff/api/push/key").status_code in (401, 302)


def test_offer_answers_are_timed_per_platform(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="P Co", owner_email="p@x.test", module_labor=1))
    conn = models.get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")          # the offers' requests are not the point here
    for i in range(6):
        conn.execute("INSERT INTO shift_offers (id, restaurant_id, request_id, name, name_key, created_at, answered_at, "
                     "status) VALUES (?,?,?,?,?,datetime('now','-30 minutes'),datetime('now'),'accepted')",
                     (100 + i, rid, 1, "Ana", "ana"))
    conn.commit()
    conn.close()
    for i in range(6):
        staff_pwa.record_offer_answer(rid, 100 + i, "android_pwa", True)
    staff_pwa.record_offer_answer(rid, 100, "android_pwa", True)          # one answer, recorded once
    got = staff_pwa.response_times()
    row = next(p for p in got["platforms"] if p["platform"] == "android_pwa")
    assert row["n"] == 6 and row["median_minutes"] == pytest.approx(30.0, abs=0.5) and row["accepted_share"] == 1.0
