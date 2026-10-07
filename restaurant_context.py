"""restaurant_context — the Restaurant Context Manager (AI orchestration
design, owner-approved 10/7/26, Phase 2).

Every model call used to assemble the restaurant's facts by hand: the labor
read its own payroll-week trend, the food read its own menu notes, the
marketing read its own brand profile — each a little differently, rebuilt on
every call, and keyed (when it was cached at all) on the whole prompt. A
caller now asks once:

    pk = restaurant_context.packet(rid, ("profile", "labor_trend"), viewer=TEAM)
    pk.text            # the sections, rendered in one fixed order
    pk.section("labor_trend").text / .data / .facts
    pk.fingerprint     # the cache key for anything built on the packet

SECTIONS. A name -> Section registry: a "module:function" BUILDER and a
"module:function" VERSION, imported at call time (memory_context.PROVIDERS'
pattern — no import order can drop a section and this module imports nothing
from the app at module scope). Builders reuse the code that already computes
each fact (models, memory_context, staff_settings, business_intelligence,
data_health, event_intel, weather); none re-implements an analysis. A builder
is `fn(req) -> {"text", "facts", "tokens", "data", "missing"}`: text the
model reads (owner-safe, M/D/YY dates, the same words on every surface),
the response_validation facts behind its figures where the source already
produces them, a token estimate, the structured values a caller needs beside
the text (`data`), and `missing` when the section says what is NOT on file —
missing data is said, never written as a zero.

VERSIONS ARE CHANGE MARKERS, NOT CLOCKS. A version function reads the cheap
markers the section's data moves with — source_health's last success and
data-through, the newest row of the tables it reads, the restaurants row's
fields — and a section is rebuilt only when its version changes. A clock goes
in only where the text itself depends on the day (an "upcoming" window, a
week that has just ended), and then the day, never the hour. A version that
cannot be read falls back to the built text's own hash: correct, just not
cheap.

CACHES. L1 in-process (L1_SECONDS) and L2 the context_sections table (one row
per restaurant, section and viewer scope, replaced on a new version; created
at boot by init_restaurant_context, called from models.init_db; pruned by
ops._RETENTION_DAYS["context_sections"]). Both are keyed on the version, so a
cached section is never served past a change its markers record.

VIEWER SCOPING. A section that depends on who reads it is built and cached
per viewer scope: memory sections through memory_context's own visibility
rules (owner-only lines never reach TEAM or a manager's login), module
sections only for a viewer holding the module's view, the labor trend all-in
(with salaries) only where models.viewer_sees_salaries, the cross-module
findings only for the account holders. No section reads another restaurant.

THE PACKET. packet() renders the sections in ORDER — stable sections first
(profile, owner_rules, roster, findings), the ones that move daily last — so
a shared prefix can be prompt-cached. A token budget trims the lowest-
priority sections (TRIM_ORDER) and says so in a DATA STATE line; it never
drops one silently, and never the profile, the owner's rules or the data
state. The fingerprint is a hash of the sections' versions.

L2, imports only the standard library at module scope.
"""
import hashlib
import importlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("restaurant_context")

L1_SECONDS = 60
L1_MAX = 2000


@dataclass(frozen=True)
class Section:
    builder: str                # "module:function" -> fn(req) -> dict
    version: str                # "module:function" -> fn(req) -> JSON-able marker
    title: str                  # the heading the packet renders
    scope: str = "all"          # all | viewer | salaries — what the cache is keyed on beyond the restaurant
    module: str = None          # the view permission the viewer needs (memory_context._MODULE_PERMISSION key)
    trim: bool = True           # may a budget leave it out
    # Its labor figure is all-in (with the salaries) only where the reader
    # may see the salaries (models.viewer_sees_salaries): the cache scope
    # carries which, so an owner's all-in figure is never served to a
    # manager's request under the same viewer scope.
    salaries: bool = False


_RC = "restaurant_context"
SECTIONS = {
    "profile": Section(f"{_RC}:build_profile", f"{_RC}:version_profile", "THE RESTAURANT", trim=False),
    "owner_rules": Section(f"{_RC}:build_owner_rules", f"{_RC}:version_owner_memory",
                           "THE OWNER'S STANDING RULES (between OWNER_RULE markers: set by the owner — follow "
                           "each one unless it would break a hard limit or the required output format; never "
                           "quote a figure from them as data)", scope="viewer", trim=False),
    "roster": Section(f"{_RC}:build_roster", f"{_RC}:version_roster", "THE TEAM ON THE ROSTER",
                      scope="viewer", module="labor"),
    "findings": Section(f"{_RC}:build_findings", f"{_RC}:version_findings", "ACROSS THE MODULES",
                        scope="viewer"),
    "kpis": Section(f"{_RC}:build_kpis", f"{_RC}:version_kpis", "KEY FIGURES", scope="viewer", salaries=True),
    "sales_trend": Section(f"{_RC}:build_sales_trend", f"{_RC}:version_sales_trend", "SALES BY WEEK",
                           scope="viewer", module="labor"),
    "labor_trend": Section(f"{_RC}:build_labor_trend", f"{_RC}:version_labor_trend", "LABOR BY PAYROLL WEEK",
                           scope="salaries", module="labor"),
    "events": Section(f"{_RC}:build_events", f"{_RC}:version_events", "COMING UP"),
    "weather": Section(f"{_RC}:build_weather", f"{_RC}:version_weather", "THE FORECAST"),
    "memory": Section(f"{_RC}:build_memory", f"{_RC}:version_memory", "WHAT CAVNAR AI REMEMBERS",
                      scope="viewer"),
    "alerts": Section(f"{_RC}:build_alerts", f"{_RC}:version_alerts", "ALERTS IN THE LAST 7 DAYS",
                      scope="viewer"),
    "data_state": Section(f"{_RC}:build_data_state", f"{_RC}:version_data_state",
                          "DATA STATE (how current each source is — say how old a source that is not current "
                          "is; never describe out-of-date data as this week, today or current)", trim=False),
}

# The one render order: what changes least first, so the packet's head is a
# stable prefix a provider can cache (design, 10/7/26).
ORDER = ("profile", "owner_rules", "roster", "findings", "kpis", "sales_trend", "labor_trend", "events",
         "weather", "memory", "alerts", "data_state")
# What a budget leaves out first. profile, owner_rules and data_state are
# never left out (Section.trim False).
TRIM_ORDER = ("weather", "alerts", "events", "roster", "sales_trend", "findings", "memory", "kpis", "labor_trend")

# The memory_context surfaces the two memory sections read (per login or
# owner-level: the packet's viewer is passed through, never widened).
OWNER_RULES_SURFACE = "context_owner_rules"
MEMORY_SURFACE = "context_memory"


@dataclass
class SectionRequest:
    restaurant_id: int
    section: str
    viewer: object = None        # the caller's viewer (a login dict, TEAM, PRINCIPALS or None)
    scope: str = "all"
    params: dict = field(default_factory=dict)
    db_path: str = None
    _restaurant: object = None

    def restaurant(self):
        if self._restaurant is None:
            import models
            self._restaurant = (models.get_restaurant(self.restaurant_id, self.db_path) if self.db_path
                                else models.get_restaurant(self.restaurant_id))
        return self._restaurant

    def today(self):
        """The restaurant's own calendar date (never the server's)."""
        try:
            from time_utils import restaurant_now_by_id
            return restaurant_now_by_id(self.restaurant_id).date()
        except Exception:
            from datetime import date
            return date.today()


@dataclass
class Built:
    name: str
    text: str = ""
    facts: list = field(default_factory=list)       # response_validation.Fact
    tokens: int = 0
    data: dict = field(default_factory=dict)
    missing: bool = False
    version: str = ""
    scope: str = "all"
    source: str = "build"        # build | l1 | l2 | error

    @property
    def rendered(self) -> str:
        if not self.text:
            return ""
        title = SECTIONS[self.name].title if self.name in SECTIONS else self.name.upper()
        return f"{title}:\n{self.text}"


@dataclass
class Packet:
    text: str
    facts: list
    versions: dict
    fingerprint: str
    sections: dict               # name -> Built, in ORDER
    trimmed: list = field(default_factory=list)
    tokens: int = 0

    def section(self, name) -> Built:
        return self.sections.get(name) or Built(name=name, missing=True)


def estimate_tokens(text) -> int:
    """About four characters a token — an estimate for budgets, never billed."""
    return (len(str(text or "")) + 3) // 4


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _resolve(path):
    mod, fn = path.split(":", 1)
    return getattr(importlib.import_module(mod), fn, None)


def _conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── schema (boot only: models.init_db calls init_restaurant_context) ────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_sections (
    restaurant_id INTEGER NOT NULL,
    section       TEXT    NOT NULL,
    viewer_scope  TEXT    NOT NULL,
    version       TEXT    NOT NULL,
    built_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    text          TEXT,
    facts_json    TEXT,
    data_json     TEXT,
    tokens        INTEGER,
    missing       INTEGER DEFAULT 0,
    PRIMARY KEY (restaurant_id, section, viewer_scope)
);
CREATE INDEX IF NOT EXISTS idx_context_sections_built ON context_sections(built_at);
"""


def init_restaurant_context(db_path=None):
    conn = _conn(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    with _L1_LOCK:
        _L1.clear()


# ── viewer scope ────────────────────────────────────────────────────────────

def viewer_scope(viewer, section=None) -> str:
    """The cache scope a viewer reads a section under. "principals" for no
    login (an owner-level output) or the account holders' view; "team" (with
    its grants) for a shared output; "login:<id>:<role hash>" for one login —
    its role and grants are in the key, so a changed permission is a new
    scope. A section whose Section.scope is "all" is one scope for everyone;
    "salaries" is whether the reader may see the salaries (the labor trend's
    all-in figure)."""
    sec = SECTIONS.get(section) if section else None
    kind = sec.scope if sec else "viewer"
    if kind == "all":
        return "all"
    if kind == "salaries":
        try:
            import models
            return "salaries" if models.viewer_sees_salaries() else "hourly"
        except Exception:
            return "hourly"
    import memory_context as mc
    if viewer is None or mc.is_principals(viewer):
        return "principals"
    if mc.is_team(viewer):
        grants = sorted((viewer.get("grants") or ()) if isinstance(viewer, dict) else ())
        return "team" + (":" + ",".join(grants) if grants else "")
    user = mc.viewer_user(viewer)
    if not isinstance(user, dict):
        return "principals"
    ident = {k: user.get(k) for k in ("role", "is_admin", "permissions", "grants", "perms", "module_permissions")}
    return f"login:{user.get('id')}:{_hash(ident)[:10]}"


def _memory_viewer(viewer):
    """The viewer memory_context reads: the caller's own, TEAM kept as TEAM,
    None as PRINCIPALS (memory_context's own default)."""
    import memory_context as mc
    if viewer is None:
        return mc.PRINCIPALS
    return viewer


def may_read(viewer, module) -> bool:
    """Whether `viewer` holds the view of `module` (None module: everyone).
    The owner-level views (None, PRINCIPALS) read every module."""
    if not module:
        return True
    import memory_context as mc
    if viewer is None or mc.is_principals(viewer):
        return True
    if mc.is_team(viewer):
        user = viewer if isinstance(viewer, dict) else mc.TEAM
    else:
        user = mc.viewer_user(viewer)
    if user is None:
        return True
    try:
        return bool(mc._may_read_module(user, module, {}))
    except Exception:
        return False


# ── L1 / L2 ─────────────────────────────────────────────────────────────────

_L1 = {}
_L1_LOCK = threading.Lock()


def invalidate(restaurant_id=None, sections=None):
    """Drop L1 entries (a restaurant's, or every one). L2 needs no drop: its
    rows are keyed on the version, so a change is a miss by itself."""
    want = set(sections or ())
    with _L1_LOCK:
        for k in [k for k in _L1 if (restaurant_id is None or k[0] == restaurant_id)
                  and (not want or k[1] in want)]:
            _L1.pop(k, None)


def _l1_get(key, version):
    with _L1_LOCK:
        hit = _L1.get(key)
    if not hit:
        return None
    at, ver, built = hit
    if ver != version or time.time() - at > L1_SECONDS:
        return None
    return built


def _l1_put(key, version, built):
    with _L1_LOCK:
        if len(_L1) >= L1_MAX:
            _L1.pop(next(iter(_L1)), None)
        _L1[key] = (time.time(), version, built)


def _facts_out(facts) -> list:
    out = []
    for f in facts or ():
        if isinstance(f, dict):
            out.append(f)
        else:
            try:
                from dataclasses import asdict
                out.append(asdict(f))
            except Exception:
                continue
    return out


def _facts_in(rows) -> list:
    """response_validation.Fact objects from stored dicts (a dict the Fact
    does not take is kept as the dict)."""
    out = []
    try:
        import response_validation as rv
    except Exception:
        return list(rows or [])
    for d in rows or ():
        try:
            out.append(rv.Fact(**d) if isinstance(d, dict) else d)
        except TypeError:
            out.append(d)
    return out


def _l2_get(rid, name, scope, version, db_path=None):
    try:
        conn = _conn(db_path)
        try:
            r = conn.execute("SELECT version, text, facts_json, data_json, tokens, missing FROM context_sections "
                             "WHERE restaurant_id=? AND section=? AND viewer_scope=?", (rid, name, scope)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        log.debug("context_sections unreadable: %s", e)
        return None
    if not r or r["version"] != version:
        return None
    try:
        facts = json.loads(r["facts_json"] or "[]")
        data = json.loads(r["data_json"] or "{}")
    except (TypeError, ValueError):
        return None
    return Built(name=name, text=r["text"] or "", facts=_facts_in(facts), tokens=int(r["tokens"] or 0),
                 data=data if isinstance(data, dict) else {}, missing=bool(r["missing"]), version=version,
                 scope=scope, source="l2")


def _l2_put(rid, built, db_path=None):
    try:
        conn = _conn(db_path)
        try:
            conn.execute(
                "INSERT INTO context_sections (restaurant_id, section, viewer_scope, version, built_at, text, "
                "facts_json, data_json, tokens, missing) VALUES (?,?,?,?,datetime('now'),?,?,?,?,?) "
                "ON CONFLICT(restaurant_id, section, viewer_scope) DO UPDATE SET version=excluded.version, "
                "built_at=excluded.built_at, text=excluded.text, facts_json=excluded.facts_json, "
                "data_json=excluded.data_json, tokens=excluded.tokens, missing=excluded.missing",
                (rid, built.name, built.scope, built.version, built.text,
                 json.dumps(_facts_out(built.facts), default=str), json.dumps(built.data or {}, default=str),
                 int(built.tokens or 0), 1 if built.missing else 0))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("context_sections not written (%s): %s", built.name, e)


# ── one section ─────────────────────────────────────────────────────────────

def _normalise(name, out) -> Built:
    out = out if isinstance(out, dict) else {"text": str(out or "")}
    text = str(out.get("text") or "").strip()
    return Built(name=name, text=text, facts=list(out.get("facts") or []),
                 tokens=int(out.get("tokens") or estimate_tokens(text)), data=dict(out.get("data") or {}),
                 missing=bool(out.get("missing")))


def _params_key(params) -> str:
    return (";p" + _hash(params)[:10]) if params else ""


def section(restaurant_id, name, viewer=None, params=None, db_path=None) -> Built:
    """One section for one viewer: from L1, else L2, else built — each only
    while its version stands. Never raises: a builder that fails gives a
    section that says it could not be read (missing), never cached."""
    sec = SECTIONS.get(name)
    if sec is None:
        raise KeyError(f"unknown context section {name!r}")
    params = dict(params or {})
    scope = viewer_scope(viewer, name) + _params_key(params)
    if sec.salaries:
        scope += ";" + viewer_scope(None, "labor_trend")
    req = SectionRequest(restaurant_id=restaurant_id, section=name, viewer=viewer, scope=scope, params=params,
                         db_path=db_path)
    if sec.module and not may_read(viewer, sec.module):
        return Built(name=name, text="Not shown: this reader does not have that module's view — say nothing "
                                     "about it.", missing=True, version="noview", scope=scope, source="build",
                     tokens=20)
    try:
        version = _hash(_resolve(sec.version)(req))
    except Exception as e:
        log.info("context section %s version unreadable for %s: %s", name, restaurant_id, e)
        version = None
    key = (restaurant_id, name, scope, db_path or "")
    if version:
        hit = _l1_get(key, version)
        if hit is not None:
            return Built(**{**hit.__dict__, "source": "l1"})
        hit = _l2_get(restaurant_id, name, scope, version, db_path)
        if hit is not None:
            _l1_put(key, version, hit)
            return hit
    try:
        built = _normalise(name, _resolve(sec.builder)(req))
    except Exception as e:
        log.warning("context section %s failed for %s: %s: %s", name, restaurant_id, type(e).__name__, e)
        return Built(name=name, text="Could not be read right now — say nothing about it.", missing=True,
                     version="error", scope=scope, source="error", tokens=12)
    built.scope = scope
    # No marker: the version is the text itself — correct, never cheap.
    built.version = version or ("c:" + _hash([built.text, _facts_out(built.facts), built.data]))
    _l1_put(key, built.version, built)
    if version:
        _l2_put(restaurant_id, built, db_path)
    return built


def packet(restaurant_id, sections, viewer=None, budget_tokens=None, params=None, db_path=None) -> Packet:
    """The named sections for `viewer`, rendered in ORDER (module docstring).
    `params` ({section: {...}}) narrows a section (the memory section's
    surface and subjects, the kpis' modules); it is part of the cache key.
    `budget_tokens` trims TRIM_ORDER's sections first and says which in a
    DATA STATE line."""
    names = [n for n in ORDER if n in set(sections or ())]
    unknown = [n for n in (sections or ()) if n not in SECTIONS]
    if unknown:
        raise KeyError(f"unknown context sections {unknown}")
    params = params or {}
    built = {n: section(restaurant_id, n, viewer=viewer, params=params.get(n), db_path=db_path) for n in names}
    trimmed = []
    if budget_tokens:
        total = sum(b.tokens for b in built.values() if b.text)
        for n in TRIM_ORDER:
            if total <= int(budget_tokens):
                break
            if n in built and built[n].text and SECTIONS[n].trim:
                total -= built[n].tokens
                trimmed.append(n)
    kept = [n for n in names if n not in trimmed]
    parts = [built[n].rendered for n in kept if built[n].rendered and n != "data_state"]
    tail = built["data_state"].text if "data_state" in kept else ""
    if trimmed:
        note = ("- Left out of this context for length: " + ", ".join(n.replace("_", " ") for n in trimmed)
                + " — not missing data; say nothing about them rather than guess.")
        tail = (tail + "\n" + note) if tail else note
    if tail:
        parts.append(f"{SECTIONS['data_state'].title}:\n{tail}")
    text = "\n\n".join(parts)
    facts = [f for n in kept for f in built[n].facts]
    versions = {n: built[n].version for n in names}
    fp = _hash({"v": versions, "trimmed": trimmed, "scopes": {n: built[n].scope for n in names}})
    return Packet(text=text, facts=facts, versions=versions, fingerprint=fp,
                  sections={n: built[n] for n in names}, trimmed=trimmed, tokens=estimate_tokens(text))


# ── markers ─────────────────────────────────────────────────────────────────

def _marker(req, sql, args=()):
    """One row of cheap markers (counts, max ids, newest stamps), as a
    list; [] when the table is not there (a section with nothing on file)."""
    try:
        conn = _conn(req.db_path)
        try:
            r = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
        return list(r) if r else []
    except Exception:
        return []


def _source_markers(req, sources=None):
    """source_health's change markers for this restaurant: per source its
    last success's DAY, the day its data runs through, and whether it is
    failing — what a section's freshness wording moves with (an hourly
    sync that brings nothing new changes none of them)."""
    q = ("SELECT source, substr(COALESCE(last_ok_at,''),1,10), COALESCE(data_through,''), "
         "COALESCE(error_class,''), consecutive_failures > 0 FROM source_health WHERE restaurant_id=?")
    try:
        conn = _conn(req.db_path)
        try:
            rows = conn.execute(q + " ORDER BY source", (req.restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [list(r) for r in rows if sources is None or r[0] in sources]


def _restaurant_fields(req, names):
    r = req.restaurant()
    return {n: getattr(r, n, None) for n in names} if r is not None else None


# ── builders and versions ───────────────────────────────────────────────────
#
# Thin adapters over the code that owns each fact. A new section is one
# SECTIONS entry; its builder may live in the module that owns the fact.

_PROFILE_FIELDS = ("name", "owner_name", "neighborhood", "vibe", "known_for", "voice_notes", "never_say",
                   "sign_off_name", "menu_notes", "inventory_notes", "skip_holidays", "menu_url", "timezone")


def version_profile(req):
    return _restaurant_fields(req, _PROFILE_FIELDS)


def build_profile(req):
    """The restaurant as every prompt names it: the restaurants row and the
    brand profile marketing.get_profile_for_restaurant reads (the one
    reader of it). The owner's typed notes — menu, kitchen counting — are
    fenced as untrusted text, as every read fenced them."""
    r = req.restaurant()
    if r is None:
        return {"text": "Restaurant not found.", "missing": True}
    from ai_guard import wrap_untrusted
    try:
        import marketing
        brand = marketing.get_profile_for_restaurant(req.restaurant_id)
    except Exception:
        brand = {}
    data = {k: (getattr(r, k, None) or "") for k in _PROFILE_FIELDS}
    data["brand"] = dict(brand or {})
    lines = [f"- Name: {r.name}"]
    if data.get("owner_name"):
        lines.append(f"- Owner: {data['owner_name']}")
    if data.get("neighborhood"):
        lines.append(f"- Where: {data['neighborhood']}")
    if data.get("known_for"):
        lines.append(f"- Known for: {data['known_for']}")
    if data.get("vibe"):
        lines.append(f"- Vibe: {data['vibe']}")
    if data.get("menu_notes"):
        lines.append("- Menu notes (the owner's own words): " + wrap_untrusted(str(data["menu_notes"])[:300]))
    if data.get("inventory_notes"):
        lines.append("- How the kitchen counts and buys (from setup): "
                     + wrap_untrusted(str(data["inventory_notes"]).strip()[:400]))
    return {"text": "\n".join(lines), "data": data}


def version_owner_memory(req):
    """The owner memory's change markers: rows written, changed, confirmed,
    pinned, archived and restored, the facts whose date has passed (an
    expiry changes what is live without a write), and the organisation's
    shared facts. Never the hour."""
    rid = req.restaurant_id
    today = req.today().isoformat()
    m = _marker(req, "SELECT COUNT(*), MAX(id), MAX(COALESCE(updated_at, created_at)), MAX(confirmed_at), "
                     "SUM(COALESCE(pinned,0)), SUM(CASE WHEN valid_until < ? THEN 1 ELSE 0 END), "
                     "SUM(CASE WHEN due_on IS NOT NULL AND date(due_on, '+7 days') < ? THEN 1 ELSE 0 END) "
                     "FROM ask_memory WHERE restaurant_id=?", (today, today, rid))
    arch = _marker(req, "SELECT COUNT(*), MAX(id) FROM ask_memory_archive WHERE restaurant_id=?", (rid,))
    org = []
    try:
        import owner_memory
        others = owner_memory._org_others(rid, db_path=req.db_path)
        if others:
            q = ",".join("?" * len(others))
            org = _marker(req, f"SELECT COUNT(*), MAX(id), MAX(COALESCE(updated_at, created_at)) FROM ask_memory "
                               f"WHERE scope='org' AND restaurant_id IN ({q})", tuple(others))
    except Exception:
        org = []
    return [m, arch, org]


def build_owner_rules(req):
    """The account holders' standing rules as memory_context renders them
    (OWNER_RULE markers, dated, who set each), for this viewer."""
    import memory_context as mc
    block = mc.memory_context(req.restaurant_id, OWNER_RULES_SURFACE, viewer=_memory_viewer(req.viewer),
                              db_path=req.db_path)
    body = (getattr(block, "bodies", None) or {}).get("owner_rules") or ""
    if not body.strip():
        return {"text": "None on file.", "missing": True}
    return {"text": body}


def version_memory(req):
    """The owner memory's markers, the recommendation answers and Cavnar AI's
    own reads and claims (decisions, what worked, the last claim), the
    people and the links — and the day, because the memory ranks by
    recency and drops a line whose date has passed."""
    rid = req.restaurant_id
    return [version_owner_memory(req),
            _marker(req, "SELECT COUNT(*), MAX(id) FROM rec_events WHERE restaurant_id=?", (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(id) FROM ai_reads WHERE restaurant_id=?", (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(id) FROM ai_claims WHERE restaurant_id=?", (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(last_seen), MAX(resolved_at) FROM bi_links WHERE restaurant_id=?",
                    (rid,)),
            req.today().isoformat(), req.params]


def build_memory(req):
    """memory_context for this viewer: the surface and subjects the caller
    names (params), else MEMORY_SURFACE — everything but the owner's rules
    (their own section) and a chat's own conversation."""
    import memory_context as mc
    surface = str(req.params.get("surface") or MEMORY_SURFACE)
    block = mc.memory_context(req.restaurant_id, surface, viewer=_memory_viewer(req.viewer),
                              subjects=list(req.params.get("subjects") or ()), db_path=req.db_path)
    text = block.text_without(("owner_rules",)) if surface == MEMORY_SURFACE or "owner_rules" in (
        mc.SURFACE_SECTIONS.get(surface) or ()) else block.text
    if not str(text or "").strip():
        return {"text": "Nothing on file yet.", "missing": True}
    return {"text": text}


def version_roster(req):
    rid = req.restaurant_id
    return [_marker(req, "SELECT COUNT(*), MAX(updated_at), SUM(active) FROM staff_settings WHERE restaurant_id=?",
                    (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(date), MAX(saved_at) FROM labor_daily_history WHERE restaurant_id=?",
                    (rid,)),
            _source_markers(req, ("labor", "pos")), req.today().isoformat()]


def build_roster(req):
    """The active people and the role each mostly works (staff_settings.
    roster, the one list the schedule and the team screen read). Names and
    roles only — never pay."""
    import staff_settings
    people = staff_settings.roster(req.restaurant_id, **({"db_path": req.db_path} if req.db_path else {}))
    if not people:
        return {"text": "No one on the roster yet — no shifts on file and nobody added by hand.", "missing": True}
    from time_utils import mdy
    lines = []
    for p in people[:60]:
        bits = [p.get("role") or "role not known"]
        if p.get("last_worked"):
            bits.append(f"last worked {mdy(p['last_worked'])}")
        lines.append(f"- {p.get('name')}: " + ", ".join(bits))
    if len(people) > 60:
        lines.append(f"- (+{len(people) - 60} more on the roster)")
    return {"text": "\n".join(lines), "data": {"count": len(people)}}


def version_labor_trend(req):
    rid = req.restaurant_id
    share = None
    try:
        import models
        share = models.salaried_day_share(req.restaurant())
    except Exception:
        share = None
    return [_marker(req, "SELECT COUNT(*), MAX(id), MAX(saved_at), MAX(period_end) FROM labor_history "
                         "WHERE restaurant_id=?", (rid,)),
            share, req.scope,
            # A payroll week is "complete" once its last day is before today.
            req.today().isoformat()]


def build_labor_trend(req):
    """The labor by payroll week and the one week-on-week comparison
    (models.get_labor_history / labor_period_change — every surface states
    the same one): the labor read's trend lines, moved here so Home, the
    DSR and Ask state the same weeks in the same words."""
    import labor
    return labor.labor_trend_section(req.restaurant_id)


def version_sales_trend(req):
    rid = req.restaurant_id
    iso = req.today().isocalendar()
    return [_marker(req, "SELECT COUNT(*), MAX(date), MAX(saved_at), SUM(sales) FROM labor_daily_history "
                         "WHERE restaurant_id=?", (rid,)),
            f"{iso[0]}-W{iso[1]:02d}"]


SALES_WEEKS = 6


def build_sales_trend(req):
    """Sales by complete ISO week (Monday to Sunday) from the nightly
    archive (labor_daily_history.sales — the figures the labor % divides
    by), newest last; a week with days missing says how many it has."""
    from datetime import timedelta
    from time_utils import mdy_range
    today = req.today()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=SALES_WEEKS)
    try:
        conn = _conn(req.db_path)
        try:
            rows = conn.execute("SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date >= ? "
                                "AND date < ? AND sales IS NOT NULL AND sales > 0",
                                (req.restaurant_id, start.isoformat(), this_monday.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    weeks = {}
    for r in rows:
        try:
            from datetime import date
            d = date.fromisoformat(str(r["date"])[:10])
        except (TypeError, ValueError):
            continue
        mon = d - timedelta(days=d.weekday())
        w = weeks.setdefault(mon, {"sales": 0.0, "days": 0})
        w["sales"] += float(r["sales"] or 0)
        w["days"] += 1
    if not weeks:
        return {"text": "No sales on file for the last six complete weeks — state no sales figure.",
                "missing": True}
    import response_validation as rv
    lines, facts, data = [], [], []
    for mon in sorted(weeks):
        w = weeks[mon]
        span = mdy_range(mon.isoformat(), (mon + timedelta(days=6)).isoformat())
        part = "" if w["days"] >= 7 else f" ({w['days']} of 7 days on file)"
        lines.append(f"- {span}: ${w['sales']:,.0f}{part}")
        facts.append(rv.Fact(f"sales.week.{mon.isoformat()}", round(w["sales"], 2), "$", "measured", "week",
                             data_days=w["days"]))
        data.append({"week_start": mon.isoformat(), "sales": round(w["sales"], 2), "days": w["days"]})
    return {"text": "\n".join(lines), "facts": facts, "data": {"weeks": data}}


KPI_MODULES = ("labor", "reviews")


def version_kpis(req):
    rid = req.restaurant_id
    mods = tuple(req.params.get("modules") or KPI_MODULES)
    iso = req.today().isocalendar()
    out = [mods, f"{iso[0]}-W{iso[1]:02d}", req.scope]
    if "labor" in mods:
        out.append(version_labor_trend(req))
    if "reviews" in mods:
        out.append(_marker(req, "SELECT COUNT(*), MAX(id), MAX(fetched_at), MAX(approved_at), "
                                "SUM(response_status IN ('posted','approved','skipped')), SUM(processed), "
                                "SUM(deleted_at IS NOT NULL) FROM reviews WHERE restaurant_id=?", (rid,)))
    return out


def build_kpis(req):
    """The headline figure of each module the viewer may read, from the
    module's own reader (the last complete payroll week's labor %; the
    reviews' total, lifetime average and the urgent ones still owed a
    reply). A module with nothing measured says so."""
    mods = tuple(req.params.get("modules") or KPI_MODULES)
    import response_validation as rv
    lines, facts, data = [], [], {}
    if "labor" in mods and may_read(req.viewer, "labor"):
        import models
        hist = [h for h in models.get_labor_history(req.restaurant_id, limit=2) if h.get("complete")]
        if hist and hist[0].get("labor_pct") is not None:
            from time_utils import mdy_range
            h = hist[0]
            lines.append(f"- Labor: {h['labor_pct']}% of sales, the payroll week "
                         f"{mdy_range(h['period_start'], h['period_end'])}")
            facts.append(rv.Fact("kpi.labor_pct.week", h["labor_pct"], "%", "measured", "week"))
            data["labor"] = {"labor_pct": h["labor_pct"], "period_start": h["period_start"],
                             "period_end": h["period_end"]}
        else:
            lines.append("- Labor: no complete payroll week on file yet")
    if "reviews" in mods and may_read(req.viewer, "reviews"):
        import models
        st = models.get_review_stats(req.restaurant_id) or {}
        total = int(st.get("total") or 0)
        if total:
            avg = st.get("avg_rating")
            lines.append(f"- Reviews: {total} on file"
                         + (f", {avg}★ lifetime average" if avg else ", no rated reviews")
                         + f"; {int(st.get('urgent') or 0)} urgent still owed a reply")
            facts += [rv.Fact("kpi.reviews.total", total, "count", "measured"),
                      rv.Fact("kpi.reviews.avg_rating", avg, "★", "measured"),
                      rv.Fact("kpi.reviews.urgent", st.get("urgent"), "count", "measured")]
            data["reviews"] = {k: st.get(k) for k in ("total", "avg_rating", "urgent", "response_rate")}
        else:
            lines.append("- Reviews: none on file yet")
    if not lines:
        return {"text": "No module this reader may see has a figure on file.", "missing": True}
    return {"text": "\n".join(lines), "facts": facts, "data": data,
            "missing": not data}


def version_findings(req):
    rid = req.restaurant_id
    return [_source_markers(req),
            _marker(req, "SELECT COUNT(*), MAX(last_seen), MAX(resolved_at) FROM bi_links WHERE restaurant_id=?",
                    (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(id) FROM rec_events WHERE restaurant_id=?", (rid,)),
            req.today().isoformat()]


def _is_owner_view(viewer) -> bool:
    import memory_context as mc
    if viewer is None or mc.is_principals(viewer):
        return True
    if mc.is_team(viewer):
        return False
    user = mc.viewer_user(viewer)
    try:
        from permissions import is_principal
        return bool(user and is_principal(user))
    except Exception:
        return False


def build_findings(req):
    """What lines up across the modules and where the money is — Ask's
    cross-module snapshot (business_intelligence.snapshot_block). It names
    food cost, comps and the owner's dollars, so it is the account holders'
    view only."""
    if not _is_owner_view(req.viewer):
        return {"text": "Not shown: the cross-module findings are the owner's view — say nothing about them.",
                "missing": True}
    import business_intelligence as bi
    block = bi.snapshot_block(req.restaurant_id, restaurant=req.restaurant())
    body = str(block or "").strip()
    if body.startswith(getattr(bi, "SNAPSHOT_HEADER", "\0")):
        body = body[len(bi.SNAPSHOT_HEADER):].strip()
    if not body:
        return {"text": "Nothing lines up across the modules right now.", "missing": True}
    return {"text": body}


EVENT_DAYS = 14


def version_events(req):
    rid = req.restaurant_id
    return [_marker(req, "SELECT COUNT(*), MAX(series_id), MAX(updated_at) FROM event_follows "
                         "WHERE restaurant_id=? AND active=1", (rid,)),
            _marker(req, "SELECT COUNT(*), MAX(updated_at) FROM catalog_events WHERE series_id IN "
                         "(SELECT series_id FROM event_follows WHERE restaurant_id=? AND active=1)", (rid,)),
            _marker(req, "SELECT COUNT(*) FROM event_dismissals WHERE restaurant_id=?", (rid,)),
            _restaurant_fields(req, ("skip_holidays",)), req.today().isoformat()]


def build_events(req):
    """The followed games and events in the next EVENT_DAYS days
    (event_intel.engine.upcoming, each in its own words) and the holidays in
    the next 30 (marketing.get_upcoming_holidays), the owner's skipped
    holidays left out."""
    lines = []
    try:
        from event_intel import engine as ev
        for u in ev.upcoming(req.restaurant_id, days=EVENT_DAYS)[:8]:
            lines.append(f"- {u.get('describe')}")
    except Exception as e:
        log.debug("events unreadable for %s: %s", req.restaurant_id, e)
    try:
        import marketing
        from time_utils import restaurant_now_by_id
        hols = marketing.get_upcoming_holidays(restaurant_now_by_id(req.restaurant_id).replace(tzinfo=None)) or ""
        skip = [h.strip().lower() for h in (getattr(req.restaurant(), "skip_holidays", "") or "").split(",")
                if h.strip()]
        hols = ", ".join(h for h in hols.split(", ") if h and not any(s in h.lower() for s in skip))
        if hols:
            lines.append(f"- Holidays in the next 30 days: {hols}")
    except Exception as e:
        log.debug("holidays unreadable for %s: %s", req.restaurant_id, e)
    if not lines:
        return {"text": "No followed event or holiday coming up.", "missing": True}
    return {"text": "\n".join(lines)}


def version_weather(req):
    return [_restaurant_fields(req, ("weather_cached_at",)), req.today().isoformat()]


def build_weather(req):
    """The next seven days from the forecast already on file (weather.
    cached_forecast_for_week — never a fetch on a prompt's path), each day
    marked when the copy is out of date."""
    from datetime import timedelta
    import weather
    from time_utils import mdy
    today = req.today()
    rows = weather.cached_forecast_for_week(req.restaurant(), [(today + timedelta(days=i)).isoformat()
                                                                for i in range(7)])
    if not rows:
        return {"text": "No forecast on file — say nothing about the weather.", "missing": True}
    lines = []
    for r in rows:
        bits = [r.get("short_forecast") or "forecast"]
        if r.get("high_f") is not None:
            bits.append(f"high {r['high_f']}°F")
        if r.get("precip_pct") is not None:
            bits.append(f"{r['precip_pct']}% chance of rain")
        lines.append(f"- {mdy(r['date'])}: " + ", ".join(bits) + (" (an out-of-date forecast)" if r.get("stale")
                                                                   else ""))
    return {"text": "\n".join(lines)}


_ALERT_MODULES = (("review", "reviews"), ("rating", "reviews"), ("labor", "labor"), ("overtime", "labor"),
                  ("schedule", "labor"), ("waste", "food"), ("food", "food"), ("price", "food"), ("stock", "food"),
                  ("invoice", "food"))


def _alert_module(alert_type):
    t = str(alert_type or "").lower()
    for word, module in _ALERT_MODULES:
        if word in t:
            return module
    return None


def version_alerts(req):
    return [_marker(req, "SELECT COUNT(*), MAX(id) FROM alert_log WHERE restaurant_id=?", (req.restaurant_id,)),
            req.today().isoformat()]


def build_alerts(req):
    """The alerts Cavnar AI sent in the last seven days, counted by kind —
    only the kinds about a module this reader may see."""
    try:
        conn = _conn(req.db_path)
        try:
            rows = conn.execute("SELECT alert_type, COUNT(*) AS n, MAX(fired_at) AS last FROM alert_log "
                                "WHERE restaurant_id=? AND fired_at >= datetime('now', '-7 days') "
                                "GROUP BY alert_type ORDER BY n DESC", (req.restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    from time_utils import mdy
    lines = []
    for r in rows:
        if not may_read(req.viewer, _alert_module(r["alert_type"])):
            continue
        lines.append(f"- {str(r['alert_type']).replace('_', ' ')}: {r['n']} (last {mdy(str(r['last'])[:10])})")
    if not lines:
        return {"text": "No alerts in the last 7 days.", "missing": True}
    return {"text": "\n".join(lines)}


def version_data_state(req):
    return [_source_markers(req), req.today().isoformat()]


def build_data_state(req):
    """Each connected source's state and the date it is current to, from the
    Data Health snapshot (data_health.snapshot — the one reading the Data
    Health page, Home and Ask show). Days, never hours, so the section moves
    with the data, not the clock."""
    import data_health
    snap = data_health.snapshot(req.restaurant_id, restaurant=req.restaurant())
    if not snap or not snap.get("ok"):
        return {"text": "Data health could not be read — treat every source's age as unknown.", "missing": True}
    lines = []
    for s in snap.get("sources") or []:
        state = s.get("state") or "unknown"
        word = {"current": "current", "aging": "aging — say how old", "stale": "OUT OF DATE — say so",
                "unknown": "age unknown — do not present as current"}.get(state, state)
        as_of = s.get("as_of")
        lines.append(f"- {s.get('label') or s.get('key')}: {word}" + (f" (as of {as_of})" if as_of else ""))
    for s in snap.get("not_connected") or []:
        lines.append(f"- {s.get('label') or s.get('key')}: not connected — say nothing about it")
    if not lines:
        return {"text": "No data source is connected yet.", "missing": True}
    return {"text": "\n".join(lines)}
