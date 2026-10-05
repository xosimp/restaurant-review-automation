"""Fix round B1 — admin tools act on THE client, and the legacy pages escape
what they render (#10, #19, #73, #130, #149).

- The legacy admin pages never call a session-scoped owner route to write:
  an admin session's restaurant is the admin's own home, so "Manage data
  (CSV)", the settings page's review import and its response templates all
  wrote there instead of into the client being managed.
- The owner routes refuse an operator session that names no restaurant.
- /admin/upload-data/<id> runs the owner route's own validation.
- Imports honour the platform, with its own columns.
- Stored XSS: tenant-controlled names (ingredients, units, categories,
  recipes, templates) never reach innerHTML or an attribute unescaped.
"""
import io
import json
import os
import re
import shutil
import subprocess

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_restaurant

CSRF = "b1-scope-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PAGES = ("client_settings.html", "client_data.html", "admin.html")


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()
    import threading

    class _Inert:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass
    # No analysis thread from an import or upload in these tests.
    monkeypatch.setattr(threading, "Thread", _Inert)
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.secret_key = "b1-scope"
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    flask_app.register_blueprint(client_api.client_bp)

    @flask_app.template_filter("format_date")
    def _fd(v):
        return str(v)
    return flask_app


def _rid(db_path, name, **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", **kw),
                             db_path=db_path)


def _session(app, db_path, uid):
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


@pytest.fixture
def world(app, db_path):
    home = _rid(db_path, "Cavnar HQ")
    admin = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    client = _rid(db_path, "Client Grill")
    owner = create_user(client, "owner", "owner@client.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, client, "client", db_path=db_path)
    support_home = _rid(db_path, "Support Home")
    support = create_user(support_home, "sup", "sup@cavnar.test", "Support-pass-2026", role="support",
                          db_path=db_path)
    return {"home": home, "client": client, "admin": _session(app, db_path, admin),
            "owner": _session(app, db_path, owner), "support": _session(app, db_path, support), "db": db_path}


def _post(c, url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _count(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


SHIFTS = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales\n"
          "2026-09-01,Tuesday,Ann Lee,Server,11:00,17:00,6,6.0,4200\n"
          "2026-09-02,Wednesday,Bo Park,Cook,10:00,18:00,8,8.0,3900\n")


# ── the static rule: admin pages name the restaurant on every write ─────────

def _script_chunks(src):
    """Each top-level function (or statement run) of the page's scripts."""
    code = "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))
    return re.split(r"\n(?=(?:async\s+)?function\s+\w+\s*\(|let\s+_\w+\s*=|const\s+\w+\s*=|var\s+\w+\s*=)", code)


@pytest.mark.parametrize("page", ADMIN_PAGES)
def test_admin_pages_never_write_to_a_session_scoped_route_without_the_restaurant(page):
    """An admin session's own restaurant is the admin's home. Every write an
    admin page makes goes to an /admin route that names the restaurant, or
    carries restaurant_id to the one owner route that accepts an admin
    target (/api/import-tripadvisor, which hands it to the admin gate)."""
    src = open(os.path.join(ROOT, "templates", page), encoding="utf-8").read()
    bad = []
    for chunk in _script_chunks(src):
        for m in re.finditer(r"""['"`](/(?:api|client|mobile)/[^'"`]*)""", chunk):
            url = m.group(1)
            window = chunk[m.start():m.start() + 300]
            writes = re.search(r"method\s*:\s*['\"](POST|PUT|PATCH|DELETE)", window, re.I) or \
                "post(" in chunk[max(0, m.start() - 12):m.start()]
            if not writes:
                continue
            if "restaurant_id" not in chunk:
                bad.append(f"{page}: {url}")
    assert bad == [], "session-scoped writes from an admin page: " + ", ".join(bad)


def test_the_legacy_pages_call_only_admin_routes_for_client_data():
    settings = open(os.path.join(ROOT, "templates", "client_settings.html"), encoding="utf-8").read()
    data = open(os.path.join(ROOT, "templates", "client_data.html"), encoding="utf-8").read()
    for src in (settings, data):
        # As URLs in code — the comments explaining the fix may name them.
        for url in ("/client/upload-data", "/api/templates", "/api/import-tripadvisor", "/api/review-count"):
            assert not re.search(r"""['"`]""" + re.escape(url), src), url
    assert "fetch('/admin/upload-data/' + restaurantId" in data
    assert 'class="upload-zone" id="inv-drop"' in data, "the inventory upload couldn't find its file input"
    assert "/admin/api/templates/{{ restaurant.id }}" in settings
    assert "/admin/import-reviews/{{ restaurant.id }}" in settings


# ── the owner routes refuse an operator session with no target ─────────────

def test_an_admin_session_cannot_upload_into_its_own_home(world):
    r = _post(world["admin"], "/client/upload-data",
              data={"data_type": "shifts", "csv_file": (io.BytesIO(SHIFTS.encode()), "s.csv")},
              content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["operator_session"] is True
    assert models.get_client_data(world["home"]) is None


@pytest.mark.parametrize("method,url", [("get", "/api/templates"), ("post", "/api/templates"),
                                        ("delete", "/api/templates/1"), ("post", "/api/templates/1/use")])
def test_an_admin_session_cannot_touch_its_home_restaurants_templates(world, method, url):
    c = world["admin"]
    r = getattr(c, method)(url, json={"title": "t", "body": "b"}, headers={"X-CSRF": CSRF})
    assert r.status_code == 400
    assert _count(world["db"], "SELECT COUNT(*) FROM response_templates") == 0


def test_an_owner_still_manages_their_own_templates(world):
    r = _post(world["owner"], "/api/templates", json={"title": "Thanks", "body": "Thank you!"})
    assert r.get_json()["ok"] is True
    assert [t["title"] for t in world["owner"].get("/api/templates").get_json()["templates"]] == ["Thanks"]


def test_the_admin_template_routes_act_on_the_named_client(world):
    c, rid = world["admin"], world["client"]
    made = _post(c, f"/admin/api/templates/{rid}", json={"title": "Five star", "body": "Thanks so much"}).get_json()
    assert made["ok"] is True
    listed = c.get(f"/admin/api/templates/{rid}").get_json()["templates"]
    assert [t["title"] for t in listed] == ["Five star"]
    assert _count(world["db"], "SELECT COUNT(*) FROM response_templates WHERE restaurant_id=?", (world["home"],)) == 0
    assert _post(c, f"/admin/api/templates/{world['home']}/{made['id']}/delete").get_json()["ok"] is True
    assert len(c.get(f"/admin/api/templates/{rid}").get_json()["templates"]) == 1, "scoped to its restaurant"
    assert _post(c, f"/admin/api/templates/{rid}/{made['id']}/delete").get_json()["ok"] is True
    assert c.get(f"/admin/api/templates/{rid}").get_json()["templates"] == []


# ── #19, #130: the admin upload is the owner upload, for the named client ───

def test_the_admin_upload_lands_in_the_client_with_the_owner_routes_checks(world, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    fired = []
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: fired.append(a))
    c, rid = world["admin"], world["client"]
    r = _post(c, f"/admin/upload-data/{rid}",
              data={"data_type": "shifts", "csv_file": (io.BytesIO(SHIFTS.encode()), "s.csv")},
              content_type="multipart/form-data")
    assert r.get_json()["ok"] is True, r.get_json()
    assert models.get_client_data(rid)["shifts_csv"] and models.get_client_data(world["home"]) is None
    assert fired == [], "a local backend's admin upload posts to no client webhook"
    assert _count(world["db"], "SELECT COUNT(*) FROM admin_events WHERE event_type='client_data.upload' "
                               "AND restaurant_id=?", (rid,)) == 1
    assert _count(world["db"], "SELECT COUNT(*) FROM email_log WHERE restaurant_id=?", (rid,)) == 0, \
        "an admin's load is not the client's first upload"


@pytest.mark.parametrize("data_type", ["reviews", "shifts_csv=NULL, inventory", ""])
def test_the_admin_upload_refuses_an_unknown_data_type(world, data_type):
    r = _post(world["admin"], f"/admin/upload-data/{world['client']}",
              data={"data_type": data_type, "csv_file": (io.BytesIO(SHIFTS.encode()), "s.csv")},
              content_type="multipart/form-data")
    assert r.status_code == 400 and models.get_client_data(world["client"]) is None


def test_the_admin_upload_refuses_rows_the_owner_route_refuses(world):
    bad = "date,employee,actual_hours\nnot-a-date,Ann,=cmd()\n"
    r = _post(world["admin"], f"/admin/upload-data/{world['client']}",
              data={"data_type": "shifts", "csv_file": (io.BytesIO(bad.encode()), "s.csv")},
              content_type="multipart/form-data")
    assert r.get_json()["ok"] is False and models.get_client_data(world["client"]) is None


def test_the_admin_upload_reads_an_excel_cp1252_file(world):
    inv = "item,current_stock,par_level,unit_cost,waste_last_week\nJalape\xf1os,3,5,2.5,0.5\n"
    r = _post(world["admin"], f"/admin/upload-data/{world['client']}",
              data={"data_type": "inventory", "source": "manual",
                    "csv_file": (io.BytesIO(inv.encode("cp1252")), "i.csv")},
              content_type="multipart/form-data")
    assert r.get_json()["ok"] is True, r.get_json()
    row = models.get_client_data(world["client"])
    assert "Jalapeños" in row["inventory_csv"] and row["inventory_source"] == "manual"


def test_support_cannot_upload(world):
    r = _post(world["support"], f"/admin/upload-data/{world['client']}",
              data={"data_type": "shifts", "csv_file": (io.BytesIO(SHIFTS.encode()), "s.csv")},
              content_type="multipart/form-data")
    assert r.status_code == 403 and models.get_client_data(world["client"]) is None


# ── #149: imports honour the platform and the target ────────────────────────

def _import(c, url, text, **form):
    data = dict(form, file=(io.BytesIO(text.encode()), "reviews.csv"))
    return _post(c, url, data=data, content_type="multipart/form-data")


def _reviews(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT platform, author, rating, text, external_id FROM reviews "
                                              "WHERE restaurant_id=? ORDER BY rating", (rid,)).fetchall()]
    finally:
        conn.close()


def test_an_admin_import_without_a_restaurant_is_refused(world):
    r = _import(world["admin"], "/api/import-tripadvisor", "rating,text\n5,Great\n", platform="tripadvisor")
    assert r.status_code == 400 and _reviews(world["db"], world["home"]) == []


def test_an_admin_import_through_the_owner_route_lands_in_the_named_client(world):
    r = _import(world["admin"], "/api/import-tripadvisor", "rating,text\n5,Great\n",
                restaurant_id=str(world["client"]), platform="tripadvisor")
    assert r.get_json()["ok"] is True
    assert [x["platform"] for x in _reviews(world["db"], world["client"])] == ["tripadvisor"]
    assert _reviews(world["db"], world["home"]) == []


def test_support_cannot_import_even_naming_a_restaurant(world):
    r = _import(world["support"], "/api/import-tripadvisor", "rating,text\n5,Great\n",
                restaurant_id=str(world["client"]))
    assert r.status_code == 403 and _reviews(world["db"], world["client"]) == []


DOORDASH = ("Order Date,Customer Name,Rating,Customer Comment,Order ID\n"
            "2026-09-01,Jane D.,2,Cold fries and late,DD-1001\n"
            "2026-09-02,Sam K.,5,Hot and fast,DD-1002\n")
UBEREATS = ("Date,Eater Name,Star Rating,Comment,Order UUID\n"
            "2026-09-03,Lee,4,Good bowl,ue-77\n")


def test_doordash_and_uber_eats_exports_are_read_with_their_own_columns(world):
    c, rid = world["admin"], world["client"]
    dd = _import(c, f"/admin/import-reviews/{rid}", DOORDASH, platform="doordash").get_json()
    ue = _import(c, f"/admin/import-reviews/{rid}", UBEREATS, platform="Uber Eats").get_json()
    assert dd["ok"] and dd["imported"] == 2 and dd["platform"] == "doordash"
    assert ue["ok"] and ue["imported"] == 1 and ue["platform"] == "ubereats"
    rows = _reviews(world["db"], rid)
    assert {(x["platform"], x["author"], x["rating"]) for x in rows} == {
        ("doordash", "Jane D.", 2), ("doordash", "Sam K.", 5), ("ubereats", "Lee", 4)}
    assert all(x["external_id"].startswith(x["platform"] + "_order_") for x in rows)
    again = _import(c, f"/admin/import-reviews/{rid}", DOORDASH, platform="doordash").get_json()
    assert again["ok"] is True and again["new"] == 0 and len(_reviews(world["db"], rid)) == 3
    assert _count(world["db"], "SELECT COUNT(*) FROM admin_events WHERE event_type='reviews.import' "
                               "AND restaurant_id=?", (rid,)) == 3


def test_a_row_already_imported_under_the_old_key_is_not_duplicated(world):
    """The pre-fix importer keyed a DoorDash row as ta_import_<hash of the
    TripAdvisor-mapped fields>; the same review under the new mapping is
    recognised by its rating, text and date."""
    rid = world["client"]
    models.save_reviews([models.Review(restaurant_id=rid, platform="doordash", external_id="ta_import_legacy",
                                       author="DoorDash customer", rating=2, text="Cold fries and late",
                                       review_date="2026-09-01")])
    out = _import(world["admin"], f"/admin/import-reviews/{rid}", DOORDASH, platform="doordash").get_json()
    assert out["new"] == 1 and out["already_had"] == 1
    assert len(_reviews(world["db"], rid)) == 2


def test_an_unknown_platform_is_refused_not_filed_as_tripadvisor(world):
    r = _import(world["admin"], f"/admin/import-reviews/{world['client']}", "rating,text\n5,Great\n",
                platform="google")
    assert r.status_code == 400 and _reviews(world["db"], world["client"]) == []


# ── #10: stored XSS on the settings page ────────────────────────────────────
#
# A small JS scanner: enough of the language (quoted strings, template
# literals and their nesting, regex literals, comments) to find every value
# interpolated or concatenated into markup on the legacy admin pages.

def _skip_string(code, i):
    """Index just past the quoted string starting at code[i]."""
    q, j, n = code[i], i + 1, len(code)
    while j < n and code[j] != q:
        j += 2 if code[j] == "\\" else 1
    return j + 1


def _skip_regex(code, i):
    """Index just past the regex literal starting at code[i] (a '/')."""
    j, n, in_class = i + 1, len(code), False
    while j < n:
        c = code[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and code[j].isalpha():
                j += 1
            return j
        j += 1
    return n


def _regex_can_start(code, i):
    k = i - 1
    while k >= 0 and code[k] in " \t\n":
        k -= 1
    return k < 0 or code[k] in "(,=:[!&|?{};+" or code[max(0, k - 5):k + 1] == "return"


def _scan_template(code, i, found):
    """Index just past the template literal starting at code[i]; appends
    (text, [expressions]) for it and every literal nested in it to `found`."""
    j, n, text, exprs = i + 1, len(code), "", []
    while j < n and code[j] != "`":
        if code[j] == "\\":
            text += code[j:j + 2]
            j += 2
            continue
        if code.startswith("${", j):
            k, depth, start = j + 2, 1, j + 2
            while k < n and depth:
                c = code[k]
                if c in "'\"":
                    k = _skip_string(code, k)
                    continue
                if c == "`":
                    k = _scan_template(code, k, found)
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                k += 1
            exprs.append(code[start:k - 1].strip())
            j = k
            continue
        text += code[j]
        j += 1
    found.append((text, exprs))
    return j + 1


def _analyse_script(code):
    """(template literals, masked code). In the masked code every quoted
    string is \\x01H\\x02 (it holds markup) or \\x01S\\x02, every template
    literal \\x01T\\x02, and comments and regex literals are gone, so an
    operator or a semicolon inside a string can't be mistaken for code."""
    found, out, i, n = [], [], 0, len(code)
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = _skip_string(code, i)
            out.append("\x01H\x02" if re.search(r"<[a-zA-Z/!]", code[i:j]) else "\x01S\x02")
            i = j
            continue
        if c == "`":
            i = _scan_template(code, i, found)
            out.append("\x01T\x02")
            continue
        if c == "/" and _regex_can_start(code, i):
            i = _skip_regex(code, i)
            out.append("\x01R\x02")
            continue
        out.append(c)
        i += 1
    return found, "".join(out)


def _operand(masked, pos, direction):
    """The operand joined by a '+' at masked[pos] going `direction` (+1
    right, -1 left), or None when no '+' joins there."""
    n = len(masked)
    k = pos
    while 0 <= k < n and masked[k] in " \t\n":
        k += direction
    if not (0 <= k < n) or masked[k] != "+":
        return None
    if direction == 1 and k + 1 < n and masked[k + 1] in "+=":
        return None
    if direction == -1 and k - 1 >= 0 and masked[k - 1] == "+":
        return None
    k += direction
    depth, chars = 0, []
    opens, closes = ("([{", ")]}") if direction == 1 else (")]}", "([{")
    while 0 <= k < n:
        c = masked[k]
        if depth == 0 and (c in "+,;:?|&=" or c in closes):
            break
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
        chars.append(c)
        k += direction
    text = "".join(chars if direction == 1 else reversed(chars)).strip()
    return re.sub(r"^(return|var|let|const)\s+", "", text)


_SAFE_OPERAND = re.compile(r"^\x01[SHT]\x02$|^(fcEsc|fcNum)\(.*\)$|^[A-Za-z_$][\w$]*Html$|^html$"
                           r"|^\(.*\?\s*\x01[SH]\x02\s*:\s*\x01[SH]\x02\s*\)$", re.S)
_SAFE_EXPR = re.compile(r"^(fcEsc|fcNum)\(.*\)$|^[A-Za-z_$][\w$]*Html$", re.S)


def unescaped_markup(code):
    """Every value put into markup unescaped: a template-literal expression,
    or an operand concatenated to a string that holds markup. Escaped means
    fcEsc(...) / fcNum(...), or a variable named ...Html that holds markup
    already built from escaped parts."""
    literals, masked = _analyse_script(code)
    bad = []
    for text, exprs in literals:
        if re.search(r"<[a-zA-Z/!]", text):
            bad += [e for e in exprs if not _SAFE_EXPR.match(e)]
    for m in re.finditer("\x01H\x02", masked):
        for pos, direction in ((m.end(), 1), (m.start() - 1, -1)):
            o = _operand(masked, pos, direction)
            if o is not None and not _SAFE_OPERAND.match(o):
                bad.append(o.replace("\x01", "").replace("\x02", ""))
    return bad


def test_the_scanner_finds_what_it_is_looking_for():
    """So the check below can't pass by finding nothing."""
    assert unescaped_markup("el.innerHTML = `<td>${ing.name}</td>`;") == ["ing.name"]
    assert unescaped_markup("el.innerHTML = '<div>' + t.title + '</div>';") == ["t.title", "t.title"]
    assert unescaped_markup("x.innerHTML = `<b>${fcEsc(a)}</b><i>${n}</i>` + '<p>' + fcNum(b) + '</p>';") == ["n"]
    assert unescaped_markup("var s = 'a;b' + name; el.textContent = `${name}`;") == []
    assert unescaped_markup("list.innerHTML = a.map(r => `<span>${r.x}</span>`).join('');") == ["r.x"]
    assert unescaped_markup("h += '<div>' + (n>1?'s':'') + fcEsc(v.replace(/'/g, '')) + '</div>';") == []


@pytest.mark.parametrize("page", ["client_settings.html", "client_data.html"])
def test_every_interpolation_into_page_markup_is_escaped(page):
    src = open(os.path.join(ROOT, "templates", page), encoding="utf-8").read()
    code = "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))
    bad = unescaped_markup(code)
    assert bad == [], f"{page}: tenant data reaches markup unescaped: {bad}"
    assert "|safe" not in src and "autoescape false" not in src


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_hostile_names_render_as_text_on_the_settings_page(world):
    """Ingredient, category, unit, recipe and template names from a tenant
    render escaped — in cells and in value="…" attributes (#10)."""
    evil = '<img src=x onerror=alert(1)>'
    attr = '" onfocus="alert(2)'
    html = world["admin"].get(f"/admin/client-settings/{world['client']}").get_data(as_text=True)
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    harness = r"""
var __els = {};
function __El(id) { this.id = id; this.innerHTML = ''; this.textContent = ''; this.value = ''; this.style = {};
  this.addEventListener = function () {}; this.focus = function () {}; }
var document = {getElementById: function (id) { return __els[id] || (__els[id] = new __El(id)); },
                querySelector: function () { return new __El('q'); }};
var window = {location: {pathname: '/admin/client-settings/1', reload: function () {}}};
var location = window.location;
var EVIL = %s, ATTR = %s;
function fetch(url) {
  var body = {ok: true};
  if (url.indexOf('/admin/inventory/ingredients/') === 0) body.ingredients = [{id: 1, name: EVIL, category: ATTR,
    unit: EVIL, par_level: ATTR, current_stock: 2, unit_cost: 1, case_size: 1, avg_daily_usage: 0, waste_last_week: 0}];
  if (url.indexOf('/admin/inventory/recipes/') === 0) { body.ingredients = [{id: 1, name: EVIL}];
    body.priority_ingredients = [{name: EVIL, has_recipe: false}];
    body.menu_items = [{id: 3, name: EVIL, pos_category: EVIL, recipe: [{id: 4, ingredient_name: EVIL,
      qty_per_unit: ATTR, unit: EVIL}]}]; }
  if (url.indexOf('/admin/api/templates/') === 0) body.templates = [{id: 9, title: EVIL, body: EVIL, use_count: ATTR}];
  return Promise.resolve({status: 200, json: function () { return Promise.resolve(body); }});
}
function confirm() { return true; } function alert() {} function prompt() { return ''; }
""" % (json.dumps(evil), json.dumps(attr))
    js = harness + script + r"""
setTimeout(function () {
  var ids = ['fc-ing-rows', 'fc-quick-ingredient', 'fc-recipes-list', 'fc-priority-list', 'tmpl-mgmt-list'];
  var out = {};
  for (var i = 0; i < ids.length; i++) out[ids[i]] = document.getElementById(ids[i]).innerHTML;
  console.log(JSON.stringify(out));
}, 50);"""
    run = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    for key, markup in out.items():
        assert markup, f"{key} rendered nothing"
        assert "<img" not in markup, f"{key} carries raw markup: {markup[:200]}"
        assert 'onfocus="alert' not in markup, f"{key} breaks out of an attribute"
    assert "&lt;img src=x onerror=alert(1)&gt;" in out["fc-ing-rows"]
    assert 'value="&quot; onfocus=&quot;alert(2)"' in out["fc-ing-rows"]


def test_support_manages_its_own_two_factor_and_the_step_up_unlocks_nothing_else(world):
    """9/29/26: a support login steps up and makes its own backup codes (or
    switches method) itself — no one resets its two-factor for it. The
    step-up changes nothing about every other admin write."""
    sup = world["support"]
    r = _post(sup, "/admin/two-factor/backup-codes")
    assert r.status_code == 403 and r.get_json().get("reauth_required"), r.get_json()
    assert _post(sup, "/admin/api/reauth", json={"password": "Support-pass-2026"}).get_json()["ok"] is True
    r = _post(sup, "/admin/two-factor/backup-codes")
    assert r.status_code == 409 and "two-factor on first" in r.get_json()["error"]   # past both gates
    r = _post(sup, f"/admin/upload-data/{world['client']}",
              data={"file": (io.BytesIO(SHIFTS.encode()), "shifts.csv")}, content_type="multipart/form-data")
    assert r.status_code == 403 and "read-only" in (r.get_json() or {}).get("error", "")
