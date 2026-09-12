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
    css = _src()
    assert "--hdr-bg:#0c0c0c" in css
