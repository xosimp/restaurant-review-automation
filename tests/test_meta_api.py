"""meta_api.py — the single Graph API version constant every Meta call now
routes through. All 19 previously-hardcoded v19.0 call sites depend on this."""
import meta_api


def test_graph_url_strips_leading_slash():
    assert meta_api.graph_url("me/accounts") == meta_api.graph_url("/me/accounts")


def test_graph_url_uses_current_version():
    url = meta_api.graph_url("123/insights")
    assert url == f"https://graph.facebook.com/{meta_api.GRAPH_VERSION}/123/insights"


def test_graph_url_not_pinned_to_the_expired_version():
    """v19.0 shipped Feb 2024 and Meta retires versions ~2 years after
    release — this is the regression test for 'someone reverts the bump'."""
    assert "v19.0" not in meta_api.graph_url("anything")


def test_oauth_dialog_url_carries_version_and_params():
    url = meta_api.oauth_dialog_url("client_id=abc&scope=x")
    assert url.startswith(f"https://www.facebook.com/{meta_api.GRAPH_VERSION}/dialog/oauth?")
    assert "client_id=abc" in url


def test_version_is_configurable_via_env(monkeypatch):
    monkeypatch.setenv("META_GRAPH_VERSION", "v99.0")
    import importlib
    reloaded = importlib.reload(meta_api)
    assert reloaded.graph_url("x") == "https://graph.facebook.com/v99.0/x"
    # restore for any tests that import meta_api afterward in the same run
    monkeypatch.delenv("META_GRAPH_VERSION", raising=False)
    importlib.reload(meta_api)
