"""Clicking another tab while the Schedule Studio is open opens THAT tab
(owner, 10/1/26: "clicking Home on the Schedule tab switches to Reports").

Any other tab closes the Studio first, in the capture phase
(studioClose({toTab: true})). It called history.back() — asynchronous, so
its popstate landed after the clicked tab had routed itself and cavRoute
sent the owner to wherever they were before the Studio (Reports). Closing
for another tab now swaps the Studio's address for the dashboard's in place.
"""
import json
import pathlib
import subprocess

SRC = (pathlib.Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()


def _studio_close():
    fn = SRC[SRC.index("function studioClose(opts) {"):]
    return fn[:fn.index("\n}\n") + 3]


def _run(call, from_dash=True):
    js = """
var calls = [];
var window = {location: {pathname: '/schedule/studio', hash: ''}};
var history = {back: function () { calls.push('back'); },
               replaceState: function (s, t, u) { calls.push('replace:' + u); window.location.pathname = u.split('#')[0]; }};
window.history = history;
var document = {getElementById: function () { return {hidden: false}; },
                body: {classList: {remove: function () {}}}};
var _ssOpen = true, _ssFromDash = %s;
function swCloseEdit() {} function ssTipHide() {} function ssDock() {} function ssTabOn() {} function ssLaunchRefresh() {}
%s
%s;
console.log(JSON.stringify({calls: calls, open: _ssOpen, fromDash: _ssFromDash}));
""" % ("true" if from_dash else "false", _studio_close(), call)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_another_tab_closes_the_studio_without_a_late_back_navigation():
    got = _run("studioClose({toTab: true})")
    assert got["calls"] == ["replace:/"] and not got["open"] and not got["fromDash"]


def test_closing_the_studio_itself_still_steps_back_to_the_dashboard():
    assert _run("studioClose()")["calls"] == ["back"]
    assert _run("studioClose()", from_dash=False)["calls"] == ["replace:/#labor"]
