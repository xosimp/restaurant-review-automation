"""The sign-in / password pages' living background — a port of the iOS
LoginBackground (static/cavnar-field.js): three drifting ember blooms, a
36-point constellation over the top 72% with hairlines inside 110px, a
vignette to Paper, 30fps cap, frozen under reduced motion, paused while
the tab is hidden, and a first frame painted synchronously at mount (a
tab that loads hidden must not sit on bare Paper).

The old CSS ember glows and film-grain overlay it replaces must stay gone.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "cavnar-field.js")
PAGES = ["login.html", "forgot_password.html", "reset_password.html", "reset_success.html"]


def _src(p):
    return open(p, encoding="utf-8").read()


def test_the_port_keeps_the_ios_constants():
    js = _src(JS)
    assert "var COUNT = 36, LINK = 110, FIELD_H = 0.72;" in js
    assert "var FRAME = 1000 / 30;" in js
    assert "makeRng(0x9E3779B9, 0x7F4A7C15)" in js, "same LCG seed as LoginConstellation, same sky every load"
    for bloom in ("{ x: 0.22, y: 0.05, r: 0.80, c: EMBER,  a: 0.66, period: 14, phase: 0 }",
                  "{ x: 0.95, y: 0.28, r: 0.64, c: EMBER2, a: 0.42, period: 18, phase: 2.1 }",
                  "{ x: 0.50, y: 1.05, r: 0.75, c: EMBER,  a: 0.34, period: 9,  phase: 4.2 }"):
        assert bloom in js
    assert "EMBER = [212, 88, 58]" in js and "EMBER2 = [232, 149, 106]" in js


def test_it_paints_before_the_loop_and_respects_motion_and_visibility():
    js = _src(JS)
    mount = js[js.index("function mount("):]
    assert "prefers-reduced-motion: reduce" in mount
    assert "else { drawAurora(1000, false); drawSky(1000); start(); }" in mount, \
        "a hidden tab never gets its first frame from the rAF loop"
    assert "visibilitychange" in mount and "document.hidden" in mount


def test_es5_only():
    js = _src(JS)
    for bad in (r"\bconst\b", r"\blet\b", r"=>", r"`"):
        assert not re.search(bad, js), bad


@pytest.mark.parametrize("name", PAGES)
def test_each_page_mounts_the_field_and_dropped_the_css_glows(name):
    t = _src(os.path.join(ROOT, "templates", name))
    assert '<canvas id="cf-aurora" class="cf-layer"' in t and '<canvas id="cf-sky" class="cf-layer"' in t
    assert '<div class="cf-vignette"' in t
    assert '<script src="/static/cavnar-field.js"></script>' in t
    assert "window.cavnarField=CavnarField.mount({aurora:document.getElementById('cf-aurora'),sky:document.getElementById('cf-sky')})" in t
    assert "body::before" not in t and "body::after" not in t
    assert "background:#0c0c0c;" in t, "Paper (dark) under the field, matching iOS"
    assert ".cf-vignette{position:fixed" in t and ".card{z-index:1}" in t
