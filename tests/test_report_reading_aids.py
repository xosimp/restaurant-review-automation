"""Reports' reading aids (owner, 10/6/26: "yes add sticky report headers and
the progress line"): an open section's heading sticks under the app's
header and tab bar, and a 2px line tracks how far through a long report
the reader is. Neither prints."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")


def test_an_open_sections_heading_sticks_under_the_tab_bar():
    assert ".dr-sec[open]>summary{position:sticky;top:var(--dr-stick,102px);z-index:5;background:var(--surface);" in SRC
    assert "@media print{.dr-progline{display:none!important}.dr-sec[open]>summary{position:static}}" in SRC


def test_the_progress_line_measures_the_header_and_only_shows_on_a_long_report():
    assert '<div class="dr-progline" id="dr-progline" aria-hidden="true"><i></i></div>' in SRC
    fn = SRC[SRC.index("  function stickTop(){"):]
    fn = fn[:fn.index("  window.drProgressDraw=soon;")]
    assert "document.documentElement.style.setProperty('--dr-stick'" in fn
    assert "root.offsetParent!==null&&r.height>view*1.5" in fn
    assert "Math.max(0,Math.min(1,p))" in fn and "{passive:true}" in fn
