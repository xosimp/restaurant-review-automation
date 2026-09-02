#!/usr/bin/env python3
"""Regenerate the lockup SVGs from brand/assets/wordmark.json.

wordmark.json is the source of truth for the CAVNAR wordmark: six Clash
Display Semibold outlines in a box whose height is the cap height (100
units), the ember cradled in the V, and the small "AI" tag (Space Grotesk
Bold, outlined) sitting `ai_tag.gap` units past the R with its cap line on
the wordmark's own cap line — so the tops of both words read as one
straight rule. Run this after touching the JSON; never hand-edit the SVGs.

Writes lockup-light.svg / lockup-dark.svg to every place they live, and
prints the Swift `Path` commands for the AI tag (paste into
CavnarWordmarkAITagShape in ios/.../DesignSystem/CavnarMotion.swift).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
JSON_PATH = os.path.join(HERE, "wordmark.json")

SEAL_X, SEAL_Y, SEAL_W = 0, 14, 120          # seal sits at the left, 120x120
WORD_X, WORD_Y = 150, 4                      # wordmark box origin inside the lockup
HEIGHT = 148

TARGETS = [
    os.path.join(ROOT, "brand", "assets"),
    os.path.join(ROOT, "static", "brand"),
    os.path.join(ROOT, "brand", "social"),
    os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Assets.xcassets", "BrandLockup.imageset"),
]
# The iOS imageset only carries the light (cream-on-dark) variant.
LIGHT_ONLY = {TARGETS[3]}


def tag_paths(tag):
    """The AI tag's outlines as SVG `d` strings in wordmark-box units."""
    return ["".join(seg) for seg in _tag_segments(tag)]


def _tag_segments(tag):
    out = []
    for contour_set in tag["glyphs"]:
        d = []
        for contour in contour_set:
            for i, (x, y) in enumerate(contour):
                d.append(("M" if i == 0 else "L") + f"{x:.2f} {y:.2f}")
            d.append("Z")
        out.append(d)
    return out


def svg(data, ink):
    letters = "".join(f'<path d="{l["d"]}"/>' for l in data["letters"])
    e = data["ember"]
    tag = data["ai_tag"]
    tag_svg = "".join(f'<path d="{d}"/>' for d in tag_paths(tag))
    width = WORD_X + tag["x1"]
    return f"""<svg viewBox="0 0 {width:.0f} {HEIGHT}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="ember-dot" cx="42%" cy="38%" r="72%">
      <stop offset="0%" stop-color="#F2B183"/><stop offset="45%" stop-color="#E8956A"/><stop offset="100%" stop-color="#C74E33"/>
    </radialGradient>
    <radialGradient id="ember-glow">
      <stop offset="0%" stop-color="#D4583A" stop-opacity="0.28"/><stop offset="100%" stop-color="#D4583A" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <!-- seal -->
  <g transform="translate({SEAL_X},{SEAL_Y})">
    <path d="M99.5,45 V44.5 A24,24 0 0 0 75.5,20.5 H44.5 A24,24 0 0 0 20.5,44.5 V75.5 A24,24 0 0 0 44.5,99.5 H75.5 A24,24 0 0 0 99.5,75.5 V75"
          fill="none" stroke="{ink}" stroke-width="19"/>
    <circle cx="99.5" cy="60" r="27" fill="url(#ember-glow)"/>
    <circle cx="99.5" cy="60" r="10" fill="url(#ember-dot)"/>
  </g>
  <!-- wordmark: CAVNAR in Clash Display Semibold, outlined, optically kerned, cap height 100; ember cradled in the V; AI tag on the same cap line -->
  <g transform="translate({WORD_X},{WORD_Y})"><g fill="{ink}">{letters}</g><circle cx="{e['cx']}" cy="{e['cy']}" r="{e['r'] * 2.2:.1f}" fill="url(#ember-glow)"/><circle cx="{e['cx']}" cy="{e['cy']}" r="{e['r']}" fill="url(#ember-dot)"/><g fill="#D4583A">{tag_svg}</g></g>
</svg>
"""


def swift(tag):
    lines = []
    for contour_set in tag["glyphs"]:
        for contour in contour_set:
            for i, (x, y) in enumerate(contour):
                fn = "move" if i == 0 else "addLine"
                lines.append(f"            p.{fn}(to: pt({x:.2f}, {y:.2f}))")
            lines.append("            p.closeSubpath()")
    return "\n".join(lines)


def main():
    data = json.load(open(JSON_PATH))
    tag = data["ai_tag"]
    for target in TARGETS:
        variants = [("lockup-light.svg", "#F0EBE0")]
        if target not in LIGHT_ONLY:
            variants.append(("lockup-dark.svg", "#0C0C0C"))
        for name, ink in variants:
            path = os.path.join(target, name)
            with open(path, "w") as f:
                f.write(svg(data, ink))
            print("wrote", os.path.relpath(path, ROOT))
    print(f"\nlockup viewBox width: {WORD_X + tag['x1']:.2f}")
    print(f"AI tag: x {tag['x0']:.2f}–{tag['x1']:.2f}, height {tag['height']}, gap {tag['gap']}")
    print("\n// Swift — CavnarWordmarkAITagShape.path(in:)")
    print(swift(tag))


if __name__ == "__main__":
    main()
