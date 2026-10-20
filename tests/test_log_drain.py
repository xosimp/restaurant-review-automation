"""The log drain (scale audit 10/19/26 OPS-8): off unless LOG_DRAIN_URL is an
https URL; on, every process's JSON lines go to the log service in batches,
off the request path, bounded, with a timeout, and a failed send never
reaches the code that logged."""
import json
import logging

import pytest

import log_drain


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    log_drain.uninstall()
    for k in ("sent", "dropped", "failed_batches"):
        log_drain._stats[k] = 0
    yield
    log_drain.uninstall()


def test_off_without_an_https_url(monkeypatch):
    monkeypatch.delenv("LOG_DRAIN_URL", raising=False)
    assert log_drain.install(start_thread=False) is None
    monkeypatch.setenv("LOG_DRAIN_URL", "http://logs.example")
    assert log_drain.install(start_thread=False) is None, "never plain http"


def test_lines_ship_in_batches_with_the_token_and_a_timeout(monkeypatch):
    import requests
    monkeypatch.setenv("LOG_DRAIN_URL", "https://in.logs.example/ingest")
    monkeypatch.setenv("LOG_DRAIN_TOKEN", "tok-1")
    posts = []

    class _R:
        status_code = 202
    monkeypatch.setattr(requests, "post", lambda url, data=None, headers=None, timeout=None:
                        posts.append((url, json.loads(data), headers, timeout)) or _R())
    assert log_drain.install(start_thread=False) is not None
    logging.getLogger("ops").warning("the scheduler stopped")
    logging.getLogger("ops").debug("not shipped: below the level")
    assert log_drain.flush() == 1
    url, body, headers, timeout = posts[0]
    assert url.endswith("/ingest") and headers["Authorization"] == "Bearer tok-1" and timeout
    assert body[0]["message"] == "the scheduler stopped" and body[0]["level"] == "warn"


def test_a_failed_send_drops_the_batch_and_never_raises(monkeypatch):
    import requests
    monkeypatch.setenv("LOG_DRAIN_URL", "https://in.logs.example")

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "post", boom)
    log_drain.install(start_thread=False)
    logging.getLogger("x").error("one")
    assert log_drain.flush() == 0
    s = log_drain.stats()
    assert s["failed_batches"] == 1 and s["dropped"] == 1 and "down" in s["last_error"]


def test_the_queue_is_bounded(monkeypatch):
    monkeypatch.setenv("LOG_DRAIN_URL", "https://in.logs.example")
    monkeypatch.setattr(log_drain, "QUEUE_MAX", 3)
    log_drain.install(start_thread=False)
    for i in range(5):
        logging.getLogger("x").warning(f"line {i}")
    assert log_drain.stats()["queued"] == 3 and log_drain.stats()["dropped"] == 2
