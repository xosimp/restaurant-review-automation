"""Blind re-audit 10/20/26: salaries reached viewers who may not see them.

R6-F1  the dashboard page's own login check put no login on flask.g, and
       viewer_sees_salaries() read "no login" as the owner's.
R9-05  the Labor read refreshed on the owner AI pool ran with no request
       (every read the owner's) and the kept read was served to managers.
R6-F3 / R9-16  good news cached per restaurant, not per salary view.
R6-F2 / R9-06  the activity feed cached per restaurant, not per login."""
import inspect

from flask import Flask, g

import activity
import ai_utils
import client_api
import good_news
import models
import pytest

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


def test_a_request_with_no_login_does_not_see_salaries():
    app = Flask(__name__)
    with app.test_request_context("/"):
        assert models.viewer_sees_salaries() is False
        g.cavnar_current_user = {"id": 1, "role": "manager", "restaurant_id": 5}
        assert models.viewer_sees_salaries() is False
    # Off any request with no view carried: NO — a job is manager-safe
    # unless it reports to the owner alone and says so (blind re-audit
    # 10/26/26 B3 P0; tests/test_rf3_salary_view.py).
    assert models.viewer_sees_salaries() is False


def test_a_carried_view_wins_wherever_the_work_runs():
    with models.salary_view(False):
        assert models.viewer_sees_salaries() is False
    with models.salary_view(True):
        app = Flask(__name__)
        with app.test_request_context("/"):
            assert models.viewer_sees_salaries() is True


def test_work_handed_to_a_pool_keeps_the_askers_view():
    app = Flask(__name__)
    seen = []
    with app.test_request_context("/"):
        g.cavnar_current_user = {"id": 2, "role": "manager", "restaurant_id": 5}
        run = ai_utils.attributed(lambda: seen.append(models.viewer_sees_salaries()))
    import threading
    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert seen == [False], "a manager's refresh, run off the request, must not come back all-in"


def test_the_dashboard_page_puts_its_login_on_g():
    # Read, not imported: importing hosted_dashboard boots the app.
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosted_dashboard.py")).read()
    body = src[src.index("def login_required(f):"):src.index("return decorated", src.index("def login_required(f):"))]
    assert "_bind_log_context" in body


def test_the_kept_labor_read_is_served_only_to_a_viewer_who_may_see_salaries():
    with models.salary_view(False):
        assert client_api.labor_stale_read(5) is None


def test_good_news_and_the_feed_are_kept_per_view():
    assert "viewer_sees_salaries" in inspect.getsource(good_news.all_good_news)
    app = Flask(__name__)
    with app.test_request_context("/"):
        g.cavnar_current_user = {"id": 7, "role": "owner"}
        a = activity._viewer_key()
        g.cavnar_current_user = {"id": 8, "role": "manager"}
        b = activity._viewer_key()
    assert a != b


def test_each_morning_brief_is_built_in_its_recipients_salary_view(monkeypatch):
    # R6-F3b: deliver() builds off a request, where every read was the owner's.
    import morning_brief
    src = inspect.getsource(morning_brief.deliver)
    assert "salary_view(sees)" in src and "_sees_salaries_mb(u)" in src
    assert "frozenset(), sees)" in src, "a manager never shares the owner's built brief"


def test_a_salary_sensitive_goal_is_judged_on_its_basis_and_its_figures_hidden(monkeypatch):
    # G9's note: goals.progress read a manager's hourly labor % against the
    # owner's all-in target ("met") and showed the figure beside it.
    import goals
    import metrics
    seen = []
    monkeypatch.setattr(metrics, "trailing", lambda rid, key, **kw: seen.append(kw.get("with_salaries"))
                        or {"value": 31.0, "detail": "all-in"})
    monkeypatch.setattr(goals, "target_band", lambda *a, **k: {})
    monkeypatch.setattr(goals, "get_conn", lambda *a, **k: type("C", (), {
        "execute": lambda self, *a: type("R", (), {"fetchall": lambda s: [
            {"id": 1, "metric": "labor_pct", "target": 30.0, "status": "active", "deadline": None,
             "baseline_value": 33.0}]})(),
        "close": lambda self: None})())
    with models.salary_view(False):
        g = goals.progress(5)[0]
    assert seen == [True] and g["current"] is None and g["gap"] is None and g["figures_hidden"]
    assert g["state"] != "met"
