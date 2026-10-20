"""Small per-load extras (scale audit 10/19/26 FE-14): the guest QR is drawn
once an hour per browser and only when its panel is shown; a tab switch logs
a tab view at most once per tab per five minutes."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_marketing_qr_is_lazy():
    assert '<img src="/api/guest-qr" alt="QR code guests scan to join" width="164" height="164" loading="lazy">' in _src()


def test_a_tab_view_is_logged_once_per_tab_per_five_minutes():
    src = _src()
    body = src.split("function switchTab(n,btn){", 1)[1].split("\n}\n", 1)[0]
    assert "if(cavStale('log-activity:'+n,300000))fetch('/api/log-activity'" in body


def test_the_qr_route_lets_the_browser_keep_it():
    src = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    body = src.split("def guest_qr_code(current_user):", 1)[1].split("\n\n\n", 1)[0]
    assert 'resp.headers["Cache-Control"] = "private, max-age=3600"' in body
    assert "if not download:" in body
