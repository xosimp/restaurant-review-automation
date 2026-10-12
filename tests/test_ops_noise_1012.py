"""Ops noise, 10/12/26: a demo account never triggers an operator email,
and a transient Google Places timeout is not paged as an error (Sentry
CAVNAR-AI-2J — EJ's competitor read hit DEADLINE_EXCEEDED and succeeded
five minutes later), nor captured twice."""
from types import SimpleNamespace

import scheduler
from ai_utils import PlacesError


def test_demo_restaurants_are_not_watched_but_still_served():
    demo = SimpleNamespace(billing_status="trial", is_demo=1)
    real = SimpleNamespace(billing_status="trial", is_demo=0)
    assert scheduler._served_client(demo) and scheduler._served_client(real)
    assert not scheduler._watched_client(demo)
    assert scheduler._watched_client(real)


def test_operator_emails_and_nudges_skip_demos():
    src = open("scheduler.py").read()
    for fn in ("def find_stale_inventory", "def find_inactive_clients", "def send_while_away_nudges"):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 5)]
        assert "_watched_client(r)" in body and "_served_client(r)" not in body, fn


def test_places_timeouts_are_transient_and_not_double_captured():
    assert PlacesError("DEADLINE_EXCEEDED").transient
    assert not PlacesError("REQUEST_DENIED").transient
    comp = open("competitor.py").read()
    assert 'if not pe.transient:' in comp and '"captured": True' in comp
    sched = open("scheduler.py").read()
    assert 'if not res.get("captured") and' in sched
