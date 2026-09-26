"""Owner round, 9/26/26 (Home, Daily report, Labor): source rules for the
fixes, read from the template so they hold in every build."""
import os
import re
import subprocess

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_escape_helper_takes_numbers():
    """A numeric quality score threw inside _escHtml and aborted reopening a
    drafted week ("Could not open that week")."""
    s = _src()
    fn = s[s.index("function _escHtml(s) {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    out = subprocess.run(["node", "-e", fn + "console.log(_escHtml(72)+'|'+_escHtml(0)+'|'+_escHtml(null)+'|'+_escHtml('<b>'))"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "72|0||&lt;b&gt;"


def test_a_weekday_is_never_a_role_and_certifications_read_as_words():
    s = _src()
    roles = s[s.index("function _rulRoles(){"):s.index("function _rulRoles(){") + 900]
    assert "wednesday:1" in roles and "WD[r.toLowerCase()]" in roles
    assert "function _certLabel(c)" in s
    assert "_chipTog(certs,reqs[roles[i]]||[],'data-cert',ro,_certLabel)" in s
    assert "'data-rst-cert',ro,_certLabel)" in s


def test_only_the_headline_figure_of_the_one_thing_is_ember():
    s = _src()
    assert ".hb-focus .lead .hb-num.plain{color:inherit}" in s
    assert "'<span class=\"hb-num plain\">'" in s


def test_section_kickers_are_all_orange():
    assert ".hb-sh .k.dim{color:var(--ember)}" in _src()


def test_the_import_picker_is_a_branded_button_with_no_native_box():
    s = _src()
    assert '<label class="cbtn cbtn-secondary cbtn-sm dr-imp-pick">Choose a file<input type="file" id="dr-imp-file"' in s
    assert ".dr-imp-pick .dr-imp-input{position:absolute;inset:0;opacity:0" in s
    assert 'id="dr-imp-name"' in s and ".dr-imp-name:empty{display:none}" in s


def test_opened_schedule_sections_have_no_box():
    assert ".lb2-srow .lb2-avail{margin-top:0;background:none;border:none" in _src()


def test_the_heaviest_weekday_is_a_red_spike_on_the_radar():
    s = _src()
    fn = s[s.index("function renderWeekRadar(){"):s.index("function renderWeekRadar(){") + 6000]
    assert 'id="lbRadHot"' in fn and 'fill="url(#lbRadHot)"' in fn
    assert "var over=vals[i]>target||i===worst;" in fn


def test_ready_button_has_no_ember_glow_and_the_rule_above_every_day_is_gone():
    s = _src()
    assert "_schedBtn.style.boxShadow = 'none';" in s
    assert "#lb2-money-all{border-top:none}" in s


def test_listing_caption_counts_what_the_figure_counts():
    """ "100%" over "4 of 11 profile items done" read as a contradiction:
    the figure is the listing items that could be read (owner, 9/26/26)."""
    s = _src()
    assert "' listing items done'" in s and "more need your Google listing connected" in s
    assert "' profile items done'" not in s


def test_select_shown_and_why_pass_match_their_neighbours():
    s = _src()
    assert "#panel-reviews .rv2-selall{display:inline-flex;align-items:center;gap:7px;font-size:12px;line-height:normal;color:var(--ink2);padding:6px 2px" in s
    assert ".in2-road .row .t .ac .cbtn-text+.rec-ans{padding-left:12px;border-left:1px solid var(--hb-line)}" in s
    assert "#aiv-roadmap-footer .how-this>div{font-size:15.5px" in s


def test_the_visibility_ring_is_larger_and_centred():
    s = _src()
    assert "var H=200,cy=100,R=68;" in s
    assert ".in2-aiv-card .in2-orbit{height:auto;margin-top:28px;display:flex;justify-content:center}" in s
