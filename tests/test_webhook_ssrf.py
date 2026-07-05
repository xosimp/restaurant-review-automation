"""The SSRF guard on client-configured webhook URLs. Uses IP literals so no
DNS lookup (and no network) is needed to run these."""
import pytest

from webhooks import _validate_webhook_url, InvalidWebhookURL


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/hook",              # loopback
    "http://localhost/hook",              # loopback by name
    "http://10.0.0.5/hook",               # RFC1918
    "http://192.168.1.1/hook",            # RFC1918
    "http://172.16.0.1/hook",             # RFC1918
    "http://169.254.169.254/latest/meta-data/",   # AWS metadata
    "http://metadata.google.internal/computeMetadata/v1/",  # GCP metadata
    "http://0.0.0.0/hook",                # unspecified
    "ftp://8.8.8.8/hook",                 # non-http scheme
])
def test_private_and_dangerous_urls_rejected(url):
    with pytest.raises(InvalidWebhookURL):
        _validate_webhook_url(url)


def test_public_ip_accepted():
    _validate_webhook_url("https://8.8.8.8/hook")  # must not raise
