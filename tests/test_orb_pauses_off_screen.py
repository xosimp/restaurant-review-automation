"""An orb animates only while it is on screen (owner, 9/28/26: a notification
click lagged; orbs in panels the owner had left kept drawing every frame for
the life of the page)."""
import os

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "cavnar-orb.js"),
           encoding="utf-8").read()


def _fn(name):
    start = SRC.index("function " + name + "(")
    depth, i = 0, SRC.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        if depth == 0:
            return SRC[start:i + 1]
        i += 1


def test_an_orb_off_screen_stops_and_starts_again_when_seen():
    mount = _fn("mount")
    assert "new IntersectionObserver" in mount and "handle._io.observe(canvas)" in mount
    assert "if (!handle.inView) stop(handle);" in mount
    # coming back to the tab, or a new state, never restarts an orb nobody can see
    assert "handle.inView !== false" in mount and "handle.inView !== false" in _fn("setState")


def test_destroy_lets_go_of_the_observer():
    assert "handle._io.disconnect()" in _fn("destroy")
