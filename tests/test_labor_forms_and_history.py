"""A round of "this text is unreadable" fixes across the rest of the Labor
module, following a screenshot walkthrough:

- The week radar's spike labels and the role-cost donut's legend rows were
  hardcoded at 10-12px in the JS that builds them — untouched by any of the
  CSS-level size passes before this one, since neither is server-rendered
  markup.
- The "Schedule" section header had no CSS at all (`.hb-sh` didn't exist
  anywhere in the file) — its subtitle rendered glued directly onto the
  heading with whatever size a bare <small> happens to inherit, and
  "optimised" was spelled the British way in American copy.
- "Employee Availability" and "Operational Score" were 10px uppercase
  kickers standing in as section titles, smaller than any other heading on
  the page, and everything in the forms underneath them (every label,
  input, paragraph, and button, across availability, ratings, thresholds,
  leader rules, shift profiles and quality weighting) was 10-12px.
- The expanded schedule-history view (day/employee/role/shift/hours table)
  was 10-12px throughout, including its own Close button — small enough
  that a real Close control read as if there were no way to collapse it.

Asserted against the template/Python source, same approach as
test_labor_module_ui.py — there's no rendered payload worth diffing for
any of this; the source is the surface.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    """One JS function's source, isolated by brace-matching. Handles both
    `function name(...)` and `window.name=function(...)` declarations —
    the schedule-history helpers use the latter."""
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
    """Flags anything still at an un-bumped size (9-11px, or a bare 12px
    with no decimal). 12.5px+ is the smallest tier this pass produced —
    a genuinely secondary detail line (e.g. "Available: Mon, Tue") reading
    a little smaller than the row's own name is real hierarchy, not a
    miss; a flat, un-bumped 10/11/12 is."""
    tiny = re.findall(r"font-size:(?:9(?:\.\d+)?|10(?:\.\d+)?|11(?:\.\d+)?|12)px", body)
    assert not tiny, "%s still has an un-bumped size: %s" % (label, tiny[:5])


# ── the "Schedule" section header ──────────────────────────────────────────

def test_hb_sh_has_a_real_css_rule():
    """It had none at all — a shared class used by Inbox, Schedule and
    History, entirely unstyled, relying on raw browser defaults for an h2
    and a bare <small> glued right onto it."""
    css = _src()
    assert re.search(r"\.hb-sh\{[^}]*display:flex", css)
    assert re.search(r"\.hb-sh h2\{[^}]*font-size:22px", css)
    m = re.search(r"\.hb-sh h2 small\{([^}]*)\}", css)
    assert m and int(re.search(r"font-size:(\d+)px", m.group(1)).group(1)) >= 14


def test_schedule_buttons_sit_below_the_heading_not_beside_it():
    """Reversed from an earlier round: the buttons lived beside the heading
    as the header row's second flex child, but that squeezed the "Building
    the Week" animation into the same flex row once Generate fired. They
    now sit directly under the subtitle instead."""
    s = _src()
    i = s.index('<div class="k">Schedule</div>')
    h2_end = s.index('</h2>', i) + len('</h2>')
    after = s[h2_end:h2_end + 40]
    assert after.startswith('<div class="lb2-actions">'), \
        "Generate/Update should sit right under the heading text, not beside it in the header row"


def test_optimized_is_spelled_the_american_way_everywhere_its_shown():
    for path in ("templates/dashboard.html", "ask_cavnar_tools.py"):
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        assert "optimised" not in text, path + " still has the British spelling"


# ── Employee Availability / Operational Score headings ────────────────────

def test_the_two_section_headings_are_promoted_not_10px_kickers():
    panel_src = _src()
    assert '<div class="lb2-subsection-title">Employee Availability</div>' in panel_src
    assert 'Operational Score <span id="team-coverage-chip" class="lb2-subsection-chip">' in panel_src
    css = panel_src
    m = re.search(r"\.lb2-subsection-title\{([^}]*)\}", css)
    assert m and int(re.search(r"font-size:(\d+)px", m.group(1)).group(1)) >= 16
    m2 = re.search(r"\.lb2-subsection-toggle\{([^}]*)\}", css)
    assert m2 and float(re.search(r"font-size:([\d.]+)px", m2.group(1)).group(1)) >= 14


# ── the forms underneath them ──────────────────────────────────────────────

def test_the_availability_and_team_forms_have_no_10_to_12px_text_left():
    for name in ("loadAvailability", "renderTeamList", "renderTeamThresholds",
                "renderTeamRules", "renderProfileList", "_profileEditor",
                "_spField", "_spDayChecks", "_spRoleGrid"):
        _no_tiny_sizes(_fn(name), label=name)


# ── the role-cost donut legend and the week-radar spike labels ────────────

def test_donut_legend_rows_are_no_longer_11_to_12px():
    body = _fn("renderRoleDonut")
    _no_tiny_sizes(body, label="renderRoleDonut's legend rows")


def test_week_radar_spike_labels_are_bigger_than_the_old_10_5px():
    body = _fn("renderWeekRadar")
    m = re.search(r'text-anchor="middle" font-size="([\d.]+)"', body)
    assert m, "couldn't find the per-spike day/percent label"
    assert float(m.group(1)) >= 13


# ── the expanded schedule-history view ─────────────────────────────────────

def test_schedule_history_detail_view_has_no_10_to_12px_text():
    body = _fn("viewScheduleHistory")
    _no_tiny_sizes(body, label="viewScheduleHistory")


def test_the_close_button_reads_as_a_real_control_not_a_stray_label():
    """It always worked — the button's onclick genuinely hid the detail
    view. At 11px next to an equally tiny "Download CSV" it just wasn't
    legible enough to register as a button, which is what made it read as
    "stuck open." Bigger and bold, distinct from its sibling."""
    body = _fn("viewScheduleHistory")
    close_idx = body.index(">Close")
    style_start = body.rindex('style="', 0, close_idx) + len('style="')
    style = body[style_start:body.index('"', style_start)]
    assert float(re.search(r"font-size:([\d.]+)px", style).group(1)) >= 13
    assert "font-weight:700" in style


# ── the labor AI insight's own kicker ──────────────────────────────────────

def test_recommendations_kicker_matches_the_consultant_boxs_other_labels():
    src = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    assert 'font-size:12px;font-weight:700;text-transform:uppercase' in src
    assert "Recommendations</div>" in src
