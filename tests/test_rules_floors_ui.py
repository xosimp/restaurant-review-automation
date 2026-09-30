"""Role floors read like the roster (owner, 9/30/26): "role floors has 3
server roles and 3 morning/night drop downs - this makes no sense."

The list was every role any source named: the default "Server" front of
house and the team screen's always-offered Manager sat beside RPOWER's
Server AM / Server PM that nobody holds; each AM or PM role was asked for
both a morning and a night count; and every row carried the one-day
override's day, shift and count controls open."""
import os

SRC = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name):
    i = SRC.index(f"function {name}(")
    return SRC[i:SRC.index("\n  }\n", i)]


def test_the_role_list_is_the_roster_first_and_fallbacks_only_without_one():
    body = _fn("_rulRoles")
    assert body.index("_roster.roster") < body.index("if(!hasPeople){")
    fallback = body[body.index("if(!hasPeople){"):]
    assert "foh_roles" in fallback and "_teamRoles" in fallback
    assert "saved(floors[k])" in body               # an empty saved floor names no role


def test_an_am_or_pm_role_is_asked_for_its_own_daypart_only():
    assert "function _rulPart(role){return /\\bAM$/i.test(role)?'morning':(/\\bPM$/i.test(role)?'night':null);}" in SRC
    body = _fn("_rulFloorHtml")
    assert 'type="hidden" data-floor="' in body     # the other daypart's saved value rides along
    assert '<input type="hidden" data-ov-newpart value="' in body


def test_the_one_day_override_opens_on_request():
    body = _fn("_rulFloorHtml")
    assert 'data-ov-open="1">+ a day</button>' in body and '<span class="rul-add" hidden>' in body
    assert ".rul-add[hidden]{display:none}" in SRC
    save = SRC[SRC.index("window.saveRules=function"):]
    assert "m=mEl?mEl.value:''" in save
