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
