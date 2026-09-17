"""A follow-up round on the Labor page's Schedule header and the header/
background theming, from a fresh screenshot pass:

- `.lb2-sched` carried its own `border-top`/`padding-top` on top of the
  `.hb-sh` divider its child already draws — two hairlines stacked right
  above "Schedule" that read as one thick line with a gap.
- The heading read "built by Cavnar" with no "AI".
- Generate/Update sat beside the heading as `.hb-sh`'s second flex child;
  once Generate fired, the "Building the Week" animation landed as a
  third flex item in that same row instead of its own block underneath.
- The Generate button's own busy indicator was a plain CSS border-spinner,
  not the `hb-orb` canvas component (`CavnarOrb`) used for every other
  loading state on the page.
- Three `.hb-chip` pills (target / overstaffed days / in overtime) carry
  the generic `.stat-n` class so `animateStatNums()` counts them up on tab
  switch — but `.stat-n` is also a page-wide class for big standalone
  stat tiles at 26px, and a directly-matching rule beats an inherited one
  regardless of specificity, so those three numbers rendered at 26px
  while the "labor" and "monthly gap" chips (no `.stat-n`) correctly
  inherited `.hb-chip .v`'s 14px.
- The overstaffed/understaffed/overtime tables were still 9-13px
  throughout, untouched by the earlier text-size passes over this page.
- The header bar and the initial dark-mode background gradient used an
  old warm-tinted near-black (#0f0d0b / #0a0806 / #0e0c0a) instead of the
  neutral obsidian (#0c0c0c) already used for the html background and
  the iOS app's dark "Paper" color.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    s = _src()
    m = re.search(r"function %s\(" % re.escape(name), s) or \
        re.search(r"window\.%s\s*=\s*function\(" % re.escape(name), s)
    assert m, "function not found: " + name
    i = s.index("{", m.start())
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[m.start():j + 1]
    raise AssertionError("unbalanced braces in " + name)


def _no_tiny_sizes(body, label=""):
    tiny = re.findall(r"font-size:(?:9(?:\.\d+)?|10(?:\.\d+)?|11(?:\.\d+)?|12)px", body)
    assert not tiny, "%s still has an un-bumped size: %s" % (label, tiny[:5])


# ── the duplicated divider above "Schedule" ─────────────────────────────────

def test_lb2_sched_no_longer_draws_its_own_divider():
    css = _src()
    m = re.search(r"\.lb2-sched\{([^}]*)\}", css)
    assert m, ".lb2-sched rule not found"
    assert "border-top" not in m.group(1) and "padding-top" not in m.group(1), \
        ".lb2-sched should leave the divider to its child .hb-sh, not draw a second one"


# ── the heading text ─────────────────────────────────────────────────────────

def test_schedule_heading_says_cavnar_ai():
    assert "Next week, built by Cavnar AI<small>" in _src()


# ── buttons moved under the heading ──────────────────────────────────────────

def test_schedule_actions_immediately_follow_the_h2_not_a_sibling_of_it():
    s = _src()
    i = s.index('<div class="k">Schedule</div>')
    h2_end = s.index("</h2>", i) + len("</h2>")
    after = s[h2_end:h2_end + 40]
    assert after.startswith('<div class="lb2-actions">'), \
        "Generate/Update should sit right under the heading text now, not beside it"


# ── the Generate button's loading state ──────────────────────────────────────

def test_generate_button_uses_the_orb_not_a_plain_css_spinner():
    body = _fn("generateSchedule")
    assert 'class="hb-orb"' in body and 'data-orb-state="working"' in body
    assert "border-top-color:transparent" not in body, \
        "the old plain CSS spinner should be gone"


def test_building_the_week_lands_outside_the_button_row():
    body = _fn("generateSchedule")
    assert "closest('.lb2-actions')" in body, \
        "the week-build animation should insert after the whole actions row, " \
        "not as a flex sibling of the button that squeezed it into the row"


# ── the pulse pills sharing one font size ────────────────────────────────────

def test_chip_stat_numbers_dont_inherit_the_page_wide_26px_stat_n_size():
    css = _src()
    m = re.search(r"\.hb-chip \.v \.stat-n\{([^}]*)\}", css)
    assert m, "missing an override for .stat-n numbers inside .hb-chip .v"
    assert "font-size:14px" in m.group(1)


# ── the three staffing tables ────────────────────────────────────────────────

def test_overstaffed_understaffed_overtime_tables_have_no_tiny_text():
    s = _src()
    start = s.index("<!-- Two col: overstaffed table + overtime alerts -->")
    end = s.index("/lb-panel-schedule", start)
    _no_tiny_sizes(s[start:end], label="the overstaffed/understaffed/overtime block")


# ── obsidian black replacing the old jet black ───────────────────────────────

def test_old_jet_black_values_are_gone():
    s = _src()
    for old in ("#0f0d0b", "#0a0806"):
        assert old not in s, "old jet-black value %s should be replaced by obsidian #0c0c0c" % old
    assert 'content="#0e0c0a"' not in s, "theme-color meta should match the obsidian header now"


def test_dark_header_uses_obsidian():
    """A later round moved the page background itself off #0c0c0c onto
    #1a1714 (var(--paper), the login card's tone) for reading as too harsh
    a jet black — the header was deliberately left alone at the time, but
    the ask came back around to make the header match too."""
    css = _src()
    assert "--hdr-bg:#1a1714" in css


# ── page background: softer obsidian + smoother gradient ────────────────────
# A follow-up round: #0c0c0c itself started reading as a harsh "jet black"
# once seen next to the login screen's card, which sits on var(--paper)
# (#1a1714) — the same warm near-black used throughout the app's own dark
# theme. The page background matches that token instead of a flat literal
# black.
#
# The background then became a layered CSS system (BACKGROUND SYSTEM in
# dashboard.html): one fixed body::before canvas — grain, vignette, a side
# glow, the module's key glow, a floor lift, and a base with a top-to-bottom
# depth change — with each module contributing only its ambient accent via
# data-page on <html>. setPageBg() no longer paints anything itself.

_PAGES = ("home", "reviews", "labor", "inventory", "marketing", "competitor", "account")


def _main_css():
    # The first <style> is the optional one-line brand-colour override;
    # the theme sheet is the one that declares the dark tokens.
    s = _src()
    i = s.index("<style>\n*,*::before,*::after{box-sizing")
    return s[i:s.index("</style>", i)]


def _bg_css():
    s = _src()
    i = s.index("body::before{")
    return s[i:s.index("}", i)]


def test_page_background_uses_the_warm_paper_black_not_flat_jet_black():
    s = _src()
    css = _main_css()
    assert "#0c0c0c" not in css.split("BACKGROUND SYSTEM", 1)[1].split("/* Header and tab strip", 1)[0]
    # The base token sits ~3 levels under #1a1714 because the dither tile
    # (below) adds a mean ~3.5 levels on top; the composite is #1a1714.
    assert "--bg-base:#171411" in css and "--bg-base-hi:#1a1714" in css
    assert "html{background:var(--bg-base)" in css
    # The pre-paint script sets the theme and the page accent — nothing else.
    pre_paint = s.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "'data-theme','dark'" in pre_paint and "data-page" in pre_paint
    assert "backgroundImage" not in pre_paint


def test_page_background_is_css_not_painted_by_setPageBg():
    fn = _fn("setPageBg")
    assert "backgroundImage" not in fn and "backgroundColor" not in fn
    assert "setAttribute('data-page'" in fn
    for page in _PAGES:
        assert page + ":1" in fn, page


def test_page_background_is_one_fixed_canvas_with_the_expected_layers():
    """position:fixed on a pseudo-element, not background-attachment:fixed
    (which repaints every scroll frame on Safari) and not extra DOM."""
    css = _bg_css()
    assert "position:fixed" in css and "z-index:-1" in css and "pointer-events:none" in css
    assert "background-attachment" not in css
    assert "var(--bg-grain)" in css                       # grain
    assert "rgba(0,0,0,0.22) 100%" in css                 # vignette
    assert "var(--bg-accent2)" in css                     # side glow
    assert "var(--bg-accent)" in css                      # key glow
    assert "rgba(240,235,224,0.04)" in css                 # floor lift
    assert "var(--bg-base-hi)" in css and "var(--bg-base-lo)" in css  # base depth
    assert "background-blend-mode:normal,normal" in css, \
        "the grain must blend normal — overlay scales with a near-black base and dithers nothing"


def test_grain_tile_is_a_real_dither_not_an_invisible_overlay():
    """Bands between 8-bit levels are only removed by per-pixel noise of
    about a level in absolute terms. The committed tile is white at a
    random 0–7/255 alpha (scripts/gen_bg_grain.py); pin that so nobody
    quietly turns it back down to something that looks like nothing and
    does nothing."""
    from PIL import Image
    path = os.path.join(ROOT, "static", "bg", "grain.png")
    assert os.path.exists(path), "run scripts/gen_bg_grain.py"
    im = Image.open(path)
    assert im.mode == "LA"
    lum = [p[0] for p in im.get_flattened_data()] if hasattr(im, "get_flattened_data") else [p[0] for p in im.getdata()]
    alpha = [p[1] for p in im.get_flattened_data()] if hasattr(im, "get_flattened_data") else [p[1] for p in im.getdata()]
    assert set(lum) == {255}, "dither pixels are white; the alpha carries the noise"
    assert min(alpha) == 0 and 5 <= max(alpha) <= 10, (min(alpha), max(alpha))
    assert 2.5 <= sum(alpha) / len(alpha) <= 5
    css = _main_css()
    assert "--bg-grain:url('/static/bg/grain.png')" in css
    assert "feTurbulence type=" not in css   # the SVG filter tile is gone (the comment may still name it)


def test_every_fade_is_cosine_sampled_not_a_handful_of_kinks():
    """A slope change at every stop reads as a faint curved line across the
    page. Each radial fade is sampled from a cosine at 8+ stops so no single
    stop carries a visible kink, and each ends on `transparent` at zero
    slope rather than a hard edge."""
    css = _bg_css()
    fades = re.findall(r"radial-gradient\((.*?)\),\n", css)
    assert len(fades) == 4, fades
    for fade in fades:
        stops = re.findall(r"(?:\)|transparent|[0-9]) (\d+(?:\.\d+)?)%(?=,|$)", fade)
        assert len(stops) >= 9, "fade with too few stops to be smooth: %s" % fade[:80]
        assert fade.rstrip().endswith("transparent %s%%" % stops[-1]) or "0.22) 100%" in fade


def test_page_background_key_glow_has_enough_stops_to_avoid_banding():
    css = _bg_css()
    assert "at 50% -8%," in css, "key glow not found in body::before"
    glow = css.split("at 50% -8%,", 1)[1].split("\n", 1)[0]
    stops = re.findall(r"transparent\)\s*\d+(?:\.\d+)?%|transparent \d+%", glow)
    assert len(stops) >= 7, "gradient should have enough stops for a smooth falloff, found %r" % stops


def test_every_module_has_its_own_ambient_accent_and_they_crossfade():
    css = _main_css()
    for page in _PAGES[1:]:
        assert 'html[data-page="%s"]{--bg-accent:#' % page in css, page
    # Home is the :root default — brand ember.
    assert "--bg-accent:#c84b2f;--bg-accent2:#e8956a" in css
    assert "@property --bg-accent{syntax:'<color>'" in css
    assert "transition:--bg-accent .8s ease,--bg-accent2 .8s ease" in css
    assert "@media (prefers-reduced-motion:reduce){body::before{transition:none}}" in css


def test_containers_share_one_elevation_system():
    """Cards float over the canvas through shared tokens, not per-card
    shadow literals — one place to tune how far everything floats."""
    s = _src()
    css = _main_css()
    assert "--elev-1:" in css and "--elev-2:" in css and "--surface-glass:" in css
    for sel in ('[data-theme="dark"] .card{', '[data-theme="dark"] .acct-card{', '[data-theme="dark"] .ac-card{'):
        i = s.index(sel)
        rule = s[i:s.index("}", i)]
        assert "var(--elev-1)" in rule and "var(--surface-glass)" in rule, sel
