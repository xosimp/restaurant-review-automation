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
     "trusted": bool}            # False (the default) fences the text: owner,
                                 # manager and model-written words never reach
                                 # a prompt outside ai_guard.wrap_untrusted

A provider that is missing, raises or returns nothing is skipped; the
failure is recorded on the block (block.errors), never raised into the
caller's model call.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
import importlib
import logging

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
    "events":       ("event_memory:memory_lines", 60),        # how events, weather, campaigns moved sales here
    "people":       ("people:memory_lines", 70),              # attendance, standing patterns, notes (staffing)
    "marketing":    ("marketing:memory_lines", 80),           # what worked in marketing, the owner's voice
    "conversation": ("owner_memory:conversation_lines", 90),  # Ask only: the rolling chat summary
}

# Which sections each surface reads. A surface not listed reads every section.
SURFACE_SECTIONS = {
    "ask": None,
    "schedule": ("constraints", "goals", "last_claim", "decisions", "what_worked", "events", "people"),
    "labor_read": ("constraints", "goals", "last_claim", "decisions", "what_worked", "events", "people"),
    "food_read": ("constraints", "goals", "last_claim", "decisions", "what_worked"),
    "review_diagnosis": ("constraints", "last_claim", "decisions", "what_worked", "people"),
    "food_diagnosis": ("constraints", "last_claim", "decisions", "what_worked"),
    "dsr_narrative": ("constraints", "goals", "last_claim", "decisions", "events"),
    "brief": ("constraints", "goals", "decisions", "events"),
    "digest": ("constraints", "goals", "decisions", "what_worked"),
    "marketing": ("constraints", "goals", "decisions", "what_worked", "events", "marketing"),
    "reply_drafter": ("constraints", "decisions", "marketing"),
    "competitor_read": ("constraints", "last_claim", "decisions"),
    "weekly_plan": ("constraints", "goals", "last_claim", "decisions", "what_worked", "events"),
}

DEFAULT_BUDGET_CHARS = 2400


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
    dropped: dict = field(default_factory=dict)    # name -> lines cut for budget
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


def _render(line):
    text = " ".join(str(line.get("text") or "").split())
    if not text:
        return ""
    if not line.get("trusted"):
        import ai_guard
        text = ai_guard.wrap_untrusted(text)
    when = fmt_date(line.get("date"))
    return f"- ({when}) {text}" if when else f"- {text}"


def _provider(path):
    mod_name, fn_name = path.split(":", 1)
    return getattr(importlib.import_module(mod_name), fn_name)


def memory_context(restaurant_id, surface, viewer=None, subjects=(), budget_chars=None, now=None, db_path=None):
    """The memory block for one model call on `surface`. Never raises."""
    req = MemoryRequest(restaurant_id=restaurant_id, surface=surface, viewer=viewer,
                        subjects=tuple(subjects or ()), now=now or datetime.now(), db_path=db_path)
    wanted = SURFACE_SECTIONS.get(surface)
    order = sorted(PROVIDERS.items(), key=lambda kv: kv[1][1])
    budget = int(budget_chars or DEFAULT_BUDGET_CHARS)
    block = MemoryBlock()
    parts = []
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
        lines.sort(key=lambda l: -(float(l.get("weight") or 0.0)))
        kept, rendered = [], []
        for line in lines:
            r = _render(line)
            if not r:
                continue
            if len(r) + 1 > budget:
                block.dropped[name] = block.dropped.get(name, 0) + 1
                continue
            budget -= len(r) + 1
            kept.append(line)
            rendered.append(r)
        if rendered:
            title = name.replace("_", " ").upper()
            parts.append(f"{title}:\n" + "\n".join(rendered))
            block.sections[name] = kept
            block.sizes[name] = sum(len(r) + 1 for r in rendered)
    block.text = "\n\n".join(parts)
    return block
