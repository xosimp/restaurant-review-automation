"""Notifications and Messages, redesigned by subtraction (owner, 9/26/26).

A notification row is an icon, a title, one short line and the time (a red
dot when it still needs someone); a conversation row is an avatar, a name,
the last message, its time and the unread count. No close buttons, no
summary, no filter, no day headings, no rules between rows. One time
formatter for both, reading the server's bare UTC stamps as UTC.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

SRC = open("templates/dashboard.html", encoding="utf-8").read()
HDR = SRC[SRC.index('<div class="hdr-pop-wrap" id="notif-wrap">'):SRC.index("<!-- The user menu:")]
TEAM = SRC[SRC.index("// ── Team messages (manager DMs)"):SRC.index("// ── Response Template Library")]
CSS = SRC[SRC.index("/* Header popovers - Notifications and Messages"):SRC.index("/* The user menu (density audit #39)")]


def _hp_ago_src():
    i = SRC.index("  window.hpAgo = function(iso) {")
    return SRC[i:SRC.index("\n  };\n", i) + 5]


def test_both_popovers_share_one_quiet_shell_with_no_close_button():
    assert 'id="notif-panel" class="hdr-pop"' in HDR and 'id="team-msg-panel" class="hdr-pop hp-msg"' in HDR
    assert HDR.count('class="hp-title"') == 2
    assert ">Notifications</h2>" in HDR and ">Messages</h2>" in HDR
    assert "✕" not in HDR and 'aria-label="Close"' not in HDR
    # Spacing, not rules: nothing in the shell draws a divider.
    assert not re.search(r"border-(bottom|top)(-width)?:", CSS)
    # "{" then "#" opens a Jinja comment: it swallowed the whole header once.
    assert "{#" not in CSS
    # The icon says whether its popover is open, on every close path.
    assert HDR.count('aria-expanded="false"') == 2
    assert "window._hdrPopExpanded(pop.id, false)" in SRC


def test_a_conversation_row_is_five_things():
    row = TEAM[TEAM.index("function tmRenderInbox("):TEAM.index("function tmLoadInbox()")]
    for part in ('class="hp-av"', 'class="tm-name"', 'class="tm-last"', 'class="tm-time"', 'class="tm-count"'):
        assert part in row, part
    assert "Say hello" not in TEAM and "No other logins on this team yet" not in TEAM
    assert "cbtn cbtn-text tm-row" in row                  # a button, not a div with onmouseover
    assert "onmouseover" not in TEAM


def test_a_send_lands_in_place_and_a_failure_comes_back_to_the_field():
    send = TEAM[TEAM.index("window.tmSendMessage = function"):TEAM.index("window.toggleTeamMsgPanel")]
    assert "_sending: true" in send and "tmOpenThread(withId, withName, true)" in send
    assert send.count("input.value = body") == 2
    opener = TEAM[TEAM.index("window.tmOpenThread = function"):TEAM.index("window.tmBackToInbox")]
    assert "if (!quiet) document.getElementById('tm-thread-body').innerHTML" in opener


def test_one_time_formatter_reads_bare_server_stamps_as_utc():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    now = datetime.now(timezone.utc)
    bare = (now - timedelta(minutes=95)).strftime("%Y-%m-%d %H:%M:%S")      # SQLite datetime('now')
    iso = (now - timedelta(days=2, hours=1)).isoformat()
    js = ("var window={};" + _hp_ago_src()
          + "console.log(JSON.stringify([window.hpAgo(%s), window.hpAgo(%s), window.hpAgo(''), window.hpAgo('2026-01-05 10:00:00')]));"
          % (json.dumps(bare), json.dumps(iso)))
    # A zone west of UTC is where the old local read said "just now" for hours.
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20,
                         env=dict(os.environ, TZ="America/Chicago"))
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    assert got[0] == "1h" and got[1] == "2d" and got[2] == ""
    assert re.fullmatch(r"1/[45]/26", got[3])                               # M/D/YY past a week
    assert "hpAgo(" in TEAM and "Date.parse(String(iso).replace(' ', 'T'))" not in TEAM
