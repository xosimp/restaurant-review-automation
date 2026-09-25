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
    """dashboard.html plus the review-card partial it includes.

    One review row moved into templates/_review_card.html so the first page
    and every "Load more" page render identical markup; every assertion in
    this file predates that split and is written against "the page", so the
    partial is appended rather than made every caller's problem. Appending
    keeps all the index-based slicing below valid."""
    card = os.path.join(ROOT, "templates", "_review_card.html")
    body = open(DASHBOARD, encoding="utf-8").read()
    if os.path.exists(card):
        body += open(card, encoding="utf-8").read()
    return body


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
    # "drafted", never "optimized" (never-say C, NS1 #11).
    assert "Next week, drafted by Cavnar AI<small>" in _src()
    assert "optimized to your" not in _src()


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


def test_header_and_tabs_match_the_workspace_background_exactly():
    """First the header/tab bar was its own literal rgba(26,23,20,.86);
    then it read --bg-base but translucent + blurred, which still never
    matched pixel-for-pixel — what showed through the blur changed with
    whatever had scrolled underneath, so the header visibly drifted from
    the flat canvas depending on scroll position. It's now solid
    var(--bg-base) at full opacity: the only way the two can match
    exactly is being the same paint, not a translucent read of it."""
    css = _main_css()
    rule_start = css.index('[data-theme="dark"] .hdr,[data-theme="dark"] .tabs{')
    rule = css[rule_start:css.index("}", rule_start)]
    assert rule == '[data-theme="dark"] .hdr,[data-theme="dark"] .tabs{background:var(--bg-base)!important'
    assert "backdrop-filter" not in rule and "color-mix" not in rule
    assert 'rgba(26,23,20,.86)' not in css


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

REVIEW_CARD = os.path.join(ROOT, "templates", "_review_card.html")


def _review_card():
    """One review row's markup. It lives in its own partial now so the
    first page (rendered into dashboard.html) and every "Load more" page
    (client_api's /api/reviews/page) render the identical card instead of
    a second copy hand-written in JS."""
    return open(REVIEW_CARD, encoding="utf-8").read()


def _reviews_panel():
    s = _src()
    i = s.index('<div class="panel" id="panel-reviews"')
    # The partial is part of this panel as far as every assertion here is
    # concerned — it is {% include %}d into it.
    return s[i:s.index('<div class="panel', i + 1)] + _review_card()  # noqa: E501


def test_reviews_tab_has_no_kicker_line_above_the_headline():
    """'Reviews · {date} · {restaurant}' sat above the H1 as a small orange
    line that repeated information already in the header bar and the '·'
    separated hb-sub row just below it — removed."""
    panel = _reviews_panel()
    assert "Reviews · {{ now_mdy }}" not in panel
    assert '<h1 class="hb-h1">Reviews. ' in panel


def test_the_cavnar_read_card_is_on_the_reviews_tab():
    """REVERSES an earlier deliberate removal of this card.

    It was taken out in a decluttering pass, and the audit found what that
    left behind: /api/review-insight is the only thing in the module that
    says WHY a number moved and what to do about it, and with the card gone
    loadReviewInsight() early-returned on the missing element, so the
    endpoint — prompt, figure-verification guard and all — reached nobody
    on web, while iOS fetched it and never rendered it. Home's own AI line
    reads the same 5-minute cache and so was almost always cold too.

    It uses the .lb2-ai treatment Labor and Intel already share rather than
    reinstating the old rv2-ai rules."""
    panel = _reviews_panel()
    assert "Cavnar AI's read on your reviews" in panel
    assert 'id="review-insight"' in panel
    assert 'class="lb2-ai"' in panel
    fn = _fn("loadReviewInsight")
    assert "fetch('/api/review-insight')" in fn
    # Every caveat the server attaches rides on it — unverified figures,
    # unverified names and staleness now go through one call.
    assert "applyInsightCaveats(el,d)" in fn
    caveats = _fn("applyInsightCaveats")
    assert "applyFigureCaveat(el,d)" in caveats
    assert "names_verified" in caveats
    # And the root-cause card, which is the point of the endpoint now: a
    # snapshot the owner can already read off their own screen is not worth
    # an AI call, and restating one was all this card used to do.
    assert "renderReviewDiagnosis(d)" in fn
    assert "renderSeverityStrip(d)" in fn
    assert 'id="review-diagnosis"' in panel
    # One global per name: a second `function renderDiagnosis(` in the same
    # script used to shadow the Reviews one, and this card never rendered.
    src = _src()
    assert src.count("function renderReviewDiagnosis(") == 1
    assert src.count("function renderDiagnosis(") == 1


def test_reviews_kicker_and_inbox_label_match_food_costs_bumped_size():
    """'Rating · last 8 weeks' and 'Inbox' are the same small-caps orange
    heading Food Cost already bumped (#panel-inventory .hb-kicker); Reviews
    now matches it for both its own .hb-kicker instances and the
    differently-classed 'Inbox' section label (.hb-sh .k)."""
    s = _src()
    assert "#panel-inventory .hb-kicker{font-size:12.5px}" in s
    assert "#panel-reviews .hb-kicker,#panel-reviews .hb-sh .k{font-size:12.5px}" in s
    panel = _reviews_panel()
    # The kicker names what the big number is (confidence audit J7): Google's
    # all-time rating when there is one, never "last 8 weeks" over it.
    assert ("<div class=\"hb-kicker\">{{ 'Google rating (all time)' if restaurant.gbp_rating"
            " else 'Average rating · all reviews' }}</div>") in panel
    assert '<div class="k">Inbox</div>' in panel


def test_no_pill_has_an_ember_glow_or_border_on_any_module():
    """The hero pills' ember box-shadow glow is gone everywhere .hb-chip is
    used (Home, Reviews, Labor, Food Cost, Marketing, Intel, Account all
    share the one class — a Reviews-only override existed for one turn;
    the fix belongs on the shared rule). Went a round further: the orange
    BORDER ring is gone too, replaced by a border the same tone as the
    canvas (var(--bg-base)) — definition now comes entirely from a real
    (colorless) drop shadow lifting the pill off the page, not from any
    accent-colored outline or glow."""
    s = _src()
    rule = re.search(r"\.hb-chip\{[^}]*\}", s).group(0)
    hover_rule = re.search(r"\.hb-chip:hover\{[^}]*\}", s).group(0)
    assert "rgba(200,75,47," not in rule and "rgba(200,75,47," not in hover_rule, \
        "no ember-colored glow on the pill itself"
    assert "rgba(232,149,106," not in rule, "no orange border ring"
    assert "border:1px solid var(--bg-base)" in rule
    assert "box-shadow:0 10px 24px rgba(0,0,0,.45),0 3px 8px rgba(0,0,0,.35)" in rule
    assert "box-shadow:0 14px 32px rgba(0,0,0,.5),0 4px 10px rgba(0,0,0,.4)" in hover_rule
    assert "#panel-reviews .hb-chip,#panel-reviews .hb-chip:hover{box-shadow:none}" not in s, \
        "page-scoped override should be gone now that the shared rule itself has no glow"
    # The .dot i/.dot b status-indicator glow is a different, small,
    # functional thing (not "the orange glow behind the pill") and stays.
    assert ".hb-chip .dot i{position:absolute;inset:0;border-radius:50%;background:var(--ember);box-shadow:0 0 6px var(--ember)}" in s


# ── Reviews "3 charts" row — bigger, one chart repositioned, no double line ──

def test_rv2_sig_three_columns_are_bigger():
    """The label, big number, caption and both sub-chart's bar/row text
    across all 3 columns (Reviews · per week / How replies got approved /
    What guests talk about) were sized for a much quieter card than they
    now sit in — bumped up across the board."""
    css = _src()
    assert ".rv2-sg .k{font-size:13px" in css
    assert ".rv2-sg .v{font-family:'Space Grotesk',sans-serif;font-size:38px" in css
    assert ".rv2-sg .v small{font-family:'Apfel Grotezk',sans-serif;font-size:14px" in css
    assert ".rv2-sg svg{display:block;width:100%;height:108px" in css
    assert ".rv2-sg .cap{font-size:13px" in css
    assert ".rv2-perf .row{display:grid;grid-template-columns:124px 1fr 62px;gap:10px;align-items:center;font-size:13.5px" in css
    assert ".rv2-perf .bar{height:13px" in css
    assert ".rv2-topic{display:grid;grid-template-columns:1fr 130px 40px;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--hb-line2);background:none;border-left:none;border-right:none;border-bottom:none;font-family:inherit;text-align:left;cursor:pointer;color:var(--ink);font-size:14px" in css
    assert ".rv2-topic .bar{height:8px" in css


def test_reviews_per_week_chart_moves_down_without_moving_its_number():
    """Only #sentiment-trend-bars (the bar chart itself) gets pushed down
    in its column — the 'k' label and the big 'v' number/caption above it
    (shared .rv2-sg rules, unscoped) are untouched, and the other two
    columns' own chart containers (#perf-body, #heatmap-grid) get no such
    margin."""
    css = _src()
    assert "#sentiment-trend-bars{margin-top:22px}" in css
    assert "#perf-body{margin-top" not in css
    assert "#heatmap-grid{margin-top" not in css


def test_rv2_inbox_no_longer_draws_a_duplicate_divider():
    """.rv2-inbox and its child .hb-sh ('Inbox') both drew a full
    margin-top/padding-top/border-top divider — two hairlines with a gap
    between them reading as one thick double line, the same bug already
    fixed on Labor's .lb2-sched. The divider now belongs to .hb-sh alone;
    .rv2-inbox carries no spacing of its own beyond what its child needs,
    and the inline style that used to zero out .hb-sh's own margin (to
    compensate for the parent's) is gone since the parent no longer has
    one to compensate for."""
    s = _src()
    m = re.search(r"\.rv2-inbox\{([^}]*)\}", s)
    assert m, ".rv2-inbox rule not found"
    assert "border-top" not in m.group(1) and "padding-top" not in m.group(1)
    # (the inbox gained its nav anchor, Friction #33)
    assert re.search(r'<div id="rv-inbox-reviews" class="rv2-inbox"[^>]*>\n    <div class="hb-sh"><div><div class="k">Inbox</div>', s)


def test_rv2_hero_kicker_has_space_before_the_rating_number():
    """'Rating · last 8 weeks' sat flush against the 3.6 below it — the
    base .hb-kicker rule carries no margin, and the rule that does add
    one (.hb-hero .hd .hb-kicker) targets a different ancestor class
    (.hb-hero, used on Home) than this hero actually has (.rv2-hero)."""
    css = _src()
    assert ".rv2-hero .hd .hb-kicker{margin-bottom:12px}" in css


def test_topic_heatmap_bars_animate_in_like_the_response_performance_bars():
    """The 3rd column's bars ('What guests talk about') were the only ones
    of the 3 charts in this row that rendered fully filled on load instead
    of growing in — now reuses the exact @keyframes rv2Grow the 2nd
    column's bars (.rv2-perf .bar i) already animate with, rather than a
    separate keyframes rule."""
    css = _src()
    assert re.search(
        r"\.rv2-topic \.bar i\{display:block;height:100%;transform-origin:left;"
        r"animation:rv2Grow \.7s cubic-bezier\(\.4,0,\.2,1\) both\}",
        css,
    )
    assert css.count("@keyframes rv2Grow{") == 1, "should reuse the one existing keyframes, not add a second"


# ── Retract button shown when it would always fail ───────────────────────

def test_retract_button_requires_a_real_google_review_name_not_just_platform():
    """_do_retract (client_api.py) refuses to retract unless
    response_status=='posted' AND platform=='google' AND review_name is
    set — review_name is the Business Profile API resource name, only
    present when OUR auto-post flow actually put the reply live. The
    button used to show for any platform=='google' review with
    response_status=='posted', regardless of review_name — an imported or
    manually-marked-posted review (no review_name) showed "Live on
    Google" AND a Retract button that always 400ed with "only supported
    for auto-posted Google replies"."""
    s = _src()
    i = s.index('<span class="rv2-status live">✓ Live on {{ r.platform|title }}</span>')
    after = s[i:i + 900]
    # The same gate, computed once server-side and exposed per review as
    # can_retract, so the phone can apply it too — iOS could not, because
    # Review.swift deliberately doesn't decode review_name, so it offered
    # "Retract from Google" on every posted review including Yelp ones.
    assert "{% if r.can_retract %}" in after
    assert "onclick=\"retractR({{ r.id }},this)\">Retract</button>{% endif %}" in after
    import models, inspect as _i
    gd = _i.getsource(models.get_reviews_data)
    assert 'd["can_retract"] = bool(' in gd
    assert 'd.get("response_status") == "posted"' in gd
    assert 'd.get("platform") == "google"' in gd
    assert 'd.get("review_name")' in gd
    # The backend gate this must match:
    import client_api
    import inspect
    src = inspect.getsource(client_api._do_retract)
    assert 'row["response_status"] != "posted"' in src
    assert 'row["platform"] != "google" or not row["review_name"]' in src


# ── Intel: redundant "Check AI visibility" button removed ────────────────

def test_intel_top_actions_no_longer_duplicate_the_aiv_section_button():
    """The AI Visibility section already has its own primary (orange)
    'Check AI visibility' button (#aiv-run-btn) that runs the real check.
    A second, secondary-styled one sat in the page's top action row doing
    the same thing via in2Jump + a synthetic click on that same button —
    pure duplication, not a different action."""
    s = _src()
    start = s.index('<h1 class="hb-h1">Intel. ')
    top = s[start:s.index('<div class="hb-pulse">', start)]
    assert "Check AI visibility" not in top
    # Refresh competitors and Track a competitor went too (9/24/26): the
    # neighbors section has its own Refresh and search box, and the empty
    # state its own Fetch button, so the header row duplicated both.
    assert "lb2-actions" not in top and "refreshCompetitorIntel" not in top and "Track a competitor" not in top
    neighbors = s[s.index('id="intel-search"') - 600:s.index('id="intel-search"')]
    assert "refreshCompetitorIntel(this)" in neighbors
    assert s.count('<button onclick="refreshCompetitorIntel(this)" class="cbtn cbtn-primary cbtn-lg">Fetch competitor data</button>') == 1
    # The real button stays exactly once.
    assert s.count("Check AI visibility") == 1
    assert 'id="aiv-run-btn"' in s and 'cbtn-primary cbtn-lg' in s


# ── Reviews pill colors: real amber, and a real threshold on "to approve" ────

def test_stat_glow_amber_is_actually_amber_not_the_brand_orange():
    """Named "amber" but colored var(--ember) — the brand orange used for
    urgent/action-now states — so a 3-point-something rating and a
    couple of pending approvals (both "warn" tier) read exactly like an
    urgent alert. Now the real amber/warn token."""
    s = _src()
    m = re.search(r"\.stat-glow-amber\{([^}]*)\}", s)
    assert m, ".stat-glow-amber rule not found"
    assert "var(--amber)" in m.group(1)
    assert "var(--ember)" not in m.group(1)


def test_to_approve_pill_is_green_only_at_zero_with_a_named_threshold():
    """Green used to mean nothing on this number — 0 pending rendered in
    plain ink, not green, while amber/red both had a color. Now: 0 is the
    only green case, 1..threshold is amber, >threshold is red — server
    render and the client-side live updater (setPendingClass, after an
    in-place approve) share the same threshold value instead of two
    independently-maintained magic numbers."""
    s = _src()
    assert "{% set _await_threshold = 2 %}" in s
    i = s.index("hb-chip {{ 'bad' if _await")
    pill = s[i:s.index('</span>\n      <span class="hb-chip', i)]
    assert "'stat-glow-red' if _await > _await_threshold else ('stat-glow-amber' if _await > 0 else 'stat-glow-green')" in pill
    assert "'bad' if _await > _await_threshold else ('warn' if _await > 0 else 'good')" in pill
    fn = _fn("setPendingClass")
    assert "PENDING_APPROVAL_WARN_THRESHOLD = 2" in s
    assert "count===0 ? 'stat-glow-green' : count<=PENDING_APPROVAL_WARN_THRESHOLD ? 'stat-glow-amber' : 'stat-glow-red'" in fn


# ── Reviews 3-column row: bottom-aligned captions ─────────────────────────

def test_rv2_sg_columns_are_flex_so_captions_align_at_the_bottom():
    """.rv2-sig's grid already stretches every .rv2-sg to the row's full
    height (set by the tallest column, the 6-row topic list) — but each
    column's own trailing .cap caption used to sit right after its own
    content instead of at the bottom of that stretched cell, so a shorter
    chart's caption ('positive/negative') landed above 'click a topic to
    filter the inbox' instead of level with it. .rv2-sg is now a flex
    column and .cap anchors to the bottom of it via margin-top:auto."""
    s = _src()
    assert re.search(r"\.rv2-sg\{display:flex;flex-direction:column;", s)
    m = re.search(r"\.rv2-sg \.cap\{([^}]*)\}", s)
    assert m, ".rv2-sg .cap rule not found"
    assert "margin-top:auto" in m.group(1)


# ── Reviews: lighter neutral gray in both charts ──────────────────────────

def test_neutral_segments_use_the_sand_text_color_in_both_charts():
    """Both charts' 'neutral' (not pos/neg) segment went through two
    rounds: first a translucent currentColor/no-fill-at-all treatment that
    read as a dark, low-contrast gray on the flat canvas; then a plain
    lighter gray. Now it's the same sand tone (--ink3) used for small text
    everywhere else in the app, solid — matching the pos/neg bars' own
    solid fills — and identical between the two charts."""
    fn = _fn("stacked")
    assert 'fill="var(--ink3)"' in fn
    assert 'fill-opacity=".25"' not in fn and 'fill-opacity=".45"' not in fn
    s = _src()
    m = re.search(r"\.rv2-topic \.bar \.mid\{([^}]*)\}", s)
    assert m, ".rv2-topic .bar .mid rule not found"
    assert m.group(1) == "background:var(--ink3)"
    fn = _fn("loadTopicHeatmap")
    assert '<i class="mid" style=' in fn, "the middle segment must carry the .mid class to pick up the fill"


def test_both_charts_have_a_neutral_legend_swatch():
    """The sand neutral segment had no legend entry in either chart's
    caption row, next to the positive/negative swatches that were
    already there. Both legends use the same short pos/neg/neu labels —
    "Reviews · per week" used to spell them out while the topic heatmap
    right next to it abbreviated, so the two didn't match."""
    s = _src()
    assert '<span style="color:var(--hb-good)">■</span> pos &nbsp; <span style="color:var(--hb-bad)">■</span> neg &nbsp; <span style="color:var(--ink3)">■</span> neu</span><span id="sentiment-trend-labels">' in s
    assert '<span style="color:var(--hb-good)">■</span> pos &nbsp; <span style="color:var(--hb-bad)">■</span> neg &nbsp; <span style="color:var(--ink3)">■</span> neu</span></div></div>' in s


# ── Reviews: approve/skip update the UI immediately, no reload needed ────

def test_approving_an_auto_posted_reply_swaps_the_ribbon_to_posted_not_approved():
    """auto_posted replies used to only ever gain the 'approved' class —
    never swapped to 'posted' — so the ribbon showed the wrong color
    (approved's) until the next full reload re-rendered the row from the
    real DB state."""
    fn = _fn("approveR")
    auto = fn[fn.index("if(d.auto_posted){"):fn.index("} else {")]
    assert "card.classList.remove('approved');" in auto
    assert "card.classList.add('posted');" in auto


def test_approve_and_skip_both_reload_the_response_performance_card():
    """'How replies got approved' only ever loaded on first tab-open or a
    full page reload — an approve (auto-posted or not) never told it
    anything changed. Reloads at whatever day-range is currently
    selected, not hardcoded back to 90d."""
    s = _src()
    assert "function _reloadResponsePerfCurrent(){" in s
    fn = _fn("approveR")
    assert fn.count("_reloadResponsePerfCurrent();") == 2, \
        "both the auto-posted and the saved-not-yet-posted branch should reload it"


def test_skip_updates_stats_and_ribbon_immediately():
    """A skip is a real decision, not a no-op — the review leaves the
    'needs action' queue exactly like an approve does, but skipR() never
    told updateReviewStats() (which drives the 'to approve' pill and the
    tab badge) or the row's own ribbon about it."""
    fn = _fn("skipR")
    assert "card.classList.add('skipped');" in fn
    assert "updateReviewStats();" in fn


# ── Reviews: tab badge is orange, not red, and sits a touch higher ───────

def test_reviews_tab_badge_is_orange_not_red():
    s = _src()
    assert "background:#c0392b" not in s.split('id="reviews-badge"', 1)[1][:200]
    assert "background:var(--ember)!important;color:white" in s
    fn = _fn("updateReviewStats")
    assert "badge.style.setProperty('background','var(--ember)','important');" in fn
    assert "badge.style.background = '#c0392b';" not in fn


def test_reviews_tab_badge_sits_a_touch_higher():
    s = _src()
    assert ";position:relative;top:-2px}" in s
    m = re.search(r"(?<!\[data-theme=\"dark\"\] )\.tab \.badge\{([^}]*)\}", s)
    assert m, ".tab .badge (base, not the dark-theme override) rule not found"
    assert "position:relative" in m.group(1) and "top:-2px" in m.group(1)


# ── Reviews: hover color matches the row's own status, not always orange ──

def test_row_hover_color_matches_its_own_ribbon_color_per_status():
    """Every row hovered the same ember tint regardless of urgent/approved/
    posted/skipped — now each status gets a hover tint in the same color
    family as its own left-edge ribbon; a plain not-yet-decided row keeps
    the original ember tint as the only status-less case."""
    s = _src()
    assert ".rv2-row:hover{background:var(--hb-tint)}" in s, \
        "the generic (no status class) row should keep its original tint"
    assert ".rv2-row.urgent:hover{background:rgba(255,92,92,.10)}" in s
    assert ".rv2-row.approved:hover{background:rgba(53,214,124,.10)}" in s
    assert ".rv2-row.posted:hover{background:rgba(106,171,255,.10)}" in s
    assert ".rv2-row.skipped:hover{background:rgba(240,235,224,.06)}" in s


def test_skipped_reviews_get_a_gray_ribbon():
    """Skipped had no ribbon color at all before — a real, deliberate
    decision with no visual sign one was made."""
    s = _src()
    assert ".rv2-row.skipped:before{background:var(--ink3)}" in s
    assert "{{ 'skipped' if r.response_status=='skipped' }}" in s


# ── heading rename, rating pill, blue reply bubble, badge rename ─────────

def test_approval_method_heading_renamed():
    s = _src()
    assert "How replies got approved" not in s
    assert '<span>Approval Method</span>' in s


def test_rating_number_carries_no_status_color_only_the_dot_does():
    """The number used to double up on the same status signal the pill's
    own pulsing dot already gives (good/warn/bad, from the container
    class) — stat-glow-amber etc. on the number itself made an ordinary
    3-point rating read like a flashing alert. Only the dot signals now;
    the star gets its own element (and a touch of margin) instead of
    running straight into the number with no gap."""
    s = _src()
    i = s.index('id="stat-rating"')
    i = s.index(">", i) + 1
    pill = s[i:s.index("</span></span>", i) + len("</span></span>")]
    assert '<span id="stat-rating-n">{{ _rating }}</span>' in pill, \
        "the number span must carry no {{ _rglow }} class"
    assert '<span class="rating-star">★</span>' in pill
    assert "_rglow" not in s
    fn = _fn("updateReviewStats")
    rating_block = fn[fn.index("// ── Avg rating"):fn.index("// ── Responded this month")]
    assert "statGlow(ratingNEl" not in rating_block
    assert "ratingNEl.textContent = d.avg_rating;" in rating_block
    assert "#panel-reviews .rating-star{margin-left:2px}" in s


def test_live_on_google_reply_bubble_is_blue_and_applies_without_a_reload():
    """Keyed off .rv2-row.posted, which card.classList already gains the
    instant an auto-post succeeds (approveR()'s class swap, fixed
    earlier) — so this CSS rule alone makes the bubble turn blue in
    frame, no extra JS and no reload needed."""
    s = _src()
    assert "#panel-reviews .rv2-row.posted .draft-box{background:rgba(106,171,255,.14)!important}" in s
    fn = _fn("approveR")
    auto = fn[fn.index("if(d.auto_posted){"):fn.index("} else {")]
    assert "card.classList.add('posted');" in auto, \
        "posted must land on the card synchronously with the approve response for the blue bubble to apply without a reload"


def test_urgent_pill_renamed_from_needs_a_reply_now():
    s = _src()
    assert "Needs a reply now" not in s
    assert '<span class="rv2-urg">Urgent</span>' in s


# ── tab badge counts the whole actionable queue, not just drafted ────────

def test_tab_badge_counts_undrafted_reviews_too_not_just_drafted():
    """awaiting_approval alone (drafted, ready to approve) undercounted the
    inbox — a review with no draft yet (get_pending_drafts' backlog) still
    needs the owner to act on it, but never showed up in the tab badge.
    Both the server-rendered badge and the live JS poller must add
    needs_response in."""
    s = _src()
    assert "{% set _badge_n = (rstats.awaiting_approval or 0) + (rstats.needs_response or 0) %}" in s
    assert 'id="reviews-badge" style="{% if _badge_n > 0 %}' in s
    assert "{{_badge_n if _badge_n > 0 else \'\'}}" in s

    fn = _fn("updateReviewStats")
    badge_block = fn[fn.index("// ── Tab badge"):fn.index("// ── Urgent stat card")]
    assert "var pending = (d.awaiting_approval || 0) + (d.needs_response || 0);" in badge_block


# ── the 4 top pills jump to what they report on ──────────────────────────

def test_the_4_top_pills_are_buttons_wired_to_reviewsPillJump():
    s = _src()
    assert 'id="stat-rating" onclick="reviewsPillJump(\'rating\')"' in s
    assert 'id="stat-rr" onclick="reviewsPillJump(\'rr\')"' in s
    assert 'id="stat-pending" onclick="reviewsPillJump(\'pending\')"' in s
    assert 'id="stat-urgent" onclick="reviewsPillJump(\'urgent\')"' in s
    # buttons, not spans — a span has no click semantics/keyboard focus
    for pid in ("stat-rating", "stat-rr", "stat-pending", "stat-urgent"):
        i = s.index('id="%s"' % pid)
        assert s.rfind("<button", 0, i) > s.rfind("<span", 0, i), \
            pid + " pill must be a <button>, not a <span>"


def test_reviewsPillJump_scrolls_chart_pills_and_filters_inbox_pills():
    fn = _fn("reviewsPillJump")
    assert "querySelector(kind==='rating'?'.rv2-hero':'#perf-card')" in fn
    assert "scrollIntoView({behavior:'smooth',block:'start'})" in fn
    # the inbox pills drive the same rfilter + fpill machinery as the
    # filter-pill row itself, so state never disagrees between the two
    assert "var f = kind==='urgent' ? 'urgent' : 'pending';" in fn
    assert "rfilter=f;" in fn
    assert "filterReviews();" in fn
    assert "getElementById(f==='urgent'?'fpill-urgent':'fpill-pending')" in fn
    s = _src()
    assert 'id="fpill-urgent"' in s and 'id="fpill-pending"' in s


def test_pill_jump_flashes_the_element_it_scrolls_to():
    s = _src()
    assert "@keyframes rv2JumpFlash{0%{background:var(--hb-tint2)}100%{background:transparent}}" in s
    assert ".rv2-jump-flash{animation:rv2JumpFlash 1.8s ease}" in s
    fn = _fn("reviewsPillJump")
    assert "_rvFlashJump(el)" in fn
    assert "_rvFlashJump(target)" in fn


# ── brighter red/green/sand, scoped to the Reviews page only ─────────────

def test_reviews_panel_overrides_green_red_sand_to_more_vivid_values():
    s = _src()
    i = s.index('[data-theme="dark"] #panel-reviews{')
    block = s[i:s.index("}", s.index("--green:", i)) + 1]
    assert "--green:#35d67c;--red:#ff5c5c;--ink3:#e3c99a" in block


def test_vivid_override_does_not_leak_into_other_modules():
    """--green/--red are only redeclared brighter inside the Reviews
    panel's own dark-theme override — every other module's #panel-* block
    (Home, Labor, Food Cost, Intel, Account) never redeclares these
    tokens, so they keep inheriting the original, muted site-wide dark
    theme values instead of picking up Reviews' brighter ones."""
    s = _src()
    assert s.count("--green:#35d67c") == 1
    assert s.count("--red:#ff5c5c") == 1
    assert s.count("--ink3:#e3c99a") == 1


def test_topic_and_approval_bars_use_the_shared_good_bad_tokens_not_literal_hex():
    s = _src()
    assert ".rv2-topic .bar .p{background:var(--hb-good)}.rv2-topic .bar .ng{background:var(--hb-bad)}" in s
    assert ".rv2-perf .bar i.g{background:linear-gradient(90deg,#1fa862,var(--hb-good));box-shadow:0 0 10px rgba(53,214,124,.45)}" in s
    assert ".rv2-perf .bar i.g{background:linear-gradient(90deg,#2d6a4f,#4ead7a)" not in s


def test_per_week_chart_bars_use_css_vars_so_they_pick_up_the_reviews_override():
    fn = _fn("stacked")
    assert 'fill="var(--red)"' in fn
    assert 'fill="var(--green)"' in fn
    assert "RED" not in fn and "GREEN" not in fn


# ── Retract: a crash mid-way used to read as "Network error" ─────────────

def test_retract_status_update_failure_returns_json_not_a_crash():
    """revert_to_drafted() ran unguarded after the live Google delete
    already succeeded — an exception there (e.g. a locked db under
    concurrent writes) went straight to Flask's generic HTML 500 page,
    which the client's r.json() can't parse, so every failure here read
    as a flat, misleading "Network error" no matter what actually broke."""
    import client_api, inspect
    src = inspect.getsource(client_api._do_retract)
    i = src.index("revert_to_drafted(rid, restaurant_id)")
    before = src[:i]
    after = src[i:]
    since_import = before[before.rindex("from models import revert_to_drafted"):]
    assert "try:" in since_import, "the call must be inside a try block"
    assert "except Exception as e:" in after[:250]
    assert '"ok": False' in after[:400] and "500" in after[:400]


def test_retractR_distinguishes_a_server_error_from_a_real_network_failure():
    s = _src()
    fn = s[s.index("window.retractR=function"):s.index("window.retractR=function") + 1400]
    assert "r.json().catch(function(){ throw new Error('Server error (HTTP '+r.status+')" in fn
    assert "toast((e&&e.message)||'Network error" in fn


# ── Approve: orange while pending, real green/blue only once confirmed ───

def test_approve_buttons_are_the_primary_ember_variant_not_green():
    """The Approve button and the done "✓ Approved" pill looked identical
    (both green) — there was no color change to confirm the click landed.
    Approve is now the one dominant action (cbtn-primary, ember); the done
    state stays cbtn-success (green), so clicking it now visibly changes
    color, not just text."""
    s = _src()
    assert 'cbtn cbtn-primary cbtn-sm" onclick="approveR(' in s
    assert 'cbtn cbtn-primary cbtn-sm" onclick="saveDraft(' in s
    assert "cbtn cbtn-success cbtn-sm\" onclick=\"approveR(" not in s
    assert "cbtn cbtn-success cbtn-sm\" onclick=\"saveDraft(" not in s
    # the done pill after a real approve/auto-post stays green
    assert 'cbtn cbtn-success cbtn-ghost cbtn-done cbtn-sm">✓ Approved</span>' in s
    fn = _fn("approveR")
    assert "document.querySelector('#rc-'+id+' .cbtn-primary')" in fn


def test_approved_and_skipped_reply_bubbles_get_their_own_tint_like_posted_does():
    s = _src()
    assert "#panel-reviews .rv2-row.approved .draft-box{background:rgba(53,214,124,.14)!important}" in s
    assert "#panel-reviews .rv2-row.skipped .draft-box{background:rgba(227,201,154,.12)!important}" in s


def test_skipR_clears_the_other_status_classes_it_can_land_on_top_of():
    """'Edit' on an approved/urgent/posted card calls skipR() to make it
    editable again — without clearing those classes first, the old one
    stuck around next to 'skipped', so the ribbon/bubble color depended on
    CSS source order rather than the card's real current status."""
    fn = _fn("skipR")
    assert "card.classList.remove('approved','urgent','posted'); card.classList.add('skipped');" in fn


# ── Approve: the Google post attempt is synchronous, not fire-and-forget ─

def test_google_post_attempt_is_synchronous_not_a_background_thread():
    """post_reply() used to run in a daemon thread kicked off right before
    the HTTP response went out — the client got auto_posted:True (or the
    'will post when connected' message) before Google had actually been
    asked, and a failure was only ever printed to server logs. A review
    stuck at 'approved' then showed a static "Posting to Google" label
    forever, with no way to tell an in-flight post from a silently failed
    one and no way to retry."""
    import client_api, inspect
    src = inspect.getsource(client_api._attempt_google_post)
    assert "threading" not in src and "Thread(" not in src
    assert "result = post_reply(restaurant_id, row[\"review_name\"], row[\"draft_response\"])" in src
    assert "return True, None" in src
    assert "return False, result[\"error\"]" in src

    approve_src = inspect.getsource(client_api._do_approve)
    assert "threading" not in approve_src and "Thread(" not in approve_src
    assert "auto_posted, post_error = _attempt_google_post(rid, restaurant_id, google)" in approve_src
    # The answer is built by _post_payload (shared with retry, F3-3).
    assert "_post_payload(rid, restaurant_id, auto_posted, post_error)" in approve_src
    assert '"post_error"' in inspect.getsource(client_api._post_payload)


def test_retry_post_route_only_allows_an_approved_google_reply():
    import client_api, inspect
    src = inspect.getsource(client_api._do_retry_post)
    assert 'row["response_status"] != "approved" or row["platform"] != "google"' in src
    assert "/api/reviews/<int:rid>/retry-post" in inspect.getsource(client_api)


def test_approved_google_state_shows_retry_only_when_the_post_actually_failed():
    """Reaching response_status=='approved' with platform=='google' now
    only happens AFTER a synchronous post attempt has already run and
    returned — so if GBP is connected, landing here always means that
    attempt failed, never "still working". The old static "Posting to
    Google" label covered both cases identically and never changed."""
    s = _src()
    i = s.index("{% elif r.response_status=='approved' %}")
    block = s[i:s.index("{% elif r.response_status=='skipped' %}", i)]
    assert "restaurant.gmb_refresh_token" in block
    assert "Couldn't post to Google" in block
    assert 'onclick="retryPostR({{ r.id }},this)"' in block
    assert "Will post to Google once connected" in block
    assert "Posting to Google" not in block


def test_retryPostR_js_function_exists_and_handles_all_three_outcomes():
    fn = _fn("retryPostR")
    assert "/api/reviews/'+id+'/retry-post" in fn
    assert "d.auto_posted" in fn and "d.post_error" in fn
    assert "card.classList.remove('approved'); card.classList.add('posted');" in fn


def test_approveR_shows_retry_ui_when_the_synchronous_post_fails():
    fn = _fn("approveR")
    assert "d.post_error" in fn
    assert 'onclick="retryPostR(\'+id+\',this)"' in fn
    assert "Couldn\\'t post to Google" in fn


# ── Reviews · per week / Approval Method / What guests talk about ────────
# columns must line up regardless of which one's heading wraps

def test_all_three_column_headings_share_a_min_height_for_alignment():
    """"Approval Method" wraps to 2 lines next to its own 30d/90d/6mo
    picker at this column width; "Reviews · per week" has no picker and
    never wraps — so its big number sat visibly higher than its two
    siblings', which also made two identically-sized numbers (38px, same
    rv2-sg .v rule) look mismatched in size since they didn't sit at the
    same height."""
    s = _src()
    assert ".rv2-sg .k{font-size:13px" in s
    i = s.index(".rv2-sg .k{font-size:13px")
    rule = s[i:s.index("}", i) + 1]
    assert "min-height:41.6px" in rule


# ── Login page must use the same flat workspace background ───────────────

def test_login_page_background_matches_the_dashboard_flat_bg_base():
    login_src = open(os.path.join(ROOT, "templates", "login.html"), encoding="utf-8").read()
    assert "background:#141110;" in login_src
    assert "background:#0c0c0c" not in login_src
    assert "rgba(20,17,16," in login_src
    assert 'content="#141110"' in login_src
