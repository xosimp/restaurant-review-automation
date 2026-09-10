#!/usr/bin/env python3
"""
check_colors.py — dark-mode color-contrast lint for the dashboard templates.

The recurring bug class: a literal near-black text color on an element whose
background comes from the theme (var(--paper) flips dark), making the text
invisible in dark mode — or near-white text that vanishes in light mode. It
was fixed by hand at least four times (format_intel_body_filter's #374151,
_highlightText, both webhook buttons). This makes the rule mechanical:

  A very dark or very light literal `color:#hex` is only allowed when the
  same style attribute / CSS declaration line also pins its own background,
  or it's part of an explicit [data-theme=...] override.

Run: python3 scripts/check_colors.py   (exit 1 on violations; used by CI)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = ["dashboard.html", "client_settings.html", "admin.html", "client_data.html"]

HEX_RE = re.compile(r"color:\s*#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _luminance(hexstr):
    if len(hexstr) == 3:
        hexstr = "".join(c * 2 for c in hexstr)
    r, g, b = (int(hexstr[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _is_risky(hexstr):
    lum = _luminance(hexstr)
    return lum < 0x55 or lum > 0xE8   # near-black/dark-gray or near-white


_PINNED_BG = re.compile(r"background(-color)?:\s*(?!none|transparent)[#a-z]")

# A light literal on top of var(--ink), in a template whose own --ink is light.
#
# background:var(--ink) with color:var(--paper) is a deliberate, correct
# inversion used throughout the dashboard chrome — the rule must not flag it.
# What broke was narrower: `background:var(--ink);color:#f2ece2` in admin.html,
# whose --ink is #F0EBE0. In dashboard.html (--ink #0e0c0a) that same line
# would have been fine, which is exactly why a template-blind rule cannot see
# it and why _PINNED_BG waved it through: a background WAS pinned, it just
# happened to be a text colour in that file.
_INK_BG = re.compile(r"background(-color)?:\s*var\(--ink[0-9]?\)")
_ROOT_INK = re.compile(r"--ink:\s*#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def check_file(path):
    raw = open(path, encoding="utf-8").read()
    lines = raw.split("\n")
    # This template's OWN --ink. Where it is light, the chrome inversion the
    # dashboard uses does not apply and an ink background needs ink-dark text.
    m_ink = _ROOT_INK.search(raw)
    ink_is_light = bool(m_ink) and _luminance(m_ink.group(1)) > 0xC0
    violations = []
    for idx, line in enumerate(lines):
        lineno = idx + 1
        if "[data-theme=" in line:
            continue  # explicit per-theme override — the fix, not the bug
        if ink_is_light:
            m_bg = _INK_BG.search(line)
            if m_bg:
                for m_fg in HEX_RE.finditer(line):
                    if _luminance(m_fg.group(1)) > 0xC0:
                        violations.append((lineno, m_bg.group(0), line.strip()[:110]))
                        break
        for m in HEX_RE.finditer(line):
            if not _is_risky(m.group(1)):
                continue
            # Self-contained pairing: the same line pins its own background.
            if _PINNED_BG.search(line):
                continue
            # Ancestor pinning: fixed-dark/light containers (insight cards,
            # modals, overlays) declare their background on an enclosing div
            # a few lines up. Look back a short window.
            lookback = "\n".join(lines[max(0, idx - 8):idx])
            if _PINNED_BG.search(lookback):
                continue
            violations.append((lineno, m.group(0), line.strip()[:110]))
    return violations


def main():
    failed = False
    for name in TEMPLATES:
        path = os.path.join(ROOT, "templates", name)
        if not os.path.exists(path):
            continue
        for lineno, color, context in check_file(path):
            failed = True
            if color.startswith("background"):
                print(f"{name}:{lineno}: {color} with a near-white literal on top — "
                      f"--ink is LIGHT in this template, so this is light-on-light. "
                      f"Use var(--paper2)/var(--paper3) as the surface.\n"
                      f"    {context}")
            else:
                print(f"{name}:{lineno}: {color} has no pinned background — "
                      f"invisible in one theme. Use var(--ink)/var(--ink2) or pin a background.\n"
                      f"    {context}")
    if failed:
        sys.exit(1)
    print("color lint OK — no theme-unsafe literal text colors")


if __name__ == "__main__":
    main()
