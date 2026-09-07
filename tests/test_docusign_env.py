"""DocuSign environment wiring: the OAuth host must follow the API base URL.

Before Sep 7 2026 get_access_token() hard-coded account-d.docusign.com (the
developer sandbox). Pointing DOCUSIGN_BASE_URL at a production shard would
have kept minting sandbox tokens and every production send would 401. These
pin the inference and the consent link used to authorise the key."""
import docusign_helper as d


def test_sandbox_base_url_uses_sandbox_auth_host(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_AUTH_HOST", raising=False)
    assert d.auth_host("https://demo.docusign.net") == "account-d.docusign.com"
    assert d.is_demo("https://demo.docusign.net")


def test_production_shard_uses_production_auth_host(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_AUTH_HOST", raising=False)
    for shard in ("https://na4.docusign.net", "https://www.docusign.net", "https://ca.docusign.net"):
        assert d.auth_host(shard) == "account.docusign.com", shard
        assert not d.is_demo(shard)


def test_auth_host_env_override_wins(monkeypatch):
    monkeypatch.setenv("DOCUSIGN_AUTH_HOST", "account.docusign.com")
    assert d.auth_host("https://demo.docusign.net") == "account.docusign.com"


def test_consent_url_targets_the_matching_environment(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_AUTH_HOST", raising=False)
    monkeypatch.setattr(d, "INTEGRATION_KEY", "ik-123")
    monkeypatch.setattr(d, "REDIRECT_URI", "https://dashboard.cavnar.ai/docusign/callback")
    prod = d.consent_url("https://na4.docusign.net")
    assert prod.startswith("https://account.docusign.com/oauth/auth?")
    assert "client_id=ik-123" in prod
    assert "scope=signature+impersonation" in prod
    assert "redirect_uri=https%3A%2F%2Fdashboard.cavnar.ai%2Fdocusign%2Fcallback" in prod
    assert d.consent_url("https://demo.docusign.net").startswith("https://account-d.docusign.com/")


def test_get_access_token_posts_to_the_environment_auth_host(monkeypatch):
    """The token request itself must go to the inferred host."""
    monkeypatch.delenv("DOCUSIGN_AUTH_HOST", raising=False)
    monkeypatch.setattr(d, "BASE_URL", "https://na4.docusign.net")
    monkeypatch.setattr(d, "INTEGRATION_KEY", "ik")
    monkeypatch.setattr(d, "USER_ID", "uid")
    monkeypatch.setattr(d, "PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----")
    import jwt, requests
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "signed")
    seen = {}

    class R:
        status_code = 200
        text = ""
        def json(self): return {"access_token": "tok"}

    def _post(url, **k):
        seen["url"] = url
        return R()
    monkeypatch.setattr(requests, "post", _post)
    assert d.get_access_token() == "tok"
    assert seen["url"] == "https://account.docusign.com/oauth/token"
