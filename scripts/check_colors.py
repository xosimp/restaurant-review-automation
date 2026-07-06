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


def check_file(path):
    lines = open(path, encoding="utf-8").read().split("\n")
    violations = []
    for idx, line in enumerate(lines):
        lineno = idx + 1
        if "[data-theme=" in line:
            continue  # explicit per-theme override — the fix, not the bug
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
            print(f"{name}:{lineno}: {color} has no pinned background — "
                  f"invisible in one theme. Use var(--ink)/var(--ink2) or pin a background.\n"
                  f"    {context}")
    if failed:
        sys.exit(1)
    print("color lint OK — no theme-unsafe literal text colors")


if __name__ == "__main__":
    main()
