"""Skeleton lines never touch (owner, 9/28/26: Home's "day" card showed its
three loading bars run together). Adjacent .hb-skel bars either sit in a
.hb-skel-stack or carry their own margin."""
import os
import re

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html"),
           encoding="utf-8").read()


def test_no_two_skeleton_bars_touch():
    bad = []
    for m in re.finditer(r'<div class="hb-skel" style="([^"]*)"></div>(?=<div class="hb-skel" style="([^"]*)")', SRC):
        a, b = m.group(1), m.group(2)
        if "margin-bottom" in a or "margin-top" in b:
            continue
        if "hb-skel-stack" not in SRC[max(0, m.start() - 400):m.start()]:
            bad.append(SRC.count("\n", 0, m.start()) + 1)
    assert not bad, bad
    assert re.search(r"\.hb-skel-stack\{display:flex;flex-direction:column;gap:12px\}", SRC)
