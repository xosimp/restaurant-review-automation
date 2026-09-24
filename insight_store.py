"""
insight_store.py — one stored model read per restaurant and data, and the
recommendation lines inside it.

Two problems the recommendation-trust audit found in every module-level AI
insight (Reviews, Food Cost, Marketing):

  #22  The web and the phone kept separate in-memory caches for the same
       read (`inv-insight:` vs `mobile-inv-insight:`, `mkt-insight:` vs
       `mobile-mkt-insight:`), each with a five-minute life. The same figures
       produced two different model answers — one on the laptop, another on
       the phone — and a third five minutes later. Here there is ONE read
       per (restaurant, kind, data fingerprint), kept in the database: it is
       regenerated only when the data it was written from changes, survives
       a deploy, and every client reads the same words.

  #21  A model-written recommendation had no identity. Nothing logged that
       it was shown, and the owner had no way to say "done" or "not for us",
       so a line they had rejected came back on every load. Each line now
       gets a stable key ("insight_food:<hash>"), is `present()`ed to
       rec_ledger when shown, and a line the owner has answered is not shown
       again (rec_ledger.present returns None for it).

Pure SQL plus rec_ledger; no model, no network.
"""
import hashlib
import json
import re
from datetime import datetime

import models as _models_mod
from models import DB_PATH

# A stored read older than this is regenerated even if the data did not
# change: the prose names "this week" and "today", which move on their own.
MAX_AGE_HOURS = 24 * 7


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_insight_store(db_path: str = DB_PATH):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS insight_cache (
            restaurant_id  INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            fingerprint    TEXT NOT NULL,
            payload        TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, kind)
        )""")
        conn.commit()
    finally:
        conn.close()


def fingerprint(*parts) -> str:
    """A short hash of whatever the read was written from."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _validation_version():
    import response_validation
    return response_validation.VERSION


def _is_wrapped(stored) -> bool:
    return isinstance(stored, dict) and "_rv" in stored and "out" in stored


def get(restaurant_id, kind, fp, db_path=DB_PATH, revalidate=None):
    """The stored read for exactly this data, or None.

    A read stored with its model text (`put(..., raw=)`, every call site
    that runs the Response Validation Layer) carries the engine version it
    was validated under. When that version is no longer current, the stored
    model text is re-validated with `revalidate(raw)` — no model call — and
    the new result replaces the old one (its age unchanged); without a
    `revalidate` it is not served. A read stored before the engine (no
    version) is re-validated from its own text, its old "UNVERIFIED:" line
    removed first, when `revalidate` is given."""
    row = _row(restaurant_id, kind, db_path)
    if not row or row["fingerprint"] != fp:
        return None
    try:
        when = datetime.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
        if (datetime.utcnow() - when).total_seconds() > MAX_AGE_HOURS * 3600:
            return None
    except (TypeError, ValueError):
        return None
    try:
        stored = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if _is_wrapped(stored):
        if stored["_rv"] == _validation_version():
            return _restore(stored)
        if revalidate is None:
            return None
        raw = stored.get("raw")
    elif revalidate is None:
        return stored
    elif isinstance(stored, str):
        import response_validation
        raw = response_validation.strip_marker(stored)
    else:
        return None           # a pre-engine structured read: regenerate it
    try:
        out = revalidate(raw)
    except Exception as e:
        print(f"[insight_store] re-validation failed: {e}")
        return None
    if out is None:
        return None
    _rewrap(restaurant_id, kind, raw, out, db_path)
    return out


def _restore(stored):
    """The stored output, with its validation object back on a text."""
    out = stored.get("out")
    if isinstance(out, str):
        import response_validation
        return response_validation.Validated(out, validation=stored.get("validation"))
    return out


def _wrap(raw, out):
    import response_validation
    return {"_rv": _validation_version(), "raw": raw, "out": out,
            "validation": response_validation.validation_of(out)}


def _rewrap(restaurant_id, kind, raw, out, db_path):
    try:
        conn = get_conn(db_path)
    except Exception:
        return
    try:
        conn.execute("UPDATE insight_cache SET payload=? WHERE restaurant_id=? AND kind=?",
                     (json.dumps(_wrap(raw, out), default=str), restaurant_id, kind))
        conn.commit()
    except Exception as e:
        print(f"[insight_store] re-validated write failed: {e}")
    finally:
        conn.close()


def latest(restaurant_id, kind, db_path=DB_PATH):
    """(payload, created_at) of the last stored read whatever its data, for
    the "a stale read beats no read" fallback when generation fails."""
    row = _row(restaurant_id, kind, db_path)
    if not row:
        return None, None
    try:
        stored = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None, None
    return (_restore(stored) if _is_wrapped(stored) else stored), row["created_at"]


def _row(restaurant_id, kind, db_path):
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        return conn.execute("SELECT fingerprint, payload, created_at FROM insight_cache "
                            "WHERE restaurant_id=? AND kind=?", (restaurant_id, kind)).fetchone()
    except Exception as e:
        print(f"[insight_store] read failed: {e}")
        return None
    finally:
        conn.close()


def put(restaurant_id, kind, fp, payload, db_path=DB_PATH, raw=None) -> bool:
    """Store the read. Never raises: a cache write must not fail a page.
    With `raw` (the model's text before validation), the read is stored
    with the validation engine's version, so a later version re-validates
    it from `raw` instead of serving the old verdict (see get)."""
    if raw is not None:
        payload = _wrap(raw, payload)
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        conn.execute("INSERT INTO insight_cache (restaurant_id, kind, fingerprint, payload, created_at) "
                     "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, kind) DO UPDATE SET "
                     "fingerprint=excluded.fingerprint, payload=excluded.payload, created_at=excluded.created_at",
                     (restaurant_id, kind, fp, json.dumps(payload, default=str)))
        conn.commit()
        return True
    except Exception as e:
        print(f"[insight_store] write failed: {e}")
        return False
    finally:
        conn.close()


def invalidate(restaurant_id, kinds=None, db_path=DB_PATH):
    try:
        conn = get_conn(db_path)
    except Exception:
        return
    try:
        if kinds:
            conn.executemany("DELETE FROM insight_cache WHERE restaurant_id=? AND kind=?",
                             [(restaurant_id, k) for k in kinds])
        else:
            conn.execute("DELETE FROM insight_cache WHERE restaurant_id=?", (restaurant_id,))
        conn.commit()
    except Exception as e:
        print(f"[insight_store] invalidate failed: {e}")
    finally:
        conn.close()


# ── recommendation lines ────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return re.sub(r"[^\w$%.\s-]", "", t)


_LINE_KEY_RE = re.compile(r"^[a-z_]+:[0-9a-f]{10}$")


def line_key(prefix: str, text: str) -> str:
    """A stable key for one model-written line: the same words (ignoring
    case, spacing and punctuation) are the same recommendation, so an answer
    holds when the read is regenerated with the same line."""
    return f"{prefix}:{hashlib.sha1(_normalise(text).encode()).hexdigest()[:10]}"


_NUMBERED = re.compile(r"^\s*(\d)[.)]\s+(.*\S)\s*$")


def numbered_lines(text: str) -> list:
    """The numbered recommendation lines ("1. …") of a model read."""
    out = []
    for line in str(text or "").splitlines():
        m = _NUMBERED.match(line)
        if m:
            out.append(m.group(2))
    return out


def present_recs(restaurant_id, module, surface, items, user_id=None, db_path=DB_PATH) -> list:
    """Log each recommendation as shown and return the ones still to show.

    items: [{key, text, ...rec_ledger attrs}]. Each returned item carries
    `key` and `rec_id`. An item the owner has already answered (rec_ledger
    returned None) is dropped. If the ledger itself is unavailable every item
    is returned with rec_id None — measurement never hides a recommendation."""
    if not items:
        return []
    try:
        import rec_ledger
        batch = [{"key": it["key"], "module": module, "title": (it.get("title") or it.get("text") or "")[:200],
                  "dollar_value": it.get("dollar_value"), "confidence_band": it.get("confidence_band"),
                  # The measured confidence it is shown with (K1), for the
                  # ledger's snapshot at delivery (K3).
                  "confidence": it.get("confidence") if isinstance(it.get("confidence"), dict) else None,
                  "evidence_sources": it.get("evidence_sources") or [module],
                  "model_written": it.get("model_written", True),
                  "cavnar_completes": it.get("cavnar_completes", False),
                  "expected_metric": it.get("expected_metric")} for it in items]
        # A read's numbered lines are keyed by a hash of their words
        # (line_key), so a regenerated read is a NEW set of keys: the old
        # read's unanswered lines were replaced, not ignored, and close as
        # superseded (rec_ledger, ROI #37). Only a read's own lines
        # (insight_<module>): a key that names a subject (reprice:<dish>, a
        # diagnosis) is not a read's line and replaces nothing.
        replaces = sorted({rec_ledger.kind_of(it["key"]) for it in items
                           if _LINE_KEY_RE.match(str(it["key"])) and str(it["key"]).startswith("insight_")})
        ids = rec_ledger.present_many(restaurant_id, batch, surface, user_id=user_id, db_path=db_path,
                                      replaces=replaces)
    except Exception as e:
        print(f"[insight_store] present failed: {e}")
        ids = {}
    out = []
    for it in items:
        if it["key"] in ids and ids[it["key"]] is None:
            continue          # answered: silenced everywhere
        out.append(dict(it, rec_id=ids.get(it["key"])))
    return out


def answered_lines(restaurant_id, prefixes, limit=12, db_path=DB_PATH) -> list:
    """The words of the lines the owner has answered (Done, Not for us,
    Track) under these key prefixes, while the answer still holds.

    A line's key is a hash of its exact text, so an answer silenced only
    those words: the read is regenerated at least daily, a rephrased line
    got a new key, and the same advice came back the next day (M-8). The
    insight prompts pass these to the model as "do not suggest again".
    Sorted by key so the same answers give the same prompt (the stored
    read's fingerprint)."""
    if not restaurant_id or not prefixes:
        return []
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        where = " OR ".join("key LIKE ?" for _ in prefixes)
        rows = conn.execute(
            f"SELECT key, title FROM rec_instances WHERE restaurant_id=? AND ({where}) "
            "AND title IS NOT NULL AND TRIM(title) != '' "
            "AND silenced_until IS NOT NULL AND silenced_until > datetime('now') "
            "AND status IN ('completed', 'dismissed', 'accepted') "
            "ORDER BY key LIMIT ?",
            (restaurant_id, *[f"{p}:%" for p in prefixes], int(limit))).fetchall()
    except Exception as e:
        print(f"[insight_store] answered lines failed: {e}")
        return []
    finally:
        conn.close()
    seen, out = set(), []
    for r in rows:
        t = re.sub(r"\s+", " ", str(r["title"])).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:200])
    return out


def do_not_repeat_block(restaurant_id, prefixes, db_path=DB_PATH) -> str:
    """The prompt section carrying answered_lines, or "" when there are none."""
    lines = answered_lines(restaurant_id, prefixes, db_path=db_path)
    if not lines:
        return ""
    return ("\n\nALREADY ANSWERED - the owner has already answered these suggestions (done them, or "
            "said they are not for this restaurant). Do NOT suggest any of them again, in these words "
            "or in any other words:\n" + "\n".join(f"- {t}" for t in lines))


def answered(restaurant_id, keys, db_path=DB_PATH) -> set:
    """The subset of `keys` the owner has answered (silenced everywhere)."""
    try:
        import rec_ledger
        s = rec_ledger.silenced_keys(restaurant_id, db_path=db_path)
    except Exception:
        return set()
    return {k for k in keys if k in s}


# ── one piece of advice across surfaces (H16) ───────────────────────────────
#
# "Not for us" held only for the key it was said to. Home's "Trim Tuesday
# staffing" is trim_day:Tuesday; the nightly report's "cut a server from
# Tuesday dinner" is dsr_action:adjust_staffing:labor; the Reviews read's
# "Do today" line was a hash of its own words. Declining one never stopped
# the others saying the same thing in other words. An advice signature is
# what the advice is ABOUT — a lever family and the one thing it names (a
# weekday, an item, a dish, a review theme) — read from the key's subject
# tags (rec_ledger.tags_for) and, for a key that carries no subject (a
# hashed line, a DSR action on a whole block), from its words. Two pieces
# of advice with the same signature are the same advice; a declined
# signature is dropped server-side on every surface that checks it.

# rec_ledger topics folded into the lever families a signature compares:
# trimming a day's staffing and cutting that day's hours are one piece of
# advice however each surface files it.
_SIG_FAMILY = {"staffing": "labor", "hours": "labor", "overtime": "labor",
               "replies": "replies", "guest_experience": "guest_experience",
               "waste": "waste", "ordering": "ordering", "purchasing": "ordering",
               "pricing": "pricing", "posting": "marketing", "marketing": "marketing",
               "guest_outreach": "guest_outreach", "training": "training", "food_cost": "food_cost",
               "sales": "sales", "competition": "competition", "visibility": "visibility"}
# Only for a key whose tags carry no topic. The earliest match in the text wins.
_SIG_TEXT_TOPICS = (
    ("replies", r"\b(?:respond\w*|repl(?:y|ies|ied)|response)\b"),
    ("labor", r"\b(?:staff\w*|schedul\w*|shifts?|headcount|trim\w*|overstaff\w*|understaff\w*|labor|"
              r"overtime|servers?|cooks?|bussers?|openers?|closers?)\b"),
    ("ordering", r"\b(?:order\w*|reorder\w*|restock\w*|pars?)\b"),
    ("waste", r"\b(?:waste\w*|spoil\w*|portion\w*)\b"),
    ("pricing", r"\b(?:pric\w*|reprice\w*)\b"),
    ("marketing", r"\b(?:post\w*|instagram|facebook|caption\w*|promot\w*)\b"),
    ("training", r"\b(?:train\w*|coach\w*|huddle\w*|pre-shift)\b"),
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Kinds whose advice is about the whole schedule, never one weekday
# (business_intelligence's schedule_to_target — its own key so it is not
# Home's trim_day:<day>; the Improve-with-Cavnar optimizer proposal).
_WHOLE_SCHEDULE_KINDS = ("schedule_to_target", "optimizer")
# Keys whose kind is a model-written line rather than a lever (besides insight_*).
_MODEL_LINE_KINDS = ("digest_move", "monthly_move", "ask_tip")
# How long a "not for us" silences (rec_ledger.SILENCE_DAYS: ten years) against
# a plain hide (14 days): a silence past this is a decline.
_DECLINE_MIN_DAYS = 60


def _text_topic(text):
    low = str(text or "").lower()
    best = None
    for family, pat in _SIG_TEXT_TOPICS:
        m = re.search(pat, low)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), family)
    return best[1] if best else None


def _text_subject(text):
    low = str(text or "").lower()
    for d in _WEEKDAYS:
        if re.search(rf"(?<![a-z]){d}(?:s|'s|’s)?(?![a-z])", low):
            return f"day:{d}"
    try:
        from analyser import CATEGORIES, category_label
        for c in CATEGORIES:
            label = category_label(c)
            if re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", low):
                return f"category:{c}"
    except Exception:
        pass
    return None


def advice_signature(key, text=None):
    """"<family>:<subject>" — what one recommendation is about, the same for
    the same advice on every surface (trim_day:Tuesday and a DSR action to
    cut Tuesday's hours are both "labor:day:tuesday"), or None when the key
    and its words do not name both a lever and a single subject. Never a
    bare lever ("labor"): declining one Tuesday cut is not declining all
    staffing advice."""
    try:
        import rec_ledger
        tags = rec_ledger.tags_for(key)
    except Exception:
        tags = []
    topic = next((t.split(":", 1)[1] for t in tags if t.startswith("topic:")), None)
    family = _SIG_FAMILY.get(topic) if topic else None
    # A model-written line's kind names only the module it was read on
    # (insight_review is "guest_experience" whatever the line says), so its
    # own words say what lever it pulls: "cut a server Tuesday" on the
    # Reviews read is the same advice as Home's trim_day:Tuesday.
    kind = str(key or "").split(":", 1)[0]
    if text and (kind.startswith("insight_") or kind in _MODEL_LINE_KINDS):
        family = _text_topic(text) or family
    family = family or _text_topic(text)
    subject = next((t for t in tags if t.startswith(("day:", "item:", "dish:", "category:"))), None)
    # A whole-schedule recommendation names a weekday only as where to
    # START ("…to your 30% target, starting with Tuesday"): it is not that
    # day's trim. Its subject is the whole schedule, so declining Tuesday's
    # trim never suppresses it, nor it Tuesday's (T2, B4 L4).
    if kind in _WHOLE_SCHEDULE_KINDS:
        subject = "schedule:whole"
    subject = subject or _text_subject(text)
    if not family or not subject:
        return None
    return f"{family}:{subject}"


def signature_key(prefix: str, text: str) -> str:
    """The key for a model-written line that names what it is about —
    "<prefix>:<signature>" (insight_review:replies:category:food_quality) —
    so the same advice tomorrow in other words is the same key. A line whose
    signature cannot be read keeps line_key's hash."""
    sig = advice_signature(f"{prefix}:x", text)
    return f"{prefix}:{sig}"[:160] if sig else line_key(prefix, text)


def declined_signatures(restaurant_id, db_path=DB_PATH) -> set:
    """The advice signatures of every recommendation this restaurant said
    "not for us" to, on any surface, while the answer holds. A plain hide
    (two weeks) is not a decline and is not carried across."""
    if not restaurant_id:
        return set()
    try:
        conn = get_conn(db_path)
    except Exception:
        return set()
    try:
        rows = conn.execute(
            "SELECT key, title FROM rec_instances WHERE restaurant_id=? AND status='dismissed' "
            "AND silenced_until IS NOT NULL AND silenced_until > datetime('now', ?)",
            (restaurant_id, f"+{_DECLINE_MIN_DAYS} days")).fetchall()
    except Exception as e:
        print(f"[insight_store] declined signatures failed: {e}")
        return set()
    finally:
        conn.close()
    out = set()
    for r in rows:
        sig = advice_signature(r["key"], r["title"])
        if sig:
            out.add(sig)
    return out


# ── forecasts the model used to write (H8) ──────────────────────────────────
#
# The labor note, the marketing brief and the Reviews read each ended on a
# FORECAST / "Next week" line the model wrote and nothing ever scored. They
# are computed in Python now, and each is logged to forecast_log — the table
# food cost's waste forecast already uses — once per ISO week, the rule
# food_cost_intelligence.record_profitability_forecast follows: a projection
# re-recorded on every page open converges on the actual and scores itself
# perfect. Kinds, each recorded in the unit forecast_log scores it in:
# labor_week (labor % of sales for the ISO week), marketing_reach_week (the
# week's posts' reach SUMMED — not the per-post figure the brief shows) and
# review_rating_week (the week's mean star rating). Scoring belongs to
# forecast_log's own helpers; record_weekly_forecast is the one adapter the
# three callers go through, so it can be pointed at forecast_log.record(rid,
# kind, value, period_of=next_week_end(today)) without touching them.
WEEKLY_FORECAST_KINDS = ("labor_week", "marketing_reach_week", "review_rating_week")


def next_week_end(today=None):
    """The Sunday that ends the ISO week after `today`'s — what "next week"
    predicts through."""
    from datetime import date, timedelta
    today = today or date.today()
    return today + timedelta(days=(6 - today.weekday()) + 7)


def record_weekly_forecast(restaurant_id, kind, predicted, basis=None, today=None, db_path=DB_PATH) -> dict:
    """Freeze one forecast for next week, once, through forecast_log.record —
    the one writer every forecast kind goes through, so its insert-once and
    period-key rules apply unchanged and scheduler.run_forecast_scoring
    scores the row once its week closes. Returns {"recorded": bool,
    "horizon_end", "reason"?}. Never raises: a forecast that could not be
    logged is still shown and says nothing it would not otherwise."""
    if kind not in WEEKLY_FORECAST_KINDS or predicted is None or not restaurant_id:
        return {"recorded": False, "reason": "nothing to record"}
    try:
        import forecast_log
        out = forecast_log.record(restaurant_id, kind, predicted, period_of=next_week_end(today),
                                  basis=(basis or "")[:300] or None, db_path=db_path)
        if not out.get("recorded") and out.get("horizon_end"):
            return {"recorded": False, "horizon_end": out["horizon_end"], "reason": "already frozen this week"}
        return out
    except Exception as e:
        print(f"[insight_store] forecast not recorded rid={restaurant_id} {kind}: {e}")
        return {"recorded": False, "reason": "could not be stored"}
