"""owner_memory — what the owner (and the team) tell Cavnar AI, typed and
served to every model call (memory audit 9/29/26: owner_lanes, owner_reach,
owner_goals, conversations, sales_audit).

Three things live here:

  THE OWNER'S WORDS, typed. A fact is one sentence in someone's own terms
  with a kind (constraint | context | preference | goal | followup), the
  modules it is about (which generators read it), an optional subject tag,
  who said it (author and authority), who may read it (audience: team,
  principals, author), and a validity (valid_until for a time-bound fact,
  due_on for a follow-up). Stored in models' ask_memory, one lane per kind,
  with what the lanes and dates remove kept in ask_memory_archive.
  remember / forget / retract_for_keys / expire / facts_for are the one
  write and read path — Ask's tools, Account, the sales audit and the
  ratings all go through them, and every write drops Ask's cached snapshot.

  THE PROVIDERS memory_context reads: constraint_lines (the facts for the
  generator asking), goal_lines (active goals for the metrics in play) and
  conversation_lines (Ask's rolling chat summary, what its last answer
  read, and the questions a login keeps asking).

  THE TARGET RESOLVER: target_for(rid, metric) — the owner's active goal on
  a metric, which thresholds.target_for and every module that judges a
  figure read before their own setting.

Nothing here writes a model's words as the owner's: a model-written summary
is labelled as one and fenced like every other untrusted line.
"""
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

KINDS = ("constraint", "context", "preference", "goal", "followup")
# A follow-up stays this many days past its due date, then leaves.
FOLLOWUP_GRACE_DAYS = 7

# Which modules' facts each memory_context surface reads. A fact with no
# modules is about the whole business and reaches every surface; None reads
# every fact.
SURFACE_MODULES = {
    "ask": None, "ask_conversation": None,
    "schedule": {"labor", "schedule", "ops"},
    "labor_read": {"labor", "schedule"},
    "food_read": {"food"},
    "food_diagnosis": {"food"},
    "review_diagnosis": {"reviews", "ops"},
    "reply_drafter": {"reviews"},
    "marketing": {"marketing", "guests"},
    "competitor_read": {"intel", "marketing"},
    "dsr_narrative": None, "brief": None, "digest": None, "weekly_plan": None,
}
# Follow-ups are for the surfaces an owner reads a to-do on.
FOLLOWUP_SURFACES = ("ask", "brief", "weekly_plan")

# The metrics whose goals each surface judges against (goal_lines). None:
# every goal. A surface not listed reads none.
SURFACE_METRICS = {
    "ask": None, "dsr_narrative": None, "brief": None, "digest": None, "weekly_plan": None,
    "labor_read": ("labor_pct", "overtime_hours", "weekday_sales"),
    "schedule": ("labor_pct", "overtime_hours", "weekday_sales", "sales"),
    "food_read": ("food_cost_pct", "weekly_waste"),
    "marketing": ("sales", "weekday_sales", "avg_rating"),
}
# The module whose view permission a goal's metric needs (memory_context
# viewer scoping reads it off the line).
_METRIC_MODULE = {"labor_pct": "labor", "overtime_hours": "labor", "sales": "labor", "weekday_sales": "labor",
                  "food_cost_pct": "food", "weekly_waste": "food", "avg_rating": "reviews",
                  "complaints": "reviews", "response_hours": "reviews", "comp_rate": "labor",
                  "void_rate": "labor"}

# Words that make a principal's fact private to the account holders by
# default, whatever audience the model asked for: personnel plans and money
# ("I'm letting Dana go in October" never reaches a manager's prompt).
_PRIVATE_RE = re.compile(
    r"\b(fir(e|ed|ing)|let(ting)?\s+\w+\s+go|replac(e|ing)\s+(the\s+)?(gm|manager|chef|\w+)|terminat\w*|"
    r"lay(ing)?\s*off|laid\s+off|salar(y|ies)|raise\s+for|pay\s+cut|payroll\s+for|sell(ing)?\s+the\s+"
    r"(restaurant|business|place)|lawsuit|lawyer|attorney|divorce|loan|debt|investor|partner(ship)?\s+"
    r"(split|buyout)|buy\s*out)\b", re.I)

# A measurable target said as a sentence ("labor under 26% by December"):
# the remember tool refuses it as a goal and points at set_goal, where it is
# measured and becomes the target the modules judge against.
_TARGET_RE = re.compile(r"\d+(\.\d+)?\s*(%|percent|points?\b)|\$\s?\d", re.I)
_METRIC_WORD_RE = re.compile(r"\b(labor|labour|food\s*cost|overtime|waste|rating|stars?|sales|revenue|"
                             r"prime\s*cost|reply\s*time|response\s*time)\b", re.I)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if db_path is None or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def _local_today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _parse_day(value):
    """YYYY-MM-DD, M/D/YY or M/D/YYYY -> date, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", s)
    if m:
        y = int(m.group(3))
        y = 2000 + y if y < 100 else y
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _mdy(value):
    from time_utils import mdy
    return mdy(value) if value else ""


# ── who said it ─────────────────────────────────────────────────────────────

_ROLE_WORDS = {"owner": "owner", "client": "owner", "manager": "manager", "member": "teammate",
               "employee": "staff", "support": "Cavnar AI support"}


def author_of(user) -> dict:
    """{"user_id", "label", "authority"} for the login writing a fact —
    "Erik, owner"; an admin (or anyone through view-as) is "Cavnar AI
    support", whose words never read as the owner's."""
    if not user:
        return {"user_id": None, "label": None, "authority": None}
    try:
        from permissions import answer_authority, normalize_role
        authority = answer_authority(user)
        role = normalize_role(user.get("role"))
    except Exception:
        authority, role = "delegate", str(user.get("role") or "")
    if authority == "admin":
        return {"user_id": user.get("id"), "label": "Cavnar AI support", "authority": "admin"}
    name = str(user.get("name") or user.get("username") or "").strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name[:1].upper() + name[1:] if name else ""
    word = _ROLE_WORDS.get(role, "owner" if authority == "principal" else "teammate")
    return {"user_id": user.get("id"), "label": f"{name}, {word}" if name else word.capitalize(),
            "authority": authority}


def _default_audience(kind, authority, text):
    if kind == "followup":
        return "author"
    if authority == "principal" and _PRIVATE_RE.search(text or ""):
        return "principals"
    return "team"


def invalidate(restaurant_id):
    """A memory write reaches Ask's next answer, not one a minute later
    (ask_cavnar caches its snapshot for 60 s)."""
    try:
        import ask_cavnar
        ask_cavnar.invalidate_context(restaurant_id)
    except Exception as e:
        log.debug("owner_memory: ask context not invalidated for %s: %s", restaurant_id, e)


# ── the write path ──────────────────────────────────────────────────────────

class MemoryRefused(ValueError):
    """A fact the store will not keep as asked; the message says what to do
    instead (set a goal, give a future date)."""


def looks_like_target(text) -> bool:
    """"Labor under 26% by December" — a figure and a metric word: a goal
    set_goal can measure, not a sentence to keep."""
    return bool(_TARGET_RE.search(text or "") and _METRIC_WORD_RE.search(text or ""))


def remember(restaurant_id, fact, kind="context", modules=None, subject=None, valid_until=None, due_on=None,
             audience=None, user=None, source=None, origin="ask", author_label=None, db_path=None,
             today=None) -> dict:
    """Keep one fact. Returns {"fact", "kind", "evicted", "audience",
    "valid_until", "due_on"}; raises MemoryRefused (a ValueError) for an
    empty fact, a past date, or a measurable target filed as a goal.

    `user` is the login saying it — its id, label and authority are stored
    on the fact, so a manager's remark is never shown as the owner's; None
    is a seeded fact, labelled by `author_label` ("Sales audit 9/8/26")."""
    import models
    text = " ".join(str(fact or "").split())[:models.ASK_MEMORY_MAX_LENGTH]
    if not text:
        raise MemoryRefused("a fact needs some text")
    kind = kind if kind in KINDS else "context"
    if kind == "goal" and looks_like_target(text):
        raise MemoryRefused("That's a measurable target — set it as a goal (set_goal, or Goals) so it is "
                            "measured and every module judges against it.")
    today = today or _local_today(restaurant_id)
    until = _parse_day(valid_until)
    due = _parse_day(due_on)
    if valid_until and until is None:
        raise MemoryRefused("valid_until must be a date (YYYY-MM-DD)")
    if due_on and due is None:
        raise MemoryRefused("due_on must be a date (YYYY-MM-DD)")
    if until is not None and until < today:
        raise MemoryRefused("that date has already passed")
    if kind == "followup" and due is None:
        due = today + timedelta(days=7)
    who = author_of(user)
    if audience not in ("team", "principals", "author"):
        audience = _default_audience(kind, who["authority"], text)
    elif who["authority"] == "principal" and audience == "team" and _PRIVATE_RE.search(text):
        # The backstop: personnel and money said by an account holder stay
        # theirs, whatever the model asked for.
        audience = "principals"
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.split(",")]
    saved = models.remember_ask_fact(
        restaurant_id, text, kind=kind, source=source, user_id=who["user_id"], db_path=db_path or models.DB_PATH,
        modules=list(modules or ()), subject=subject, audience=audience,
        author_label=author_label or who["label"], authority=who["authority"] or ("system" if user is None else None),
        valid_until=until.isoformat() if until else None, due_on=due.isoformat() if due else None, origin=origin)
    expire(restaurant_id, today=today, db_path=db_path)
    invalidate(restaurant_id)
    return {"fact": saved["fact"], "kind": saved["kind"], "evicted": saved.get("evicted", 0), "audience": audience,
            "valid_until": until.isoformat() if until else None, "due_on": due.isoformat() if due else None}


def _may_edit(fact_row, user) -> bool:
    """Who may forget a fact: an account holder (or an internal caller) any
    fact; anyone else only what they wrote themselves."""
    if user is None:
        return True
    try:
        from permissions import answer_authority
        if answer_authority(user) in ("principal", "admin"):
            return True
    except Exception:
        pass
    return fact_row.get("user_id") is not None and fact_row.get("user_id") == user.get("id")


def forget(restaurant_id, fact, user=None, db_path=None) -> dict:
    """Drop one fact. Exact text first; otherwise a loose match, but only
    when exactly ONE readable fact matches (it used to drop the first of
    several). {"forgotten": text} | {"error", "candidates"?, "remembered"?}."""
    import models
    text = " ".join(str(fact or "").split())
    if not text:
        return {"error": "name the note to drop"}
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path, include_expired=True)
    exact = [r for r in rows if r["fact"] == text]
    if not exact:
        low = text.lower()
        exact = [r for r in rows if low in r["fact"].lower() or r["fact"].lower() in low]
        if len(exact) > 1:
            return {"error": "more than one note matches — say which", "candidates": [r["fact"] for r in exact][:6]}
    if not exact:
        return {"error": "no note like that", "remembered": [r["fact"] for r in rows][:30]}
    row = exact[0]
    if not _may_edit(row, user):
        return {"error": "that note was added by someone else; only its author or the owner can drop it"}
    models.forget_ask_fact(restaurant_id, row["fact"], db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return {"forgotten": row["fact"]}


def retract_for_keys(restaurant_id, keys, titles=(), archived_by=None, db_path=None) -> int:
    """"Use again" on a card (home_brief.undismiss) or a restored kind
    (decisions.restore_kind): the owner reversed an answer, so a remembered
    "Not doing X" about it must stop steering answers away from X. Archives
    (reason 'retracted') every fact whose subject is one of `keys`, and the
    legacy Home facts ("Not doing “<title or key>”: …") for them. Returns how
    many facts moved."""
    import models
    keys = [str(k).strip() for k in keys or () if str(k or "").strip()]
    if not keys:
        return 0
    names = set(keys) | {str(t).strip() for t in titles or () if str(t or "").strip()}
    try:
        conn = get_conn(db_path)
        try:
            for k in keys:
                for r in conn.execute("SELECT title FROM rec_instances WHERE restaurant_id=? AND key=? "
                                      "AND title IS NOT NULL", (restaurant_id, k)).fetchall():
                    names.add(str(r["title"]).strip())
        finally:
            conn.close()
    except Exception:
        pass
    ids = []
    for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH):
        subj = str(f.get("subject") or "").strip()
        text = f.get("fact") or ""
        if subj and subj in keys:
            ids.append(f["id"])
            continue
        if text.startswith("Not doing “") and "”: " in text:
            title = text[len("Not doing “"):].split("”: ", 1)[0]
            if any(title == n[:80] for n in names):
                ids.append(f["id"])
    if not ids:
        return 0
    n = models.archive_ask_facts(restaurant_id, ids, "retracted", archived_by=archived_by,
                                 db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return n


def expire(restaurant_id, today=None, db_path=None) -> int:
    """Archive what has run out: a time-bound fact past its valid_until, a
    follow-up FOLLOWUP_GRACE_DAYS past its due date. Returns how many."""
    import models
    today = today or _local_today(restaurant_id)
    ids = []
    for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH):
        until = _parse_day(f.get("valid_until"))
        due = _parse_day(f.get("due_on"))
        if until is not None and until < today:
            ids.append(f["id"])
        elif (f.get("kind") == "followup" and due is not None
              and due + timedelta(days=FOLLOWUP_GRACE_DAYS) < today):
            ids.append(f["id"])
    if not ids:
        return 0
    n = models.archive_ask_facts(restaurant_id, ids, "expired", db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return n


# ── the read path ───────────────────────────────────────────────────────────

def _modules_of(row):
    return {m for m in str(row.get("modules") or "").split(",") if m}


def facts_for(restaurant_id, viewer=None, surface=None, kinds=None, today=None, db_path=None,
              include_expired=False) -> list:
    """The live facts `viewer` may read (memory_context's own rule, applied
    here too so Account and the tools agree with the prompt), for `surface`
    (SURFACE_MODULES) and `kinds`. None viewer: every fact (internal)."""
    import models
    import memory_context
    today = today or _local_today(restaurant_id)
    if not include_expired:
        try:
            expire(restaurant_id, today=today, db_path=db_path)
        except Exception as e:
            log.debug("owner_memory: expiry skipped for %s: %s", restaurant_id, e)
    rows = models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH, kinds=kinds)
    user = memory_context.viewer_user(viewer)
    authority = memory_context._authority(user)
    want = SURFACE_MODULES.get(surface, None) if surface else None
    out = []
    for r in rows:
        line = {"audience": r.get("audience") or "team", "author_id": r.get("user_id")}
        if not memory_context.visible(line, user, authority):
            continue
        mods = _modules_of(r)
        if want is not None and mods and not (mods & want):
            continue
        out.append(r)
    return out


def _who(row):
    label = str(row.get("author_label") or "").strip()
    if label:
        return label
    src = str(row.get("source") or "").strip()
    return src or None


_KIND_WEIGHT = {"constraint": 3.0, "context": 2.0, "preference": 1.5, "followup": 2.5, "goal": 1.0}


def _line(row, weight_bonus=0.0):
    kind = row.get("kind") or "context"
    mods = sorted(_modules_of(row))
    text = row["fact"]
    if kind == "followup" and row.get("due_on"):
        text = f"Follow up by {_mdy(row['due_on'])}: {text}"
    elif kind == "constraint":
        text = f"Constraint: {text}"
    elif kind == "preference":
        text = f"Preference: {text}"
    return {"text": text, "date": str(row.get("created_at") or "")[:10] or None, "source": "owner",
            "subject": row.get("subject") or (mods[0] if len(mods) == 1 else None),
            "weight": _KIND_WEIGHT.get(kind, 1.0) + weight_bonus, "trusted": False,
            "who": _who(row), "until": row.get("valid_until"), "audience": row.get("audience") or "team",
            "author_id": row.get("user_id"), "module": mods[0] if len(mods) == 1 else None,
            "kind": kind, "fact_id": row.get("id")}


def constraint_lines(req):
    """memory_context provider: the owner's and the team's facts for
    req.surface — constraints, context, preferences, and on the owner's
    own reading surfaces the follow-ups coming due — fenced, dated, with
    who said each. A fact about this surface's modules outranks a general
    one. Goal-kind facts (aims no metric reads) go with the goals."""
    surface = getattr(req, "surface", None)
    kinds = ["constraint", "context", "preference"]
    if surface in FOLLOWUP_SURFACES or surface is None:
        kinds.append("followup")
    rows = facts_for(req.restaurant_id, viewer=None, surface=surface, kinds=kinds, db_path=req.db_path)
    want = SURFACE_MODULES.get(surface) if surface else None
    today = _local_today(req.restaurant_id)
    out = []
    for r in rows:
        if r.get("kind") == "followup":
            due = _parse_day(r.get("due_on"))
            # A follow-up is said from a week before it is due.
            if due is not None and due - timedelta(days=7) > today:
                continue
        bonus = 0.5 if (want and (_modules_of(r) & want)) else 0.0
        out.append(_line(r, bonus))
    return out


def goal_lines(req):
    """memory_context provider: the owner's active goals for the metrics in
    play on req.surface, each with where it stands (goals.progress — the
    measured reading, trusted) and who set it; a goal a teammate proposed
    that is waiting for the owner; and the owner's goal-kind facts (aims no
    metric reads), fenced."""
    surface = getattr(req, "surface", None)
    if surface not in SURFACE_METRICS and surface is not None:
        want_metrics = ()
    else:
        want_metrics = SURFACE_METRICS.get(surface) if surface else None
    out = []
    try:
        import goals
        rows = goals.progress(req.restaurant_id, db_path=req.db_path or _default_db())
        proposed = goals.proposed(req.restaurant_id, db_path=req.db_path or _default_db())
    except Exception as e:
        log.debug("owner_memory: goals unreadable for %s: %s", req.restaurant_id, e)
        rows, proposed = [], []
    labels = _user_labels([g.get("created_by") for g in rows + proposed], db_path=req.db_path)

    def _wanted(metric):
        base = str(metric or "").split(":", 1)[0]
        return want_metrics is None or base in (want_metrics or ())
    for g in rows:
        if not _wanted(g.get("metric")):
            continue
        base = str(g.get("metric") or "").split(":", 1)[0]
        state = g.get("state")
        who = labels.get(g.get("created_by"))
        out.append({"text": "Goal — " + goals.summarise(g), "date": str(g.get("created_at") or "")[:10] or None,
                    "source": "system", "trusted": True, "subject": base,
                    "weight": 3.0 if state in ("moving_wrong_way", "missed", "flat") else 2.0,
                    "who": f"set by {who}" if who else None, "module": _METRIC_MODULE.get(base)})
    for g in proposed:
        if not _wanted(g.get("metric")):
            continue
        base = str(g.get("metric") or "").split(":", 1)[0]
        who = labels.get(g.get("created_by")) or "a teammate"
        out.append({"text": f"Proposed goal, waiting for the owner to confirm — {goals.describe_target(g)}",
                    "date": str(g.get("created_at") or "")[:10] or None, "source": "system", "trusted": True,
                    "subject": base, "weight": 0.5, "who": f"proposed by {who}", "module": _METRIC_MODULE.get(base),
                    "audience": "principals", "author_id": g.get("created_by")})
    if surface in (None, "ask", "brief", "weekly_plan", "dsr_narrative", "digest"):
        for r in facts_for(req.restaurant_id, viewer=None, surface=surface, kinds=["goal"], db_path=req.db_path):
            line = _line(r)
            line["text"] = f"Aim: {r['fact']}"
            out.append(line)
    return out


def _default_db():
    import models
    return models.DB_PATH


def _user_labels(user_ids, db_path=None) -> dict:
    """{user_id: "Erik, owner"} for the logins behind goals."""
    ids = sorted({int(u) for u in user_ids or () if u is not None})
    if not ids:
        return {}
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(f"SELECT id, username, role, is_admin FROM users WHERE id IN "
                                f"({','.join('?' for _ in ids)})", ids).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {r["id"]: author_of(dict(r))["label"] for r in rows}


def conversation_lines(req):
    """memory_context provider (Ask only): for a "conversation:<id>" in
    req.subjects, the chat's rolling summary and what its last answer read;
    and the questions this login keeps asking. See ask_conversations."""
    try:
        import ask_conversations
        return ask_conversations.memory_lines(req)
    except ImportError:
        return []


# ── the target a module judges against ─────────────────────────────────────

# The spellings callers use for a goals metric (metrics._REGISTRY keys).
_METRIC_ALIASES = {"labor": "labor_pct", "labor_pct": "labor_pct", "food": "food_cost_pct",
                   "food_cost": "food_cost_pct", "food_cost_pct": "food_cost_pct", "nightly_sales": "sales",
                   "daily_sales": "sales", "sales": "sales", "overtime": "overtime_hours",
                   "overtime_hours": "overtime_hours", "waste": "weekly_waste", "weekly_waste": "weekly_waste",
                   "rating": "avg_rating", "avg_rating": "avg_rating", "response_hours": "response_hours",
                   "prime_cost_pct": "prime_cost_pct"}
_TARGET_TTL_SECONDS = 5.0
_TARGET_CACHE = {}
_TARGET_LOCK = threading.Lock()


def invalidate_targets(restaurant_id=None):
    """Called by every goals write, so a goal set is the target at once."""
    with _TARGET_LOCK:
        if restaurant_id is None:
            _TARGET_CACHE.clear()
        else:
            for k in [k for k in _TARGET_CACHE if k[0] == int(restaurant_id)]:
                _TARGET_CACHE.pop(k, None)


def _goal_label(metric, value, deadline):
    try:
        import metrics
        unit = metrics.describe(metric)["unit"]
    except Exception:
        unit = "%"
    shown = f"${value:,.0f}" if unit == "$" else f"{value:g}{unit}"
    by = f" by {_mdy(deadline)}" if deadline else ""
    return f"your goal of {shown}{by}"


def target_for(restaurant_id, metric, db_path=None):
    """The target a module judges `metric` against when the owner set one
    through a goal — ("labor_pct" | "food_cost_pct" | "prime_cost_pct" |
    "nightly_sales" | ...) -> {"value": float, "source": "goal", "goal_id":
    int, "until": date|None, "label": "your goal of 28% by 12/1/26",
    "metric": str, "set_by": int|None} — or None, and the module keeps its
    own setting. Only an ACTIVE goal (a teammate's proposal is not one until
    the owner confirms it) whose date has not passed: a missed goal is shown
    as missed in Goals, and stops overriding the module's own target."""
    if not restaurant_id:
        return None
    key = _METRIC_ALIASES.get(str(metric or "").strip().lower(), str(metric or "").strip())
    if not key:
        return None
    ck = (int(restaurant_id), key, db_path or "")
    now = time.monotonic()
    with _TARGET_LOCK:
        hit = _TARGET_CACHE.get(ck)
        if hit and now - hit[0] < _TARGET_TTL_SECONDS:
            return dict(hit[1]) if hit[1] else None
    out = None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT id, metric, target, deadline, created_by FROM owner_goals "
                               "WHERE restaurant_id=? AND metric=? AND status='active' ORDER BY id DESC LIMIT 1",
                               (int(restaurant_id), key)).fetchone()
        finally:
            conn.close()
        if row is not None and row["target"] is not None:
            until = _parse_day(row["deadline"])
            if until is None or until >= _local_today(restaurant_id):
                value = float(row["target"])
                out = {"value": value, "source": "goal", "goal_id": int(row["id"]), "until": until,
                       "label": _goal_label(key, value, until), "metric": key, "set_by": row["created_by"]}
    except Exception as e:
        log.debug("owner_memory: target_for %s/%s unreadable: %s", restaurant_id, key, e)
        out = None
    with _TARGET_LOCK:
        if len(_TARGET_CACHE) > 4000:
            _TARGET_CACHE.clear()
        _TARGET_CACHE[ck] = (now, dict(out) if out else None)
    return out


# ── Account's view of the memory ────────────────────────────────────────────

def account_view(restaurant_id, user, db_path=None) -> dict:
    """What Account shows (GET /account/memory): the facts this login may
    read, each with who added it, its type, its dates and whether this login
    may forget it; the lanes and how full each is; and what left without
    anyone asking (the archive), so forgetting is visible and reversible."""
    import models
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path)
    facts = []
    for r in rows:
        facts.append({"id": r["id"], "fact": r["fact"], "kind": r.get("kind") or "context",
                      "source": r.get("source"), "created_at": r.get("created_at"),
                      "created_on": _mdy(str(r.get("created_at") or "")[:10]),
                      "author": _who(r), "author_id": r.get("user_id"), "audience": r.get("audience") or "team",
                      "modules": sorted(_modules_of(r)), "subject": r.get("subject"),
                      "valid_until": r.get("valid_until"), "valid_until_label": _mdy(r.get("valid_until")),
                      "due_on": r.get("due_on"), "due_label": _mdy(r.get("due_on")),
                      "origin": r.get("origin"), "can_forget": _may_edit(r, user)})
    counts = {}
    for r in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH):
        k = r.get("kind") or "context"
        counts[k] = counts.get(k, 0) + 1
    lanes = [{"kind": k, "count": counts.get(k, 0), "cap": models.ASK_MEMORY_CAPS[k]} for k in KINDS]
    archived = []
    try:
        from permissions import answer_authority
        principal = user is None or answer_authority(user) in ("principal", "admin")
    except Exception:
        principal = False
    for a in models.get_ask_memory_archive(restaurant_id, db_path=db_path or models.DB_PATH):
        line = {"audience": a.get("audience") or "team", "author_id": a.get("user_id")}
        import memory_context
        if not memory_context.visible(line, memory_context.viewer_user(user)):
            continue
        archived.append({"id": a["id"], "fact": a["fact"], "kind": a.get("kind") or "context",
                         "author": a.get("author_label") or a.get("source"), "reason": a.get("reason"),
                         "reason_label": {"evicted": "its lane was full", "expired": "its date passed",
                                          "retracted": "you used that recommendation again"}.get(a.get("reason"),
                                                                                                a.get("reason")),
                         "archived_on": _mdy(str(a.get("archived_at") or "")[:10]),
                         "can_restore": principal or (a.get("user_id") is not None and user is not None
                                                      and a.get("user_id") == user.get("id"))})
    return {"facts": facts, "lanes": lanes, "archived": archived}
