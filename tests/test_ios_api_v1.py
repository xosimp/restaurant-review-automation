"""The paths, methods and request bodies the INSTALLED iPhone builds use are
frozen (blind re-audit 10/27/26, B6 "[P1] paths and request bodies of
shipped builds are not frozen").

tests/test_client_paths_resolve.py and tests/test_ios_request_body_parity.py
compare the server with the Swift in the SAME commit: renaming
`/mobile/api/labor/inbox/<id>/decide` to `/resolve` in mobile_api.py and in the
Swift passes both, and every phone already installed gets a 404 on that
action (no decode report: a 404 is not a decode failure). Phones update over
weeks, and today none of them names its build.

`tests/fixtures/ios_api_v1.json` is what the supported builds send, read
from the Swift at the commits they were built from (`generated_from`: the
last commit before the build header, abc4d51f, and the build after it):

* `reads`  — every path literal the app names, with the HTTP method where
  the call site says (test_client_paths_resolve's reading);
* `writes` — every write: method, path, the JSON keys its body sends, and
  which of those the server read when the snapshot was taken
  (test_ios_request_body_parity's reading of both halves).

The rules, until MOBILE_MIN_BUILD retires those builds:

1. every v1 path still reaches a route (taking its method, when known);
2. every v1 write still reaches a route that takes its method;
3. a body key a v1 build sends that the server read then is still read —
   a rename (`count` -> `eligible`) silently drops what old phones send;
4. a key the server reads now that a v1 build does not send, and that it did
   not read then, is listed in V1_OPTIONAL with why it is optional (read
   with a default) — or the server needs a key old phones never send.

Replace the snapshot (from the build last sent to the App Store, unioned
with the one being archived) in the commit that raises MOBILE_MIN_BUILD:

    python tests/test_ios_api_v1.py --write --ref <store build's commit> --ref HEAD
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_client_paths_resolve as paths_test  # noqa: E402
import test_ios_request_body_parity as body_test  # noqa: E402

# The deploy gate's contract slice (TESTING.md, Tiers): a few seconds, and what it
# guards reaches real people (a send) or every installed phone (a path).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "tests", "fixtures", "ios_api_v1.json")

# (method, rule, key) -> why a v1 build may leave it out: the route reads it
# with a default, so an old phone's request does what it always did. Add one
# only after reading the route; never to let a required key through.
_FLOOR_COVER = ("who runs the floor, sent only to answer a 409 needs_cover: without it a change that leaves a "
                "published minute with no manager is refused, and any other change lands as before (blind re-audit "
                "B2, the manager rule after publish)")
V1_OPTIONAL = {
    ('POST', '/mobile/api/labor/time-off/<int:request_id>/decide', 'covered_by'): _FLOOR_COVER,
    ('POST', '/mobile/api/labor/availability-changes/<int:change_id>/decide', 'covered_by'): _FLOOR_COVER,
    ('POST', '/mobile/api/labor/availability', 'covered_by'): _FLOOR_COVER,
    ('POST', '/mobile/api/labor/staff-settings', 'covered_by'): _FLOOR_COVER,
    # Priority on busy nights (10/30/26): read with b.get, None keeps what is
    # stored (staff_settings.upsert), so an old phone's save changes nothing.
    ('POST', '/mobile/api/labor/staff-settings', 'priority_roles'): (
        "the roles somebody is filled first in on busy shifts, set from the web's Team screen: absent, the stored "
        "value is kept"),
}


# ── reading the Swift of a commit ───────────────────────────────────────────

def _extract(ref, dest):
    """`ios/CavnarAI` as it was at `ref`, under `dest`."""
    out = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", ref, "ios/CavnarAI"], capture_output=True,
                         check=True, timeout=120).stdout
    tar_path = os.path.join(dest, "src.tar")
    with open(tar_path, "wb") as fh:
        fh.write(out)
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest, filter="data")
    os.unlink(tar_path)


def _swift_at(root):
    files = []
    ios = os.path.join(root, "ios", "CavnarAI")
    for dirpath, dirnames, filenames in os.walk(ios):
        dirnames[:] = [d for d in dirnames if d not in ("CavnarAITests", "build", "DerivedData")
                       and not d.endswith(".xcodeproj")]
        for name in filenames:
            if name.endswith(".swift"):
                p = os.path.join(dirpath, name)
                with open(p, encoding="utf-8") as fh:
                    files.append(body_test.SwiftFile(p, fh.read()))
    return body_test.Swift(files)


def read_client(root):
    """(reads, writes) of the Swift under `root`."""
    reads = {}
    for _f, _line, raw, lit, prefix, method in paths_test._swift_literals(root):
        if raw in paths_test.NOT_A_REQUEST:
            continue
        key = (lit, bool(prefix))
        reads.setdefault(key, set())
        if method:
            reads[key].add(method)
    writes = {}
    for w in body_test.collect_writes(_swift_at(root))[0]:
        keys = None if (w.shape.opaque or "{dynamic}" in w.shape.keys) else set(w.shape.keys)
        for p in w.paths:
            if not p.startswith(body_test.PREFIXES):
                continue
            slot = writes.setdefault((w.method, p.split("?")[0]), {"keys": set(), "opaque": False})
            if keys is None:
                slot["opaque"] = True
            else:
                slot["keys"] |= keys
    return reads, writes


def build_snapshot(refs, rules):
    reads, writes = {}, {}
    for ref in refs:
        tmp = tempfile.mkdtemp(prefix="cavnar-iosv1-")
        try:
            _extract(ref, tmp)
            r, w = read_client(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for k, methods in r.items():
            reads.setdefault(k, set()).update(methods)
        for k, slot in w.items():
            cur = writes.setdefault(k, {"keys": set(), "opaque": False})
            cur["keys"] |= slot["keys"]
            cur["opaque"] = cur["opaque"] or slot["opaque"]
    out_writes = []
    for (method, path), slot in sorted(writes.items()):
        hit = body_test.match_rules(path, method, rules)
        server = set().union(*(set(r["keys"]) for r in hit)) if hit else set()
        out_writes.append({"method": method, "path": path, "body": sorted(slot["keys"]),
                           "opaque": slot["opaque"], "server_read": sorted(server),
                           "read": sorted(slot["keys"] & server)})
    shas = [subprocess.run(["git", "-C", ROOT, "rev-parse", "--short=12", r], capture_output=True, text=True,
                           check=True).stdout.strip() for r in refs]
    return {"about": "What the supported iPhone builds call: tests/test_ios_api_v1.py. Replace only in the commit "
                     "that raises MOBILE_MIN_BUILD.",
            "generated_from": shas,
            "reads": [{"path": p, "prefix": prefix, "methods": sorted(m)} for (p, prefix), m in sorted(reads.items())],
            "writes": out_writes}


# ── the checks ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def snapshot():
    with open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def url_rules():
    import blueprints
    app = blueprints.url_map_app(template_folder=os.path.join(ROOT, "templates"))
    return [paths_test._segments(r.rule, r.methods) for r in app.url_map.iter_rules() if r.endpoint != "static"]


@pytest.fixture(scope="module")
def server_rules():
    return body_test._server_rules()


def unreached_reads(reads, rules):
    out = []
    for r in reads:
        if not paths_test.resolves(r["path"], r["prefix"], rules):
            out.append(f"{r['path']}: no route serves it")
            continue
        for m in r["methods"]:
            if not paths_test.resolves(r["path"], r["prefix"], rules, method=m):
                out.append(f"{m} {r['path']}: the route no longer takes {m}")
    return out


def write_problems(writes, rules):
    out = []
    for w in writes:
        hit = body_test.match_rules(w["path"], w["method"], rules)
        if not hit:
            out.append(f"{w['method']} {w['path']}: no route takes it")
            continue
        now = set().union(*(set(r["keys"]) for r in hit))
        for r in hit:
            for group in r["aliases"]:
                if now & set(group):
                    now |= set(group)
        for k in w["read"]:
            if k not in now:
                out.append(f"{w['method']} {w['path']}: the server no longer reads `{k}`, which installed builds "
                           "send — renamed or dropped")
        if w["opaque"]:
            continue
        for k in sorted(now - set(w["body"]) - set(w["server_read"])):
            rule = hit[0]["rule"]
            if (w["method"], rule, k) not in V1_OPTIONAL:
                out.append(f"{w['method']} {w['path']}: the server now reads `{k}`, which installed builds never "
                           f"send — list it in V1_OPTIONAL ({w['method']!r}, {rule!r}, {k!r}) with why it is optional")
    return out


def test_the_snapshot_is_the_installed_builds_and_is_not_empty(snapshot):
    assert snapshot["generated_from"] and len(snapshot["reads"]) >= 400 and len(snapshot["writes"]) >= 250
    assert any(r["path"] == "/mobile/api/me" for r in snapshot["reads"])
    assert any(w["path"] == "/staff/api/tasks/complete" and w["method"] == "POST" for w in snapshot["writes"])


def test_every_path_an_installed_build_reads_still_reaches_a_route(snapshot, url_rules):
    bad = unreached_reads(snapshot["reads"], url_rules)
    assert not bad, ("a path the installed builds call no longer resolves — every phone on those builds gets a "
                     "404/405 until MOBILE_MIN_BUILD retires them (keep the old route beside the new one):\n"
                     + "\n".join(bad[:40]))


def test_every_write_an_installed_build_sends_still_lands(snapshot, server_rules):
    bad = write_problems(snapshot["writes"], server_rules)
    assert not bad, ("a write the installed builds send changed under them:\n" + "\n".join(bad[:40]))


def test_every_v1_optional_entry_is_still_needed(snapshot, server_rules):
    writes = {(w["method"], w["path"]): w for w in snapshot["writes"]}
    for (method, rule, key), why in V1_OPTIONAL.items():
        assert why, (method, rule, key)
        live = [r for r in server_rules if r["rule"] == rule and method in r["methods"]]
        assert live and key in set().union(*(set(r["keys"]) for r in live)), f"{method} {rule} no longer reads {key}"
        assert any(body_test.match_rules(p, m, live) for (m, p) in writes if m == method), (method, rule)


# ── the guard guards ────────────────────────────────────────────────────────

def test_a_removed_route_a_narrowed_method_and_a_renamed_key_each_fail():
    rules = [paths_test._segments("/mobile/api/labor/inbox/<int:item_id>/resolve", ("POST",)),
             paths_test._segments("/mobile/api/home", ("GET", "HEAD"))]
    reads = [{"path": "/mobile/api/labor/inbox/{*}/decide", "prefix": False, "methods": ["POST"]},
             {"path": "/mobile/api/home", "prefix": False, "methods": ["POST"]},
             {"path": "/mobile/api/home", "prefix": False, "methods": ["GET"]}]
    assert unreached_reads(reads, rules) == ["/mobile/api/labor/inbox/{*}/decide: no route serves it",
                                             "POST /mobile/api/home: the route no longer takes POST"]
    server = [{"rule": "/mobile/api/x", "methods": ["POST"], "keys": ["eligible", "note"], "aliases": [],
               "_re": body_test._rule_regex("/mobile/api/x")}]
    renamed = [{"method": "POST", "path": "/mobile/api/x", "body": ["count", "note"], "opaque": False,
                "server_read": ["count", "note"], "read": ["count", "note"]}]
    got = write_problems(renamed, server)
    assert any("no longer reads `count`" in g for g in got), got
    assert any("now reads `eligible`" in g for g in got), got
    gone = [dict(renamed[0], path="/mobile/api/y")]
    assert write_problems(gone, server) == ["POST /mobile/api/y: no route takes it"]
    same = [dict(renamed[0], body=["eligible", "note"], server_read=["eligible", "note"], read=["eligible", "note"])]
    assert write_problems(same, server) == []


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--server-reads" in args:
        body_test._server_main(args[args.index("--server-reads") + 1])
    elif "--write" in args:
        refs = [args[i + 1] for i, a in enumerate(args) if a == "--ref"] or ["HEAD"]
        snap = build_snapshot(refs, body_test._server_rules())
        with open(SNAPSHOT, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"wrote {SNAPSHOT}: {len(snap['reads'])} reads, {len(snap['writes'])} writes from {snap['generated_from']}")
    else:
        print(__doc__)
