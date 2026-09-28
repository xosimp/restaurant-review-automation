"""Account hours and quiet hours are dropdowns, not <input type=time>
(owner, 9/28/26: Erik's Monday, Wednesday and Sunday close times vanished on
Save hours while the opens stayed).

Safari draws an empty time field as a grey "12:30 PM" that reads as a real
value, and one edited only in part stays empty underneath; the save dropped
those days and still said "Saved". A select has no half-entered state."""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    start = SRC.index("function " + name + "(")
    depth, i = 0, SRC.index("{", start)
    while True:
        if SRC[i] == "{":
            depth += 1
        elif SRC[i] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:i + 1]
        i += 1


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_native_time_field_left_where_an_owner_sets_hours():
    grid = _fn("buildAccountHoursGrid")
    assert 'type="time"' not in grid and "<select" in grid
    for el in ("al-quiet-start", "al-quiet-end"):
        tag = re.search(r'<(\w+) id="' + el + '"', SRC)
        assert tag and tag.group(1) == "select", el


def test_the_dropdown_keeps_every_stored_time_and_labels_it_the_house_way():
    js = "\n".join(_fn(n) for n in ("cavTimeLabel", "cavTimeValue", "cavTimeOptions")) + """
    function opts(v){ var h = cavTimeOptions(v), out = [], re = /<option value="([^"]*)"( selected)?>([^<]*)</g, m;
      while ((m = re.exec(h))) out.push([m[1], !!m[2], m[3]]); return out; }
    var a = opts('02:00'), b = opts('23:05'), c = opts(''), d = opts('9:30');
    console.log(JSON.stringify({
      n: a.length, first: a[0], sel: a.filter(function(o){return o[1];}),
      off: b.filter(function(o){return o[1];}), offCount: b.length,
      none: c.filter(function(o){return o[1];}).length,
      short: d.filter(function(o){return o[1];}),
      labels: [cavTimeLabel('00:00'), cavTimeLabel('12:00'), cavTimeLabel('00:30'), cavTimeLabel('23:00'), cavTimeLabel('14:45')]
    }));"""
    got = _node(js)
    assert got["n"] == 97 and got["first"] == ["", False, "—"]      # unset + 96 quarter hours
    assert got["sel"] == [["02:00", True, "2am"]]
    assert got["off"] == [["23:05", True, "11:05pm"]] and got["offCount"] == 98   # an off-grid time is kept
    assert got["none"] == 0
    assert got["short"] == [["09:30", True, "9:30am"]]
    assert got["labels"] == ["Midnight", "Noon", "12:30am", "11pm", "2:45pm"]


def test_a_day_with_one_time_but_not_the_other_is_named_not_saved():
    save = _fn("saveAccountHours")
    assert "half.push(d)" in save and "as-hours-err" in save
    assert save.index("if (half.length)") < save.index("asPost(")
    assert 'id="as-hours-err" role="alert"' in SRC
