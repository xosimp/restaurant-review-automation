#!/usr/bin/env python3
"""Regenerate the transactional-email logo assets from the brand SVGs.

Email clients don't reliably load @font-face (Gmail strips it outright), so
every email uses these raster PNGs instead of live SVG/text — a lockup for
headers and a small seal for footers, each in both an ink-dark variant (for
light card backgrounds) and an ink-light/cream variant (for the one
dark-themed template, reporter.py's weekly digest). Run this after touching
lockup-dark.svg/lockup-light.svg/seal-color.svg/seal-mono-dark.svg; never
hand-edit the PNGs. Writes to static/brand/, which every email references at
https://dashboard.cavnar.ai/static/brand/<file>.png — a fresh push+deploy is
what actually makes a regenerated asset live.
"""
import base64
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(ROOT, "static", "brand")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def render(svg_path, out_name, w, h, pad=0):
    svg = open(svg_path).read()
    b64 = base64.b64encode(svg.encode()).decode()
    html = (
        "<style>*{margin:0}html,body{background:transparent}</style>"
        f'<body style="width:{w}px;height:{h}px;display:flex;align-items:center;'
        f'justify-content:center"><img src="data:image/svg+xml;base64,{b64}" '
        f'style="width:{w - pad * 2}px;display:block"></body>'
    )
    tmp = f"/tmp/{out_name}.html"
    open(tmp, "w").write(html)
    out_path = os.path.join(OUT, f"{out_name}.png")
    subprocess.run(
        [
            CHROME, "--headless=new", "--hide-scrollbars", "--disable-gpu",
            f"--window-size={w},{h}", f"--screenshot={out_path}",
            "--default-background-color=00000000", f"file://{tmp}",
        ],
        check=True, capture_output=True,
    )
    print("wrote", os.path.relpath(out_path, ROOT))


def main():
    assets = os.path.join(HERE)
    # 3x-retina for a ~150-220px email display width.
    render(os.path.join(assets, "lockup-dark.svg"), "lockup-dark-email", 672, 128, pad=6)
    render(os.path.join(assets, "lockup-light.svg"), "lockup-light-email", 672, 128, pad=6)
    # Wordmark WITHOUT the seal — the header mark every email actually uses.
    # Putting the seal beside the wordmark in the header (the lockup) reads
    # as the same brand mark shown twice, side by side, redundantly — the
    # user's own words: "it should be wordmark OR logo, not both." The seal
    # stays for the small standalone footer mark below, which sits next to
    # plain text, not another rendering of the wordmark. wordmark-*.svg's
    # viewBox (-6 -14 727 128) is a different aspect ratio than the
    # lockup's, so this uses its own canvas sized to match it, not the
    # lockup's 672x128.
    render(os.path.join(assets, "wordmark-dark.svg"), "wordmark-dark-email", 728, 128, pad=6)
    render(os.path.join(assets, "wordmark-light.svg"), "wordmark-light-email", 728, 128, pad=6)
    # seal-color.svg's ring is cream (for dark cards); seal-mono-dark.svg's
    # ring is dark (for light cards) — NOT interchangeable with "dark"/
    # "light" meaning what you'd guess from the lockup naming.
    render(os.path.join(assets, "seal-mono-dark.svg"), "seal-dark-email", 60, 60, pad=2)
    render(os.path.join(assets, "seal-color.svg"), "seal-light-email", 60, 60, pad=2)


if __name__ == "__main__":
    main()
