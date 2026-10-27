"""Cross-tenant crawl: every rule of every blueprint, called as restaurant A
with restaurant B's ids (audit #25 A03-05 / R19).

Isolation here is the app's own WHERE clauses. tests/test_tenant_isolation.py
checks specific helpers and scripts/check_tenant_scope.py counts SQL shapes;
nothing called the ROUTES with another tenant's ids. This does:

- **The app.** A subprocess imports `hosted_dashboard` (a throwaway volume,
  no provider keys, no .env, the network blocked, no scheduler) — the real
  composed app, all 22 blueprints and their hooks (the way
  tests/test_ios_request_body_parity.py builds it).
- **Tenant B** owns one row in EVERY table with a `restaurant_id` column, each
  with id `B_ID` and the marker string in its text columns; B's restaurant and
  logins carry the marker too. One id for every table means a route that
  reads `<int:anything>` by primary key without the tenant check meets B's row
  whatever table it reads.
- **As tenant A** (an owner on the full plan, every module, billing active —
  so no gate refuses the call before the handler runs; /staff rules as A's
  employee), every GET/POST/PUT/PATCH/DELETE of every rule is called with
  `B_ID` in each `<int:...>` path parameter, in the common JSON id keys of the
  body and in the query string. Fresh sessions per request, so a logout or a
  revoke in the crawl cannot silently turn the rest of it into 401s.
- **Asserted:** no response — of any status — carries B's marker (a READ
  leak; an error that echoes another tenant's row is a leak too), and
  B's rows are byte-for-byte unchanged after every rule (a WRITE leak) — any
  change is recorded against the rule and B's rows are put back.

A rule that serves another tenant's data by design (a public page whose token
is the credential) goes in `PUBLIC_BY_DESIGN` with the reason. Guards on the
guard: the crawl must reach most handlers (2xx), seed most tenant tables, and
run on the same blueprints `blueprints.all_blueprints()` lists, with the hooks
`blueprints.plan()` wires.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = "ZQXBRAVOMARKERQZX"
B_ID = 900001

CANARY = "/api/crawl-canary"

# (method, rule) -> why another tenant's marker may appear in the body.
PUBLIC_BY_DESIGN = {}

# Rules not called, with the reason. Kept tiny: everything else is crawled.
SKIP_RULES = {
    "/static/<path:filename>": "Flask's static files",
}

# JSON body keys that commonly carry a record id or a tenant id.
_ID_KEYS = ("id", "ids", "review_id", "item_id", "row_id", "shift_id", "employee_id", "user_id", "member_id",
            "membership_id", "contact_id", "guest_id", "campaign_id", "issue_id", "goal_id", "outcome_id",
            "task_id", "sheet_id", "doc_id", "cert_id", "request_id", "invoice_id", "order_id", "recipe_id",
            "ingredient_id", "product_id", "template_id", "note_id", "alert_id", "action_id", "rec_id",
            "decision_id", "draft_id", "post_id", "version_id", "history_id", "schedule_id", "week_id",
            "report_id", "file_id", "vendor_id", "supplier_id", "location_id", "conversation_id",
            "message_id", "thread_id", "announcement_id", "offer_id", "swap_id", "contact", "job_id")
_TENANT_KEYS = ("restaurant_id", "rid", "location", "location_id", "target_restaurant_id")
_VALUE_FIELDS = {"text": "crawl", "body": "crawl", "name": "Crawl", "title": "Crawl", "note": "crawl",
                 "reason": "crawl", "message": "crawl", "content": "crawl", "status": "done", "value": 1,
                 "amount": 1, "quantity": 1, "price": 1, "rating": 5, "enabled": True, "role": "server",
                 "decision": "approve", "action": "approve", "answer": "yes", "kind": "note",
                 "date": "2026-10-19", "day": "2026-10-19", "start": "2026-10-19", "end": "2026-10-20",
                 "start_date": "2026-10-19", "end_date": "2026-10-20", "week_start": "2026-10-19",
                 "employee_name": "Bravo Emp", "item": "crawl", "line": "crawl", "cert": "crawl"}


# ═════════════════════════════════════════════════════════════════════════════
# The subprocess: boot the real app, seed A and B, crawl
# ═════════════════════════════════════════════════════════════════════════════

def _crawl_main(out_path):
    import contextlib
    import hashlib
    import io
    import re
    import socket
    import sqlite3
    import time

    vol = tempfile.mkdtemp(prefix="cavnar-crawl-")
    sqlite3.connect(os.path.join(vol, "reviews.db")).close()          # pre-create: no legacy adoption
    for k in list(os.environ):
        if k.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_AUTH_TOKEN", "_KEY", "_PASSWORD", "_DSN")) or \
                k in ("DATABASE_URL", "CAVNAR_TEST_PG_URL", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
            os.environ.pop(k)
    os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", SECRET_KEY="test-secret",
                      CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                      REQUEST_LIMITS="0", AI_SHADOW_REVIEW="0", HOME_SNAPSHOT_COLD="request",
                      CAVNAR_POSTAL_ADDRESS="100 Test St, Testville, IL 60000")

    def _blocked(*a, **k):
        raise OSError("network blocked in the cross-tenant crawl")
    socket.create_connection = _blocked
    socket.socket.connect = lambda self, *a, **k: _blocked()
    sys.path.insert(0, ROOT)
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import hosted_dashboard
    import logging
    logging.disable(logging.CRITICAL)
    import auth
    import models
    import blueprints
    app = hosted_dashboard.app
    app.config["PROPAGATE_EXCEPTIONS"] = False
    auth.ENFORCE_PASSWORD_POLICY = False

    out = {"blueprints": sorted(app.blueprints),
           "expected_blueprints": sorted(bp.name for bp in blueprints.all_blueprints()),
           "before_request": [f.__module__ + "." + f.__qualname__ for f in app.before_request_funcs.get(None, [])],
           "after_request": [f.__module__ + "." + f.__qualname__ for f in app.after_request_funcs.get(None, [])],
           "plan_hooks": [[step, obj.__module__ + "." + obj.__qualname__] for step, obj in blueprints.plan()
                          if step in ("before_request", "after_request")]}

    # ── the canary: two deliberately unscoped routes, so the crawl proves it
    # sees a read leak and a write leak (registered before any request) ──
    def _canary_read(current_user, review_id):
        from flask import jsonify
        c = models.get_conn()
        row = c.execute("SELECT text FROM reviews WHERE id=?", (review_id,)).fetchone()      # unscoped ON PURPOSE
        c.close()
        return jsonify(ok=True, text=row["text"] if row else None)

    def _canary_write(current_user, review_id):
        from flask import jsonify
        c = models.get_conn()
        c.execute("UPDATE reviews SET text='canary' WHERE id=?", (review_id,))              # unscoped ON PURPOSE
        c.commit()
        c.close()
        return jsonify(ok=True)
    app.add_url_rule(CANARY + "/<int:review_id>", "crawl_canary_read", auth.login_required(_canary_read),
                     methods=["GET"])
    app.add_url_rule(CANARY + "/<int:review_id>/edit", "crawl_canary_write", auth.login_required(_canary_write),
                     methods=["POST"])

    # ── tenant A: an owner on the full plan, and one employee ──────────────
    from models import Restaurant, create_restaurant
    # A's login is a multi-location OWNER (it holds the location switcher),
    # and B sits in a location group of the SAME NAME under another owner's
    # email: a group check that compared only the group name would hand A
    # B's numbers.
    a_rid = create_restaurant(Restaurant(name="Alpha Crawl", owner_email="alpha-owner@crawl.test",
                                         service_tier="full", billing_status="active", module_reviews=1,
                                         module_labor=1, module_inventory=1, module_marketing=1,
                                         location_group="Crawl Group"))
    a_owner = auth.create_user(a_rid, "alphaowner", "alpha-owner@crawl.test", "alpha-crawl-pass-1")
    auth.set_user_role(a_owner, "owner")
    auth.upsert_membership(a_owner, a_rid, "owner")
    a_emp = auth.create_user(a_rid, "emp.alpha", "emp.alpha@staff.invalid", "x" * 24)
    auth.upsert_membership(a_emp, a_rid, "employee", employee_name="Alpha Emp")

    # ── tenant B: the restaurant row at B_ID, then one marked row per table ─
    b_tmp = create_restaurant(Restaurant(name=f"Bravo {MARKER}", owner_email=f"bravo-{MARKER.lower()}@crawl.test",
                                         service_tier="full", billing_status="active", module_reviews=1,
                                         module_labor=1, module_inventory=1, module_marketing=1,
                                         location_group="Crawl Group"))
    conn = sqlite3.connect(models.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE restaurants SET id=? WHERE id=?", (B_ID, b_tmp))
    conn.commit()
    b_owner = auth.create_user(B_ID, f"bravo{MARKER.lower()}", f"bravo-{MARKER.lower()}@crawl.test", "bravo-pass-123")
    auth.upsert_membership(b_owner, B_ID, "client")

    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    tenant_tables, cols_of = [], {}
    for t in tables:
        cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        cols_of[t] = cols
        if any(c["name"] == "restaurant_id" for c in cols) and t != "restaurants":
            tenant_tables.append(t)

    def _value(t, c):
        name, typ = c["name"].lower(), (c["type"] or "").upper()
        if name == "restaurant_id":
            return B_ID
        if name == "id" or name.endswith("_id"):
            return B_ID if ("INT" in typ or not typ) else f"{MARKER}-{name}"
        if "INT" in typ or "BOOL" in typ:
            return 1
        if any(x in typ for x in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
            return 1.0
        if "BLOB" in typ:
            return MARKER.encode()
        if name.endswith("_at") or name in ("created", "updated", "ts", "at", "when") or "expires" in name:
            # Far future: a platform-wide sweep of EXPIRED rows (2FA
            # challenges past their day, async jobs past their TTL) is by
            # design tenant-agnostic; B's rows are never expired, so only a
            # write aimed at B's row changes them.
            return "2099-01-01 12:00:00"
        if any(x in name for x in ("date", "day", "week", "month", "_on", "start", "end", "since", "until")):
            return "2026-10-19"
        if any(x in name for x in ("json", "payload", "data", "meta", "config", "items", "facts", "state",
                                   "context", "answers", "rows", "list", "settings", "options")):
            return json.dumps({"marker": MARKER, "text": MARKER})
        return MARKER

    seeded, unseeded = [], {}
    check_in = re.compile(r"CHECK constraint failed: (\w+) IN \('([^']*)'")
    for t in tenant_tables:
        names = [c["name"] for c in cols_of[t]]
        vals = [_value(t, c) for c in cols_of[t]]
        for _attempt in range(6):
            try:
                conn.execute(f'INSERT INTO "{t}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                             f'VALUES ({", ".join("?" for _ in names)})', vals)
                conn.commit()
                seeded.append(t)
                unseeded.pop(t, None)
                break
            except sqlite3.IntegrityError as e:
                conn.rollback()
                unseeded[t] = f"{type(e).__name__}: {e}"[:160]
                m = check_in.search(str(e))          # an enum column: take its first allowed value
                if not m or m.group(1) not in names:
                    break
                vals[names.index(m.group(1))] = m.group(2)
            except sqlite3.Error as e:
                conn.rollback()
                unseeded[t] = f"{type(e).__name__}: {e}"[:160]
                break
    # The restaurants row's own text columns carry the marker too (its name does).
    out["tenant_tables"] = len(tenant_tables)
    out["seeded"] = len(seeded)
    out["unseeded"] = unseeded
    models._invalidate_request_cache(B_ID)

    # ── B's snapshot: every tenant table's B rows, and B's restaurants row ──
    snap_tables = seeded + [t for t in tenant_tables if t not in seeded]

    def _rows(t):
        if t == "restaurants":
            return [tuple(r) for r in conn.execute("SELECT * FROM restaurants WHERE id=?", (B_ID,))]
        return [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" WHERE restaurant_id=? ORDER BY rowid',
                                               (B_ID,))]

    def _digest(rows):
        return hashlib.sha1(repr(rows).encode("utf-8", "replace")).hexdigest()

    def snapshot():
        return {t: _digest(_rows(t)) for t in snap_tables + ["restaurants"]}

    saved = {t: _rows(t) for t in snap_tables + ["restaurants"]}
    base = snapshot()

    def restore(t):
        cols = [c["name"] for c in cols_of[t]]
        if t == "restaurants":
            conn.execute("DELETE FROM restaurants WHERE id=?", (B_ID,))
        else:
            conn.execute(f'DELETE FROM "{t}" WHERE restaurant_id=?', (B_ID,))
        for row in saved[t]:
            conn.execute(f'INSERT INTO "{t}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                         f'VALUES ({", ".join("?" for _ in cols)})', row)
        conn.commit()

    # A's own state, put back after each request (a route the crawl calls
    # with nonsense may legitimately change A's settings).
    a_keep = conn.execute("SELECT service_tier, billing_status, module_reviews, module_labor, module_inventory, "
                          "module_marketing FROM restaurants WHERE id=?", (a_rid,)).fetchone()

    def keep_a():
        conn.execute("UPDATE restaurants SET service_tier=?, billing_status=?, module_reviews=?, module_labor=?, "
                     "module_inventory=?, module_marketing=?, pos_owner_hold=NULL WHERE id=?",
                     tuple(a_keep) + (a_rid,))
        conn.execute("UPDATE users SET role='owner', is_active=1 WHERE id=?", (a_owner,))
        conn.commit()
        models._invalidate_request_cache(a_rid)

    # ── the crawl ────────────────────────────────────────────────────────────
    def fill(rule):
        def sub(m):
            conv, _, name = m.group(1).rpartition(":")
            conv = conv or "string"
            if conv == "int":
                return str(B_ID)
            if conv == "float":
                return "1.0"
            low = name.lower()
            if "date" in low or low in ("day", "week", "week_start", "start", "end"):
                return "2026-10-19"
            if low in ("rid", "restaurant_id"):
                return str(B_ID)
            return "x"
        return re.sub(r"<([^>]+)>", sub, rule)

    body = {k: B_ID for k in _ID_KEYS}
    body["ids"] = [B_ID]
    body.update({k: B_ID for k in _TENANT_KEYS})
    # The second body also carries the common value fields, so a handler
    # that validates its fields before it looks the id up still reaches the
    # lookup.
    full_body = dict(body, **_VALUE_FIELDS)
    exceptions = {}

    def _record(sender, exception, **extra):
        from flask import request as _rq
        key = f"{_rq.method} {_rq.url_rule.rule if _rq.url_rule else _rq.path}"
        exceptions.setdefault(key, f"{type(exception).__name__}: {str(exception)[:200]}")
    from flask import got_request_exception
    got_request_exception.connect(_record, app)
    query = "id={0}&restaurant_id={0}&rid={0}&review_id={0}&location_id={0}".format(B_ID)
    marker = MARKER.encode()
    methods = ("GET", "POST", "PUT", "PATCH", "DELETE")
    requests_made = reached = 0
    leaks, writes, server_errors, slow = [], [], [], []
    by_status, by_prefix = {}, {}
    rules = sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, sorted(r.methods or ())))
    started = time.monotonic()
    for rule in rules:
        if rule.rule in SKIP_RULES:
            continue
        path = fill(rule.rule)
        staff = rule.rule.startswith("/staff")
        calls = [(m, b) for m in methods if m in (rule.methods or ())
                 for b in ((None,) if m == "GET" else (body, full_body))]
        for method, payload in calls:
            c = app.test_client()
            if staff:
                tok = auth.create_staff_session(a_emp, a_rid)
                c.set_cookie("staff_session", tok)
                headers = {"Authorization": f"Bearer {tok}", "X-CSRF": "crawl-csrf"}
            else:
                c.set_cookie("session_token", auth.create_session(a_owner, restaurant_id=a_rid))
                headers = {"Authorization": f"Bearer {auth.create_session(a_owner, device_type='ios', restaurant_id=a_rid)}",
                           "X-CSRF": "crawl-csrf", "Accept": "application/json"}
            c.set_cookie("csrf_js", "crawl-csrf")
            t0 = time.monotonic()
            try:
                if method == "GET":
                    resp = c.open(f"{path}?{query}", method=method, headers=headers)
                else:
                    resp = c.open(path, method=method, json=payload, headers=headers)
                status, data = resp.status_code, resp.get_data()
            except Exception as e:                       # the test client raised (PROPAGATE off: rare)
                status, data = 599, repr(e).encode()
            took = time.monotonic() - t0
            requests_made += 1
            by_status[str(status)] = by_status.get(str(status), 0) + 1
            pre = "/" + rule.rule.split("/")[1]
            by_prefix.setdefault(pre, {}).setdefault(str(status), 0)
            by_prefix[pre][str(status)] += 1
            if 200 <= status < 300:
                reached += 1
            if took > 3:
                slow.append([method, rule.rule, round(took, 1)])
            if status >= 500 and [method, rule.rule, status] not in server_errors:
                server_errors.append([method, rule.rule, status])
            if marker in data and not any(l[:2] == [method, rule.rule] for l in leaks):
                i = data.find(marker)
                leaks.append([method, rule.rule, status, data[max(0, i - 160):i + 60].decode("utf-8", "replace")])
            keep_a()
        now = snapshot()
        changed = sorted(t for t in now if now[t] != base[t])
        if changed:
            writes.append([rule.rule, sorted(set(rule.methods or ()) & set(methods)), changed])
            for t in changed:
                restore(t)
            models._invalidate_request_cache(B_ID)
    out.update(requests=requests_made, reached=reached, by_status=by_status, by_prefix=by_prefix, leaks=leaks, writes=writes,
               server_errors=server_errors, exceptions=exceptions, slow=slow, seconds=round(time.monotonic() - started, 1),
               rules=len(rules))
    with open(out_path, "w") as fh:
        json.dump(out, fh)
    import shutil
    shutil.rmtree(vol, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# The tests
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def crawl():
    out = tempfile.NamedTemporaryFile(prefix="crawl-", suffix=".json", delete=False).name
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
    env.pop("DATABASE_URL", None)
    proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--crawl", out], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-2000:] + "\n" + proc.stderr[-4000:]
    with open(out) as fh:
        result = json.load(fh)
    os.unlink(out)
    return result


def test_the_crawl_ran_on_the_real_app_with_every_blueprint(crawl):
    assert crawl["blueprints"] == crawl["expected_blueprints"]
    assert len(crawl["blueprints"]) >= 22
    assert crawl["rules"] >= 1500


def test_the_real_app_wires_the_hooks_blueprints_plans(crawl):
    """hosted_dashboard registers through blueprints.register_all: the booted
    app's app-wide hooks include, in order, the ones the plan names (support
    masking, then the CSRF cookie last; the staff portal's CSRF check)."""
    after = crawl["after_request"]
    before = crawl["before_request"]
    for step, name in crawl["plan_hooks"]:
        assert name in (before if step == "before_request" else after), name
    planned_after = [n for s, n in crawl["plan_hooks"] if s == "after_request"]
    assert [n for n in after if n in planned_after] == planned_after
    assert after[-1] == "csrf.ensure_csrf_cookie"


def test_the_crawl_reached_the_handlers(crawl):
    """Guards the guard: a crawl refused at the door (401/403/402 on
    everything) would pass every assertion below."""
    assert crawl["requests"] >= 1800, crawl["by_status"]
    assert crawl["reached"] >= 600, crawl["by_status"]
    assert crawl["seeded"] >= 0.9 * crawl["tenant_tables"], crawl["unseeded"]


def test_the_crawl_catches_the_canarys_read_and_write_leaks(crawl):
    """Guards the guard: the two deliberately unscoped routes the crawl
    registers are both caught — a read by primary key that returns B's text,
    and a write by primary key that changes B's row."""
    assert any(l[1].startswith(CANARY) and l[0] == "GET" for l in crawl["leaks"]), crawl["leaks"]
    assert any(w[0].startswith(CANARY) and w[2] == ["reviews"] for w in crawl["writes"]), crawl["writes"]


def test_no_route_serves_another_tenants_row(crawl):
    leaks = [l for l in crawl["leaks"] if (l[0], l[1]) not in PUBLIC_BY_DESIGN and not l[1].startswith(CANARY)]
    assert not leaks, "\n".join(f"{m} {r} -> {s}: …{ctx}…" for m, r, s, ctx in leaks)


def test_no_route_changes_another_tenants_rows(crawl):
    writes = [w for w in crawl["writes"] if not w[0].startswith(CANARY)]
    assert not writes, "\n".join(f"{r} {m}: {t}" for r, m, t in writes)


def test_no_route_raises_on_another_tenants_ids(crawl):
    """Another tenant's id (or a number where a string was expected) is a
    4xx, never an unhandled exception: a handler that crashes on B's id has
    usually just read B's row."""
    assert crawl["exceptions"] == {}, crawl["exceptions"]


def test_every_public_by_design_entry_still_answers_with_the_marker(crawl):
    """A stale allowance is a hole waiting for a route: each entry must still
    be a rule that, by design, shows another tenant's row."""
    seen = {(l[0], l[1]) for l in crawl["leaks"]}
    assert set(PUBLIC_BY_DESIGN) <= seen, set(PUBLIC_BY_DESIGN) - seen


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--crawl":
    _crawl_main(sys.argv[2])
    os._exit(0)
