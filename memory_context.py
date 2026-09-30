"""memory_context — the one reader that assembles a restaurant's memory for a
model call (memory audit 9/29/26, "assembler").

Every prompt builder used to pick its own memory by hand, with fixed caps
and no budget, so only Ask and the schedule prompt saw any of it. Now a
builder asks once:

    block = memory_context.memory_context(rid, "schedule", viewer=user,
                                          subjects=["labor:day:friday"])
    prompt += block.text          # "" when there is nothing to say

and gets fenced, dated (M/D/YY) blocks in priority order, within a budget.

Sections come from PROVIDERS: a name -> "module:function" registry, imported
lazily at call time (the pos.PROVIDERS pattern), so no import order can drop
a section and this module imports nothing above L1 at module scope. A
provider is `fn(req) -> list[dict]`, each dict one memory line:

    {"text": str,                # what the model reads
     "date": date|datetime|str|None,   # rendered M/D/YY, never ISO
     "source": "owner"|"manager"|"model"|"system",
     "subject": str|None,        # an advice signature ("labor:day:friday"), a
                                 # module ("labor"), or None
     "weight": float,            # relevance within its section (higher first)
     "trusted": bool,            # False (the default) fences the text: owner,
                                 # manager and model-written words never reach
                                 # a prompt outside ai_guard.wrap_untrusted
     # optional — read by the assembler, never required:
     "who": str,                 # who said it ("Erik, owner"), shown beside
                                 # the text, outside the fence
     "until": date|str,          # "until M/D/YY" for a time-bound fact
     "audience": "team"|"principals"|"author",   # who may read the line
     "author_id": int,           # the login that wrote it (audience "author")
     "module": str,              # the module whose view permission the line
                                 # needs ("labor", "food", "reviews", ...;
                                 # "loss" for comps and voids)
     "modules": list}            # every module a line is about — each one's
                                 # view is needed (a "food,labor" fact)

What the assembler does, once, for every caller:

  * VIEWER SCOPING. A line a viewer may not read never reaches the prompt:
    `audience` "principals" only for an account holder (or an internal
    caller), "author" only for the login that wrote it, and a line about a
    module the login may not open (`module` / `modules` — every one) not
    at all. `viewer` is the login dict (auth's current_user); None is an
    owner-level output with no login behind it, assembled as PRINCIPALS
    (the account holders' view — never one login's own "author" line).
  * SHARED OUTPUTS READ AS THE TEAM. A surface whose output more than one
    login reads (a stored read, a diagnosis, the schedule draft, a public
    reply or post, the nightly report — SHARED_SURFACES) is always
    assembled for TEAM, whatever viewer the caller passes: owner-only lines
    ("principals" / "author") and comps-and-voids lines never reach it, and
    a module line only when every reader of that output holds the module's
    view (team_viewer).
  * RELEVANCE ORDER. Within a section, a line about a subject in play
    (`subjects`: advice signatures, ledger tags, "conversation:<id>") ranks
    first, then its own weight, then recency — so a relevant old decline
    displaces an irrelevant recent one.
  * PER-SECTION BUDGETS. Each section that has something to say gets its
    share of `budget_chars` (SECTION_SHARES); a section that uses less
    passes the rest down, and whatever is left after every section had its
    share goes back, in priority order, to sections that had to drop lines.
  * SIZES LOGGED. Every call logs each section's size and what it dropped
    (block.sizes / block.dropped, one INFO line, and size_stats() — a
    bounded in-process aggregate an admin read can show), so the growth of
    memory in prompts is measured, not guessed.

A provider that is missing, raises or returns nothing is skipped; the
failure is recorded on the block (block.errors), never raised into the
caller's model call.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
import importlib
import logging
import threading

log = logging.getLogger(__name__)

# name -> ("module:function", priority). Lower priority number = earlier in
# the prompt and first to keep its budget. Workstreams implement the function
# at the path; until it exists the section is skipped.
PROVIDERS = {
    "constraints":  ("owner_memory:constraint_lines", 10),   # hard constraints, time-bound owner facts
    "goals":        ("owner_memory:goal_lines", 20),          # goals for the metrics in play
    "last_claim":   ("ai_reads:claim_lines", 30),             # what this surface said last time, and its verdict
    "decisions":    ("decisions:memory_lines", 40),           # relevant answers and declines
    "what_worked":  ("rec_learning:what_worked_lines", 50),   # measured results for this kind
    "links":        ("link_memory:link_lines", 55),           # cross-module links this surface acts on, kept
    "events":       ("event_memory:memory_lines", 60),        # how events, weather, campaigns moved sales here
    "market":       ("event_memory:market_lines", 65),        # competitors opening/closing/moving, own rating (INVENTORY-6)
    "people":       ("people:memory_lines", 70),              # attendance, standing patterns, notes (staffing)
    "marketing":    ("marketing:memory_lines", 80),           # what worked in marketing, the owner's voice
    "conversation": ("owner_memory:conversation_lines", 90),  # Ask only: the rolling chat summary
}

# Which sections each surface reads. A surface not listed reads every section.
SURFACE_SECTIONS = {
    # Ask's cached snapshot (ask_cavnar.build_context). Not "decisions": the
    # snapshot already carries decisions.context, and a second copy would
    # pay twice for the same memory. "what_worked" is here, and the
    # intelligence section no longer carries its own copy of the record
    # (intelligence.memory.own_record, which the viewer projection did not
    # cover — memory re-audit QUALITY-16 / PEOPLE-11): one record, module-,
    # loss- and owner-only-gated like every other surface's. Not
    # "conversation": that is per chat and per turn, so it rides on
    # "ask_conversation" below, outside the snapshot the viewer's other
    # chats share.
    "ask": ("constraints", "goals", "last_claim", "what_worked", "events", "market", "people", "marketing"),
    # Ask, per turn: the chat's rolling summary, what its last answer read,
    # and the questions this login keeps asking — the first context block.
    "ask_conversation": ("conversation",),
    # "links" (re-audit 9/29/26): what a fill campaign's night measured,
    # once its link ended — the schedule's verdict for that weekday
    # (link_memory, CROSSMODULE-12).
    "schedule": ("constraints", "goals", "last_claim", "decisions", "what_worked", "events", "people", "links"),
    "labor_read": ("constraints", "goals", "last_claim", "decisions", "what_worked", "events", "people"),
    "food_read": ("constraints", "goals", "last_claim", "decisions", "what_worked"),
    "review_read": ("constraints", "goals", "last_claim", "decisions", "what_worked", "market"),
    # "links" (re-audit 9/29/26, CROSSMODULE-9): the links joining these
    # complaints to a lean or no-show weekday, or to the visibility drop.
    "review_diagnosis": ("constraints", "last_claim", "decisions", "what_worked", "people", "links"),
    "food_diagnosis": ("constraints", "last_claim", "decisions", "what_worked", "links"),
    # Not "decisions" on the nightly report or the Monday plan: each prompt
    # carries decisions.context itself, read as the same team viewer — a
    # second copy paid for the same answers twice, under two viewers
    # (memory re-audit PROMPTS-14).
    "dsr_narrative": ("constraints", "goals", "last_claim", "what_worked", "events"),
    "brief": ("constraints", "goals", "decisions", "events"),
    "digest": ("constraints", "goals", "decisions", "what_worked"),
    # Not "marketing" on the marketing generators or the reply drafter: they
    # build richer blocks of their own (marketing_signals.generation_context,
    # guest_marketing.returns_block, marketing_voice.voice_block; the
    # drafter's voice learning), and marketing:memory_lines says nothing
    # there rather than say it twice (INT_NOTES #30).
    "marketing": ("constraints", "goals", "decisions", "what_worked", "events", "market", "links"),
    "reply_drafter": ("constraints", "decisions"),
    # "market" (re-audit 9/29/26, INVENTORY-6): the public history — who
    # opened, closed or moved nearby, and this restaurant's own rating —
    # kept forever and until now read by one screen.
    "competitor_read": ("constraints", "last_claim", "decisions", "market"),
    "weekly_plan": ("constraints", "goals", "last_claim", "what_worked", "events", "market"),
}

DEFAULT_BUDGET_CHARS = 2400

# Each section's share of the budget, relative to the others that have
# something to say on this call. Owner constraints, decisions and the
# conversation carry the most weight: they are the memory the owner would
# otherwise have to repeat.
SECTION_SHARES = {"constraints": 3, "goals": 2, "last_claim": 2, "decisions": 3, "what_worked": 2,
                  "events": 2, "market": 2, "people": 2, "marketing": 2, "conversation": 3, "links": 2}
DEFAULT_SHARE = 2

# How each section is headed in the prompt. The heading says what the lines
# are and how to treat them; a section with no entry is its name in capitals.
SECTION_TITLES = {
    "constraints": ("WHAT THE OWNER AND THE TEAM HAVE TOLD CAVNAR AI (their words, not measured data — respect "
                    "them, and never quote a figure from them as data)"),
    "goals": "THE OWNER'S GOALS (measured against their own target and date)",
    "last_claim": "WHAT CAVNAR AI SAID LAST TIME ON THIS, AND HOW IT TURNED OUT",
    "decisions": "WHAT THE OWNER DECIDED",
    "what_worked": "WHAT HAS WORKED HERE, AND WHAT THE OWNER KEEPS PASSING ON",
    "events": "WHAT EVENTS, WEATHER AND CAMPAIGNS HAVE DONE HERE",
    "market": ("WHAT THE LOCAL MARKET AND THIS RESTAURANT'S PUBLIC RATING HAVE DONE (Google's public listings as "
               "Cavnar AI's competitor check saw them — what happened, never why)"),
    "people": "THE PEOPLE",
    "marketing": "MARKETING MEMORY",
    "links": ("WHAT TWO MODULES KEEP POINTING AT TOGETHER (found by Cavnar AI's cross-module read, with how long "
              "each has stood — a co-occurrence, never a proven cause)"),
    "conversation": ("EARLIER IN THIS CONVERSATION, AND WHAT THIS PERSON OFTEN ASKS (notes on turns no longer shown, "
                     "what your last answer read — use them to resolve what they refer back to)"),
}

# Relevance: a line about a subject in play outranks any weight a provider
# can give; recency breaks ties (a year-old line loses RECENCY_POINTS).
SUBJECT_BOOST = 100.0
RECENCY_POINTS = 1.0
RECENCY_DAYS = 365.0

# The module a line may carry, as the permission module that gates it
# (permissions.MODULE_VIEW_PERMISSIONS keys).
# A line about comps and voids carries module "loss" (LOSS_VIEW, not a module
# view — _may_read_module).
LOSS_MODULE = "loss"
_MODULE_PERMISSION = {"food": "inventory", "inventory": "inventory", "food_cost": "inventory",
                      "labor": "labor", "schedule": "labor", "reviews": "reviews",
                      "marketing": "marketing", "guests": "marketing", "intel": "intel"}


# ── the team viewer (shared outputs) ────────────────────────────────────────
#
# One stored labor read, one schedule draft, one review diagnosis serve every
# login that can open them — and a reply or a post goes out in public. Their
# memory is assembled as the least-privileged console login that can open the
# output would read it: a manager (permissions.ROLE_MANAGER — every module but
# food cost, no comps and voids, a delegate's authority), plus the output's
# own module when a manager lacks it (a food read's readers all hold food-cost
# view). Owner-only lines — personnel plans and money (owner_memory's
# "principals" audience), a teammate's own ("author") — never reach it.
# Memory audit 9/29/26 (INT #41): M3 and M5 each built this by hand as a
# synthetic {"id": None, "role": "manager"}; it is one viewer here.

class TeamViewer(dict):
    """The viewer a shared output is assembled for. A login-shaped dict (so
    permissions.has_permission and every provider's viewer check read it as
    a manager with no id), recognised by memory_context.is_team."""
    team = True


def _team(grants=()):
    return TeamViewer(id=None, role="manager", is_admin=0, team=True, grants=frozenset(grants))


TEAM = _team()

# Surfaces whose output more than one login reads, and the module view the
# output's own readers all hold beyond a manager's (None: a manager's view).
# Not shared: "ask" / "ask_conversation" and "brief" (built per login, the
# login is the viewer), "digest" (emailed to the owner) — ai_reads.SURFACE_MODULE
# marks those owner-level. "weekly_plan" is the owner's Monday question, but
# its items become issues every console login reads, so it is shared; it keeps
# the owner's food-cost view because the plan's own prompt reads those figures.
SHARED_SURFACES = {
    "schedule": None, "labor_read": None,
    "review_read": None, "review_diagnosis": None, "reply_drafter": None,
    "food_read": "inventory", "food_diagnosis": "inventory",
    "marketing": None, "competitor_read": None,
    "dsr_narrative": None,
    "weekly_plan": "inventory",
}


def is_team(viewer) -> bool:
    # A dict copy of TEAM (viewer_restaurant stamps dict(user)) is still TEAM.
    return (isinstance(viewer, TeamViewer) or viewer == "team"
            or (isinstance(viewer, dict) and viewer.get("team") is True and viewer.get("id") is None))


# ── the principals viewer (owner-level unattended outputs) ──────────────────
#
# The weekly digest is emailed to the account holders; an unattended Ask
# run answers for the owner. Assembled with no login they used to read
# EVERY line, a manager's own ("author") note and rating-derived preference
# included, and present them as the owner's (memory re-audit PEOPLE-8,
# QUALITY-9, PROMPTS-8). PRINCIPALS reads what an account holder reads —
# "team" and "principals" lines, every module and comps and voids — and no
# login's own line: symmetric with TEAM. memory_context() assembles any
# call with no viewer as PRINCIPALS; `visible(line, None)` stays the
# internal "everything" the data paths filter later.

class PrincipalsViewer(dict):
    """The viewer an owner-level output with no login behind it is
    assembled for: login-shaped (an owner role with no id), recognised by
    memory_context.is_principals."""
    principals = True


PRINCIPALS = PrincipalsViewer(id=None, role="client", is_admin=0, principals=True)


def is_principals(viewer) -> bool:
    return (isinstance(viewer, PrincipalsViewer) or viewer == "principals"
            or (isinstance(viewer, dict) and viewer.get("principals") is True and viewer.get("id") is None))


def team_viewer(surface=None) -> TeamViewer:
    """TEAM for `surface`: a manager's view, plus the view of the surface's
    own module when every reader of its output holds it."""
    module = SHARED_SURFACES.get(surface)
    if not module:
        return TEAM
    try:
        from permissions import MODULE_VIEW_PERMISSIONS
        return _team((MODULE_VIEW_PERMISSIONS[module],))
    except Exception:
        return TEAM                              # fail closed: a manager's view


@dataclass
class MemoryRequest:
    restaurant_id: int
    surface: str
    viewer: dict = None
    subjects: tuple = ()
    now: datetime = None
    db_path: str = None


@dataclass
class MemoryBlock:
    text: str = ""
    sections: dict = field(default_factory=dict)   # name -> [line dicts kept]
    sizes: dict = field(default_factory=dict)      # name -> chars rendered
    dropped: dict = field(default_factory=dict)    # name -> lines cut for budget (or for the viewer)
    errors: dict = field(default_factory=dict)     # name -> error text

    @property
    def empty(self):
        return not self.text


def fmt_date(value):
    """M/D/YY for a memory line's date — the model echoes what it reads, so
    it must never read an ISO date (time_utils.mdy)."""
    if value is None or value == "":
        return ""
    import time_utils
    if isinstance(value, (date, datetime)):
        return time_utils.mdy(value)
    return time_utils.mdy(str(value))


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _meta(line):
    """"9/28/26 · Erik, owner · until 10/31/26" — the structure beside a
    line: dates and who said it, never the words."""
    bits = []
    when = fmt_date(line.get("date"))
    if when:
        bits.append(when)
    who = " ".join(str(line.get("who") or "").split())
    if who:
        bits.append(who[:60])
    until = fmt_date(line.get("until"))
    if until:
        bits.append(f"until {until}")
    return " · ".join(bits)


def _render(line):
    """One line as the prompt reads it: "- (meta) text". Untrusted text is
    fenced; `_render_section` groups consecutive fenced lines into one
    fence, which is the same boundary at a fraction of the markers."""
    text = " ".join(str(line.get("text") or "").split())
    if not text:
        return ""
    meta = _meta(line)
    body = f"- ({meta}) {text}" if meta else f"- {text}"
    if not line.get("trusted"):
        import ai_guard
        body = ai_guard.wrap_untrusted(body)
    return body


def _line_text(line):
    text = " ".join(str(line.get("text") or "").split())
    if not text:
        return ""
    meta = _meta(line)
    return f"- ({meta}) {text}" if meta else f"- {text}"


def _fence_overhead():
    import ai_guard
    return len(ai_guard.UNTRUSTED_OPEN) + len(ai_guard.UNTRUSTED_CLOSE) + 2


def _render_section(title, lines):
    """The section's text: its heading, trusted lines as they are, and each
    run of untrusted lines inside ONE fence."""
    import ai_guard
    out, run = [f"{title}:"], []

    def flush():
        if run:
            out.append(ai_guard.wrap_untrusted("\n".join(run)))
            run.clear()
    for line in lines:
        t = _line_text(line)
        if not t:
            continue
        if line.get("trusted"):
            flush()
            out.append(t)
        else:
            run.append(t)
    flush()
    return "\n".join(out)


def _provider(path):
    mod_name, fn_name = path.split(":", 1)
    return getattr(importlib.import_module(mod_name), fn_name)


# ── viewer scoping ──────────────────────────────────────────────────────────

def viewer_user(viewer):
    """The login dict a caller passed: a user dict, a viewer_restaurant (Ask
    stamps the login on it as _ask_dsr_user), or None (internal)."""
    if viewer is None:
        return None
    if isinstance(viewer, dict):
        return viewer
    if viewer == "team":
        return TEAM
    if viewer == "principals":
        return PRINCIPALS
    user = getattr(viewer, "_ask_dsr_user", None)
    return user if isinstance(user, dict) else None


def _authority(user):
    if user is None:
        return "internal"
    if is_principals(user):
        return "principal"
    try:
        from permissions import answer_authority
        return answer_authority(user)
    except Exception:
        return "delegate"


def _may_read_module(user, module, _cache):
    if user is None or not module:
        return True
    key = str(module).strip().lower()
    if key == LOSS_MODULE:
        # Comps and voids (a goal on comp_rate, a loss kind's record): the
        # loss rule, not a module view — they can name the manager who
        # approved the comps (issues.viewer_sees_loss; memory re-audit
        # PEOPLE-2). TEAM holds no LOSS_VIEW, so a shared output never reads
        # one; PRINCIPALS is the owner's view.
        if LOSS_MODULE not in _cache:
            try:
                import issues
                _cache[LOSS_MODULE] = is_principals(user) or bool(issues.viewer_sees_loss(user))
            except Exception:
                _cache[LOSS_MODULE] = False      # fail closed
        return _cache[LOSS_MODULE]
    perm_module = _MODULE_PERMISSION.get(key)
    if perm_module is None:
        return True
    if perm_module not in _cache:
        try:
            from permissions import MODULE_VIEW_PERMISSIONS, has_permission
            _cache[perm_module] = bool(user.get("is_admin")) or has_permission(
                user, MODULE_VIEW_PERMISSIONS[perm_module])
        except Exception:
            _cache[perm_module] = False          # fail closed
    return _cache[perm_module]


def line_modules(line):
    """Every module a line is about: its `module` and each of its `modules`
    (a fact tagged "food,labor" carries both — memory re-audit PROMPTS-7)."""
    out = []
    for m in [line.get("module")] + list(line.get("modules") or ()):
        m = str(m or "").strip().lower()
        if m and m not in out:
            out.append(m)
    return out


def visible(line, user, authority=None, _cache=None) -> bool:
    """Whether the login `user` may read this memory line (None: internal —
    every line, for the data paths that filter later; PRINCIPALS: the
    account holders' view, never one login's own "author" line; TEAM: a
    shared output — "team" lines only, and a module line only where a
    manager, or the output's own module, may). A line about several
    modules needs the view of EVERY one of them (a fact tagged "food,labor"
    lost its food gate — memory re-audit PROMPTS-7 / INVENTORY-4); "loss"
    needs the loss rule. Fails closed on an unknown audience."""
    if user is None:
        return True
    authority = authority or _authority(user)
    cache = _cache if _cache is not None else {}
    for m in line_modules(line):
        if not _may_read_module(user, m, cache):
            return False
    audience = str(line.get("audience") or "team").strip().lower()
    if audience == "team":
        return True
    if is_team(user):
        return False                             # a shared output reads no one's private line
    if is_principals(user):
        # The owner-level unattended outputs (the weekly digest, an
        # unattended Ask): what the account holders share, never one
        # login's own note or rating-derived preference (memory re-audit
        # PEOPLE-8 / QUALITY-9).
        return audience == "principals"
    # Through view-as the reader is the admin behind it, never the owner it
    # views as: support does not read the owner's author-only lines
    # (permissions.acting_login_id — PEOPLE-20).
    try:
        from permissions import acting_login_id
        uid = acting_login_id(user)
    except Exception:
        uid = None
    if audience == "author":
        return line.get("author_id") is not None and uid is not None and int(line["author_id"]) == int(uid)
    if audience == "principals":
        if authority in ("principal", "admin"):
            return True
        return line.get("author_id") is not None and uid is not None and int(line["author_id"]) == int(uid)
    return False


# ── relevance ───────────────────────────────────────────────────────────────

def _subject_match(subject, subjects):
    """A line's subject matches one in play exactly, or one is the other's
    prefix at a ":" boundary ("labor" matches "labor:day:friday")."""
    if not subject or not subjects:
        return False
    s = str(subject).strip().lower()
    for want in subjects:
        w = str(want or "").strip().lower()
        if not w:
            continue
        if s == w or s.startswith(w + ":") or w.startswith(s + ":"):
            return True
    return False


def _relevance(line, subjects, today):
    score = float(line.get("weight") or 0.0)
    if _subject_match(line.get("subject"), subjects):
        score += SUBJECT_BOOST
    d = _as_date(line.get("date"))
    if d is not None and today is not None:
        age = max(0, (today - d).days)
        score += RECENCY_POINTS * max(0.0, 1.0 - age / RECENCY_DAYS)
    return score


# ── the size log ────────────────────────────────────────────────────────────

_STATS = {}
_STATS_LOCK = threading.Lock()


def _note_sizes(surface, sizes, dropped):
    with _STATS_LOCK:
        for name in set(sizes) | set(dropped):
            s = _STATS.setdefault((str(surface)[:40], name), {"calls": 0, "chars": 0, "max": 0, "dropped": 0})
            n = int(sizes.get(name) or 0)
            s["calls"] += 1
            s["chars"] += n
            s["max"] = max(s["max"], n)
            s["dropped"] += int(dropped.get(name) or 0)


def size_stats() -> dict:
    """{surface: {section: {calls, avg, max, dropped}}} since this process
    started — how big each memory section runs on each surface."""
    with _STATS_LOCK:
        out = {}
        for (surface, name), s in _STATS.items():
            out.setdefault(surface, {})[name] = {"calls": s["calls"], "avg": round(s["chars"] / max(1, s["calls"])),
                                                 "max": s["max"], "dropped": s["dropped"]}
        return out


# ── the assembler ───────────────────────────────────────────────────────────

def memory_context(restaurant_id, surface, viewer=None, subjects=(), budget_chars=None, now=None, db_path=None):
    """The memory block for one model call on `surface`. Never raises."""
    try:
        return _assemble(restaurant_id, surface, viewer, subjects, budget_chars, now, db_path)
    except Exception as e:                 # the assembler itself must never break a model call
        log.warning("memory_context: assembly failed for rid=%s on %s: %s", restaurant_id, surface, e)
        block = MemoryBlock()
        block.errors["_assembler"] = str(e)[:200]
        return block


def _assemble(restaurant_id, surface, viewer, subjects, budget_chars, now, db_path):
    # A shared output is read as the team, whoever asked for it (SHARED_SURFACES):
    # the owner's own view never shapes what a manager, or the public, reads.
    user = team_viewer(surface) if (surface in SHARED_SURFACES or is_team(viewer)) else viewer_user(viewer)
    # No login behind an owner-level output (the digest, an unattended Ask):
    # the account holders' view, never every login's own lines (PRINCIPALS).
    if user is None:
        user = PRINCIPALS
    req = MemoryRequest(restaurant_id=restaurant_id, surface=surface, viewer=user,
                        subjects=tuple(subjects or ()), now=now or datetime.now(), db_path=db_path)
    wanted = SURFACE_SECTIONS.get(surface)
    order = sorted(PROVIDERS.items(), key=lambda kv: kv[1][1])
    budget = int(budget_chars or DEFAULT_BUDGET_CHARS)
    today = req.now.date() if isinstance(req.now, datetime) else _as_date(req.now)
    authority = _authority(user)
    perm_cache = {}
    block = MemoryBlock()

    # 1. Every wanted section's lines, scoped to the viewer and ranked.
    gathered = []
    for name, (path, _prio) in order:
        if wanted is not None and name not in wanted:
            continue
        try:
            lines = list(_provider(path)(req) or [])
        except (ImportError, AttributeError):
            continue                       # not built yet: the section is skipped
        except Exception as e:             # a provider's failure never breaks the call
            block.errors[name] = str(e)[:200]
            log.warning("memory_context: section %s failed for rid=%s on %s: %s", name, restaurant_id, surface, e)
            continue
        kept, hidden = [], 0
        for line in lines:
            if not isinstance(line, dict) or not _line_text(line):
                continue
            if not visible(line, user, authority, perm_cache):
                hidden += 1
                continue
            kept.append(line)
        if hidden:
            block.dropped[name] = hidden
        if not kept:
            continue
        ranked = sorted(enumerate(kept), key=lambda il: (-_relevance(il[1], req.subjects, today), il[0]))
        gathered.append((name, [l for _i, l in ranked]))

    # 2. Budgets: each section's share, the unused part carried down, then
    #    what is left handed back in priority order.
    if gathered:
        fence = _fence_overhead()
        total_share = sum(SECTION_SHARES.get(n, DEFAULT_SHARE) for n, _ in gathered)
        taken = {n: [] for n, _ in gathered}
        cost = {n: 0 for n, _ in gathered}
        fenced = {n: False for n, _ in gathered}
        position = {n: 0 for n, _ in gathered}

        def title_cost(n):
            return len(SECTION_TITLES.get(n) or n.replace("_", " ").upper()) + 3

        def try_add(n, lines, allowance):
            """Take lines in rank order while they fit `allowance`; a line
            that does not fit is skipped, not the end (a shorter one after it
            may fit). Returns the chars used."""
            used = 0
            for i in range(position[n], len(lines)):
                line = lines[i]
                if line is None:
                    continue
                c = len(_line_text(line)) + 1
                if not taken[n]:
                    c += title_cost(n)
                if not line.get("trusted") and not fenced[n]:
                    c += fence
                if used + c > allowance:
                    continue
                used += c
                taken[n].append((i, line))
                if not line.get("trusted"):
                    fenced[n] = True
                lines[i] = None
            while position[n] < len(lines) and lines[position[n]] is None:
                position[n] += 1
            cost[n] += used
            return used

        pools = {n: list(lines) for n, lines in gathered}
        carry = 0.0
        spent = 0
        for n, _lines in gathered:
            allowance = budget * SECTION_SHARES.get(n, DEFAULT_SHARE) / total_share + carry
            allowance = min(allowance, budget - spent)
            used = try_add(n, pools[n], int(allowance))
            spent += used
            carry = max(0.0, allowance - used)
        for n, _lines in gathered:
            left = budget - spent
            if left <= 0:
                break
            if any(l is not None for l in pools[n]):
                spent += try_add(n, pools[n], left)

        parts = []
        for n, _lines in gathered:
            left_out = sum(1 for l in pools[n] if l is not None)
            if left_out:
                block.dropped[n] = block.dropped.get(n, 0) + left_out
            if not taken[n]:
                continue
            # In rank order, whichever pass took each line.
            kept = [l for _i, l in sorted(taken[n], key=lambda il: il[0])]
            text = _render_section(SECTION_TITLES.get(n) or n.replace("_", " ").upper(), kept)
            parts.append(text)
            block.sections[n] = kept
            block.sizes[n] = sum(len(_line_text(l)) + 1 for l in kept)
        block.text = "\n\n".join(parts)
        # The owner's facts this prompt carried: lane eviction goes least
        # used first (owner_memory.mark_used; memory re-audit R3).
        used = [l.get("fact_id") for ls in block.sections.values() for l in ls
                if l.get("fact_id") and l.get("source") == "owner"]
        if used:
            try:
                import owner_memory
                owner_memory.mark_used(restaurant_id, used, db_path=db_path)
            except Exception as e:
                log.debug("memory_context: use not stamped for rid=%s: %s", restaurant_id, e)

    _note_sizes(surface, block.sizes, block.dropped)
    if block.sizes or block.dropped or block.errors:
        log.info("memory_context rid=%s surface=%s budget=%d used=%d sizes=%s dropped=%s errors=%s",
                 restaurant_id, surface, budget, len(block.text), block.sizes, block.dropped,
                 sorted(block.errors))
    return block
