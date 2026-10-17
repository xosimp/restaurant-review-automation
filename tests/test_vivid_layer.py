"""The vivid layer (owner, 10/17/26): colour, depth and motion over the card
materials, never layout. DESIGN_SYSTEM.md section 4, "the vivid layer"."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
DOC = open(os.path.join(ROOT, "DESIGN_SYSTEM.md"), encoding="utf-8").read()


def _vivid():
    m = re.search(r'<style id="cv-vivid">(.*?)</style>', DASH, re.S)
    assert m, "the vivid layer is missing"
    return m.group(1)


def test_vivid_layer_sits_after_the_card_materials():
    assert DASH.index('<style id="cv-cards">') < DASH.index('<style id="cv-vivid">')


def test_spectrum_is_built_around_the_signature_orange():
    css = _vivid()
    assert "--vx-ember:#c84b2f" in css
    for tok in ("--vx-gain", "--vx-loss", "--vx-warn", "--vx-info", "--vx-gold", "--vx-plum", "--vx-heat", "--vx-yield"):
        assert tok + ":" in css, tok


def test_text_tokens_shared_with_ios_are_untouched():
    # The spectrum is for marks; --green / --red / --amber stay one value on both platforms.
    css = _vivid()
    assert not re.search(r"--(green|red|amber|ember|ink[23]?):", css)


def test_gauges_draw_their_scale_at_the_track_width():
    css = _vivid()
    assert "container-type:inline-size" in css
    assert "var(--vx-heat) 0 0/100cqw 100% no-repeat" in css
    assert "var(--vx-yield) 0 0/100cqw 100% no-repeat" in css


def test_glow_chart_series_match_the_spectrum():
    m = re.search(r"var GLOW_HEX = \{([^}]*)\}", DASH)
    assert m
    for hexv in ("#f2b84b", "#5aa8ff", "#c77dff"):
        assert hexv in m.group(1)
        assert hexv in _vivid()


def test_score_card_has_no_full_ember_ring_in_dark():
    css = _vivid()
    m = re.search(r'#panel-dsr \.dr-score::before\{([^}]*)\}', css)
    assert m and "conic-gradient" not in m.group(1) and "138deg" in m.group(1)


def test_instruments_and_registers_are_never_warmed():
    m = re.search(r"var STILL='([^']*)'", DASH)
    assert m and ".dr-hourly" in m.group(1) and ".dr-srv" in m.group(1)
    assert "if(el&&el.matches(STILL))el=null;" in DASH


def test_no_faint_css_radial_glow_in_the_layer():
    # Faint glows are error-diffused images (render_glows.py); the only
    # radial-gradients here are the instruments' reticle dots, the LEDs'
    # beads and the docket's punched holes.
    for m in re.finditer(r"radial-gradient\(([^;]*)", _vivid()):
        body = m.group(1)
        assert ("circle" in body) or (".8px" in body), body[:80]


def test_corner_mask_sprite_exists():
    assert os.path.exists(os.path.join(ROOT, "static", "img", "glow", "mask-corner-tl-v1.png"))
    assert '"mask-corner-tl"' in open(os.path.join(ROOT, "scripts", "render_glows.py"), encoding="utf-8").read()


def test_every_container_is_documented():
    for name in ("**Ledger**", "**Slate**", "**Instrument**", "**Ember glass**", "**Register**", "**Signal**",
                 "**Docket**", "**KPI tile**", "--vx-heat", "white-hot", "molten", "silver", "foam head", "pour line"):
        assert name in DOC, name


def test_endless_motions_never_run_layout_or_blur():
    # Food Cost lagged (owner, 10/17/26): the ember thread animated top/left
    # forever, the hero's ambient blob carried a blur filter, and the glass
    # re-blurred its backdrop every frame.
    for name in ("emberTravel", "emberTravelH", "brandAmbient"):
        m = re.search(r"@keyframes " + name + r"\{(.*?)\}\}", DASH, re.S)
        assert m, name
        assert not re.search(r"(^|[{;])\s*(top|left|width|height)\s*:", m.group(1)), name
    amb = re.search(r"\.rv2-hero:before,\.lb2-hero:before\{([^}]*)\}", DASH)
    assert amb and "filter:blur" not in amb.group(1)
    glass = re.search(r'\[data-theme="dark"\] \.lb2-ai,\[data-theme="dark"\] \.cp-ai,[^{]*\{([^}]*)\}', DASH)
    assert glass and "backdrop-filter" not in glass.group(1)


def test_sheen_is_a_fixed_top_lip():
    assert "sheen-v1.png) 0 0/100% 100%" not in DASH
    assert "sheen-v1.png) 0 0/100% 120px no-repeat" in DASH
