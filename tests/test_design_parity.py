"""Web and iOS read as one product (DESIGN_SYSTEM.md).

The parity audit (9/25/26) found two embers and two reds on one dark
screen (web #e06444 / #e05555 beside the iOS #D4583A / #E3333F the dark
buttons already used), a second Home amber, raw stat-glow hexes, a CSS
spinner and bare "Loading…" text where the ember pulse is the rule, an
overshooting badge pop, three confidence-meter sizes, and Bricolage on
every inline 800 weight. These pin the converged values; the iOS side is
pinned by source reads here and by CavnarAITests/DesignParityTests.swift.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _web_sources():
    out = {}
    for d in ("templates", os.path.join("static", "css")):
        for f in sorted(os.listdir(os.path.join(ROOT, d))):
            if f.endswith((".html", ".css")):
                out[os.path.join(d, f)] = _read(d, f)
    for f in sorted(os.listdir(os.path.join(ROOT, "static"))):
        if f.endswith(".js"):
            out[os.path.join("static", f)] = _read("static", f)
    return out


def test_dark_ember_and_red_are_the_ios_values_everywhere_on_web():
    stale = re.compile(r"#e06444|%23e06444|rgba\(224,\s*100,\s*68,|#e05555|rgba\(224,\s*85,\s*85,", re.I)
    bad = [p for p, s in _web_sources().items() if stale.search(s)]
    assert not bad, bad
    dash = _read("templates", "dashboard.html")
    dark = dash[dash.index('[data-theme="dark"]{'):]
    dark = dark[:dark.index("}")]
    assert "--ember:#d4583a" in dark and "--red:#e3333f" in dark


def test_ios_colorsets_hold_the_same_dark_values():
    def dark(name):
        s = _read("ios", "CavnarAI", "CavnarAI", "Assets.xcassets", "Colors", name + ".colorset", "Contents.json")
        block = s[s.index('"dark"'):]
        return tuple(round(float(re.search(r'"%s"\s*:\s*"([0-9.]+)"' % k, block).group(1)) * 255)
                     for k in ("red", "green", "blue"))
    assert dark("Ember") == (212, 88, 58)      # #D4583A
    assert dark("Red") == (227, 51, 63)        # #E3333F


def test_home_has_one_amber_and_stat_glows_sit_on_tokens():
    dash = _read("templates", "dashboard.html")
    assert "--hb-amber:#" not in dash
    assert "--hb-amber:var(--amber)" in dash
    assert ".stat-glow-green{color:var(--green)!important}" in dash
    assert ".stat-glow-red{color:var(--red)!important}" in dash
    assert "#3ecf6e" not in dash.lower() and "#e02828" not in dash.lower()


def test_loading_is_the_pulse_not_a_spinner_or_ellipsis_text():
    dash = _read("templates", "dashboard.html")
    assert "animation:spin" not in dash
    assert ">Loading&hellip;<" not in dash
    assert not re.search(r"innerHTML\s*=\s*'[^']*>?Loading…", dash)
    assert "el.innerHTML = 'Loading…'" not in dash
    # Every pulse carries its sliding light.
    assert '<div class="dr-pulse" aria-hidden="true"></div>' not in dash


def test_no_overshoot_in_the_badge_pop_toast_or_tab_indicator():
    dash = _read("templates", "dashboard.html")
    pop = re.search(r"@keyframes cmBadgePop\{[^\n]*", dash).group(0)
    assert "1.25" not in pop
    assert "cubic-bezier(.34,1.56" not in dash
    toast = re.search(r"@keyframes toastBounce\{[^\n]*", dash).group(0)
    assert "scale(1.0" not in toast and "-10px" not in toast


def _ios_swift_files():
    """Every Swift file an owner or a staff member sees: the app and its
    extensions (widgets, share, notification content, shared), not tests."""
    base = os.path.join(ROOT, "ios", "CavnarAI")
    for d, _dirs, files in os.walk(base):
        if "CavnarAITests" in d:
            continue
        for f in files:
            if f.endswith(".swift"):
                yield os.path.join(d, f)


def test_ios_has_no_spring_or_system_spinner_in_features():
    # ProgressView in any form — the bare spinner, ProgressView(value:) and
    # ProgressView("…") — and the system .bordered / .borderedProminent
    # button styles: loading is the ember pulse or CavnarSkeletonBar, and
    # a button is a Cavnar style (parity audit #84; TaskSheetsScreen had
    # a ProgressView(value:) and a .bordered the old `ProgressView()`
    # pattern missed).
    pattern = re.compile(
        r"\.spring\(|\.bouncy\b|interpolatingSpring|\bProgressView\s*[({]"
        r"|\.borderedProminent\b|\.bordered\b|\bBordered(Prominent)?ButtonStyle\b"
    )
    bad = []
    for path in _ios_swift_files():
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            code = line.split("//", 1)[0]
            if pattern.search(code):
                bad.append("%s:%d %s" % (os.path.basename(path), n, line.strip()))
    assert not bad, bad


def test_ios_pull_to_refresh_is_always_the_ember_drop():
    # A bare .refreshable draws nothing (the app clears the system tint), so
    # it is only legal inside the one modifier that draws the ember drop.
    bad = []
    for path in _ios_swift_files():
        if os.path.basename(path) == "CavnarMotion.swift":
            continue
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            if re.search(r"\.refreshable\s*\{", line.split("//", 1)[0]):
                bad.append("%s:%d" % (os.path.basename(path), n))
    assert not bad, bad


def test_ios_every_sensory_feedback_honours_the_haptics_toggle():
    # Account → Haptics off must silence every buzz, including the ones
    # SwiftUI fires for a button style (parity audit #84). Each
    # .sensoryFeedback carries a condition that reads the toggle.
    bad = []
    for path in _ios_swift_files():
        lines = open(path, encoding="utf-8").read().split("\n")
        for i, line in enumerate(lines):
            if ".sensoryFeedback(" not in line.split("//", 1)[0]:
                continue
            window = "\n".join(lines[i:i + 3])
            if "hapticsEnabledSnapshot" not in window:
                bad.append("%s:%d" % (os.path.basename(path), i + 1))
    assert not bad, bad


def test_one_confidence_meter_size_on_both_platforms():
    dash = _read("templates", "dashboard.html")
    assert ".cf-m{width:38px;height:6px;" in dash
    swift = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "ConfidenceLine.swift")
    assert "var width: CGFloat? = 38" in swift and "var height: CGFloat = 6" in swift
    assert "38×6" in _read("DESIGN_SYSTEM.md")


def test_bricolage_is_gone_every_number_is_space_grotesk():
    # The tab badges and .stat-n were the last Bricolage (parity audit #85).
    dash = _read("templates", "dashboard.html")
    assert "Bricolage" not in dash
    assert re.search(r"^\.stat-n\{[^}\n]*font-family:'Space Grotesk'", dash, re.M)
    assert re.search(r"^\.tab \.badge\{[^}\n]*font-family:'Space Grotesk'", dash, re.M)


def test_ios_neutral_tone_is_not_ember_and_secondary_button_is_quiet():
    glass = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "GlassCard.swift")
    neutral = re.search(r"case \.neutral: return (\.[a-zA-Z0-9]+)", glass).group(1)
    assert neutral != ".cavnarEmber"
    vm = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "ViewModifiers.swift")
    sec = vm[vm.index("struct CavnarSecondaryButtonStyle"):]
    sec = sec[:sec.index("\n}\n")]
    assert "cavnarEmber" not in sec
    assert "struct CavnarSoftButtonStyle" in vm
    assert "cornerRadius: CavnarRadius.control, style: .continuous))" in vm
    assert "cornerRadius: 6," not in vm


def test_ios_bell_follows_the_web_badge_rule():
    chrome = _read("ios", "CavnarAI", "CavnarAI", "Core", "AppChrome.swift")
    assert "urgentCount > 0" in chrome
    motion = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "CavnarMotion.swift")
    badge = motion[motion.index("struct CavnarAlertBadge"):]
    badge = badge[:badge.index("\n}\n")]
    assert "cavnarRed" in badge and "cavnarInk3" in badge and "cavnarEmber)" not in badge
