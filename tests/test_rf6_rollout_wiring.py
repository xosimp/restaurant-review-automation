"""Blind re-audit 10/27/26 (RF6, B4/B5 P2) — cohort rollout gates real
behaviour. The risky new behaviours of the release-safety round each have a
rollout feature, ON by default (today's behaviour), that the console can
narrow or kill at run time with no deploy:

- client_min_build          the 426 for an iPhone build under MOBILE_MIN_BUILD
- client_headerless_cutoff  the 426 for a headerless request past the cutoff
- stale_tab_reload          the web dashboard's "Cavnar AI was updated" bar
"""
import pytest
from flask import Flask, g

import client_compat
import rollout

OLD_BUILD = {"X-Cavnar-App": "1.0 (4000)", "X-Cavnar-Platform": "ios", "Authorization": "Bearer tok-7"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for f in rollout.FEATURES:
        monkeypatch.delenv(rollout.env_name(f), raising=False)
    monkeypatch.delenv("MOBILE_MIN_BUILD", raising=False)
    monkeypatch.delenv("MOBILE_HEADERLESS_CUTOFF", raising=False)
    rollout._flag_cache.clear()
    yield
    rollout._flag_cache.clear()


@pytest.fixture
def app(monkeypatch):
    """A bare app with the gate, and sessions that resolve without a database:
    tok-7 is restaurant 7's, tok-8 restaurant 8's."""
    import auth
    monkeypatch.setattr(auth, "get_session_user",
                        lambda token, *a, **k: {"tok-7": {"restaurant_id": 7}, "tok-8": {"restaurant_id": 8}}.get(token))
    a = Flask(__name__)

    @a.route("/mobile/api/ping")
    def ping():
        return {"ok": True}
    client_compat.install(a)
    return a.test_client()


def test_every_feature_is_registered_with_a_default_and_where_it_is_read():
    assert {"client_min_build", "client_headerless_cutoff", "stale_tab_reload"} <= set(rollout.FEATURES)
    for name, info in rollout.FEATURES.items():
        assert rollout._FEATURE.match(name), name
        assert isinstance(info["default"], bool) and info["where"] and info["what"]
    names = [k["feature"] for k in rollout.known_features()]
    assert names == sorted(rollout.FEATURES)


def test_min_build_refusal_is_on_by_default_and_killed_by_the_switch(app, monkeypatch):
    monkeypatch.setenv("MOBILE_MIN_BUILD", "5000")
    assert app.get("/mobile/api/ping", headers=OLD_BUILD).status_code == 426
    monkeypatch.setenv("ROLLOUT_CLIENT_MIN_BUILD", "off")
    assert app.get("/mobile/api/ping", headers=OLD_BUILD).status_code == 200


def test_min_build_refusal_reaches_only_its_cohort(app, monkeypatch):
    monkeypatch.setenv("MOBILE_MIN_BUILD", "5000")
    monkeypatch.setenv("ROLLOUT_CLIENT_MIN_BUILD", "0;allow=7")
    assert app.get("/mobile/api/ping", headers=OLD_BUILD).status_code == 426            # restaurant 7: in
    other = dict(OLD_BUILD, Authorization="Bearer tok-8")
    assert app.get("/mobile/api/ping", headers=other).status_code == 200                # restaurant 8: out
    no_session = {k: v for k, v in OLD_BUILD.items() if k != "Authorization"}
    assert app.get("/mobile/api/ping", headers=no_session).status_code == 200, \
        "a request with no known restaurant is refused only at 100%"
    monkeypatch.setenv("ROLLOUT_CLIENT_MIN_BUILD", "100")
    assert app.get("/mobile/api/ping", headers=no_session).status_code == 426


def test_a_current_build_is_never_consulted_about(app, monkeypatch):
    monkeypatch.setenv("MOBILE_MIN_BUILD", "5000")
    calls = []
    real = rollout.enabled
    monkeypatch.setattr(rollout, "enabled", lambda *a, **k: calls.append(a) or real(*a, **k))
    current = dict(OLD_BUILD, **{"X-Cavnar-App": "1.0 (5001)"})
    assert app.get("/mobile/api/ping", headers=current).status_code == 200
    assert calls == [], "the switch is read only when a refusal is about to be sent"


def test_headerless_cutoff_has_its_own_switch(app, monkeypatch):
    monkeypatch.setenv("MOBILE_HEADERLESS_CUTOFF", "2020-01-01")
    headerless = {"Authorization": "Bearer tok-7"}
    assert app.get("/mobile/api/ping", headers=headerless).status_code == 426
    monkeypatch.setenv("ROLLOUT_CLIENT_MIN_BUILD", "off")                    # the other rule's switch
    assert app.get("/mobile/api/ping", headers=headerless).status_code == 426
    monkeypatch.setenv("ROLLOUT_CLIENT_HEADERLESS_CUTOFF", "off")
    assert app.get("/mobile/api/ping", headers=headerless).status_code == 200


def test_the_reload_bar_follows_its_cohort(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234567890")
    a = Flask(__name__)
    with a.test_request_context("/"):
        g.cavnar_current_user = {"restaurant_id": 7}
        assert client_compat.page_build() == "abcdef123456"
        monkeypatch.setenv("ROLLOUT_STALE_TAB_RELOAD", "0;allow=8")
        assert client_compat.page_build() == "", "restaurant 7 is outside the cohort: no bar"
        g.cavnar_current_user = {"restaurant_id": 8}
        assert client_compat.page_build() == "abcdef123456"
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert client_compat.page_build() == "", "a local run has no build, so no bar, as before"
