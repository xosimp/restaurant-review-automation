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


# ── page background: from decoration to a flat workspace tone ───────────────
# Three rounds, in order: (1) #0c0c0c itself read as a harsh jet black next
# to the login card's #1a1714, so the page background moved to that token;
# (2) it became a layered body::before canvas — grain, vignette, a side
# glow, a per-module key glow via data-page, a floor lift — smoothed twice
# over for banding; (3) that whole system was the actual problem: a radial
# glow plus a film-grain dither is decoration competing with the content it
# sits behind, however carefully tuned. It is gone. The workspace
# background is now one flat, matte `html{background:var(--bg-base)}` —
# no pseudo-element, no gradient, no grain, no per-module accent, nothing
# that can band because there is no adjacent shade to band against.
# setPageBg() is an intentional no-op kept only as a seam for call sites.

_PAGES = ("home", "reviews", "labor", "inventory", "marketing", "competitor", "account")


def _main_css():
    # The first <style> is the optional one-line brand-colour override;
    # the theme sheet is the one that declares the dark tokens.
    s = _src()
    i = s.index("<style>\n*,*::before,*::after{box-sizing")
    return s[i:s.index("</style>", i)]


def test_workspace_background_is_one_flat_tone_with_no_decoration():
    """The whole point of the rewrite: nothing left to band, glow, or draw
    the eye. No pseudo-element canvas, no gradient/radial/grain/vignette,
    no per-module accent variable, no @property color animation."""
    css = _main_css()
    assert "html{background:var(--bg-base)" in css
    assert "--bg-base:#141110" in css
    # Scoped to the workspace-background block itself (through the next
    # unrelated rule) — a blanket search over the whole stylesheet would
    # also flag other components' own, unrelated gradients (e.g. the Ask
    # panel's), which is not what this test is about.
    block = css.split("WORKSPACE BACKGROUND", 1)[1].split("/* Review status badge", 1)[0]
    for gone in (
        "body::before", "background-image:", "radial-gradient(", "linear-gradient(180deg,var(--bg-base",
        "--bg-accent", "--bg-grain", "data-page", "@property", "feTurbulence", "grain.png",
    ):
        assert gone not in block, "workspace background should carry no decoration: found %r" % gone
    assert not os.path.exists(os.path.join(ROOT, "static", "bg", "grain.png"))
    assert not os.path.exists(os.path.join(ROOT, "scripts", "gen_bg_grain.py"))


def test_pre_paint_script_only_sets_the_theme():
    """The pre-paint <script> used to also stamp a per-page accent (first
    from data-page, keyed off the URL hash) before first paint. With no
    per-page accent left to avoid flashing, it should do exactly one thing."""
    s = _src()
    pre_paint = s.split("<script>", 1)[1].split("</script>", 1)[0]
    assert pre_paint.strip() == "document.documentElement.setAttribute('data-theme','dark');"


def test_setPageBg_is_an_intentional_noop():
    fn = _fn("setPageBg")
    assert "setAttribute" not in fn and "backgroundImage" not in fn and "backgroundColor" not in fn
    # Still called from every tab switch — the seam stays even though it
    # currently does nothing, so no call site needed to change.
    s = _src()
    assert s.count("setPageBg(") >= 3


def test_header_and_tabs_match_the_workspace_background_not_a_separate_literal():
    """The header/tab bar used to be its own literal rgba(26,23,20,.86) —
    close to --bg-base but not derived from it, so the two could drift
    apart the next time either changed. It now reads --bg-base directly
    (via color-mix, so it stays a translucent PLANE above the canvas
    rather than becoming fully opaque and hiding it) so header, tabs and
    canvas are provably the same tone."""
    css = _main_css()
    assert "color-mix(in srgb,var(--bg-base) 86%,transparent)" in css
    assert 'rgba(26,23,20,.86)' not in css
    rule_start = css.index('[data-theme="dark"] .hdr,[data-theme="dark"] .tabs{')
    rule = css[rule_start:css.index("}", rule_start)]
    assert "var(--bg-base)" in rule


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


# ── Reviews tab decoration pass ──────────────────────────────────────────

def _reviews_panel():
    s = _src()
    i = s.index('<div class="panel" id="panel-reviews"')
    return s[i:s.index('<div class="panel', i + 1)]


def test_reviews_tab_has_no_kicker_line_above_the_headline():
    """'Reviews · {date} · {restaurant}' sat above the H1 as a small orange
    line that repeated information already in the header bar and the '·'
    separated hb-sub row just below it — removed."""
    panel = _reviews_panel()
    assert "Reviews · {{ now_mdy }}" not in panel
    assert '<h1 class="hb-h1">Reviews. ' in panel


def test_reviews_tab_has_no_cavnar_read_container():
    """The whole 'Cavnar's read on your reviews' AI-summary card is gone —
    markup, its two CSS rules, and its loading state. loadReviewInsight()
    still exists (its callers also use it to gate loading the sentiment
    trend / topic heatmap next to it) but early-returns now that
    #review-insight is gone, so no fetch ever fires for it."""
    panel = _reviews_panel()
    assert 'class="rv2-ai"' not in panel
    assert "Cavnar's read on your reviews" not in panel
    assert 'id="review-insight"' not in panel
    s = _src()
    assert ".rv2-ai{" not in s and ".rv2-ai .tag{" not in s
    fn = _fn("loadReviewInsight")
    assert "if(!el)return;" in fn


def test_reviews_kicker_and_inbox_label_match_food_costs_bumped_size():
    """'Rating · last 8 weeks' and 'Inbox' are the same small-caps orange
    heading Food Cost already bumped (#panel-inventory .hb-kicker); Reviews
    now matches it for both its own .hb-kicker instances and the
    differently-classed 'Inbox' section label (.hb-sh .k)."""
    s = _src()
    assert "#panel-inventory .hb-kicker{font-size:12.5px}" in s
    assert "#panel-reviews .hb-kicker,#panel-reviews .hb-sh .k{font-size:12.5px}" in s
    panel = _reviews_panel()
    assert '<div class="hb-kicker">Rating · last 8 weeks</div>' in panel
    assert '<div class="k">Inbox</div>' in panel


def test_reviews_top_pills_have_no_ember_glow():
    """The 4 hero pills (rating, answered, to approve, urgent) lose their
    box-shadow glow on this page only — other modules that still use
    .hb-chip (Home, Labor, Food Cost, Marketing, Intel, Account) keep it;
    this is a page-scoped override, not a change to the shared class."""
    s = _src()
    assert "#panel-reviews .hb-chip,#panel-reviews .hb-chip:hover{box-shadow:none}" in s
    # The shared .hb-chip glow itself must still exist for every other page.
    assert re.search(r"\.hb-chip\{[^}]*box-shadow:0 0 18px rgba\(200,75,47,", s)
