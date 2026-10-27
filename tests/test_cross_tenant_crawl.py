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
- **Numbers too** (blind re-audit 10/26/26 B3): B's numeric columns carry a
  canary (NUM_INT / NUM_REAL), so an aggregate that drops its tenant filter —
  a SUM of B's sales beside A's empty data — is caught as surely as B's text.
- **As A's manager**, every GET again, asserting no response carries A's
  salaries: A has a salaried person (SALARY_ANNUAL) and labor history, so an
  all-in figure, a day's salary share or the salary itself in a manager's
  answer is a leak (the owner's alone — CLAUDE.md, labor all-in).
- **On Postgres** when the suite is (CAVNAR_TEST_PG_URL — the nightly's
  `postgres` job): the app is routed to a scratch database of its own on
  that server, seeded the same way, so a WHERE clause SQLite forgives and
  Postgres reads differently is crawled where production runs.

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
# On Postgres it runs nightly, not in gate-postgres (`pg_nightly`); the crawl
# is one module fixture, so its ceiling covers the whole crawl.
pytestmark = [pytest.mark.contract, pytest.mark.pg_nightly, pytest.mark.timeout(900)]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = "ZQXBRAVOMARKERQZX"
B_ID = 900001
# B's numeric canaries: every INTEGER column that is not an id carries
# NUM_INT, every REAL one NUM_REAL; neither may appear in A's answers.
NUM_INT = 7319468
NUM_REAL = 7319468.25
NUM_MARKER = str(NUM_INT)
# A's salaried person: a manager's answer may never carry the salary, a
# day's share of it, or an all-in figure (MANAGER_SALARY_KEYS with a value).
SALARY_ANNUAL = 1873452.0
MANAGER_SALARY_KEYS = ("salaried_cost", "salaried_total_cost", "salaried_total_pct", "salaried_day",
                       "salaried_per_day", "all_in_pct", "all_in_projected", "salaried_vs_target_pts")

CANARY = "/api/crawl-canary"
# Unscoped ON PURPOSE, registered in the crawl's own app: a SUM over every
# tenant (the numeric canary must catch it) and a salary handed to anyone
# (the manager pass must catch it).
CANARY_SUM = CANARY + "-sum"
CANARY_SALARY = CANARY + "-salary"

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
    # A scratch Postgres database of the crawl's own (the fixture creates and
    # drops it on CAVNAR_TEST_PG_URL's server), reached the way the suite's
    # Postgres mode reaches one: models._PG_ROUTES — never DATABASE_URL.
    pg_url = os.environ.pop("CRAWL_PG_URL", "") or ""
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
    if pg_url:
        import models as _models_route
        _models_route._PG_ROUTES[_models_route.DB_PATH] = pg_url
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

    out = {"dialect": "postgres" if pg_url else "sqlite", "blueprints": sorted(app.blueprints),
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

    def _canary_sum(current_user):
        from flask import jsonify
        c = models.get_conn()
        row = c.execute("SELECT SUM(value) AS s FROM dsr_metrics").fetchone()          # unscoped ON PURPOSE
        c.close()
        return jsonify(ok=True, total=row["s"] if row else None)

    def _canary_salary(current_user):
        from flask import jsonify
        r = models.get_restaurant(current_user["restaurant_id"])
        return jsonify(ok=True, per_day=round(float(models.salaried_day_share(r) or 0), 2))    # ungated ON PURPOSE
    app.add_url_rule(CANARY_SUM, "crawl_canary_sum", auth.login_required(_canary_sum), methods=["GET"])
    app.add_url_rule(CANARY_SALARY, "crawl_canary_salary", auth.login_required(_canary_salary), methods=["GET"])

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
    # A's manager, and what a manager must never read: a salaried person,
    # and labor history for the all-in figures to be built from.
    a_mgr = auth.create_user(a_rid, "alphamgr", "alpha-mgr@crawl.test", "alpha-crawl-pass-2")
    auth.set_user_role(a_mgr, "manager")
    auth.upsert_membership(a_mgr, a_rid, "manager")
    models.update_restaurant(a_rid, {"salaried_staff_json": json.dumps([{"name": "Salary Canary",
                                                                          "annual": SALARY_ANNUAL}])})
    from time_utils import restaurant_now_by_id
    from datetime import timedelta as _td
    _today = restaurant_now_by_id(a_rid).date()
    _lc = models.get_conn()
    try:
        for i in range(1, 15):
            d = _today - _td(days=i)
            _lc.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                        "labor_pct, total_hours) VALUES (?,?,?,?,?,?,?)",
                        (a_rid, d.isoformat(), d.strftime("%A"), 4000.0, 1100.0, 27.5, 60.0))
        _lc.commit()
    finally:
        _lc.close()
    models._invalidate_request_cache(a_rid)
    _share = float(models.salaried_day_share(models.get_restaurant(a_rid)) or 0)
    # The salary and a day's share of it, in every way a figure is written.
    salary_canaries = sorted({s.encode() for s in (
        f"{SALARY_ANNUAL:.0f}", f"{SALARY_ANNUAL:,.0f}", f"{_share:.2f}", f"{_share:,.2f}", f"{_share:,.0f}",
        f"{round(_share):d}") if len(s.replace(",", "")) >= 4})
    out["salary_canaries"] = [s.decode() for s in salary_canaries]
    # A canary counts only as a whole figure: never inside a longer number
    # or a token ("5147" is in session id 451473fd…, which failed a run).
    salary_figures = [(s, re.compile(rb"(?<![0-9A-Za-z.,])" + re.escape(s) + rb"(?![0-9A-Za-z])"))
                      for s in salary_canaries]

    # ── tenant B: the restaurant row at B_ID, then one marked row per table ─
    b_tmp = create_restaurant(Restaurant(name=f"Bravo {MARKER}", owner_email=f"bravo-{MARKER.lower()}@crawl.test",
                                         service_tier="full", billing_status="active", module_reviews=1,
                                         module_labor=1, module_inventory=1, module_marketing=1,
                                         location_group="Crawl Group"))
    if pg_url:
        conn = models.get_conn()
        # The crawl's own connection outlives every request on this thread: a
        # request's teardown closes what the thread opened (models.
        # close_thread_connections), so it is taken off that list.
        _bag = getattr(models._open_conns, "bag", None)
        if _bag is not None:
            _bag.discard(conn)
        try:
            # B's rows are seeded table by table in name order: foreign keys
            # (enforced on Postgres, off on SQLite) are not what this tests.
            conn.execute("SET session_replication_role = replica")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        conn = sqlite3.connect(models.DB_PATH)
        conn.row_factory = sqlite3.Row
    conn.execute("UPDATE restaurants SET id=? WHERE id=?", (B_ID, b_tmp))
    conn.commit()
    b_owner = auth.create_user(B_ID, f"bravo{MARKER.lower()}", f"bravo-{MARKER.lower()}@crawl.test", "bravo-pass-123")
    auth.upsert_membership(b_owner, B_ID, "client")

    if pg_url:
        tables = [r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relispartition ORDER BY c.relname")]
    else:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    def _columns(t):
        """[{name, type}] — insertable columns (a generated one is left out)."""
        if pg_url:
            return [{"name": r[0], "type": r[1]} for r in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name=? AND COALESCE(is_generated, 'NEVER')='NEVER' "
                "AND COALESCE(identity_generation, '')!='ALWAYS' ORDER BY ordinal_position", (t,)).fetchall()]
        return [{"name": c["name"], "type": c["type"]} for c in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]

    tenant_tables, cols_of = [], {}
    for t in tables:
        cols = _columns(t)
        cols_of[t] = cols
        if any(c["name"] == "restaurant_id" for c in cols) and t != "restaurants":
            tenant_tables.append(t)

    def _value(t, c, numeric=True):
        name, typ = c["name"].lower(), (c["type"] or "").upper()
        if name == "restaurant_id":
            return B_ID
        if name == "id" or name.endswith("_id"):
            return B_ID if ("INT" in typ or not typ) else f"{MARKER}-{name}"
        if "BOOL" in typ:
            return True if pg_url else 1
        if "INT" in typ:
            return NUM_INT if numeric else 1
        if any(x in typ for x in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
            return NUM_REAL if numeric else 1.0
        if "BLOB" in typ or "BYTEA" in typ:
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

    seeded, unseeded, numeric_fallback = [], {}, []
    check_in = re.compile(r"CHECK constraint failed: (\w+) IN \('([^']*)'")
    pg_check = re.compile(r'violates check constraint "([^"]+)"')
    pg_any = re.compile(r"\(?(\w+)\)?(?:::\w+)?\s*=\s*(?:ANY\s*\(\s*\(?ARRAY\[\s*)?'([^']*)'")

    def _enum_fix(err):
        """(column, its first allowed value) a CHECK refusal names, or None."""
        m = check_in.search(err)
        if m:
            return m.group(1), m.group(2)
        m = pg_check.search(err) if pg_url else None
        if not m:
            return None
        try:
            row = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=?",
                               (m.group(1),)).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            return None
        hit = pg_any.search(row[0]) if row else None
        return (hit.group(1), hit.group(2)) if hit else None

    for t in tenant_tables:
        names = [c["name"] for c in cols_of[t]]
        vals = [_value(t, c) for c in cols_of[t]]
        numeric = True
        for _attempt in range(8):
            try:
                conn.execute(f'INSERT INTO "{t}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                             f'VALUES ({", ".join("?" for _ in names)})', vals)
                conn.commit()
                seeded.append(t)
                unseeded.pop(t, None)
                if not numeric:
                    numeric_fallback.append(t)
                break
            except sqlite3.IntegrityError as e:
                conn.rollback()
                unseeded[t] = f"{type(e).__name__}: {e}"[:160]
                fix = _enum_fix(str(e))              # an enum column: take its first allowed value
                if fix and fix[0] in names and vals[names.index(fix[0])] != fix[1]:
                    vals[names.index(fix[0])] = fix[1]
                    continue
                if numeric:
                    # A numeric CHECK (a 0/1 flag, a bounded score): this
                    # table's numbers fall back to 1 — its text still marks it.
                    numeric = False
                    vals = [v if isinstance(v, str) else _value(t, c, numeric=False)
                            for c, v in zip(cols_of[t], vals)]
                    continue
                break
            except sqlite3.Error as e:
                conn.rollback()
                unseeded[t] = f"{type(e).__name__}: {e}"[:160]
                break
    # The restaurants row's own text columns carry the marker too (its name does).
    out["tenant_tables"] = len(tenant_tables)
    out["seeded"] = len(seeded)
    out["unseeded"] = unseeded
    out["numeric_seeded"] = len(seeded) - len(numeric_fallback)
    models._invalidate_request_cache(B_ID)

    # ── B's snapshot: every tenant table's B rows, and B's restaurants row ──
    snap_tables = seeded + [t for t in tenant_tables if t not in seeded]

    def _rows(t):
        if t == "restaurants":
            got = [tuple(r) for r in conn.execute("SELECT * FROM restaurants WHERE id=?", (B_ID,))]
        else:
            got = [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}" WHERE restaurant_id=? ORDER BY '
                                                  + ("ctid" if pg_url else "rowid"), (B_ID,))]
        conn.commit()
        return got

    def _digest(rows):
        return hashlib.sha1(repr(rows).encode("utf-8", "replace")).hexdigest()

    def snapshot():
        return {t: _digest(_rows(t)) for t in snap_tables + ["restaurants"]}

    saved = {t: _rows(t) for t in snap_tables + ["restaurants"]}
    base = snapshot()

    def restore(t):
        if t == "restaurants":
            # Updated in place: tenant rows point at it (a foreign key on Postgres).
            names = [k for k in conn.execute("SELECT * FROM restaurants WHERE id=?", (B_ID,)).fetchone().keys()]
            row = saved[t][0]
            sets = [(n, v) for n, v in zip(names, row) if n != "id"]
            conn.execute("UPDATE restaurants SET " + ", ".join(f'"{n}"=?' for n, _ in sets) + " WHERE id=?",
                         [v for _, v in sets] + [B_ID])
            conn.commit()
            return
        cols = [c["name"] for c in cols_of[t]]
        conn.execute(f'DELETE FROM "{t}" WHERE restaurant_id=?', (B_ID,))
        for row in saved[t]:
            conn.execute(f'INSERT INTO "{t}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                         f'VALUES ({", ".join("?" for _ in cols)})', row[:len(cols)])
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
    num_marker = NUM_MARKER.encode()
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
            hit = marker if marker in data else (num_marker if num_marker in data else None)
            if hit and not any(l[:2] == [method, rule.rule] for l in leaks):
                i = data.find(hit)
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

    # ── the manager pass: every GET as A's manager, no salaries in any answer ─
    key_leak = re.compile(r'"(?:' + "|".join(MANAGER_SALARY_KEYS) + r')"\s*:\s*-?[0-9]')
    m_requests = m_reached = 0
    salary_leaks = []
    for rule in rules:
        if rule.rule in SKIP_RULES or rule.rule.startswith(("/staff", "/admin")) \
                or (rule.rule.startswith(CANARY) and rule.rule != CANARY_SALARY) or "GET" not in (rule.methods or ()):
            continue
        c = app.test_client()
        c.set_cookie("session_token", auth.create_session(a_mgr, restaurant_id=a_rid))
        c.set_cookie("csrf_js", "crawl-csrf")
        headers = {"Authorization": f"Bearer {auth.create_session(a_mgr, device_type='ios', restaurant_id=a_rid)}",
                   "X-CSRF": "crawl-csrf", "Accept": "application/json"}
        try:
            resp = c.open(f"{fill(rule.rule).replace(str(B_ID), str(a_rid))}", method="GET", headers=headers)
            status, data = resp.status_code, resp.get_data()
        except Exception as e:
            status, data = 599, repr(e).encode()
        m_requests += 1
        if 200 <= status < 300:
            m_reached += 1
        hit = next((m for m in (rx.search(data) for _s, rx in salary_figures) if m), None)
        found = hit.group(0) if hit else None
        km = key_leak.search(data.decode("utf-8", "replace")) if not found else None
        if (found or km) and not any(l[0] == rule.rule for l in salary_leaks):
            what = found.decode() if found else km.group(0)
            i = hit.start() if hit else data.find(km.group(0).encode())
            salary_leaks.append([rule.rule, status, what, data[max(0, i - 160):i + 60].decode("utf-8", "replace")])
        keep_a()
        conn.execute("UPDATE users SET role='manager', is_active=1 WHERE id=?", (a_mgr,))
        conn.commit()
    out.update(manager_requests=m_requests, manager_reached=m_reached, salary_leaks=salary_leaks)
    with open(out_path, "w") as fh:
        json.dump(out, fh)
    import shutil
    shutil.rmtree(vol, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# The tests
# ═════════════════════════════════════════════════════════════════════════════

def _pg_scratch():
    """(url, drop) — a fresh database on CAVNAR_TEST_PG_URL's scratch server
    for the crawl's app, or (None, None) when the suite is on SQLite."""
    base = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
    if not base:
        return None, None
    import uuid
    import psycopg
    from urllib.parse import urlsplit, urlunsplit
    name = "cavnar_crawl_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(base)

    def drop():
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    return urlunsplit(parts._replace(path="/" + name)), drop


@pytest.fixture(scope="module")
def crawl():
    out = tempfile.NamedTemporaryFile(prefix="crawl-", suffix=".json", delete=False).name
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "_SECRET"))}
    env.pop("DATABASE_URL", None)
    pg_url, drop = _pg_scratch()
    if pg_url:
        env["CRAWL_PG_URL"] = pg_url
    try:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--crawl", out], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=900)
    finally:
        if drop:
            drop()
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


def test_the_crawl_catches_a_numeric_leak(crawl):
    """Guards the guard (blind re-audit 10/26/26 B3): B's numbers carry a
    canary, and an aggregate that drops its tenant filter — a SUM over every
    restaurant's rows — is caught. Most tenant tables kept their numeric
    canary (a CHECK on a flag falls back to 1 for that table)."""
    assert any(l[1] == CANARY_SUM for l in crawl["leaks"]), crawl["leaks"]
    assert crawl["numeric_seeded"] >= 0.8 * crawl["seeded"], crawl["numeric_seeded"]


def test_the_manager_pass_reached_the_handlers_and_catches_a_salary(crawl):
    """Guards the guard: the manager pass reaches handlers, and the canary
    that hands a salary share to any login is caught."""
    assert crawl["manager_requests"] >= 450 and crawl["manager_reached"] >= 300, crawl["manager_reached"]
    assert crawl["salary_canaries"], "A's salaried person must give the pass something to look for"
    assert any(l[0] == CANARY_SALARY for l in crawl["salary_leaks"]), crawl["salary_leaks"]


def test_no_route_shows_a_manager_the_salaries(crawl):
    """A manager never reads salaries in any surface (CLAUDE.md, labor
    all-in; blind re-audit 10/26/26 B3): no GET a manager may make answers
    with the salary, a day's share of it, or an all-in figure."""
    leaks = [l for l in crawl["salary_leaks"] if l[0] != CANARY_SALARY]
    assert not leaks, "\n".join(f"{r} -> {s}: {what}: …{ctx}…" for r, s, what, ctx in leaks)


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
