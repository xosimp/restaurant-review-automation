"""Theme-safety color lint — the bug class that was hand-fixed four times
(near-black text inheriting a theme background goes invisible in dark mode)
is now mechanical."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from check_colors import check_file, _is_risky, TEMPLATES


def test_templates_have_no_theme_unsafe_colors():
    for name in TEMPLATES:
        path = os.path.join(ROOT, "templates", name)
        if not os.path.exists(path):
            continue
        violations = check_file(path)
        assert not violations, f"{name}: {violations}"


def test_catches_the_historical_bug_pattern(tmp_path):
    """The exact shipped bug: #374151 text with no pinned background."""
    p = tmp_path / "frag.html"
    p.write_text('<div class="card">\n<div>\n<span style="color:#374151">x</span>\n</div>')
    assert check_file(str(p)), "linter must catch naked near-black text"


def test_self_paired_and_ancestor_pinned_are_allowed(tmp_path):
    p = tmp_path / "frag.html"
    p.write_text(
        '<span style="background:#e8f0fe;color:#1a1714">chip</span>\n'
        '<div style="background:linear-gradient(135deg,#1a1410,#1e1a14)">\n'
        '  <div style="color:#f0ebe0">text on fixed dark card</div>\n'
        "</div>"
    )
    assert check_file(str(p)) == []


def test_risk_thresholds():
    assert _is_risky("374151")      # the shipped bug
    assert _is_risky("f0ebe0")      # near-white
    assert not _is_risky("6fcf97")  # mid-luminance green is fine anywhere
