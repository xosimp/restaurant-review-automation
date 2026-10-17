"""scripts/render_glows.py — the dashboard's glows as error-diffused images.

A glow faint enough to sit behind text (an ember wash at 9-22%, a warm key
light at 7%) has only 20-60 alpha levels to fall through, and a CSS gradient
drawing it steps from level to level in visible rings on an 8-bit screen
(owner, 10/16/26: "all of the glows ... have terrible banding"). Noise over
the card hides the rings but reads as grain, which was refused too. So each
glow is rendered here at its final strength, every pixel's rounding error
carried into its neighbours (Floyd-Steinberg), and saved as a PNG the CSS
stretches over its box (`0 0/100% 100% no-repeat`): smooth, no grain.

Each sprite is box-relative — its ellipse's centre and radii are fractions
of the box it is stretched over — so one file fits any card size. A colour
sprite bakes its RGB; a mask sprite is alpha only, for `mask-image` over a
solid `var(--colour)` background, so a theme colour still drives it.

Run from the repo root; writes static/img/glow/*.png. Re-run after changing a
spec, and bump the -vN in the name (static files are cached).
"""
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "img", "glow")

EMBER, EMBER2 = (212, 88, 58), (232, 149, 106)
RED, AMBER, GREEN, BLUE = (227, 51, 63), (212, 160, 48), (78, 173, 122), (106, 171, 255)
WARM = (255, 228, 200)
WHITE = (255, 255, 255)

# name: (rgb, peak alpha 0-255, width, height, centre (fx, fy), radii (fx, fy), end, kind)
# end: the fraction of the radius where it reaches nothing; kind "radial", "linear-x" or "linear-y".
SPECS = {
    # the "Where the money is" tiles' corner wash (was --br-lit, 16% to 60%), one per tone
    "lit-ember2": (EMBER2, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    "lit-ember": (EMBER, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    "lit-red": (RED, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    "lit-amber": (AMBER, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    "lit-green": (GREEN, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    "lit-blue": (BLUE, 41, 512, 384, (0, 0), (1.2, 0.9), 0.60, "radial"),
    # masks: the Bar hero's bloom and the equalizer's (15% / 14% to 74%), the lead tile's halo (22%)
    "mask-bloom": (WHITE, 38, 512, 512, (0.5, 0.5), (0.5, 0.5), 0.74, "radial"),
    "mask-halo": (WHITE, 56, 384, 384, (0.5, 0.5), (0.5, 0.5), 1.0, "radial"),
    # ember from the top-left (matching header 13%, the AI's glass) and top-right (count form 9%)
    "corner-tl-ember": (EMBER, 33, 768, 384, (0, 0), (1.0, 1.35), 0.67, "radial"),
    "corner-tr-ember": (EMBER, 23, 768, 384, (1, 0), (0.8, 1.0), 0.60, "radial"),
    # the same corner in green, for a good-news card on the glass (the Bar's Good to know lead, 10/17/26)
    "corner-tl-green": (GREEN, 33, 768, 384, (0, 0), (1.0, 1.35), 0.67, "radial"),
    # the pedestal's overhead key light (7%) and the waste shell's (5%)
    "keylight": (WARM, 17, 768, 384, (0.42, -0.18), (1.2, 0.75), 0.80, "radial"),
    "keylight-soft": (WARM, 13, 768, 384, (0.42, -0.18), (1.2, 0.75), 0.80, "radial"),
    # a signal card's spill from its LED edge (11% to 46% of the width), one per tone
    "signal-amber": (AMBER, 28, 768, 64, (0, 0), (1, 1), 0.46, "linear-x"),
    "signal-green": (GREEN, 28, 768, 64, (0, 0), (1, 1), 0.46, "linear-x"),
    "signal-red": (RED, 28, 768, 64, (0, 0), (1, 1), 0.46, "linear-x"),
    "signal-ember": (EMBER, 28, 768, 64, (0, 0), (1, 1), 0.46, "linear-x"),
    "signal-ink": ((240, 235, 224), 18, 768, 64, (0, 0), (1, 1), 0.46, "linear-x"),
    # a plate's top sheen (2% white over the top 46%)
    "sheen": (WHITE, 6, 64, 512, (0, 0), (1, 1), 0.46, "linear-y"),
}


def falloff(t):
    """1 at the centre to 0 at t=1, eased at both ends (no edge, no kink)."""
    if t >= 1:
        return 0.0
    u = 1 - t
    return u * u * (3 - 2 * u) * (0.35 + 0.65 * u)


def render(rgb, amax, w, h, centre, radii, end, kind):
    cx, cy = centre[0] * w, centre[1] * h
    rx, ry = radii[0] * w * end, radii[1] * h * end
    cur = [0.0] * (w + 2)
    nxt = [0.0] * (w + 2)
    out = []
    for y in range(h):
        row = bytearray([0])
        for i in range(w + 2):
            nxt[i] = 0.0
        for x in range(w):
            if kind == "linear-x":
                t = (x + 0.5) / (w * end)
            elif kind == "linear-y":
                t = (y + 0.5) / (h * end)
            else:
                dx, dy = (x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry
                t = (dx * dx + dy * dy) ** 0.5
            v = amax * falloff(t) + cur[x + 1]
            q = max(0, min(255, int(round(v))))
            e = v - q
            cur[x + 2] += e * 7 / 16
            nxt[x] += e * 3 / 16
            nxt[x + 1] += e * 5 / 16
            nxt[x + 2] += e / 16
            row += bytes((rgb[0], rgb[1], rgb[2], q))
        out.append(bytes(row))
        cur, nxt = nxt, cur
    return b"".join(out)


def png(raw, w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (rgb, amax, w, h, c, r, end, kind) in SPECS.items():
        data = png(render(rgb, amax, w, h, c, r, end, kind), w, h)
        path = os.path.join(OUT, f"{name}-v1.png")
        with open(path, "wb") as f:
            f.write(data)
        print(f"{path}  {len(data):,} bytes")


if __name__ == "__main__":
    main()
