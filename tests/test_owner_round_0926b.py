"""Owner round, 9/26/26 evening: the publish page, sent dates, the Home
checks, the Labor removals. Source rules, run through node where the rule
is a function."""
import json
import os
import shutil
import subprocess

import pytest

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_the_warning_list_arrives_folded_and_opens_only_when_a_send_asked():
    rb = _between("function _psRenderBlockers(list, arm, keys, open) {", "\n}\n")
    assert "al.open = !!open;" in rb and "if (arm) al.open = true" not in rb
    assert "_psRenderBlockers(d.blockers, true, d.blocker_keys, false);" in SRC     # on load: folded
    assert "_psRenderBlockers(d.blockers || [], true, d.blocker_keys, true);" in SRC  # a refused send: open


def test_a_sent_week_keeps_a_sent_button_and_says_it_once():
    label = _between("  window.psLabel = function () {", "  function psReach()")
    assert "var sent = window.psIsSent();" in label and "'Sent'" in label and "b.disabled = true" in label
    ok = _between("function publishScheduleNow(ack){", "/* A blocker list as the server sends it")
    assert "window._psSent = {hid: _schedHistoryId" in ok
    # The done block says the sentence; the green line under the button does not.
    assert "studioPublished(d, result.textContent); result.textContent = '';" in ok


def test_the_top_publish_publishes():
    assert 'id="ss-top-publish" onclick="ssTopPublish(this)"' in SRC
    top = _between("function ssTopPublish(btn) {", "\n}\n")
    assert "saveAndSendSchedule(b)" in top and "studioGo('publish')" in top


def test_a_sent_date_is_the_viewers_day_not_utcs():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    i = SRC.index("  function mdy(x){")
    mdy = SRC[i:SRC.index("\n", i)]
    j = SRC.index("  window.mdyAt=function(ts){")
    at = SRC[j:SRC.index("\n", j)]
    js = ("function esc(v){return String(v);} var window={};" + mdy + at
          + "console.log(JSON.stringify([window.mdyAt('2026-09-27 00:35:12'), window.mdyAt('2026-09-27')]));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20,
                         env=dict(os.environ, TZ="America/Chicago"))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == ["9/26/26", "9/27/26"]
    assert "mdyAt(r.published_at)" in _between("function ssLaunchRefresh() {", "\n}\n")
    assert "mdyAt(r.published_at)" in _between("function ssLoadHistory() {", "\n}\n")


def test_captions_are_words_in_the_chrome_face():
    cap = _between(".cm-cap{", "}")
    assert "'Apfel Grotezk'" in cap and "Space Grotesk" not in cap
    assert ".ss-done .cm-plabel{font-size:11.5px;color:var(--ember)}" in SRC


def test_every_home_check_is_a_softer_disc_with_a_dark_green_tick():
    rc = _between(".hb-rcpt .ok{", "}")
    assert "color:var(--hb-pop-ink)" in rc and "#fff" not in rc
    assert ".hb-clear .ok{background:radial-gradient(circle at 35% 30%,var(--hb-pop2),var(--hb-pop) 72%);color:var(--hb-pop-ink)" in SRC


def test_studio_history_keeps_the_csv_and_delete_labor_had():
    h = _between("function ssLoadHistory() {", "\n}\n")
    assert "ssHistCsv(" in h and "ssHistDelete(" in h
    assert "schedule_csv" in _between("function ssHistCsv(id, btn) {", "\n}\n")
    ios = open("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift", encoding="utf-8").read()
    assert "LaborMoneyWentCard" not in ios
