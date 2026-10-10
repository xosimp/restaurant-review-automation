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
import os
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


def org_limits() -> dict:
    """{"otpm", "itpm"}: the organisation's output / input tokens a minute
    the gateway allocates (0: not allocated)."""
    return {"otpm": _f("GATEWAY_ORG_OTPM", 0), "itpm": _f("GATEWAY_ORG_ITPM", 0)}


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
    __slots__ = ("klass", "minute", "est_in", "est_out", "restaurant_id", "settled", "waited")

    def __init__(self, klass, minute, est_in, est_out, restaurant_id, waited=0.0):
        self.klass, self.minute, self.est_in, self.est_out = klass, minute, est_in, est_out
        self.restaurant_id, self.settled, self.waited = restaurant_id, False, waited


def estimate(kwargs) -> tuple:
    """(input, output) tokens a call is reserved before it says: its prompt's
    characters / 4, and its max_tokens capped at OUTPUT_RESERVE_CAP."""
    chars = 0
    for m in (kwargs or {}).get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            chars += sum(len(str(b.get("text") or "")) for b in c if isinstance(b, dict))
    sys_ = (kwargs or {}).get("system")
    chars += len(sys_) if isinstance(sys_, str) else sum(len(str(b.get("text") or "")) for b in (sys_ or [])
                                                         if isinstance(b, dict))
    out = min(int((kwargs or {}).get("max_tokens") or 1024), OUTPUT_RESERVE_CAP)
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


def _try_reserve(klass, est_in, est_out, restaurant_id, now=None, db_path=None):
    """Reserve in this minute's windows when the class (and the tenant) has
    room; returns the minute reserved in, or None."""
    lim = org_limits()
    tenant_cap = _f("GATEWAY_TENANT_OTPM", 0)
    minute = _minute(now)
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT klass, input_tokens, output_tokens FROM model_token_windows WHERE minute=?",
                            (minute,)).fetchall()
        out_use = {r["klass"]: int(r["output_tokens"]) for r in rows}
        in_use = {r["klass"]: int(r["input_tokens"]) for r in rows}
        sh = shares()
        if est_out > _allowance(klass, out_use, lim["otpm"], sh) or \
                est_in > _allowance(klass, in_use, lim["itpm"], sh):
            conn.rollback()
            return None
        if tenant_cap > 0 and restaurant_id is not None:
            t = conn.execute("SELECT output_tokens FROM model_tenant_windows WHERE minute=? AND restaurant_id=?",
                             (minute, restaurant_id)).fetchone()
            if t is not None and int(t["output_tokens"]) + est_out > tenant_cap:
                conn.rollback()
                return None
        conn.execute("INSERT INTO model_token_windows (minute, klass, input_tokens, output_tokens, calls) "
                     "VALUES (?,?,?,?,1) ON CONFLICT(minute, klass) DO UPDATE SET input_tokens=input_tokens+"
                     "excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens, calls=calls+1",
                     (minute, klass, est_in, est_out))
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
    return lim["otpm"] > 0 or lim["itpm"] > 0 or _f("GATEWAY_TENANT_OTPM", 0) > 0


def admit(restaurant_id, kwargs, klass=None, sleep=time.sleep, db_path=None):
    """Reserve this call's estimate in the organisation's minute (see the
    module doc), waiting for the next minute while over the class's
    allowance. Returns a Ticket (settle it), or None when the allocator is
    off. Raises GatewayBusy for a bulk call that waited out its allowance."""
    if not enabled():
        return None
    klass = klass or call_class()
    est_in, est_out = estimate(kwargs)
    waited, give_up = 0.0, WAIT_SECONDS.get(klass, 60)
    while True:
        try:
            minute = _try_reserve(klass, est_in, est_out, restaurant_id, db_path=db_path)
        except Exception:
            return None                      # bookkeeping never stops a call
        if minute is not None:
            return Ticket(klass, minute, est_in, est_out, restaurant_id, waited)
        if waited >= give_up:
            if klass == "bulk":
                raise GatewayBusy(klass, retry_after=60 - (time.time() % 60))
            # An owner, or a generation on its clock, goes anyway: the
            # provider's own limit and the retry are the hard stop.
            return Ticket(klass, _minute(), 0, 0, restaurant_id, waited)
        step = min(give_up - waited, max(0.5, 60 - (time.time() % 60)))
        sleep(step)
        waited += step


def settle(ticket, message=None, db_path=None) -> None:
    """Replace the ticket's estimate with what the call really used (zero for
    a call that failed before an answer). Never raises."""
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
            conn.execute("UPDATE model_token_windows SET input_tokens=MAX(0, input_tokens+?), "
                         "output_tokens=MAX(0, output_tokens+?) WHERE minute=? AND klass=?",
                         (d_in, d_out, ticket.minute, ticket.klass))
            if ticket.restaurant_id is not None:
                conn.execute("UPDATE model_tenant_windows SET output_tokens=MAX(0, output_tokens+?) "
                             "WHERE minute=? AND restaurant_id=?", (d_out, ticket.minute, ticket.restaurant_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def max_concurrent_generations():
    """How many generations may stream at once within the organisation's
    output budget for the interactive and scheduled classes, or None when
    no budget is set (job_queue.group_concurrency then uses its own)."""
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
    return {"limits": org_limits(), "shares": shares(),
            "classes": {r["klass"]: {"input": r["input_tokens"], "output": r["output_tokens"], "calls": r["calls"]}
                        for r in rows}}


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
    except Exception:
        pass
