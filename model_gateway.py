"""
model_gateway.py — the one door every Anthropic call passes (scheduler audit
10/10/26: SCALE-32, ARCH-4; the AI side of RM-7).

ai_utils.create_with_retry is already the one place a call is sent (budgets,
readiness, the breaker, the ledger). Before this module it knew nothing
about the ORGANISATION's provider rate limits or about who else was calling:
two generation slots per process were the de-facto limiter, and once
generation scales out a Sunday-evening wave of drafts would take every
output token a minute the organisation has, Ask and review replies starving
behind it until the provider answered 429 to everyone. Here, between the
gates and the send:

  CLASSES. Every call has a class — the queue job's (job_queue: interactive,
  scheduled, bulk), else read from who asked (an owner or an admin waiting:
  interactive; the scheduler or the system: bulk). call_class().

  THE ORGANISATION'S TOKENS A MINUTE (admit / settle). With GATEWAY_ORG_OTPM
  (output tokens a minute) and/or GATEWAY_ORG_ITPM (input) set, each class
  is guaranteed its share (GATEWAY_SHARE_<CLASS>; 0.5 / 0.3 / 0.2) and may
  borrow what the others leave — except that the scheduled and bulk classes
  never borrow into the interactive class's share. A call reserves its
  estimate in this minute's row (`model_token_windows`, one row per minute
  and class, in the database so every process shares it) and settles to its
  real usage when it returns. Over its allowance a call waits for the next
  minute: an interactive one at most GATEWAY_INTERACTIVE_WAIT_SECONDS and
  then goes anyway (an owner is never refused on a soft limit — the
  provider's own 429 and the retry are the hard one), a scheduled one at
  most GATEWAY_SCHEDULED_WAIT_SECONDS then goes (a generation's clock is
  running), a bulk one at most GATEWAY_BULK_WAIT_SECONDS then raises
  GatewayBusy — its queue row is tried again later (job_queue.fail).
  Unset (the default) the allocator is off: nothing waits.

  PER MODEL FAMILY (scale audit 10/19/26 A4-02). The provider meters its
  limits per model (Sonnet 5.5, Opus 5.5, Haiku 5.5 each have their own
  bucket; the Opus 4.x line shares one), so the minute is kept per (family,
  class): a burst of Opus schedule streams never eats the Sonnet minute Ask
  runs on. Each family gets GATEWAY_ORG_OTPM / GATEWAY_ORG_ITPM unless
  GATEWAY_FAMILY_LIMITS (JSON: {"claude-sonnet-5-5": {"itpm": .., "otpm":
  ..}}) names its own. The lane is stored in the `klass` column as
  "<family>|<class>" (model_family, _lane) — rows live two hours, so no
  migration. The input reserved is the prompt AFTER its last cache
  breakpoint, tools included (estimate): a cache read does not count toward
  the provider's input limit; a cache write does and settles in with the
  real usage.

  STREAMS AT ONCE (scale audit 10/19/26 A5-F8). The generation group's cap
  from the output budget (max_concurrent_generations) holds the rows that
  are STREAMING a model answer — a generation call waits for one of those
  places in admit (_stream_slot_wait) — never the CPU slots a code-only
  generation needs (it used to be folded into job_queue.group_concurrency,
  throttling solver weeks that stream nothing).

  A TENANT'S TOKENS A MINUTE. GATEWAY_TENANT_OTPM caps one restaurant's
  output tokens a minute (`model_tenant_windows`), so one restaurant's loop
  cannot take the organisation's minute. Off by default.

  HOW MANY GENERATIONS AT ONCE (max_concurrent_generations): the generation
  queue's global concurrency is held to what the organisation's output
  budget for interactive + scheduled calls can stream at once
  (GATEWAY_GENERATION_STREAM_OTPM per generation, ~120 tokens/s measured),
  so scaling workers out never just converts into provider 429s.

  BATCH ROUTING (should_batch): a workflow's unattended calls (the scheduled
  and bulk classes) go through Message Batches at half price when its policy
  allows (ai_batches.enabled) — the auto-draft's path (schedule_batch).

  PER-TENANT DOLLAR BUDGETS. The tiers' daily and monthly ceilings
  (ai_utils) can be raised or lowered for one restaurant
  (`ai_tenant_budgets`, set_tenant_budget) — a 250-person restaurant
  generating several weeks a day outgrows the paid tier's $10 a day (H1's
  SCALE-11 note) without raising every restaurant's ceiling.

  SHARED BREAKERS. ai_utils' provider breakers open and close in every
  process: an opening, a closing and the console's reset are written to
  `ai_breakers` and read back by every process within BREAKER_SYNC_SECONDS
  (ai_utils._breaker_sync), so one worker's outage verdict stops the others
  and "Reset breaker" reaches them all.

Not here (later stages, docs/plans/POSTGRES_AND_WORKERS_PLAN.md): streaming
the cost ledger (`ai_usage`) to a warehouse — Stage 4, change data capture.
"""
import contextlib
import contextvars
import json
import os
import re
import threading
import time

CLASSES = ("interactive", "scheduled", "bulk")

_CLASS = contextvars.ContextVar("model_gateway_class", default=None)


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _f(name, default):
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return float(default)


def model_family(model) -> str:
    """The provider's rate-limit bucket a model draws on (A4-02): the model
    id without a date suffix; every Opus 4.x shares one."""
    m = re.sub(r"-\d{8}$", "", str(model or "").strip().lower())
    if m.startswith("claude-opus-4"):
        return "claude-opus-4"
    return m or "unknown"


def _family_limits() -> dict:
    raw = os.getenv("GATEWAY_FAMILY_LIMITS", "") or ""
    if not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(val, dict):
        return {}
    return {model_family(k): v for k, v in val.items() if isinstance(v, dict)}


def org_limits(family=None) -> dict:
    """{"otpm", "itpm"}: the organisation's output / input tokens a minute
    the gateway allocates for one model family (0: not allocated) —
    GATEWAY_FAMILY_LIMITS' entry for it, else GATEWAY_ORG_OTPM / ITPM."""
    out = {"otpm": _f("GATEWAY_ORG_OTPM", 0), "itpm": _f("GATEWAY_ORG_ITPM", 0)}
    if family is None:
        return out
    own = _family_limits().get(model_family(family)) or {}
    for k in ("otpm", "itpm"):
        try:
            if own.get(k) is not None:
                out[k] = max(0.0, float(own[k]))
        except (TypeError, ValueError):
            pass
    return out


def _lane(family, klass) -> str:
    """The `klass` column's value for one (family, class) window."""
    return f"{model_family(family)}|{klass}"


def _split_lane(value):
    """(family, class) of a stored lane; a row written before families
    (a bare class) is (None, class)."""
    v = str(value or "")
    if "|" in v:
        fam, _sep, k = v.rpartition("|")
        return fam, k
    return None, v


def shares() -> dict:
    raw = {"interactive": _f("GATEWAY_SHARE_INTERACTIVE", 0.5), "scheduled": _f("GATEWAY_SHARE_SCHEDULED", 0.3),
           "bulk": _f("GATEWAY_SHARE_BULK", 0.2)}
    total = sum(max(0.0, v) for v in raw.values()) or 1.0
    return {k: max(0.0, v) / total for k, v in raw.items()}


WAIT_SECONDS = {"interactive": _f("GATEWAY_INTERACTIVE_WAIT_SECONDS", 10),
                "scheduled": _f("GATEWAY_SCHEDULED_WAIT_SECONDS", 120),
                "bulk": _f("GATEWAY_BULK_WAIT_SECONDS", 60)}
# The output a call is assumed to spend before it says (its max_tokens,
# capped here: a schedule call's 64k ceiling would reserve a whole minute).
OUTPUT_RESERVE_CAP = int(_f("GATEWAY_OUTPUT_RESERVE_CAP", 16000))
# Output tokens a minute one streaming generation spends (~120 tokens/s,
# schedule_engine.ASSUMED_OUTPUT_TOKENS_PER_SECOND).
GENERATION_STREAM_OTPM = _f("GATEWAY_GENERATION_STREAM_OTPM", 7200)


class GatewayBusy(RuntimeError):
    """A bulk call found the organisation's minute spent and waited out its
    allowance: its work is tried again later (a queue row's retry)."""

    def __init__(self, klass, retry_after):
        super().__init__(f"the {klass} class's token allowance is spent for this minute")
        self.klass, self.retry_after = klass, retry_after


# ── schema (boot) ───────────────────────────────────────────────────────────

_DDL = (
    """CREATE TABLE IF NOT EXISTS model_token_windows (
        minute         INTEGER NOT NULL,
        klass          TEXT NOT NULL,
        input_tokens   INTEGER NOT NULL DEFAULT 0,
        output_tokens  INTEGER NOT NULL DEFAULT 0,
        calls          INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (minute, klass)
    )""",
    """CREATE TABLE IF NOT EXISTS model_tenant_windows (
        minute         INTEGER NOT NULL,
        restaurant_id  INTEGER NOT NULL,
        output_tokens  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (minute, restaurant_id)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_tenant_budgets (
        restaurant_id  INTEGER PRIMARY KEY,
        daily_usd      REAL,
        monthly_usd    REAL,
        reason         TEXT,
        set_by         TEXT,
        updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS ai_breakers (
        provider       TEXT PRIMARY KEY,
        open_until     REAL NOT NULL DEFAULT 0,
        opened_at      REAL,
        reason         TEXT,
        failures       INTEGER NOT NULL DEFAULT 0,
        updated_at     REAL NOT NULL
    )""",
)


def init_model_gateway(db_path=None):
    """The gateway's tables — at boot (models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── the class of a call ─────────────────────────────────────────────────────

@contextlib.contextmanager
def call_class_scope(klass):
    """`with call_class_scope("bulk"):` — every call inside is that class."""
    token = _CLASS.set(klass if klass in CLASSES else None)
    try:
        yield
    finally:
        _CLASS.reset(token)


def call_class() -> str:
    """The class of the call about to be sent: an explicit scope, else the
    queue job's, else who asked (ai_utils attribution)."""
    k = _CLASS.get()
    if k:
        return k
    try:
        import job_queue
        job = job_queue.current()
        if job is not None and job.get("priority_class") in CLASSES:
            return job["priority_class"]
    except Exception:
        pass
    try:
        import ai_utils
        trigger = (ai_utils.current_ai_context() or {}).get("trigger")
        if trigger is None and ai_utils.on_request_thread():
            return "interactive"
    except Exception:
        trigger = None
    return "interactive" if trigger in ("owner", "admin") else "bulk"


# ── the allocator ───────────────────────────────────────────────────────────

class Ticket:
    __slots__ = ("klass", "minute", "est_in", "est_out", "restaurant_id", "settled", "waited", "family")

    def __init__(self, klass, minute, est_in, est_out, restaurant_id, waited=0.0, family="unknown"):
        self.klass, self.minute, self.est_in, self.est_out = klass, minute, est_in, est_out
        self.restaurant_id, self.settled, self.waited = restaurant_id, False, waited
        self.family = family


# An image or a PDF page in a prompt, in characters (~1,600 tokens).
_MEDIA_BLOCK_CHARS = 6400


def _block_chars(b) -> int:
    if isinstance(b, str):
        return len(b)
    if not isinstance(b, dict):
        return len(str(b or ""))
    kind = b.get("type")
    if kind in ("image", "document"):
        return _MEDIA_BLOCK_CHARS
    if kind == "tool_use":
        return len(json.dumps(b.get("input") or {}, default=str)) + len(str(b.get("name") or ""))
    if kind == "tool_result":
        c = b.get("content")
        if isinstance(c, list):
            return sum(_block_chars(x) for x in c)
        return len(str(c or ""))
    return len(str(b.get("text") or ""))


def _marked(b) -> bool:
    return isinstance(b, dict) and bool(b.get("cache_control"))


def estimate(kwargs) -> tuple:
    """(input, output) tokens a call is reserved before it says: its
    prompt's characters / 4 — tools, system and messages in the order the
    provider reads them, counted from after the LAST cache breakpoint (a
    cache read does not count toward the provider's input limit; a write
    settles in with the real usage — scale audit 10/19/26 A4-02; the tools
    were not counted at all before) — and its max_tokens capped at
    OUTPUT_RESERVE_CAP."""
    kw = kwargs or {}
    seq = []
    for t in kw.get("tools") or []:
        seq.append((len(json.dumps(t, default=str)) if isinstance(t, dict) else len(str(t)), _marked(t)))
    sys_ = kw.get("system")
    if isinstance(sys_, str):
        seq.append((len(sys_), False))
    else:
        for b in sys_ or []:
            seq.append((_block_chars(b), _marked(b)))
    for m in kw.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            seq.append((len(c), False))
        elif isinstance(c, list):
            for b in c:
                seq.append((_block_chars(b), _marked(b)))
    last = max((i for i, (_n, mk) in enumerate(seq) if mk), default=-1)
    chars = sum(n for i, (n, _mk) in enumerate(seq) if i > last)
    out = min(int(kw.get("max_tokens") or 1024), OUTPUT_RESERVE_CAP)
    return int(chars / 4) + 1, out


def _minute(now=None) -> int:
    return int((now or time.time()) // 60)


def _allowance(klass, usage, limit, share):
    """Tokens `klass` may still take this minute given every class's use
    (`usage` {class: tokens}): its own guaranteed share left, plus what the
    others leave unused — never the interactive reserve for the others."""
    if limit <= 0:
        return float("inf")
    used_total = sum(usage.values())
    free_total = max(0.0, limit - used_total)
    own_left = max(0.0, limit * share[klass] - usage.get(klass, 0))
    if klass == "interactive":
        return max(own_left, free_total)
    reserve_left = max(0.0, limit * share["interactive"] - usage.get("interactive", 0))
    return max(own_left, min(free_total, max(0.0, free_total - reserve_left)))


def _try_reserve(klass, est_in, est_out, restaurant_id, now=None, db_path=None, force=False, family="unknown"):
    """Reserve in this minute's windows when the class (and the tenant) has
    room in the model family's minute; returns the minute reserved in, or
    None. `force` reserves whatever the room: a call that waited out its
    allowance and goes anyway still counts its estimate, so the calls after
    it see the minute as it is (re-audit 10/11/26 SCALE-25)."""
    fam = model_family(family)
    lim = org_limits(fam)
    tenant_cap = _f("GATEWAY_TENANT_OTPM", 0)
    minute = _minute(now)
    conn = get_conn(db_path)
    try:
        # One minute's windows are read and reserved together; other
        # minutes and every other writer are not held (SCALE-12).
        import db_dialect
        db_dialect.begin_write(conn, "model_gateway", minute)
        rows = conn.execute("SELECT klass, input_tokens, output_tokens FROM model_token_windows WHERE minute=?",
                            (minute,)).fetchall()
        # This family's minute only: the provider meters each model apart.
        out_use, in_use = {}, {}
        for r in rows:
            r_fam, r_k = _split_lane(r["klass"])
            if r_fam != fam:
                continue
            out_use[r_k] = out_use.get(r_k, 0) + int(r["output_tokens"])
            in_use[r_k] = in_use.get(r_k, 0) + int(r["input_tokens"])
        sh = shares()
        if not force and (est_out > _allowance(klass, out_use, lim["otpm"], sh) or
                          est_in > _allowance(klass, in_use, lim["itpm"], sh)):
            conn.rollback()
            return None
        if tenant_cap > 0 and restaurant_id is not None and not force:
            t = conn.execute("SELECT output_tokens FROM model_tenant_windows WHERE minute=? AND restaurant_id=?",
                             (minute, restaurant_id)).fetchone()
            if t is not None and int(t["output_tokens"]) + est_out > tenant_cap:
                conn.rollback()
                return None
        conn.execute("INSERT INTO model_token_windows (minute, klass, input_tokens, output_tokens, calls) "
                     "VALUES (?,?,?,?,1) ON CONFLICT(minute, klass) DO UPDATE SET input_tokens=input_tokens+"
                     "excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens, calls=calls+1",
                     (minute, _lane(fam, klass), est_in, est_out))
        if tenant_cap > 0 and restaurant_id is not None:
            conn.execute("INSERT INTO model_tenant_windows (minute, restaurant_id, output_tokens) VALUES (?,?,?) "
                         "ON CONFLICT(minute, restaurant_id) DO UPDATE SET output_tokens=output_tokens+"
                         "excluded.output_tokens", (minute, restaurant_id, est_out))
        conn.execute("DELETE FROM model_token_windows WHERE minute < ?", (minute - 120,))
        conn.execute("DELETE FROM model_tenant_windows WHERE minute < ?", (minute - 120,))
        conn.commit()
        return minute
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def enabled() -> bool:
    lim = org_limits()
    if lim["otpm"] > 0 or lim["itpm"] > 0 or _f("GATEWAY_TENANT_OTPM", 0) > 0:
        return True
    for v in _family_limits().values():
        try:
            if float(v.get("otpm") or 0) > 0 or float(v.get("itpm") or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def admit(restaurant_id, kwargs, klass=None, sleep=time.sleep, db_path=None):
    """Reserve this call's estimate in the organisation's minute (see the
    module doc), waiting for the next minute while over the class's
    allowance. Returns a Ticket (settle it), or None when the allocator is
    off. Raises GatewayBusy for a bulk call that waited out its allowance."""
    if not enabled():
        return None
    klass = klass or call_class()
    family = model_family((kwargs or {}).get("model"))
    est_in, est_out = estimate(kwargs)
    waited, give_up = 0.0, WAIT_SECONDS.get(klass, 60)
    waited += _stream_slot_wait(give_up, sleep)
    while True:
        try:
            minute = _try_reserve(klass, est_in, est_out, restaurant_id, db_path=db_path, family=family)
        except Exception:
            return None                      # bookkeeping never stops a call
        if minute is not None:
            return Ticket(klass, minute, est_in, est_out, restaurant_id, waited, family=family)
        if waited >= give_up:
            if klass == "bulk":
                raise GatewayBusy(klass, retry_after=60 - (time.time() % 60))
            # An owner, or a generation on its clock, goes anyway: the
            # provider's own limit and the retry are the hard stop. Its
            # estimate is still RESERVED (SCALE-25): a zero ticket made the
            # minute look emptier than it was exactly at saturation, so more
            # calls went out and the provider's 429s followed.
            try:
                minute = _try_reserve(klass, est_in, est_out, restaurant_id, db_path=db_path, force=True,
                                      family=family)
            except Exception:
                minute = None
            if minute is None:
                return Ticket(klass, _minute(), 0, 0, restaurant_id, waited, family=family)
            return Ticket(klass, minute, est_in, est_out, restaurant_id, waited, family=family)
        step = min(give_up - waited, max(0.5, 60 - (time.time() % 60)))
        # Waiting holds no generation slot (SCALE-25): the queue job is
        # parked for the wait, so another restaurant's generation can do its
        # code work meanwhile (job_queue.parked).
        with _parked(step):
            sleep(step)
        waited += step


def _stream_slot_wait(give_up, sleep=time.sleep) -> float:
    """Inside a generation queue job, with an output budget set: wait (at
    most `give_up` seconds, parked) while max_concurrent_generations rows
    of the group are already streaming a model answer — the cap on streams
    (scale audit 10/19/26 A5-F8), which used to hold the CPU slots of
    code-only generations instead. Then the call goes anyway, as an owner's
    or a running generation's always has. Returns the seconds waited;
    anywhere else, 0 at once."""
    cap = max_concurrent_generations()
    if not cap:
        return 0.0
    try:
        import job_queue
        job = job_queue.current()
        if job is None or job.get("grp") != "generation":
            return 0.0
        waited = 0.0
        while job_queue.rows_in_phase("generation", "model", exclude_id=job.get("id")) >= cap:
            if waited >= give_up:
                break
            step = min(max(0.0, give_up - waited), 2.0) or 0.5
            with _parked(step):
                sleep(step)
            waited += step
        return waited
    except Exception:
        return 0.0                           # bookkeeping never stops a call


def _parked(seconds):
    """job_queue.parked for the running queue job, or nothing outside one."""
    try:
        import job_queue
        return job_queue.parked(seconds)
    except Exception:
        return contextlib.nullcontext()


def settle(ticket, message=None, db_path=None) -> None:
    """Replace the ticket's estimate with what the call really used (zero for
    a call that failed before an answer), in the minute it was reserved in —
    inserted when that minute has no row (re-audit 10/11/26 SCALE-25: an
    UPDATE alone lost a call's real tokens whenever its row was missing, a
    go-anyway call's or a pruned minute's). Never raises."""
    if ticket is None or ticket.settled:
        return
    ticket.settled = True
    u = getattr(message, "usage", None) if message is not None else None
    real_in = int(getattr(u, "input_tokens", 0) or 0) + int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    real_out = int(getattr(u, "output_tokens", 0) or 0)
    d_in, d_out = real_in - ticket.est_in, real_out - ticket.est_out
    if not d_in and not d_out:
        return
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO model_token_windows (minute, klass, input_tokens, output_tokens, calls) "
                         "VALUES (?,?,?,?,1) ON CONFLICT(minute, klass) DO UPDATE SET "
                         "input_tokens=MAX(0, model_token_windows.input_tokens+?), "
                         "output_tokens=MAX(0, model_token_windows.output_tokens+?)",
                         (ticket.minute, _lane(ticket.family, ticket.klass), max(0, d_in), max(0, d_out), d_in,
                          d_out))
            if ticket.restaurant_id is not None:
                conn.execute("INSERT INTO model_tenant_windows (minute, restaurant_id, output_tokens) VALUES (?,?,?) "
                             "ON CONFLICT(minute, restaurant_id) DO UPDATE SET "
                             "output_tokens=MAX(0, model_tenant_windows.output_tokens+?)",
                             (ticket.minute, ticket.restaurant_id, max(0, d_out), d_out))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Accounting only — a lost write never fails the model call, but it
        # is recorded, never swallowed (silent-handler lint).
        try:
            import ops
            ops.capture(e, job="model_gateway", context="gateway accounting write")
        except Exception:
            print(f"[model_gateway] accounting write lost: {e!r}")


def max_concurrent_generations():
    """How many generations may stream at once within the organisation's
    output budget for the interactive and scheduled classes, or None when
    no budget is set. Held in admit (_stream_slot_wait) against the rows
    streaming, never against the generation group's CPU slots (A5-F8)."""
    otpm = org_limits()["otpm"]
    if otpm <= 0 or GENERATION_STREAM_OTPM <= 0:
        return None
    sh = shares()
    return max(1, int(otpm * (sh["interactive"] + sh["scheduled"]) // GENERATION_STREAM_OTPM))


def should_batch(workflow, klass=None) -> bool:
    """Whether this workflow's call goes through Message Batches: never an
    interactive call (an owner is waiting), otherwise when the workflow's
    policy allows batches (ai_batches.enabled)."""
    if (klass or call_class()) == "interactive":
        return False
    try:
        import ai_batches
        return bool(ai_batches.enabled(workflow))
    except Exception:
        return False


def usage_now(db_path=None) -> dict:
    """This minute's tokens by class and the limits — the console's view."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT klass, input_tokens, output_tokens, calls FROM model_token_windows "
                            "WHERE minute=?", (_minute(),)).fetchall()
    finally:
        conn.close()
    classes, families = {}, {}
    for r in rows:
        fam, k = _split_lane(r["klass"])
        for bucket in (classes.setdefault(k, {"input": 0, "output": 0, "calls": 0}),
                       families.setdefault(fam or "unknown", {}).setdefault(k, {"input": 0, "output": 0,
                                                                                 "calls": 0})):
            bucket["input"] += int(r["input_tokens"] or 0)
            bucket["output"] += int(r["output_tokens"] or 0)
            bucket["calls"] += int(r["calls"] or 0)
    return {"limits": org_limits(), "family_limits": _family_limits(), "shares": shares(),
            "classes": classes, "families": families}


# ── per-tenant dollar budgets ───────────────────────────────────────────────

_budget_memo = {}
_budget_lock = threading.Lock()
BUDGET_MEMO_SECONDS = 60


def tenant_budget(restaurant_id, db_path=None):
    """(daily_usd, monthly_usd) set for this restaurant, either None when
    not set — read through a one-minute memo."""
    if restaurant_id is None:
        return None, None
    key = (db_path, int(restaurant_id))
    with _budget_lock:
        hit = _budget_memo.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT daily_usd, monthly_usd FROM ai_tenant_budgets WHERE restaurant_id=?",
                               (int(restaurant_id),)).fetchone()
        finally:
            conn.close()
        val = (row["daily_usd"], row["monthly_usd"]) if row else (None, None)
    except Exception:
        val = (None, None)
    with _budget_lock:
        _budget_memo[key] = (time.time() + BUDGET_MEMO_SECONDS, val)
    return val


def set_tenant_budget(restaurant_id, daily_usd=None, monthly_usd=None, reason=None, set_by=None, db_path=None):
    """Raise or lower one restaurant's AI ceilings (None: the tier's).
    Both None removes the override."""
    conn = get_conn(db_path)
    try:
        if daily_usd is None and monthly_usd is None:
            conn.execute("DELETE FROM ai_tenant_budgets WHERE restaurant_id=?", (int(restaurant_id),))
        else:
            conn.execute("INSERT INTO ai_tenant_budgets (restaurant_id, daily_usd, monthly_usd, reason, set_by, "
                         "updated_at) VALUES (?,?,?,?,?, datetime('now')) ON CONFLICT(restaurant_id) DO UPDATE SET "
                         "daily_usd=excluded.daily_usd, monthly_usd=excluded.monthly_usd, reason=excluded.reason, "
                         "set_by=excluded.set_by, updated_at=excluded.updated_at",
                         (int(restaurant_id), daily_usd, monthly_usd, reason, set_by))
        conn.commit()
    finally:
        conn.close()
    with _budget_lock:
        _budget_memo.clear()
    try:
        import ai_utils
        ai_utils.invalidate_budget_memo(restaurant_id)
    except Exception:
        pass


# ── shared breakers ─────────────────────────────────────────────────────────

def breaker_row(provider, db_path=None):
    """The shared breaker row for `provider` as a dict, or None."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT * FROM ai_breakers WHERE provider=?", (provider,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def write_breaker(provider, open_until, opened_at=None, reason=None, failures=0, db_path=None) -> None:
    """Publish a breaker transition (open, close, reset) to every process.
    Never raises: a breaker that cannot be shared still works here."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO ai_breakers (provider, open_until, opened_at, reason, failures, updated_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET open_until=excluded.open_until, "
                         "opened_at=excluded.opened_at, reason=excluded.reason, failures=excluded.failures, "
                         "updated_at=excluded.updated_at",
                         (provider, float(open_until or 0), opened_at, reason, int(failures or 0), time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # Accounting only — a lost write never fails the model call, but it
        # is recorded, never swallowed (silent-handler lint).
        try:
            import ops
            ops.capture(e, job="model_gateway", context="gateway accounting write")
        except Exception:
            print(f"[model_gateway] accounting write lost: {e!r}")
