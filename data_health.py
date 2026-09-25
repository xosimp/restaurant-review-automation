"""
data_health.py — the Restaurant Data Health Score: what Cavnar knows about a
restaurant's data, how current it is, and what that does to the advice.

Built ON the freshness registry, never beside it (the Data Freshness audit,
9/24/26, DH5 §2):

  source_health      one row per (restaurant, source), written by every
                     ingest point through record_attempt(): last attempt,
                     last success, error and its class, consecutive
                     failures, the last RECENT_MAX outcomes. data_freshness
                     reads it (_with_health), so a sync that stopped is an
                     error on the source — and so a lower confidence — and
                     never a quiet day.
  snapshot()         the owner's Data Health: one line per source, the
                     overall % (a weighted mean with named caps so one
                     broken source cannot hide), what is not connected, and
                     per module the decision and the Recommendation
                     Confidence Impact ("81% → 94% once counts are current").
  readiness()        asked BEFORE a model call: proceed | caveat | wait |
                     refuse, the DATA STATE block for the prompt and the
                     data_state for the Response Validation Layer.
                     ai_utils.create_with_retry(readiness=) enforces refuse.

The overall % never enters a recommendation's confidence: freshness reaches
confidence through the registry alone (an erroring source is held under
ERROR_CEILING), and the impact figure replays confidence_engine._combine
with freshness current — nothing is counted twice.

Never raises to a caller: an unreadable piece is left out and said.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import models as _models_mod
from models import DB_PATH
import confidence_engine as ce
import data_freshness as df


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


RECENT_MAX = 20
# Reliability is said only over this many recorded attempts.
RELIABILITY_MIN_ATTEMPTS = 5
# The overall %'s named caps (DH5 §2.4), in the confidence_engine idiom.
ANY_ERROR_CAP = 74
ANY_STALE_CAP = 74
BLOCKING_DOWN_CAP = 49
CACHE_TTL = 60
_CACHE_MAX = 2000
_CACHE = {}
_LOCK = threading.Lock()

# What an owner calls each source in a status line.
OWNER_LABEL = {
    "pos": "POS sales", "labor": "Shifts", "sales": "Sales", "reviews": "Reviews",
    "inventory": "Inventory counts", "purchases": "Deliveries", "waste": "Waste log", "prices": "Prices",
    "marketing": "Marketing metrics",
    "visibility": "AI visibility", "competitor": "Competitors", "weather": "Weather",
    "dsr": "Daily report", "depletion": "Depletion",
    # Benchmarking audit BM3-9 (Top-50 #22): the peer bands comparisons rest on.
    "cohort": "Peer comparison",
}

# The module flags an owner sees, and the modules each one turns on. Intel
# and AI visibility have no flag: they are read whenever their source is
# connected.
_MODULE_FLAGS = (
    ("module_reviews", ("reviews",)),
    ("module_labor", ("labor", "schedule")),
    ("module_inventory", ("food_cost",)),
    ("module_marketing", ("marketing",)),
    ("dsr_enabled", ("dsr",)),
)
_ALWAYS = ("intel", "visibility")
# rec_instances.module values each health module covers.
_REC_MODULES = {"food_cost": ("food_cost", "food", "inventory"), "labor": ("labor",),
                "schedule": ("schedule",), "reviews": ("reviews",), "marketing": ("marketing",),
                "intel": ("intel",), "visibility": ("visibility",), "dsr": ("dsr", "ops")}

NOT_APPLICABLE = {"decision": "proceed", "not_applicable": True, "reason": "rests on no data source",
                  "sources": [], "data_state": {}, "prompt_block": "", "retry_after": None, "blocking": None}


def _utc(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _stamp(now=None):
    return _utc(now).strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── recording ──────────────────────────────────────────────────────────────

def classify_error(text) -> str:
    """auth | rate_limit | timeout | empty | provider | internal, from an
    error message. auth never retries automatically — the owner has to
    reconnect."""
    t = str(text or "").lower()
    if not t:
        return "internal"
    if any(w in t for w in ("401", "403", "unauthor", "forbidden", "invalid_grant", "revoked", "expired token",
                            "token expired", "credential", "invalid token", "not authorized")):
        return "auth"
    if "429" in t or "rate limit" in t or "rate-limit" in t or "too many requests" in t:
        return "rate_limit"
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "no data" in t or "empty" in t or "nothing returned" in t:
        return "empty"
    if any(c in t for c in ("500", "502", "503", "504", "server error", "unavailable", "truncated")):
        return "provider"
    return "internal"


def record_attempt(restaurant_id, source, ok, *, provider=None, error=None, error_class=None,
                   data_through=None, duration_ms=None, next_retry_at=None, db_path=None, now=None) -> None:
    """One sync attempt for (restaurant, source): the ledger every freshness
    line, reliability figure and automatic retry reads. Updated in place;
    never raises (a sync must not fail because its bookkeeping did)."""
    if not restaurant_id or not source:
        return
    at = _stamp(now)
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[data_health] no connection to record {source} for {restaurant_id}: {e}")
        return
    try:
        old = conn.execute("SELECT * FROM source_health WHERE restaurant_id=? AND source=?",
                           (restaurant_id, source)).fetchone()
        old = dict(old) if old else {}
        prior_fails = int(old.get("consecutive_failures") or 0)
        recent = (str(old.get("recent") or "") + ("1" if ok else "0"))[-RECENT_MAX:]
        err = None if ok else (str(error or "failed").strip()[:300] or "failed")
        conn.execute(
            """INSERT OR REPLACE INTO source_health
               (restaurant_id, source, provider, last_attempt_at, last_ok_at, first_failed_at, last_error,
                error_class, consecutive_failures, recent, last_duration_ms, data_through, next_retry_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (restaurant_id, source, provider or old.get("provider"), at,
             at if ok else old.get("last_ok_at"),
             None if ok else (old.get("first_failed_at") if prior_fails else at),
             err, None if ok else (error_class or classify_error(err)),
             0 if ok else prior_fails + 1, recent,
             int(duration_ms) if duration_ms is not None else None,
             str(data_through)[:10] if data_through else old.get("data_through"),
             None if ok else next_retry_at))
        conn.commit()
    except Exception as e:
        print(f"[data_health] record_attempt {source} for {restaurant_id} failed: {e}")
    finally:
        conn.close()
    invalidate(restaurant_id)


def health_rows(restaurant_id, db_path=None) -> dict:
    """{source: source_health row} for one restaurant."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return {}
    try:
        rows = conn.execute("SELECT * FROM source_health WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        return {r["source"]: dict(r) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def due_retries(source, now=None, db_path=None) -> list:
    """Restaurant ids whose `source` failed with a retry now due (the
    automatic-recovery sweep's work list). auth failures never retry."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT restaurant_id FROM source_health WHERE source=? AND consecutive_failures>0 "
            "AND next_retry_at IS NOT NULL AND next_retry_at<=? AND COALESCE(error_class,'')!='auth'",
            (source, _stamp(now))).fetchall()
        return [r["restaurant_id"] for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# ── cache ──────────────────────────────────────────────────────────────────

def invalidate(restaurant_id=None):
    with _LOCK:
        if restaurant_id is None:
            _CACHE.clear()
        else:
            for k in [k for k in _CACHE if k[0] == restaurant_id]:
                _CACHE.pop(k, None)


def _cache_get(key):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
    return None


def _cache_put(key, value):
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            cutoff = time.monotonic() - CACHE_TTL
            for k in [k for k, v in _CACHE.items() if v[0] < cutoff]:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
        _CACHE[key] = (time.monotonic(), value)


# ── one source, in the owner's words ───────────────────────────────────────

def ago(stamp, now=None) -> str:
    """"2 min ago", "5 hours ago", "3 days ago", or M/D/YY past a week."""
    d = _parse(stamp)
    if d is None:
        return ""
    secs = max(0, (_utc(now) - d).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 48 * 3600:
        h = int(secs // 3600)
        return f"{h} hour{'' if h == 1 else 's'} ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)} days ago"
    return ce._mdy(d.date().isoformat()) or ""


def _reliability(s):
    rel = s.get("reliability") or {}
    attempts = int(rel.get("attempts") or 0)
    if attempts < RELIABILITY_MIN_ATTEMPTS:
        return None
    ok = int(rel.get("ok") or 0)
    return {"pct": int(round(100.0 * ok / attempts)), "ok": ok, "attempts": attempts,
            "consecutive_failures": int(s.get("consecutive_failures") or 0),
            "basis": f"{ok} of the last {attempts} syncs succeeded"}


def _is_live(key, s, now=None) -> bool:
    hours = (df.CADENCE.get(key) or (None, ""))[0]
    last = _parse(s.get("last_ok_at"))
    return (hours is not None and hours <= df.LIVE_WITHIN_HOURS and last is not None
            and (_utc(now) - last) <= timedelta(hours=hours) and not s.get("error"))


def source_line(s, now=None, tz=None, pos_connected=None) -> dict:
    """One source as the owner reads it: {key, label, state, tone, pct,
    health_pct, line, as_of, last_ok_at, synced, cadence, reliability,
    error, error_class, pending, expected_by, expected_line, last_sync_local,
    counts_current, can_sync_now}. tone: ok | warn | bad | off.

    `pending` is a connected source whose first sync has not landed;
    `expected_by` (ISO-Z) is the next scheduler slot that refreshes it and
    `expected_line` says it in the restaurant's clock ("POS sync runs
    tonight 3am"). Shifts and sales ride the POS sync only when a POS is
    connected (`pos_connected`); uploaded shifts have no schedule."""
    key = s.get("key")
    label = OWNER_LABEL.get(key, s.get("label") or key)
    state = s.get("state")
    pct = s.get("pct")
    synced = ago(s.get("last_ok_at"), now) if s.get("last_ok_at") else ""
    rel = _reliability(s)
    cadence = (df.CADENCE.get(key) or (None, ""))[1]
    scheduled = key not in ("labor", "sales") or bool(pos_connected)
    nxt = next_slot(key, now) if (scheduled and state not in ("not_connected", "disconnected")) else None
    expected = expected_phrase(nxt, tz, now) if nxt else ""
    pending = is_pending(s)
    expected_line = f"{SLOT_WORD.get(key, label)} runs {expected}" if expected else None
    if state == "not_connected":
        line, tone = f"{label}: not connected", "off"
    elif pending:
        line = (f"{label}: first sync runs {expected}" if expected else f"{label}: first sync pending")
        tone = "off"
    elif _is_live(key, s, now):
        line, tone = f"{label}: Live ({synced})", "ok"
    else:
        basis = str(s.get("basis") or "").strip()
        # "Shifts through 9/25/26", not "Shifts: Shifts through 9/25/26" -
        # a basis that already opens with the source's name keeps it once.
        if basis and basis.lower().startswith(str(label).lower()):
            line = basis
        else:
            line = f"{label}: {basis}" if basis else f"{label}: {state}"
        if synced and "synced" not in basis:
            line += f" (synced {synced})"
        if s.get("error") or state in ("stale", "unknown"):
            tone = "bad"
        elif state in ("aging", "disconnected"):
            tone = "warn"
        else:
            tone = "ok"
    vals = [v for v in (pct, (rel or {}).get("pct")) if v is not None]
    return {"key": key, "label": label, "state": state, "tone": tone, "pct": pct,
            "health_pct": min(vals) if vals else None, "line": line, "as_of": s.get("as_of"),
            "as_of_iso": s.get("as_of_iso"), "last_ok_at": s.get("last_ok_at"), "synced": synced or None,
            "cadence": cadence, "reliability": rel, "error": s.get("error"),
            "error_class": s.get("error_class"), "next_retry_at": s.get("next_retry_at"),
            "basis": s.get("basis"), "pending": pending,
            "expected_by": nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None,
            "expected_line": expected_line,
            "last_sync_local": when_local(s.get("last_ok_at"), tz, now) if s.get("last_ok_at") else None,
            "counts_current": counts_as_current(s, now),
            "can_sync_now": key == "pos" and state not in ("not_connected", "disconnected")}


# ── when a source next refreshes, in the owner's clock (DH4-18, #27) ───────
#
# The scheduler's own slots, in its clock (America/Chicago — scheduler.py's
# main loop: the POS sync at 3am, the Meta metrics sync at 4am, the review
# fetch at 8/12/4/8, competitors Monday 6am, AI visibility Monday 7am). The
# scheduler owns the times; this reads them so a source can say when it next
# refreshes ("POS sync runs tonight 3am"), and tests/test_data_health_owner.py
# holds the two in step. (hours, weekday or None for every day)
SCHED_TZ = "America/Chicago"
SLOTS = {
    "pos": ((3,), None), "labor": ((3,), None), "sales": ((3,), None),
    "reviews": ((8, 12, 16, 20), None), "marketing": ((4,), None),
    "competitor": ((6,), 0), "visibility": ((7,), 0),
}
SLOT_WORD = {"pos": "POS sync", "labor": "Shift sync", "sales": "Sales sync", "reviews": "Review check",
             "marketing": "Metrics sync", "competitor": "Competitor read", "visibility": "AI visibility check"}
# A nightly sync that ran at 3:02am still counts as current until the next
# slot has had this long to finish (job jitter, a slow provider).
CADENCE_SLACK_HOURS = 2


def _tz(restaurant_or_tz=None):
    from time_utils import restaurant_tz
    try:
        return restaurant_tz(restaurant_or_tz or None)
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo(SCHED_TZ)


def next_slot(key, now=None):
    """The next scheduled refresh of source `key` as an aware UTC datetime,
    or None for a source nothing refreshes on a clock (counts, deliveries —
    the owner does them — and weather, fetched on read)."""
    slot = SLOTS.get(key)
    if not slot:
        return None
    from zoneinfo import ZoneInfo
    hours, weekday = slot
    local = _utc(now).astimezone(ZoneInfo(SCHED_TZ))
    for back in range(0, 8):
        d = (local + timedelta(days=back)).date()
        if weekday is not None and d.weekday() != weekday:
            continue
        for h in sorted(hours):
            at = datetime(d.year, d.month, d.day, h, tzinfo=local.tzinfo)
            if at > local:
                return at.astimezone(timezone.utc)
    return None


def clock(dt) -> str:
    """"3am", "3:02am", "4pm" — the design system's time (no seconds, no
    leading zero)."""
    if dt is None:
        return ""
    h = dt.hour % 12 or 12
    return f"{h}{'' if dt.minute == 0 else f':{dt.minute:02d}'}{'am' if dt.hour < 12 else 'pm'}"


def when_local(stamp, tz=None, now=None) -> str:
    """A stamp in the restaurant's clock, never UTC: "3:02am" today,
    "9/21/26 · 3:02am" another day. "" when unreadable."""
    d = stamp if isinstance(stamp, datetime) else _parse(stamp)
    if d is None:
        return ""
    z = _tz(tz)
    loc = d.astimezone(z)
    today = _utc(now).astimezone(z).date()
    if loc.date() == today:
        return clock(loc)
    return f"{ce._mdy(loc.date().isoformat())} · {clock(loc)}"


def expected_phrase(at, tz=None, now=None) -> str:
    """When a slot falls, in the owner's words and clock: "today 4pm",
    "tonight 3am" (the small hours after today), "tomorrow 8am", else
    "9/28/26 · 6am"."""
    if at is None:
        return ""
    z = _tz(tz)
    loc = at.astimezone(z)
    today = _utc(now).astimezone(z).date()
    days = (loc.date() - today).days
    if days == 0:
        return f"{'tonight' if loc.hour >= 18 else 'today'} {clock(loc)}"
    if days == 1 and loc.hour < 6:
        return f"tonight {clock(loc)}"
    if days == 1:
        return f"tomorrow {clock(loc)}"
    return f"{ce._mdy(loc.date().isoformat())} · {clock(loc)}"


def is_pending(s) -> bool:
    """Connected, but its first sync has not landed yet: nothing has been
    read from it, so it is neither current nor broken — it is waiting."""
    if not s or s.get("state") == "not_connected" or s.get("error"):
        return False
    if s.get("never_synced"):
        return True
    return (s.get("state") == "unknown" and not s.get("last_ok_at") and not s.get("as_of_iso")
            and "never" in str(s.get("basis") or "").lower())


def counts_as_current(s, now=None) -> bool:
    """Whether a source may be counted as current on an owner surface ("4
    current sources", iOS "running on AI"): its state is current, nothing is
    failing, and — when its sync history is known — it last succeeded within
    one cadence (plus CADENCE_SLACK_HOURS) of now. A nightly POS synced two
    days ago can still read 80% by its data date; it is not "live" (DH4-14,
    DH5-15)."""
    if not s or s.get("state") != "current" or s.get("error"):
        return False
    key = s.get("source") or s.get("key")
    hours = (df.CADENCE.get(key) or (None, ""))[0]
    last = _parse(s.get("last_ok_at"))
    if hours is None or last is None:
        return True
    return (_utc(now) - last) <= timedelta(hours=hours + CADENCE_SLACK_HOURS)


def affects(key, restaurant=None) -> list:
    """The owner's names for the enabled modules that rest on source `key`
    (data_freshness.MODULE_SOURCES, reversed) — "what this affects"."""
    mods = enabled_modules(restaurant) if restaurant is not None else list(_MODULE_TITLES)
    out = []
    for m in mods:
        if key in df.sources_for([m]):
            t = module_title(m)
            if t not in out:
                out.append(t)
    return out


def fix_hint(key, s=None) -> str:
    """The one action that fixes a source that stopped updating."""
    s = s or {}
    prov = {"rpower": "RPOWER"}.get(s.get("provider"), str(s.get("provider") or "your POS").title()
                                     if s.get("provider") else "your POS")
    auth = s.get("error_class") == "auth"
    return {"pos": (f"Reconnect {prov} in Account → Connections" if auth else
                    f"Open Account → Connections and tap Sync now on {prov}"),
            "labor": "Open Account → Connections and tap Sync now, or upload shifts on Labor",
            "sales": f"Open Account → Connections and tap Sync now on {prov}",
            "reviews": "Reconnect Google in Account → Connections",
            "marketing": "Reconnect Instagram or Facebook in Account → Connections",
            "inventory": "Count your inventory on Food Cost",
            "purchases": "Log a delivery on Food Cost"}.get(key, _connect_hint(key))


def connection_lines(restaurant, now=None, db_path=None) -> dict:
    """What Account → Connections says about the POS and Google, from the
    registry (never the sync-run stamp — DH4-2) and in the restaurant's
    clock (never UTC): {"pos": {...}, "google": {...}}, each {line, tone
    ok|warn|bad|off, state, last_sync, data_through, next} or None when not
    connected. Never raises."""
    out = {"pos": None, "google": None}
    tz = df._get(restaurant, "timezone")
    try:
        s = df.source_state(restaurant, "pos", db_path=db_path, now=now)
        if s.get("state") != "not_connected":
            # The ledger's last success, else the provider's own sync stamp
            # (full time, UTC) — the registry row carries only its date.
            last = s.get("last_ok_at")
            if not last:
                try:
                    import pos_health
                    last = pos_health.pos_sync_state(restaurant, now=now).get("last_synced")
                except Exception:
                    last = None
            when = when_local(last, tz, now) if last else ""
            sales_day = s.get("as_of") if "sales through" in str(s.get("basis") or "") else None
            nxt = expected_phrase(next_slot("pos", now), tz, now)
            if s.get("state") == "disconnected":
                # A POS removed after use keeps its last data's recency
                # (data_freshness): a warning, and no next sync to promise.
                line, tone, nxt = "Disconnected" + (f" · Sales through {sales_day}" if sales_day else ""), "warn", ""
            elif is_pending(s):
                line, tone = f"Connected — first sync runs {nxt}" if nxt else "Connected — first sync pending", "off"
            else:
                bits = []
                if s.get("error"):
                    bits.append("Sync failing" + (f" · last good sync {when}" if when else ""))
                elif when:
                    bits.append(f"Last sync {when}")
                bits.append(f"Sales through {sales_day}" if sales_day else "No sales on file yet")
                line = " · ".join(bits)
                tone = ("bad" if (s.get("error") or s.get("state") in ("stale", "unknown"))
                        else "warn" if s.get("state") == "aging" else "ok")
            out["pos"] = {"line": line, "tone": tone, "state": s.get("state"), "last_sync": when or None,
                          "data_through": sales_day, "next": nxt or None, "provider": s.get("provider"),
                          "error": s.get("error"), "pct": s.get("pct")}
    except Exception as e:
        print(f"[data_health] pos connection line failed: {e}")
    try:
        f = df.review_fetch_state(restaurant, now=now)
        if f.get("state") != "not_connected":
            nxt = expected_phrase(next_slot("reviews", now), tz, now)
            at = None
            try:
                import admin_ops
                at = admin_ops.fetched_at_ct(df._get(restaurant, "last_fetched_at"))
            except Exception:
                at = None
            if f.get("state") == "unknown" and not f.get("future") and at is None:
                line, tone = (f"First check runs {nxt}" if nxt else "First check pending"), "off"
            elif f.get("error") or f.get("state") in ("stale", "unknown"):
                missed = f.get("missed") or 0
                line = (f"Last check {ce._mdy(f.get('as_of_iso'))}" if f.get("as_of_iso") else "Last check unknown") \
                    + (f" — {missed} check{'' if missed == 1 else 's'} missed" if missed else "")
                tone = "bad"
            else:
                line = f"Checked {when_local(at, tz, now)}" + (f" · next check {nxt}" if nxt else "")
                tone = "warn" if f.get("state") == "aging" else "ok"
            out["google"] = {"line": line, "tone": tone, "state": f.get("state"),
                             "last_sync": when_local(at, tz, now) if at else None,
                             "data_through": f.get("as_of"), "next": nxt or None, "error": f.get("error"),
                             "missed": f.get("missed"), "pct": f.get("pct")}
    except Exception as e:
        print(f"[data_health] google connection line failed: {e}")
    return out


def metrics_line(restaurant, now=None, db_path=None):
    """The Marketing screens' "Metrics synced 9/21/26" line (DH4-8), from
    the registry's marketing source: {line, tone ok|warn, synced_at, state,
    error}; tone warn (amber) when the metrics sync is failing, stale or
    has never run. None when no social account is connected. Never raises."""
    try:
        s = df.source_state(restaurant, "marketing", db_path=db_path, now=now)
    except Exception:
        return None
    if not s or s.get("state") == "not_connected":
        return None
    at = _parse(s.get("metrics_synced_at"))
    tz = df._get(restaurant, "timezone")
    day = ce._mdy(at.astimezone(_tz(tz)).date().isoformat()) if at else None
    line = f"Metrics synced {day}" if day else "Metrics haven't synced yet"
    bad = bool(s.get("error")) or s.get("state") in ("stale", "unknown") or not day
    if s.get("error") and day and "last synced" not in str(s["error"]):
        line += f" — {s['error']}"
    elif s.get("error"):
        line = str(s["error"])
    return {"line": line, "tone": "warn" if bad else "ok", "synced_at": s.get("metrics_synced_at"),
            "state": s.get("state"), "error": s.get("error")}


def compact(snap) -> dict:
    """What Home embeds beside its legacy freshness[] (web /api/home/brief,
    mobile /mobile/api/home): {overall, worst_line}. None when the snapshot
    could not be read."""
    if not isinstance(snap, dict) or not snap.get("ok"):
        return None
    return {"overall": snap.get("overall"), "worst_line": snap.get("worst_line")}


# ── the restaurant ─────────────────────────────────────────────────────────

def enabled_modules(restaurant) -> list:
    out = []
    for flag, mods in _MODULE_FLAGS:
        v = df._get(restaurant, flag)
        if v is None or bool(int(v or 0)):
            for m in mods:
                if m not in out:
                    out.append(m)
    for m in _ALWAYS:
        if m not in out:
            out.append(m)
    return out


def _states(restaurant, keys, ctx=None, db_path=None, now=None):
    if ctx is not None:
        return ctx.sources(tuple(keys))
    return df.states(restaurant, keys, db_path=db_path, now=now)


def _down(s) -> bool:
    """A blocking source that cannot be stood on: age unknown, past its
    horizon, or its credentials refused."""
    if not s or s.get("state") == "not_connected":
        return False
    return s.get("state") == "unknown" or (s.get("pct") == 0) or s.get("error_class") == "auth"


def overall(lines, modules) -> dict:
    """{pct, state, label, caps_applied, reason} — the weighted mean of the
    in-use sources' health, held by the named caps. None when nothing is
    connected — never 0, never 100."""
    # A source waiting on its first sync is neither healthy nor broken:
    # nothing has been read from it, so it is never scored (it says when
    # its first sync runs instead).
    in_use = [l for l in lines if l.get("state") != "not_connected" and l.get("health_pct") is not None
              and not l.get("pending")]
    waiting = [l for l in lines if l.get("pending")]
    if not in_use and waiting:
        return {"pct": None, "state": "pending", "label": "Waiting for the first sync", "caps_applied": [],
                "reason": waiting[0].get("line")}
    if not in_use:
        return {"pct": None, "state": "not_connected", "label": "No data sources connected yet",
                "caps_applied": [], "reason": "Connect a data source to see how current your data is."}
    weight = {}
    for m in modules:
        for k in df.sources_for([m]):
            weight[k] = weight.get(k, 0) + 1
    num = sum(max(1, weight.get(l["key"], 1)) * l["health_pct"] for l in in_use)
    den = sum(max(1, weight.get(l["key"], 1)) for l in in_use)
    pct = num / den
    caps, reason = [], None
    by_key = {l["key"]: l for l in lines}

    def apply(name, cap, why):
        nonlocal pct, reason
        if pct > cap:
            pct = float(cap)
            caps.append(name)
            reason = why

    worst = min(in_use, key=lambda l: (l["health_pct"], l["label"]))
    stale = [l for l in in_use if (l.get("pct") is not None and l["pct"] < ce.STALE_BELOW)]
    errs = [l for l in in_use if l.get("error")]
    if errs:
        apply("any_error", ANY_ERROR_CAP, errs[0]["line"])
    if stale:
        apply("any_stale", ANY_STALE_CAP, stale[0]["line"])
    for m in modules:
        b = df.BLOCKING.get(m)
        if b and b in by_key and not by_key[b].get("pending") and _down({**by_key[b], "pct": by_key[b].get("pct")}):
            apply("blocking_down", BLOCKING_DOWN_CAP, by_key[b]["line"])
            break
    pct = int(round(pct))
    return {"pct": pct, "state": ce.state(pct), "label": f"{pct}% data health", "caps_applied": caps,
            "reason": reason or (worst["line"] if worst["health_pct"] < 100 else "Every connected source is current")}


def module_impact(restaurant_id, module, states_by_key, db_path=None) -> dict:
    """The Recommendation Confidence Impact for one module: the median of its
    open recommendations' confidence now and with every source current,
    from their stored evidence and track-record figures (rec_instances) and
    the module's live freshness. None with no open recommendation, or when
    fresher data would not move it IMPACT_MIN_DELTA points."""
    srcs = df.sources_for([module])
    fr = ce.freshness([states_by_key[k] for k in srcs if k in states_by_key])
    mods = _REC_MODULES.get(module, (module,))
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        q = ",".join("?" * len(mods))
        rows = conn.execute(
            f"SELECT evidence_pct, accuracy_pct FROM rec_instances WHERE restaurant_id=? AND status='open' "
            f"AND module IN ({q}) AND evidence_pct IS NOT NULL ORDER BY id DESC LIMIT 25",
            (restaurant_id, *mods)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    nows, bests = [], []
    for r in rows:
        acc = {"pct": r["accuracy_pct"]} if r["accuracy_pct"] is not None else None
        i = ce.impact({"pct": r["evidence_pct"]}, acc, fr)
        if i["now"] is not None and i["when_current"] is not None:
            nows.append(i["now"])
            bests.append(i["when_current"])
    if not nows:
        return None
    nows.sort()
    bests.sort()
    now_m, best_m = nows[len(nows) // 2], bests[len(bests) // 2]
    if best_m - now_m < ce.IMPACT_MIN_DELTA:
        return None
    n = len(nows)
    return {"now": now_m, "when_current": best_m, "delta": best_m - now_m, "blocked_by": fr.get("stalest"),
            "basis": f"median of {n} open recommendation{'' if n == 1 else 's'}",
            "line": (f"{module_title(module)} recommendations {now_m}% → {best_m}% once "
                     f"{OWNER_LABEL.get(fr.get('stalest'), fr.get('stalest') or 'the data').lower()} "
                     f"{'are' if str(fr.get('stalest') or '') in ('inventory', 'purchases', 'reviews', 'competitor') else 'is'} current")}


_MODULE_TITLES = {"food_cost": "Food cost", "labor": "Labor", "schedule": "Schedule", "reviews": "Review",
                  "marketing": "Marketing", "intel": "Competitor", "visibility": "AI visibility",
                  "dsr": "Daily report"}


def module_title(module) -> str:
    return _MODULE_TITLES.get(module, str(module or "").replace("_", " ").capitalize())


def _decision(module, by_key, delivery="interactive"):
    """(decision, reason, blocking key) for one module from its sources'
    states — the rule readiness() and the snapshot share."""
    srcs = [by_key[k] for k in df.sources_for([module]) if k in by_key]
    b = df.BLOCKING.get(module)
    bs = by_key.get(b) if b else None
    label = OWNER_LABEL.get(b, b) if b else None
    if bs is not None and bs.get("state") == "not_connected":
        if delivery == "unattended":
            return "refuse", f"no {label.lower()} on file", b
        return "caveat", f"no {label.lower()} on file", b
    if bs is not None and _down(bs):
        why = bs.get("error") or bs.get("basis") or f"{label} can't be confirmed current"
        if delivery == "unattended":
            return "refuse", why, b
        return "caveat", why, b
    live = [s for s in srcs if s.get("pct") is not None]
    errs = [s for s in srcs if s.get("error")]
    if errs:
        return "caveat", errs[0].get("error"), b
    if live and min(s["pct"] for s in live) < ce.CURRENT_AT:
        worst = min(live, key=lambda s: s["pct"])
        return "caveat", worst.get("basis") or f"{worst.get('label')} is not current", b
    return "proceed", "every source it rests on is current", b


def snapshot(restaurant_id, restaurant=None, ctx=None, db_path=None, now=None, use_cache=True) -> dict:
    """The Restaurant Data Health payload (web /api/data-health, its mobile
    twin, Home, Ask's read_data_health). Cached CACHE_TTL seconds per
    restaurant; any recorded sync clears it."""
    key = (restaurant_id, "snapshot")
    if use_cache and ctx is None and now is None:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    try:
        if restaurant is None:
            restaurant = (ctx.row() if ctx is not None else
                          (_models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path
                           else _models_mod.get_restaurant(restaurant_id)))
        if restaurant is None:
            return {"ok": False, "error": "Restaurant not found"}
        modules = enabled_modules(restaurant)
        keys = []
        for m in modules:
            for k in df.sources_for([m]):
                if k not in keys:
                    keys.append(k)
        states = _states(restaurant, keys, ctx=ctx, db_path=db_path, now=now)
        by_key = {s.get("key"): s for s in states}
        tz = df._get(restaurant, "timezone")
        pos_on = (by_key.get("pos") or {}).get("state") not in (None, "not_connected", "disconnected")
        lines = [source_line(s, now, tz=tz, pos_connected=pos_on) for s in states]
        connected = [l for l in lines if l["state"] != "not_connected"]
        not_connected = [{"key": l["key"], "label": l["label"], "next": _connect_hint(l["key"])}
                         for l in lines if l["state"] == "not_connected"]
        ov = overall(lines, modules)
        mods = []
        for m in modules:
            decision, reason, b = _decision(m, by_key)
            mods.append({"module": m, "title": module_title(m), "decision": decision, "reason": reason,
                         "blocking": b, "sources": list(df.sources_for([m])),
                         "confidence_impact": module_impact(restaurant_id, m, by_key, db_path=db_path)})
        scored = [l for l in connected if not l.get("pending")]
        worst = min(scored, key=lambda l: (l["health_pct"] if l["health_pct"] is not None else 101,
                                           l["label"])) if scored else None
        out = {"ok": True, "generated_at": _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"), "overall": ov,
               "worst_line": worst["line"] if worst and (worst["health_pct"] or 0) < 100 else None,
               "sources": connected, "not_connected": not_connected, "modules": mods,
               # How many sources may honestly be called current right now
               # (counts_as_current — the cadence rule, #27), and what
               # Account → Connections says about the POS and Google.
               "count_current": sum(1 for l in connected if l.get("counts_current")),
               "connections": connection_lines(restaurant, now=now, db_path=db_path)}
    except Exception as e:
        print(f"[data_health] snapshot failed for {restaurant_id}: {e}")
        return {"ok": False, "error": "Data health could not be read right now."}
    if use_cache and ctx is None and now is None:
        _cache_put(key, out)
    return out


def _connect_hint(key) -> str:
    return {"pos": "Connect your POS in Account → Connections",
            "labor": "Connect your POS or upload shifts on Labor",
            "sales": "Connect your POS in Account → Connections",
            "reviews": "Connect Google in Account → Connections",
            "inventory": "Count your inventory on Food Cost",
            "purchases": "Log a delivery on Food Cost",
            "waste": "Log waste on Food Cost",
            "prices": "Apply a supplier invoice on Food Cost",
            "marketing": "Connect Instagram or Facebook in Account → Connections",
            "competitor": "Refresh competitors on Intel",
            "visibility": "Run an AI visibility check on Intel",
            "weather": "Weather starts once your address is set",
            "dsr": "Turn on the daily report in Account"}.get(key, "Connect it in Account")


# ── before a model call ────────────────────────────────────────────────────

# The sources whose data is a period a sentence can call "this week".
_PERIOD_SOURCES = ("pos", "labor", "sales", "reviews", "marketing", "dsr")


def validation_state(states) -> dict:
    """The Response Validation Layer's data_state from registry states:
    stale_sources (labels of every stale, unknown or failing source — M1
    turns them into a disclosure), as_of (the stalest date, M/D/YY) and
    data_age_days (the oldest lag), so "this week" on old data is caught."""
    stale, ages, stalest, missing = [], [], None, []
    for s in states or ():
        if not s or s.get("state") == "not_connected":
            continue
        # Expected but never obtained (weather with no forecast fetched yet,
        # DH2-17) is a missing input, not out-of-date data: M2 holds any
        # claim about it, and nothing tells the owner old data is in use.
        if s.get("never_fetched") and not s.get("error"):
            if s.get("key") not in missing:
                missing.append(s.get("key"))
            continue
        if s.get("error") or s.get("state") in ("stale", "unknown"):
            label = OWNER_LABEL.get(s.get("key"), s.get("label") or s.get("key"))
            stale.append(f"{label}: {s.get('as_of')}" if s.get("as_of") else label)
        lag = s.get("lag_days")
        if isinstance(lag, (int, float)):
            ages.append(lag)
            if stalest is None or lag > (stalest.get("lag_days") or 0):
                stalest = s
    out = {"stale_sources": stale}
    if missing:
        out["missing_inputs"] = missing
    # Present tense only while current (DH5-3): a period source that is
    # not current — aging, stale, unknown or failing — is named here, and
    # the Response Validation Layer's M1 holds "this week" / "today" /
    # "currently" to its date. Counts, deliveries, the weather and the
    # weekly Intel reads are snapshots, not the week a claim describes.
    not_current = []
    for s in states or ():
        if not s or s.get("state") == "not_connected" or s.get("key") not in _PERIOD_SOURCES:
            continue
        if s.get("error") or s.get("state") != "current":
            not_current.append(OWNER_LABEL.get(s.get("key"), s.get("label") or s.get("key")))
    if not_current:
        out["not_current"] = not_current
    if ages:
        out["data_age_days"] = max(ages)
        if stalest is not None and stalest.get("as_of"):
            out["as_of"] = stalest.get("as_of")
    return out


def prompt_block(states) -> str:
    """The DATA STATE block a prompt carries, so the model knows the age and
    health of what it reads before it writes (DH1-2, DH5-2)."""
    rows = []
    for s in states or ():
        if not s:
            continue
        label = OWNER_LABEL.get(s.get("key"), s.get("label") or s.get("key"))
        if s.get("state") == "not_connected":
            rows.append(f"- {label}: not connected — say nothing about it")
            continue
        word = {"current": "current", "aging": "aging — say how old", "stale": "OUT OF DATE — say so",
                "unknown": "AGE UNKNOWN — do not present as current"}.get(s.get("state"), s.get("state") or "")
        err = f"; FAILING: {s.get('error')}" if s.get("error") else ""
        rows.append(f"- {label}: {s.get('basis') or ''} — {word}{err}".replace(":  —", ": —"))
    if not rows:
        return ""
    return ("DATA STATE (how current each source behind this is — when you use a source that is not "
            "current, say how old it is; never describe out-of-date data as this week, today or current):\n"
            + "\n".join(rows))


def readiness(restaurant_id, module, ctx=None, delivery="interactive", restaurant=None, db_path=None,
              now=None, sources=None, include_not_connected=True) -> dict:
    """Asked before a model call: {decision, module, reason, blocking,
    sources (owner lines), data_state (for ValidationContext.data_state),
    prompt_block (for the prompt), retry_after}.

    decision, first match wins:
      refuse   unattended only: the module's blocking source is not
               connected, or its age is unknown / past its horizon / its
               credentials were refused. (Interactive surfaces caveat.)
      wait     unattended only: the blocking source is failing and a retry
               is scheduled (source_health.next_retry_at).
      caveat   any source is failing or under CURRENT_AT.
      proceed  otherwise.
    `include_not_connected=False` leaves sources that are not connected out
    of the prompt block (a read built from its own inputs, like the nightly
    report, where "not connected — say nothing about it" would silence it).
    Never raises: an unreadable state proceeds with an empty block, and the
    validation layer still runs."""
    try:
        keys = tuple(sources) if sources else df.sources_for([module])
        if restaurant is None and ctx is None:
            restaurant = (_models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path
                          else _models_mod.get_restaurant(restaurant_id))
        if restaurant is None and ctx is not None:
            restaurant = ctx.row()
        states = _states(restaurant, keys, ctx=ctx, db_path=db_path, now=now)
        by_key = {s.get("key"): s for s in states}
        decision, reason, b = _decision(module, by_key, delivery)
        retry_after = None
        bs = by_key.get(b) if b else None
        if delivery == "unattended" and bs is not None and bs.get("error") and bs.get("next_retry_at") \
                and decision in ("caveat", "refuse") and bs.get("error_class") != "auth":
            decision, retry_after = "wait", bs.get("next_retry_at")
        return {"decision": decision, "module": module, "reason": reason, "blocking": b,
                "sources": [source_line(s, now)["line"] for s in states], "data_state": validation_state(states),
                "prompt_block": prompt_block(states if include_not_connected else
                                             [s for s in states if s.get("state") != "not_connected"]),
                "retry_after": retry_after}
    except Exception as e:
        print(f"[data_health] readiness failed for {restaurant_id}/{module}: {e}")
        return {"decision": "proceed", "module": module, "reason": "data state unreadable", "blocking": None,
                "sources": [], "data_state": {}, "prompt_block": "", "retry_after": None}


def unattended_readiness(restaurant_id, module, ctx=None, db_path=None, **kw) -> dict:
    """readiness(delivery="unattended") for a job that already checks it has
    data (a diagnosis needs drivers or a complaint cluster, the digest a
    live module): a blocking source that is NOT CONNECTED is left to that
    check — its refuse becomes a caveat — so only data that is there but
    can't be stood on (unknown age, past its horizon, credentials refused,
    a retry due) holds the output. Never raises."""
    try:
        if ctx is None:
            import rec_trust
            ctx = rec_trust.Context(restaurant_id, db_path=db_path)
        rd = readiness(restaurant_id, module, ctx=ctx, delivery="unattended", db_path=db_path, **kw)
        b = rd.get("blocking")
        if rd.get("decision") == "refuse" and b and \
                (ctx.sources((b,)) or [{}])[0].get("state") == "not_connected":
            rd = dict(rd, decision="caveat")
        return rd
    except Exception as e:
        print(f"[data_health] unattended readiness unreadable for {restaurant_id}/{module}: {e}")
        return {"decision": "proceed", "module": module, "reason": "data state unreadable", "blocking": None,
                "sources": [], "data_state": {}, "prompt_block": "", "retry_after": None}


def unattended_hold(restaurant_id, module, ctx=None, db_path=None):
    """Why unattended output (the digest, the weekly plan) leaves `module`
    out this run, or None — unattended_readiness said refuse or wait — per
    module, so the rest of the output still goes. Never raises (None)."""
    rd = unattended_readiness(restaurant_id, module, ctx=ctx, db_path=db_path)
    if rd.get("decision") not in ("refuse", "wait"):
        return None
    return str(rd.get("reason") or "its data isn't current")


def merge_data_state(base, extra) -> dict:
    """A caller's own data_state with readiness's folded in: lists joined
    without repeats, the older age and its as-of kept."""
    out = dict(base or {})
    extra = extra or {}
    for k in ("stale_sources", "required_disclosures", "partial_flags", "missing_inputs", "not_current"):
        vals = list(out.get(k) or [])
        for v in extra.get(k) or []:
            if v not in vals:
                vals.append(v)
        if vals:
            out[k] = vals
    a, b = out.get("data_age_days"), extra.get("data_age_days")
    if isinstance(b, (int, float)) and (not isinstance(a, (int, float)) or b > a):
        out["data_age_days"] = b
        if extra.get("as_of"):
            out["as_of"] = extra["as_of"]
    return out


# ── the daily record ───────────────────────────────────────────────────────

def record_daily(restaurant_id, day, db_path=None) -> bool:
    """One snapshot a day (data_health_daily) for the admin rollup and trend
    lines, so nothing recomputes every restaurant live."""
    snap = snapshot(restaurant_id, db_path=db_path, use_cache=False)
    if not snap.get("ok"):
        return False
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        compact = [{"key": s["key"], "state": s["state"], "pct": s["pct"], "health_pct": s["health_pct"],
                    "error": s["error"]} for s in snap.get("sources") or []]
        conn.execute("INSERT OR REPLACE INTO data_health_daily (restaurant_id, date, overall, sources_json) "
                     "VALUES (?,?,?,?)", (restaurant_id, str(day)[:10], (snap.get("overall") or {}).get("pct"),
                                         json.dumps(compact)))
        conn.commit()
        return True
    except Exception as e:
        print(f"[data_health] record_daily failed for {restaurant_id}: {e}")
        return False
    finally:
        conn.close()


def payload_for(user) -> dict:
    """The route body both /api/data-health and /mobile/api/data-health
    return, scoped to the login's restaurant."""
    rid = (user or {}).get("restaurant_id")
    if not rid:
        return {"ok": False, "error": "No restaurant on this login."}
    return snapshot(rid)
