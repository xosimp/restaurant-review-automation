"""
bar/mapping.py — which POS sales count as beer, liquor, wine, non-alcoholic
drinks, or not bar at all: an explicit mapping per restaurant, at the POS's
own item-category level, confirmed once by the account holder (bar fix round
10/15/26, V7 / RPW-3 / RPW-8).

The Bar used to find its sales by substring in the owner's report category
names: "Draft", "Bottles & Cans", "Bar" and "Alcohol" were dropped, "N/A
Beer", "Root Beer" and "Ginger Beer" counted as beer, and at Simple EJ's the
department Beer held "56. Brunch Drinks" — $2,835 of Bloody Marys and
mimosas in four weeks counted as beer while the vodka and sparkling wine
behind them were bought as liquor and wine.

Keys are the POS's own names, as the nightly report stores them:

  item_category    "Beer › 56. Brunch Drinks" — a POS sales category inside
                   its department (dsr store.sub_name). The mapping's level.
  department       "Beer" — for a night (or the part of a department) whose
                   item categories are not on file.
  report_category  "Beer" — the owner's report category, for a night that
                   kept only those (an imported workbook, a night built
                   before departments were stored).

Every key has a SUGGESTION (suggest(): RPOWER's salescategory.is_bar and the
words in the names). A suggestion is used until the account holder decides,
and while any key with sales is undecided the payload says `mapping_needed`
— a suggestion is never silently final. Once a key is confirmed its choice is
the owner's and no name rule touches it again. A key whose suggestion is
"can't tell" (Bar, Alcohol, Wine & Spirits) counts nowhere until mapped, and
is named as unmapped (R10), never dropped without a word.

Tables (init_bar_mapping, boot only): bar_sales_map (the owner's choices)
and bar_sales_nights (each archived night's sales by POS item category, net
of check discounts allocated per ticket — bar.sales.derive_nights; mapping-
independent, so a change of mapping never needs it derived again).
"""
import re

from bar.common import _mdy, get_conn

# What a POS name can be mapped to. `na` is counted (and shown) but has no
# pour-cost reading; `not_bar` is decided and excluded.
BAR_KINDS = ("beer", "liquor", "wine", "na", "not_bar")
BAR_KIND_LABELS = {"beer": "Beer & seltzer", "liquor": "Liquor & cocktails", "wine": "Wine",
                   "na": "Non-alcoholic drinks", "not_bar": "Not bar"}
KEY_KINDS = ("item_category", "department", "report_category")
SUB_SEP = " › "              # dsr store.SUB_SEP: "Department › Item category"


def init_bar_mapping(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_sales_map (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            kind            TEXT NOT NULL,
            pos_name        TEXT NOT NULL,
            bar_category    TEXT,
            suggested       TEXT,
            suggested_why   TEXT,
            confirmed_at    TEXT,
            confirmed_by    INTEGER,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, kind, pos_name)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_sales_nights (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT NOT NULL,
            department      TEXT NOT NULL,
            sales_category  TEXT NOT NULL DEFAULT '',
            sales           REAL NOT NULL DEFAULT 0,
            discounts       REAL NOT NULL DEFAULT 0,
            comps100        REAL NOT NULL DEFAULT 0,
            comp_lines      REAL NOT NULL DEFAULT 0,
            discount_lines  REAL NOT NULL DEFAULT 0,
            basis           TEXT,
            computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date, department, sales_category)
        )""")
        # Retention deletes by business_date across restaurants (ops._RETENTION_DAYS).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_sales_nights_date ON bar_sales_nights(business_date)")
        conn.commit()
    finally:
        conn.close()


def key_for(department, item_category=None) -> tuple:
    """(kind, pos_name) of one POS cell."""
    dep = str(department or "").strip()
    sub = str(item_category or "").strip()
    if sub:
        return "item_category", f"{dep}{SUB_SEP}{sub}"
    return "department", dep


def split_key(pos_name):
    parts = str(pos_name or "").split(SUB_SEP.strip(), 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return str(pos_name or "").strip(), None


# ── suggestions: RPOWER's is_bar and the words in the names ────────────────
#
# Only ever a SUGGESTION, shown with its reason and confirmed by the owner.
# Order matters: a non-alcoholic word beats "beer" (N/A Beer, Root Beer,
# Ginger Beer), and a name with words of two drink kinds ("Wine & Spirits")
# is "can't tell", never a guess.
_MONEY_RE = re.compile(r"discount|\bcomps?\b|gratuit|gift\s*card|\btax|\bfees?\b|service\s*charge|deposit|"
                       r"received\s*on\s*account|\bpaid\s*(?:in|out)\b|\btips?\b", re.I)
_NOT_BAR_RE = re.compile(r"\bdarts?\b|\bpool\b|retail|merch|apparel|t-?shirt|room\s*rental|\bfood\b|appeti|"
                         r"entree|entr\u00e9e|dessert|kitchen|sandwich|burger|pizza|salad|\bwings?\b|\bsides?\b|"
                         r"kids|breakfast|\bsoup|\btacos?\b|catering|banquet", re.I)
_NA_RE = re.compile(r"\bn\s*/\s*a\b|\bna\s+(?:beer|bev|beverage|drinks?)|non[\s-]*alc|zero[\s-]*proof|mocktail|"
                    r"\bsoda|soft\s*drink|\bpop\b|juice|coffee|espresso|\btea\b|root\s*beer|ginger\s*beer|"
                    r"kombucha|\bwater\b|red\s*bull|energy\s*drink|lemonade|\bmilk", re.I)
_WINE_RE = re.compile(r"\bwines?\b|champagne|prosecco|sparkling|\bcava\b|\bros[eé]\b|sangria|\bsake\b|"
                      r"\bby\s+the\s+glass\b|\bbtl\b|\bvino\b", re.I)
_LIQUOR_RE = re.compile(r"liquor|spirits?\b|cocktails?|\bshots?\b|shooters?|whiske?y|bourbon|vodka|tequila|"
                        r"mezcal|\bgin\b|\brum\b|scotch|cordials?|martinis?|margaritas?|\bwell\b|\bcall\b|"
                        r"premium|top\s*shelf|liqueur|\bmixed\s*drinks?", re.I)
_BEER_RE = re.compile(r"\bbeers?\b|draft|draught|\bbottles?\b|\bcans?\b|bottles?\s*&\s*cans?|seltzers?|"
                      r"ciders?|\bkegs?\b|\bipa\b|lager|\bales?\b|buckets?|pitchers?|\bbrews?\b|\btap\b", re.I)


def _words(name) -> tuple:
    """(kind, why) from a name's words, or (None, why) when it can't tell."""
    n = str(name or "")
    if not n.strip():
        return None, None
    if _MONEY_RE.search(n):
        return "not_bar", "its name reads like a discount, fee or tax"
    if _NA_RE.search(n):
        return "na", "its name reads non-alcoholic"
    hits = [k for k, rx in (("beer", _BEER_RE), ("liquor", _LIQUOR_RE), ("wine", _WINE_RE)) if rx.search(n)]
    if len(hits) == 1:
        return hits[0], f"its name reads {BAR_KIND_LABELS[hits[0]].lower()}"
    if len(hits) > 1:
        return None, "its name could be more than one kind of drink"
    if _NOT_BAR_RE.search(n):
        return "not_bar", "its name reads like food or something other than drinks"
    return None, None


def suggest(department=None, item_category=None, is_bar=None, report_category=None) -> dict:
    """{kind: beer|liquor|wine|na|not_bar|None, why} for one POS name: the
    item category's own words, then RPOWER's is_bar flag, then the
    department's words. None is "can't tell": the owner says."""
    if report_category is not None:
        kind, why = _words(report_category)
        return {"kind": kind, "why": why or "a report category Cavnar AI can't place from its name"}
    if not str(item_category or "").strip():
        kind, why = _words(department)
        return {"kind": kind, "why": why or "its name doesn't say"}
    kind, why = _words(item_category)
    if kind is not None:
        return {"kind": kind, "why": why}
    ambiguous = why
    dep_kind, dep_why = _words(department)
    if is_bar:
        # RPOWER marks the category as the bar's; which drink it is comes
        # from the department's words, else the owner.
        if dep_kind in ("beer", "liquor", "wine", "na"):
            return {"kind": dep_kind, "why": f"RPOWER marks it as a bar category and its department {dep_why[4:]}"}
        return {"kind": None, "why": "RPOWER marks it as a bar category; which drink is yours to say"}
    if ambiguous:
        return {"kind": None, "why": ambiguous}
    if dep_kind is not None:
        return {"kind": dep_kind, "why": f"its department {dep_why[4:]}" if dep_why else None}
    if dep_why:
        return {"kind": None, "why": f"its department {dep_why[4:]}"}
    if is_bar is not None and not is_bar and item_category:
        return {"kind": None, "why": "RPOWER doesn't mark it as a bar category, and its name doesn't say"}
    return {"kind": None, "why": "its name doesn't say"}


# ── what the owner decided ────────────────────────────────────────────────

def stored(restaurant_id, db_path=None) -> dict:
    """{(kind, pos_name lower): row} of the owner's saved choices."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT kind, pos_name, bar_category, suggested, suggested_why, confirmed_at, "
                            "confirmed_by, updated_at FROM bar_sales_map WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {(r["kind"], str(r["pos_name"]).strip().lower()): dict(r) for r in rows}


def catalog(restaurant_id, db_path=None) -> dict:
    """{(department lower, category lower): {department, name, is_bar,
    alt_qty_unit, aqu_in_aru}} from the POS's kept sales catalog
    (pos_archive.sync_catalog); {} when none is kept."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT name, department, is_bar, alt_qty_unit, aqu_in_aru FROM pos_sales_categories "
                            "WHERE restaurant_id=?", (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["name"]:
            out[(str(r["department"] or "").strip().lower(), str(r["name"]).strip().lower())] = dict(r)
    return out


class Mapping:
    """The mapping as it stands: resolve() gives each POS cell's bar kind,
    whether the owner confirmed it, and why."""

    def __init__(self, restaurant_id, db_path=None, report_map=None):
        self.restaurant_id = int(restaurant_id)
        self.rows = stored(restaurant_id, db_path)
        self.catalog = catalog(restaurant_id, db_path)
        # {report category lower: [departments mapped to it]} — the nightly
        # report's own category map (dsr_category_map), for report_category keys.
        self.report_map = report_map if report_map is not None else _report_map(restaurant_id, db_path)
        # {POS name lower: the report category the owner filed it under} —
        # the owner's own words for a department ("Beverage" -> "NA
        # Beverage"), a hint when the POS name itself says nothing.
        self.report_of = _report_of(restaurant_id, db_path)
        self._memo = {}

    def suggestion(self, kind, pos_name) -> dict:
        """{kind, why}: suggest() for the name, then — when it can't tell —
        the report category the owner filed the name (or its department)
        under, read for its words."""
        if kind == "report_category":
            return suggest(report_category=pos_name)
        dep, sub = split_key(pos_name) if kind == "item_category" else (str(pos_name or "").strip(), None)
        info = self.catalog.get((dep.lower(), (sub or "").lower())) or {}
        s = suggest(dep, sub, info.get("is_bar") if info else None)
        if s["kind"] is not None:
            return s
        for name in ([str(pos_name or "").strip().lower()] if sub else []) + [dep.lower()]:
            cat = self.report_of.get(name)
            if not cat:
                continue
            k, _why = _words(cat)
            if k is not None:
                return {"kind": k, "why": f"your report files it under {cat}"}
        return s

    def _row_choice(self, kind, name):
        row = self.rows.get((kind, str(name or "").strip().lower()))
        if row and row.get("bar_category") in BAR_KINDS:
            return row
        return None

    def resolve(self, kind, pos_name) -> dict:
        """{kind: bar kind|None, confirmed, source owner|suggested|derived, why}."""
        memo_key = (kind, str(pos_name or "").strip().lower())
        hit = self._memo.get(memo_key)
        if hit is not None:
            return hit
        out = self._resolve(kind, pos_name)
        self._memo[memo_key] = out
        return out

    def _resolve(self, kind, pos_name):
        row = self._row_choice(kind, pos_name)
        if row:
            return {"kind": row["bar_category"], "confirmed": bool(row.get("confirmed_at")), "source": "owner",
                    "why": "your choice"}
        if kind == "item_category":
            s = self.suggestion(kind, pos_name)
            return {"kind": s["kind"], "confirmed": False, "source": "suggested", "why": s["why"]}
        if kind == "department":
            # A department with no choice of its own follows the item
            # categories inside it when they all agree (the owner's choices
            # first), else its own name.
            inside = [_c for (d, _c) in self.catalog if d == str(pos_name or "").strip().lower()]
            kinds = set()
            for (d, c), info in self.catalog.items():
                if d != str(pos_name or "").strip().lower():
                    continue
                kinds.add(self.resolve("item_category", f"{info['department']}{SUB_SEP}{info['name']}")["kind"])
            if inside and len(kinds) == 1 and None not in kinds:
                k = kinds.pop()
                return {"kind": k, "confirmed": False, "source": "derived",
                        "why": "every item category inside it maps there"}
            s = self.suggestion(kind, pos_name)
            return {"kind": s["kind"], "confirmed": False, "source": "suggested", "why": s["why"]}
        # report_category: through the report's own category map when every
        # department filed under it maps the same way.
        deps = self.report_map.get(str(pos_name or "").strip().lower()) or []
        kinds = {self.resolve("department", d)["kind"] for d in deps}
        if deps and len(kinds) == 1 and None not in kinds:
            return {"kind": kinds.pop(), "confirmed": False, "source": "derived",
                    "why": "the departments your report files under it map there"}
        s = suggest(report_category=pos_name)
        return {"kind": s["kind"], "confirmed": False, "source": "suggested", "why": s["why"]}

    def confirmed_any(self) -> bool:
        return any(r.get("confirmed_at") for r in self.rows.values())


def _report_of(restaurant_id, db_path=None) -> dict:
    """{POS name lower ("Beverage", "Other › Darts"): report category as the
    owner spelled it} — the owner's report map (dsr_category_map)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT pos_name, category FROM dsr_category_map WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {str(r["pos_name"]).strip().lower(): str(r["category"]).strip() for r in rows
            if r["category"] and r["pos_name"]}


def _report_map(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT pos_name, category FROM dsr_category_map WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["category"] and r["pos_name"] and SUB_SEP.strip() not in str(r["pos_name"]):
            out.setdefault(str(r["category"]).strip().lower(), []).append(str(r["pos_name"]).strip())
    return out


# ── the page's mapping screen (GET / POST /api/food-cost/bar/mapping) ───────

def mapping_payload(restaurant_id, db_path=None, today=None) -> dict:
    """Everything the mapping screen needs: each POS name with sales in the
    last 28 days (and every sales category the POS lists, with none), its
    suggestion and why, the current choice, whether the account holder
    confirmed it, and its sales; whether a decision is still needed."""
    from datetime import date, timedelta
    from bar.common import WINDOW_DAYS
    from bar.common import local_today
    from bar.sales import bar_sales
    today = today or local_today(restaurant_id)        # the restaurant's date (audit #25 F4)
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    detail = bar_sales(restaurant_id, db_path=db_path, since=since)
    m = detail["mapping"]
    sales = {}
    for night in detail["nights"].values():
        for cell in night["cells"]:
            k = (cell["key_kind"], cell["key"])
            sales[k] = sales.get(k, 0.0) + cell["gross"]
    entries = {}
    for (kind, name), amount in sales.items():
        entries[(kind, name.lower())] = {"kind": kind, "key": name, "sales_28d": round(amount, 2)}
    for (dep, cat), info in m.catalog.items():
        name = f"{info['department'] or ''}{SUB_SEP}{info['name']}"
        entries.setdefault(("item_category", name.lower()), {"kind": "item_category", "key": name, "sales_28d": 0.0})
    for (kind, low), row in m.rows.items():
        entries.setdefault((kind, low), {"kind": kind, "key": row["pos_name"], "sales_28d": 0.0})
    out = []
    for (kind, low), e in entries.items():
        res = m.resolve(kind, e["key"])
        dep, sub = split_key(e["key"]) if kind == "item_category" else (e["key"], None)
        info = m.catalog.get((dep.lower(), (sub or "").lower())) or {}
        sug = m.suggestion(kind, e["key"])
        row = m.rows.get((kind, low)) or {}
        out.append({"key": e["key"], "kind": kind, "department": dep if kind != "report_category" else None,
                    "item_category": sub, "label": e["key"],
                    "choice": res["kind"], "choice_label": BAR_KIND_LABELS.get(res["kind"]) if res["kind"] else None,
                    "source": res["source"], "why": res["why"],
                    "suggestion": sug["kind"], "suggestion_why": sug["why"],
                    "is_bar": (bool(info.get("is_bar")) if info else None),
                    "alt_qty_unit": info.get("alt_qty_unit") if info else None,
                    "confirmed": bool(row.get("confirmed_at")),
                    "confirmed_mdy": _mdy(row.get("confirmed_at")) if row.get("confirmed_at") else None,
                    "sales_28d": e["sales_28d"],
                    "needs_you": e["sales_28d"] > 0 and not row.get("confirmed_at")})
    out.sort(key=lambda r: (-(r["sales_28d"] or 0), r["key"].lower()))
    basis = detail["basis_all"]
    return {"ok": True, "entries": out,
            "options": [{"key": k, "label": BAR_KIND_LABELS[k]} for k in BAR_KINDS],
            "mapping_needed": any(r["needs_you"] for r in out),
            "unmapped": [r for r in out if r["choice"] is None and r["sales_28d"] > 0],
            "confirmed_any": m.confirmed_any(),
            "since_mdy": _mdy(since), "basis": basis}


def save_mapping(restaurant_id, entries, confirm=False, user_id=None, db_path=None, today=None) -> dict:
    """Save the account holder's choices ([{key, kind, bar_category}];
    bar_category "" / None clears a key back to its suggestion). With
    `confirm`, every key on the screen that has a choice — the suggestions
    the owner left as they were included — is confirmed as theirs, once;
    a key with no choice stays unmapped and is returned. Returns
    {ok, saved, confirmed, still_unmapped, changes: [{key, before, after}]}."""
    rid = int(restaurant_id)
    m = Mapping(rid, db_path)
    clean, changes = [], []
    for e in entries or []:
        kind = str((e or {}).get("kind") or "item_category").strip()
        key = str((e or {}).get("key") or "").strip()[:200]
        cat = (e or {}).get("bar_category")
        cat = str(cat).strip().lower() if cat not in (None, "") else None
        if kind not in KEY_KINDS or not key:
            raise ValueError("Which POS category?")
        if cat is not None and cat not in BAR_KINDS:
            raise ValueError("That isn't one of beer, liquor, wine, non-alcoholic or not bar.")
        clean.append((kind, key, cat))
    conn = get_conn(db_path)
    saved = confirmed = 0
    try:
        for kind, key, cat in clean:
            before = m.resolve(kind, key)["kind"]
            sug = m.suggestion(kind, key)
            if cat is None:
                conn.execute("DELETE FROM bar_sales_map WHERE restaurant_id=? AND kind=? AND lower(pos_name)=?",
                             (rid, kind, key.lower()))
            else:
                conn.execute(
                    "INSERT INTO bar_sales_map (restaurant_id, kind, pos_name, bar_category, suggested, suggested_why, "
                    "confirmed_at, confirmed_by, updated_at) VALUES (?,?,?,?,?,?,datetime('now'),?,datetime('now')) "
                    "ON CONFLICT(restaurant_id, kind, pos_name) DO UPDATE SET bar_category=excluded.bar_category, "
                    "suggested=excluded.suggested, suggested_why=excluded.suggested_why, "
                    "confirmed_at=excluded.confirmed_at, confirmed_by=excluded.confirmed_by, "
                    "updated_at=excluded.updated_at",
                    (rid, kind, key, cat, sug["kind"], sug["why"], user_id))
            saved += 1
            if before != cat:
                changes.append({"key": key, "kind": kind, "before": before, "after": cat})
        conn.commit()
    finally:
        conn.close()
    still = []
    if confirm:
        given = {(k, n.lower()) for k, n, _c in clean}
        screen = mapping_payload(rid, db_path=db_path, today=today)["entries"]
        conn = get_conn(db_path)
        try:
            for e in screen:
                if (e["kind"], e["key"].lower()) in given or e["confirmed"]:
                    continue
                if e["choice"] is None:
                    if e["sales_28d"] > 0:
                        still.append(e["key"])
                    continue
                conn.execute(
                    "INSERT INTO bar_sales_map (restaurant_id, kind, pos_name, bar_category, suggested, suggested_why, "
                    "confirmed_at, confirmed_by, updated_at) VALUES (?,?,?,?,?,?,datetime('now'),?,datetime('now')) "
                    "ON CONFLICT(restaurant_id, kind, pos_name) DO UPDATE SET bar_category=excluded.bar_category, "
                    "confirmed_at=excluded.confirmed_at, confirmed_by=excluded.confirmed_by, "
                    "updated_at=excluded.updated_at",
                    (rid, e["kind"], e["key"], e["choice"], e["suggestion"], e["suggestion_why"], user_id))
                confirmed += 1
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "saved": saved, "confirmed": confirmed, "still_unmapped": still, "changes": changes}
