#!/usr/bin/env python3
"""
gen_bg_grain.py — the dither tile behind the dashboard background.

The page background (BACKGROUND SYSTEM in templates/dashboard.html) is a
set of very slow gradients over a near-black base. Between two adjacent
8-bit colour levels a slow gradient spans 50–150px, and every one of those
steps shows as a visible band — on any display, in any browser, however
many stops the gradient has. The only cure is dithering: per-pixel noise
of about one level, so the eye averages the steps away.

The first version blended an SVG feTurbulence tile with `overlay`. Overlay
scales with the colour underneath, and the base is ~#1a1714, so ±7% noise
came out at under one level — invisible, and useless as a dither. This
tile is blended `normal` instead: every pixel is white with a tiny random
alpha, so it ADDS a random 0–MAX levels regardless of what is beneath it
(the glows included). The mean lift (MAX/2) is paid back by --bg-base
tokens that sit that much darker in the stylesheet.

Deterministic (seeded) so the committed PNG is reproducible and
tests/test_schedule_header_pill_and_theme.py can pin its amplitude.

Run: python3 scripts/gen_bg_grain.py   (writes static/bg/grain.png)
"""
import os
import random

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "static", "bg", "grain.png")

SIZE = 192          # tile edge in px; repeats, so it only needs to be aperiodic to the eye
MAX_ALPHA = 7       # of 255 — adds 0..~6.3 levels over a #1a1714 base, mean ~3
SEED = 20260917


def build(size=SIZE, max_alpha=MAX_ALPHA, seed=SEED):
    rng = random.Random(seed)
    img = Image.new("LA", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = (255, rng.randint(0, max_alpha))
    return img


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    build().save(OUT, optimize=True)
    print("wrote %s (%d bytes)" % (os.path.relpath(OUT, ROOT), os.path.getsize(OUT)))
