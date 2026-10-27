"""The phone's top GET payloads, as the server really sends them (release-
safety audit #25: A04-04, A06-03, A05-14; widened in the blind re-audit
10/27/26 to every main screen's read, the manager's views and the staff app).

About 580 of the app's Decodable structs are strict: a key renamed, a null
where Swift holds a non-optional, a fraction where it holds an Int, and the
whole screen fails to decode on every installed build until a new build
clears App Store review. TRUST-1 (10/10/26) was exactly that, and the iOS
test that should have caught it decoded a hand-written fixture.
tests/test_ios_schedule_payload_contract.py fixed it for the schedule; this
extends the same pattern to every screen's main read:

    me, account, home (it carries the brief), reviews, notifications, labor,
    food_cost (analytics), food_cost_bar, marketing, intel, actions,
    staff_shifts and staff_me (the staff app's Today and Me), and the Daily
    Sales Report's night (dsr_preview) and week (dsr_week), each as the
    owner and as a manager.

* Each payload is captured from the real route (tests/ios_shape_capture.py:
  the fictional demo store, built by the real pipelines) and compared with
  its committed fixture, ios/CavnarAI/CavnarAITests/Fixtures/<route>.json —
  every key, the kind of every value, all the way down
  (test_ios_schedule_payload_contract.shape_mismatches). A server change that
  would break the phone fails HERE.
* ResponseShapeContractTests.swift decodes every fixture with the app's real
  model, so a model change that cannot read the server fails there.
* <route>.v1.json is the shape the builds installed today read. It is never
  rewritten by a re-capture: a key in it that the server stops sending fails
  (`test_no_key_a_supported_build_reads_was_removed`) — fields are added,
  never renamed or removed, until MOBILE_MIN_BUILD retires every build that
  reads them (client_compat). Then the v1 copy is replaced by a v2 one, in
  the same commit that raises the minimum.
* The hand-written fixtures the DSR and Bar view tests read their content
  from (dsr_preview_scenario, dsr_week_scenario, food_cost_bar_scenario —
  they were dsr_preview, dsr_week and food_cost_bar until these captures took
  those names) are held to the server's shape too.

Re-capture after an intended payload change (then check the Swift tests):
    CAVNAR_WRITE_IOS_FIXTURES=1 python -m pytest -q tests/test_ios_response_shapes.py
A new route's first capture also writes its <route>.v1.json; an existing one
is only ever rewritten with CAVNAR_WRITE_IOS_V1=1, deliberately.
"""
import json
import os
import re

import pytest

import ios_shape_capture as cap
import ios_swift_optionality as swift_opt
from test_ios_schedule_payload_contract import _merged, shape_mismatches

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
FIXTURES = os.path.join(IOS, "CavnarAITests", "Fixtures")
SWIFT_TEST = os.path.join(IOS, "CavnarAITests", "ResponseShapeContractTests.swift")
ROUTE_NAMES = tuple(cap.ROUTES) + cap.DSR_ROUTES
# The hand-written fixtures the view tests read content from, and the live
# payload each part of them must stay shaped like.
SCENARIOS = {
    "dsr_preview_scenario": (("owner", "dsr_preview", "owner"), ("manager", "dsr_preview", "manager"),
                             ("v1_owner", "dsr_preview", "owner")),
    "dsr_week_scenario": (("owner", "dsr_week", "owner"), ("manager", "dsr_week", "manager")),
    "food_cost_bar_scenario": ((None, "food_cost_bar", None),),
}

# Keys a v1 build reads that the server sends only some nights — absent is
# not removed. Each with why; matched on the end of the key path.
CONDITIONAL = {
    # dsr.kpis week pace: the typical sales still to come and the budget still
    # needed are written only while nights are left in the fiscal week, and
    # last year only when every measured night has its own last-year night.
    "pace.week.typical_left": "only while nights are left in the fiscal week",
    "pace.week.typical_per_night": "only while nights are left in the fiscal week",
    "pace.week.budget_left": "only with a budget for the week",
    "pace.week.budget_per_night_needed": "only while nights are left and a budget is set",
    "pace.week.last_year": "only when every measured night has a last-year night",
    "pace.week.last_year_pct": "only when every measured night has a last-year night",
}

# A null the server may send where a fixture holds a value, though a model
# that decodes the route reads the key strictly (ios_swift_optionality says
# "required") — route -> {key path: why it is safe}. Add one only after
# checking every Swift model that decodes the route tolerates the null (a
# custom init the parser misreads), never to make a real break pass.
NULLABLE = {}

# A list of objects says which keys every element carries only from this
# many elements up; fewer is too little to tell a key from a coincidence.
MIN_LIST = 3
# A schema key: snake_case. Anything else is data used as a key — a
# department, an hour, "cat:Food" — whose presence is the night's, not the
# contract's.
_SCHEMA_KEY = re.compile(r"^[a-z_][a-z0-9_]*$")
# Volatile values a re-capture always changes, kept out of the files so a
# re-capture is a reviewable diff (the schedule contract's list, and the
# capture's clocks).
_VOLATILE = re.compile(r'("(?:[a-z_]*_at|as_of|capability_version|generation_id|run_id|correlation_id|job_id|'
                       r'local_now|data_as_of|portal_token|join_code|token)"\s*:\s*)"[^"]*"')


def _path(name):
    return os.path.join(FIXTURES, f"{name}.json")


def _load(name):
    with open(_path(name), encoding="utf-8") as f:
        return json.load(f)


# A staff calendar feed's secret path segment (staff_calendar_link): a token
# minted by the capture's own database, kept out of the files all the same.
_FEED_TOKEN = re.compile(r"(/staff/cal/)[A-Za-z0-9_-]{16,}(\.ics)")


def _write(name, payload):
    text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
    text = _VOLATILE.sub(r'\1"<volatile>"', text)
    text = _FEED_TOKEN.sub(r"\1<volatile>\2", text)
    with open(_path(name), "w", encoding="utf-8") as f:
        f.write(text + "\n")


_LIVE = {}


@pytest.fixture
def live(db_path, monkeypatch):
    """Every payload, captured once per test process (inside the first test
    that asks, under that test's guards: no network, no live model)."""
    if "payloads" not in _LIVE:
        payloads = cap.capture(monkeypatch, db_path)
        if os.environ.get("CAVNAR_WRITE_IOS_FIXTURES") == "1":
            for name in ROUTE_NAMES:
                _write(name, payloads[name])
                if not os.path.exists(_path(name + ".v1")) or os.environ.get("CAVNAR_WRITE_IOS_V1") == "1":
                    _write(name + ".v1", payloads[name])
        _LIVE["payloads"] = payloads
    return _LIVE["payloads"]


# ── what a supported build reads ────────────────────────────────────────────

def required_paths(v, p="$"):
    """The key paths a build reading `v` relies on: every schema key of every
    object, and of a list of at least MIN_LIST objects the keys every element
    carries (with their own required keys, in common)."""
    out = set()
    if isinstance(v, dict):
        for k, x in v.items():
            if not _SCHEMA_KEY.match(k):
                continue
            q = f"{p}.{k}"
            out.add(q)
            out |= required_paths(x, q)
    elif isinstance(v, list):
        objs = [x for x in v if isinstance(x, dict)]
        if len(objs) >= MIN_LIST and len(objs) == len(v):
            common = set.intersection(*[{k for k in o if _SCHEMA_KEY.match(k)} for o in objs])
            for k in common:
                q = f"{p}[].{k}"
                out.add(q)
                out |= set.intersection(*[required_paths(o[k], q) for o in objs])
    return out


def _present(v, p="$", keys=None, objs=None):
    """(key paths, object paths) the live payload has; a list's elements
    merged under `[]`."""
    keys = set() if keys is None else keys
    objs = set() if objs is None else objs
    if isinstance(v, dict):
        objs.add(p)
        for k, x in v.items():
            keys.add(f"{p}.{k}")
            _present(x, f"{p}.{k}", keys, objs)
    elif isinstance(v, list):
        for x in v:
            _present(x, p + "[]", keys, objs)
    return keys, objs


def removed_paths(v1, live):
    """Key paths a v1 build reads that `live` no longer sends where their
    object is still there (an object absent today — an empty list, a null —
    says nothing either way), less the CONDITIONAL ones."""
    keys, objs = _present(live)
    out = []
    for q in sorted(required_paths(v1)):
        parent = q.rsplit(".", 1)[0]
        if parent not in objs or q in keys:
            continue
        if any(q.endswith("." + c) for c in CONDITIONAL):
            continue
        out.append(q)
    return out


def null_paths(fixture, live, path="$"):
    """Key paths (list elements merged under `[]`) where `live` is null and
    `fixture` holds a value — the break shape_mismatches' "null matches
    anything" never saw."""
    if live is None:
        return [path] if fixture is not None else []
    if fixture is None:
        return []
    out = []
    if isinstance(fixture, dict) and isinstance(live, dict):
        for k in sorted(set(fixture) & set(live)):
            out += null_paths(fixture[k], live[k], f"{path}.{k}")
    elif isinstance(fixture, list) and isinstance(live, list):
        values = [x for x in fixture if x is not None]
        if values:
            ref = _merged(values)
            for item in live:
                out += null_paths(ref, item, path + "[]")
    return out


def _views(name, payload):
    """(label, payload) per view the phone decodes: the DSR captures hold an
    owner's and a manager's, each one report."""
    if name in cap.DSR_ROUTES:
        return [(v, (payload or {}).get(v)) for v in ("owner", "manager")]
    return [(None, payload)]


def decode_problems(name, payload):
    """What the Swift models that decode route `name` would refuse in
    `payload`, simulated (ios_swift_optionality.decode_problem) — the
    server-side mirror of ResponseShapeContractTests while it cannot run in
    CI, and a check on the LIVE payload before any re-capture."""
    out = []
    for view, part in _views(name, payload):
        out += [f"{p}{' [' + view + ']' if view else ''}" for p in swift_opt.route_decode_problems(name, part)]
    return out


def null_breaks(name, fixture, live):
    """A null in `live` where `fixture` holds a value AND a Swift model that
    decodes route `name` reads the key strictly — a decode failure on every
    installed phone — less NULLABLE."""
    allowed = NULLABLE.get(name, {})
    out = []
    for (view, fx), (_v, lv) in zip(_views(name, fixture), _views(name, live)):
        for q in sorted(set(null_paths(fx, lv))):
            if q in allowed:
                continue
            required, why = swift_opt.requirement(name, q)
            if required:
                out.append(f"{q}{' [' + view + ']' if view else ''}: the server sent null where {why}")
    return out


# ── the tests ───────────────────────────────────────────────────────────────

def test_the_phone_payloads_match_the_fixtures_the_ios_tests_decode(live):
    problems = []
    for name in ROUTE_NAMES:
        assert os.path.exists(_path(name)), f"{name}.json is missing — capture it (CAVNAR_WRITE_IOS_FIXTURES=1)"
        mm = (shape_mismatches(_load(name), live[name]) + null_breaks(name, _load(name), live[name])
              + decode_problems(name, live[name]))
        problems += [f"{name}: {m}" for m in mm[:8]]
    assert problems == [], (
        "the server's payload changed shape under the phone — re-capture (CAVNAR_WRITE_IOS_FIXTURES=1) and "
        "check ResponseShapeContractTests.swift still decodes it:\n" + "\n".join(problems))


def test_no_key_a_supported_build_reads_was_removed(live):
    """<route>.v1.json is what the builds installed today read. Removing or
    re-typing a key in it breaks them until MOBILE_MIN_BUILD retires them."""
    problems = []
    for name in ROUTE_NAMES:
        assert os.path.exists(_path(name + ".v1")), f"{name}.v1.json is missing"
        v1 = _load(name + ".v1")
        problems += [f"{name}: {q} is no longer sent" for q in removed_paths(v1, live[name])[:8]]
        problems += [f"{name}: {m}" for m in shape_mismatches(v1, live[name])[:8]]
        problems += [f"{name}: {m}" for m in null_breaks(name, v1, live[name])[:8]]
    assert problems == [], (
        "a key the installed builds read was removed or changed kind. Keep sending it until MOBILE_MIN_BUILD "
        "retires those builds (then replace <route>.v1.json with a v2 copy in that commit). A key the server "
        "only sends some nights is CONDITIONAL here, with why:\n" + "\n".join(problems))


def test_every_fixture_decodes_with_the_swift_models_simulated():
    """Every committed fixture (current and v1) read the way the app's models
    read it: a required key missing or null, a string where a number is held,
    a fraction where an Int is (blind re-audit 10/27/26). No capture needed."""
    problems = []
    for name in ROUTE_NAMES:
        for suffix in ("", ".v1"):
            problems += [f"{name}{suffix}: {p}" for p in decode_problems(name, _load(name + suffix))]
    assert problems == [], "\n".join(problems[:30])


def test_the_decode_simulation_refuses_what_jsondecoder_refuses():
    me = _load("me.v1")
    broken = json.loads(json.dumps(me))
    broken["user"].pop("username")
    assert decode_problems("me", broken) == [
        "SessionStore.MeResponse: $.user.username: missing — User.username is a non-optional String"]
    broken = json.loads(json.dumps(me))
    broken["user"]["id"] = 1.5
    assert "1.5" in decode_problems("me", broken)[0]
    broken = json.loads(json.dumps(me))
    broken["ok"] = "yes"
    assert "where Bool is held" in decode_problems("me", broken)[0]
    assert decode_problems("me", me) == []


def test_the_hand_written_scenarios_stay_shaped_like_the_server(live):
    problems = []
    for name, parts in SCENARIOS.items():
        scenario = _load(name)
        for part, route, view in parts:
            fixture = scenario[part] if part else scenario
            payload = live[route][view] if view else live[route]
            problems += [f"{name}[{part}]: {m}" for m in shape_mismatches(fixture, payload)[:8]]
    assert problems == [], "\n".join(problems)


def test_every_captured_fixture_is_decoded_by_the_ios_contract_test():
    src = open(SWIFT_TEST, encoding="utf-8").read()
    for name in ROUTE_NAMES:
        assert f'"{name}"' in src, f"ResponseShapeContractTests.swift does not decode {name}.json"
    for name in SCENARIOS:
        assert os.path.exists(_path(name)), name


def test_the_fixtures_are_the_fictional_store_and_carry_no_credential():
    """The repo is public (audit #25 R1): every address in a fixture is a
    reserved one, and no token or key is in any of them."""
    emails = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    for name in ROUTE_NAMES:
        for suffix in ("", ".v1"):
            text = open(_path(name + suffix), encoding="utf-8").read()
            for m in emails.finditer(text):
                domain = m.group(1).lower()
                assert domain.endswith((".example", ".invalid", ".test", "cavnar.ai")), (name + suffix, m.group(0))
            for secret in (r"demo-store:", r"Bearer\s", r"sk_live_", r"\bre_[A-Za-z0-9]{16,}", r"AKIA[0-9A-Z]{16}",
                           r"/staff/cal/[A-Za-z0-9_-]{16,}"):
                assert not re.search(secret, text), (name + suffix, secret)


def test_the_removal_check_catches_the_break_it_exists_for():
    v1 = {"ok": True, "user": {"id": 1, "name": "A"}, "rows": [{"n": 1, "x": 1}, {"n": 2}, {"n": 3}],
          "metrics": {"net": 1.0, "cat:Food": 2.0}, "pace": {"week": {"typical_left": 3.0, "nights": 2}}}
    same = json.loads(json.dumps(v1))
    assert removed_paths(v1, same) == []
    # A renamed key in an object and in every element of a list: both fail.
    gone = json.loads(json.dumps(v1))
    gone["user"]["full_name"] = gone["user"].pop("name")
    for r in gone["rows"]:
        r["count"] = r.pop("n")
    assert removed_paths(v1, gone) == ["$.rows[].n", "$.user.name"]
    # Not a removal: a data key, a key only some elements carry, a list or
    # object absent today, a conditional key.
    fine = json.loads(json.dumps(v1))
    fine["metrics"].pop("cat:Food")
    fine["rows"][0].pop("x")
    fine["pace"]["week"].pop("typical_left")
    assert removed_paths(v1, fine) == []
    assert removed_paths(v1, dict(fine, rows=[], user=None)) == []
    # Too few elements to say what every element carries.
    assert required_paths({"a": [{"k": 1}, {"k": 2}]}) == {"$.a"}


def test_the_null_check_catches_a_value_that_goes_null_under_a_non_optional():
    """The third break class the guard names (blind re-audit 10/27/26): a
    null where Swift holds a non-optional. `me.v1.json` with user.username,
    ok or the whole user set to null passed every check before."""
    v1 = _load("me.v1")
    for mutate, want in (
            (lambda d: d["user"].update(username=None), "$.user.username"),
            (lambda d: d.update(ok=None), "$.ok"),
            (lambda d: d.update(user=None), "$.user")):
        live = json.loads(json.dumps(v1))
        mutate(live)
        assert removed_paths(v1, live) == [] and shape_mismatches(v1, live) == []     # the old checks
        got = null_breaks("me", v1, live)
        assert len(got) == 1 and got[0].startswith(want + ":") and "non-optional" in got[0], got
    # An optional key going null is fine (MeResponse.timezone: String?).
    live = json.loads(json.dumps(v1))
    live["timezone"] = None
    assert null_breaks("me", v1, live) == []
    # A null element of a list the model holds as [T] breaks; a key no model reads never does.
    home = _load("home.v1")
    live = json.loads(json.dumps(home))
    live["modules"] = [None] + live["modules"]
    live["not_read_by_any_model"] = None
    assert [m.split(":")[0] for m in null_breaks("home", dict(home, not_read_by_any_model=1), live)] == [
        "$.modules[]"]
    # Each DSR view is checked against the report model.
    dsr = _load("dsr_preview.v1")
    live = json.loads(json.dumps(dsr))
    live["manager"]["business_date"] = None
    assert [m.split(":")[0] for m in null_breaks("dsr_preview", dsr, live)] == ["$.business_date [manager]"]


def test_the_optionality_is_read_from_the_swift_models():
    req = swift_opt.requirement
    assert req("me", "$.user.username")[0] and req("me", "$.user")[0] and req("me", "$.ok")[0]
    assert not req("me", "$.timezone")[0]                               # String?
    assert req("home", "$.restaurant_name")[0] and not req("home", "$.location_name")[0]
    assert req("staff_me", "$.employee")[0], "guard let e = try? … else { throw }: required after all"
    assert not req("labor", "$.data_caveat")[0] and req("labor", "$.role_summary[].role")[0]
    assert not req("me", "$.no_model_reads_this")[0]
    # Every route the capture holds is decoded by some model the parser finds.
    roots = swift_opt.root_types()
    for name in ROUTE_NAMES:
        assert roots.get(name), f"no ResponseShapeContractTests decode names {name}"
        for t in roots[name]:
            assert swift_opt.props(swift_opt._find(t)), f"the parser read nothing of {t} ({name})"


def test_every_nullable_entry_is_still_needed():
    for name, paths in NULLABLE.items():
        assert name in ROUTE_NAMES, name
        for q, why in paths.items():
            assert why and swift_opt.requirement(name, q)[0], f"{name} {q}: no model requires it any more"
