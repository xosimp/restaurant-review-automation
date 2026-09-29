"""ai_reads — Cavnar AI's history of its own reasoning (memory audit 9/29/26:
ai_reads, claims, dsr_own, stale_diagnoses).

Every read the product wrote used to be overwritten: insight_cache holds one
row per (restaurant, kind), both diagnosis tables upsert per subject, and the
digest, the brief and the monthly review kept only a subject line. So
nothing could ask whether a cause Cavnar AI named was right, and no prompt
knew what it had said the day before. This module keeps the history:

  ai_reads            append-only: one row per read that was WRITTEN (a
                      module read, a diagnosis, the nightly report's
                      narrative, a brief, a digest, the monthly review) —
                      surface, subject, the shown and the raw text, the
                      validation verdict, a hash of the facts it was written
                      from, the ids it cited and the recommendation keys it
                      carried. A read re-served unchanged is not a new row;
                      a new read supersedes the previous one of its surface
                      and subject (superseded_at / superseded_by). The
                      insight_cache and diagnosis tables stay as the
                      "current" pointers the screens read.
  ai_claims           the checkable claims inside those reads: a
                      diagnosis's cause and the outcome it expected ("if the
                      cause is right, cold-food complaints fall within two
                      weeks"), a nightly-report action — each with the
                      metric that settles it, the expected direction, a
                      horizon, the model's own band and the capped one. One
                      open claim per (surface, subject) at a time — a
                      diagnosis restated daily is ONE claim until its
                      horizon, never thirty (forecast_log's insert-once
                      rule). The nightly learning job scores each at its
                      horizon (score_due): measured over the days after the
                      read against the same number of days before, with
                      this restaurant's own noise band (metrics.compare),
                      or the linked tracker's verdict when the advice was
                      tracked. A claim whose advice was never taken is
                      "untested", never "wrong".
  ai_read_summaries   one row per quarter, surface and subject — "what we
                      said, what was done, what happened" — kept forever;
                      the raw reads and claims are kept 13 months
                      (ops._RETENTION_DAYS) and summarised before then.

Read back three ways: recent_reads (Ask's "what did you tell me", the
digest), claim_lines (the memory_context provider: the next prompt on the
same subject gets "LAST READ (9/2/26): cause X; recommended Y" and what
happened since, before and after, never proof), and claims_record (the
diagnosis kinds' Historical Accuracy, rec_learning.kind_record, and the
admin check of the model's own confidence).

Never raises into the caller's write: a history row that could not be kept
must not fail a page, a diagnosis or a send.
"""
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# ── what is kept ─────────────────────────────────────────────────────────────

# Raw reads and claims are kept 13 months (ops._RETENTION_DAYS["ai_reads"] /
# ["ai_claims"], 400 days); each closed quarter is summarised into
# ai_read_summaries (kept forever) long before its rows go.
RAW_KEEP_DAYS = 400
# Stored text is capped: a read is a paragraph, and a runaway payload must
# not grow the volume.
MAX_SHOWN_CHARS = 4000
MAX_RAW_CHARS = 6000
MAX_SUMMARY_CHARS = 280

# The surfaces a read is filed under — the memory_context surface names
# where one exists, so a prompt asking "what did this surface say last time"
# reads its own rows.
SURFACE_LABELS = {
    "food_read": "the Food Cost read",
    "review_read": "the Reviews read",
    "marketing_read": "the Marketing read",
    "labor_read": "the Labor read",
    "marketing_feed": "the Marketing feed",
    "review_diagnosis": "the review diagnosis",
    "food_diagnosis": "the food cost diagnosis",
    "monthly_review": "the monthly review",
    "dsr_narrative": "the nightly report",
    "brief": "the morning brief",
    "digest": "the weekly digest",
    "competitor_read": "the competitor read",
    "weekly_plan": "the Monday plan",
}
# insight_cache kind -> the surface its read is filed under (insight_store.put).
STORE_SURFACE = {"food": "food_read", "reviews": "review_read", "marketing": "marketing_read",
                 "labor": "labor_read", "mkt_opps": "marketing_feed"}
# The recommendation-line prefix each stored read's numbered lines carry
# (client_api.present_read_lines), for the read's rec_keys.
STORE_LINE_PREFIX = {"food": "insight_food", "marketing": "insight_marketing", "labor": "insight_labor"}


# ── the claims ───────────────────────────────────────────────────────────────

CLAIM_TYPES = ("cause", "action", "forecast")
# A claim's horizon: the model says "roughly when" in words ("within two
# weeks"), read here and held inside these bounds; without one, the
# metric's own default comparison window.
HORIZON_MIN_DAYS = 14
HORIZON_MAX_DAYS = 60
DEFAULT_HORIZON_DAYS = 28
# A closed horizon whose number still cannot be read this long after is
# given up on (forecast_log.UNSCORABLE_AFTER_DAYS).
UNSCORABLE_AFTER_DAYS = 21
# How far back a prompt's "last read" looks, and the interim reading's floor.
CLAIM_LOOKBACK_DAYS = 180
INTERIM_MIN_DAYS = 7
# Verdicts. `untested`: the advice was not taken, so the number moving says
# nothing about the cause. `unmeasurable`: the number could not be read.
HELD, NOT_HELD, UNTESTED, UNMEASURABLE = "held", "not_held", "untested", "unmeasurable"
VERDICTS = (HELD, NOT_HELD, UNTESTED, UNMEASURABLE)
# Kinds whose Historical Accuracy counts scored claims (rec_learning.kind_record):
# the advice a read's claim carried, answered under this rec key kind.
CLAIM_KINDS = ("diag_review", "diag_food", "dsr_action")
CAVEAT = "before and after, not proof"


def init_ai_reads(db_path: str = DB_PATH):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_reads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            surface         TEXT    NOT NULL,
            kind            TEXT,
            subject         TEXT,
            fingerprint     TEXT,
            text_hash       TEXT    NOT NULL,
            shown_text      TEXT,
            raw_text        TEXT,
            verdict         TEXT,
            facts_hash      TEXT,
            cited_ids       TEXT,
            rec_keys        TEXT,
            meta            TEXT,
            call_id         TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            superseded_at   TEXT,
            superseded_by   INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_reads_subject ON ai_reads(restaurant_id, surface, subject, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_reads_rest_created ON ai_reads(restaurant_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_reads_created ON ai_reads(created_at)")      # retention
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_claims (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            read_id         INTEGER,
            last_read_id    INTEGER,
            surface         TEXT    NOT NULL,
            subject         TEXT    NOT NULL,
            signature       TEXT,
            claim_type      TEXT    NOT NULL,
            text            TEXT    NOT NULL,
            last_text       TEXT,
            action          TEXT,
            expected        TEXT,
            rec_key         TEXT,
            metric          TEXT,
            direction       TEXT,
            baseline_value  REAL,
            baseline_start  TEXT,
            baseline_end    TEXT,
            after_start     TEXT,
            horizon_date    TEXT,
            model_band      TEXT,
            capped_band     TEXT,
            restated_n      INTEGER NOT NULL DEFAULT 0,
            restated_at     TEXT,
            verdict         TEXT,
            verdict_value   REAL,
            verdict_basis   TEXT,
            verdict_source  TEXT,
            action_state    TEXT,
            tracker_id      INTEGER,
            verdict_at      TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            superseded_at   TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_claims_subject ON ai_claims(restaurant_id, surface, subject, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_claims_due ON ai_claims(restaurant_id, verdict, horizon_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_claims_rec ON ai_claims(restaurant_id, rec_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_claims_created ON ai_claims(created_at)")    # retention
        conn.execute("""CREATE TABLE IF NOT EXISTS ai_read_summaries (
            restaurant_id   INTEGER NOT NULL,
            quarter         TEXT    NOT NULL,
            surface         TEXT    NOT NULL,
            subject         TEXT    NOT NULL DEFAULT '',
            reads_n         INTEGER NOT NULL DEFAULT 0,
            first_at        TEXT,
            last_at         TEXT,
            said            TEXT,
            done            TEXT,
            happened        TEXT,
            claims_n        INTEGER NOT NULL DEFAULT 0,
            held_n          INTEGER NOT NULL DEFAULT 0,
            not_held_n      INTEGER NOT NULL DEFAULT 0,
            untested_n      INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, quarter, surface, subject)
        )""")
        conn.commit()
    finally:
        conn.close()


# ── small helpers ────────────────────────────────────────────────────────────

def _now_stamp():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _day(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _mdy(v):
    try:
        from time_utils import mdy
        return mdy(v)
    except Exception:
        return str(v or "")[:10]


def _clip(text, n):
    t = str(text or "")
    return t if len(t) <= n else t[:n - 1] + "…"


def _one_line(text, n=MAX_SUMMARY_CHARS):
    return _clip(" ".join(str(text or "").split()), n)


def _hash(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:24]


def _json(v):
    if v in (None, "", [], {}):
        return None
    try:
        return json.dumps(v, default=str)[:20000]
    except (TypeError, ValueError):
        return None


def _loads(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _local_today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def shown_text_of(payload) -> str:
    """The words an owner was shown for a stored read — a text, a read
    payload's `insight`, or a feed's card titles."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return str(payload)
    if isinstance(payload, dict):
        if isinstance(payload.get("insight"), str):
            return payload["insight"]
        cards = payload.get("cards") or payload.get("items")
        if isinstance(cards, list):
            titles = [str(c.get("title") or c.get("text") or c.get("headline") or "")
                      for c in cards if isinstance(c, dict)]
            return "\n".join(f"- {t}" for t in titles if t.strip())
        if isinstance(payload.get("text"), str):
            return payload["text"]
    try:
        return json.dumps(payload, default=str)[:MAX_SHOWN_CHARS]
    except (TypeError, ValueError):
        return str(payload)[:MAX_SHOWN_CHARS]


def verdict_of(payload):
    """The Response Validation Layer's verdict a stored read carried, or None."""
    val = None
    try:
        import response_validation
        val = response_validation.validation_of(payload)
    except Exception:
        val = None
    if val is None and isinstance(payload, dict):
        val = payload.get("validation")
    if isinstance(val, dict):
        return val.get("verdict")
    return None


# ── writing a read ───────────────────────────────────────────────────────────

def record_read(restaurant_id, surface, text, subject=None, meta=None, call_id=None, db_path=None):
    """Keep one read (a module read, a diagnosis, a digest, a brief, a DSR
    narrative) as history instead of overwriting it. Returns its id or None.

    `text` is what the owner was shown. `meta` (all optional): kind,
    fingerprint (the data it was written from), raw (the model's text
    before validation), verdict, facts_hash, cited_ids, rec_keys,
    model_band, capped_band, summary, and `claims` — [{claim_type, text,
    action, expected, rec_key, metric, direction, horizon_days, model_band,
    capped_band, signature, subject}] — the checkable claims inside it. A
    nightly-report read ("dsr_narrative") may pass its narrative
    (`narrative`) or its actions (`actions`) instead: each action whose kind
    carries an honest number becomes a claim.

    The same words as the current read of this surface and subject are
    that read being re-served: its id is returned and nothing is written.
    Never raises."""
    if not restaurant_id or not surface:
        return None
    meta = dict(meta or {})
    shown = _clip(shown_text_of(text), MAX_SHOWN_CHARS)
    if not shown.strip():
        return None
    subject = (str(subject).strip()[:160] or None) if subject not in (None, "") else None
    raw = meta.pop("raw", None)
    claims = meta.pop("claims", None)
    narrative = meta.pop("narrative", None)
    actions = meta.pop("actions", None)
    if claims is None and surface == "dsr_narrative":
        claims = dsr_action_claims(actions if actions is not None else
                                   ((narrative or {}).get("actions_tomorrow") if isinstance(narrative, dict) else None))
    kind = meta.pop("kind", None)
    fp = meta.pop("fingerprint", None)
    verdict = meta.pop("verdict", None)
    facts_hash = meta.pop("facts_hash", None) or fp
    cited = meta.pop("cited_ids", None)
    rec_keys = meta.pop("rec_keys", None)
    th = _hash(shown)
    try:
        conn = get_conn(db_path)
    except Exception as e:
        log.warning("ai_reads: no connection for rid=%s %s: %s", restaurant_id, surface, e)
        return None
    read_id = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT id, text_hash FROM ai_reads WHERE restaurant_id=? AND surface=? AND "
            + ("subject=?" if subject is not None else "subject IS NULL")
            + " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            (restaurant_id, surface, subject) if subject is not None else (restaurant_id, surface)).fetchone()
        if cur is not None and cur["text_hash"] == th:
            # The current read, re-served: nothing new was said.
            conn.rollback()
            read_id = cur["id"]
        else:
            new = conn.execute(
                "INSERT INTO ai_reads (restaurant_id, surface, kind, subject, fingerprint, text_hash, shown_text, "
                "raw_text, verdict, facts_hash, cited_ids, rec_keys, meta, call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, surface, kind, subject, fp, th, shown,
                 _clip(raw, MAX_RAW_CHARS) if raw not in (None, "") and str(raw) != shown else None,
                 verdict, facts_hash, _json(cited), _json(rec_keys), _json(meta), call_id))
            read_id = new.lastrowid
            if cur is not None:
                conn.execute("UPDATE ai_reads SET superseded_at=datetime('now'), superseded_by=? WHERE id=?",
                             (read_id, cur["id"]))
            conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ai_reads: read not kept rid=%s %s: %s", restaurant_id, surface, e)
        return None
    finally:
        conn.close()
    # A restated read's claims are restated (one open claim per subject —
    # record_claims), so this runs for a re-served read too.
    if claims:
        try:
            record_claims(restaurant_id, surface, subject, claims, read_id=read_id, db_path=db_path)
        except Exception as e:
            log.warning("ai_reads: claims not kept rid=%s %s: %s", restaurant_id, surface, e)
    return read_id


def record_store_read(restaurant_id, kind, fingerprint, payload, raw=None, call_id=None, db_path=None):
    """insight_store.put's history row: the stored read of `kind` filed under
    its surface (STORE_SURFACE), with its numbered lines' recommendation
    keys. Never raises."""
    try:
        surface = STORE_SURFACE.get(kind, f"{kind}_read")
        shown = shown_text_of(payload)
        rec_keys = []
        try:
            import insight_store
            prefix = STORE_LINE_PREFIX.get(kind)
            if prefix:
                rec_keys = [insight_store.line_key(prefix, ln) for ln in insight_store.numbered_lines(shown)]
            if kind == "reviews":
                m = re.search(r"(?m)^.*Do today:\s*(.+)$", shown or "")
                if m:
                    rec_keys.append(insight_store.signature_key("insight_review", m.group(1).strip()))
            if isinstance(payload, dict):
                for c in payload.get("cards") or []:
                    if isinstance(c, dict) and c.get("key"):
                        rec_keys.append(str(c["key"]))
        except Exception:
            pass
        return record_read(restaurant_id, surface, shown, meta={
            "kind": kind, "fingerprint": fingerprint, "raw": raw, "verdict": verdict_of(payload),
            "rec_keys": rec_keys[:20] or None}, call_id=call_id, db_path=db_path)
    except Exception as e:
        log.warning("ai_reads: stored read not kept rid=%s %s: %s", restaurant_id, kind, e)
        return None


# ── reading reads back ───────────────────────────────────────────────────────

def recent_reads(restaurant_id, surfaces=None, days=30, limit=20, db_path=None):
    """What Cavnar AI told this restaurant lately: [{surface, subject,
    summary, created_at, id}], newest first — plus `label` (the surface in
    words), `as_of` (M/D/YY), `text` (the shown text) and `current` (not
    replaced by a later read of the same surface and subject). Never
    raises."""
    if not restaurant_id:
        return []
    surfaces = [s for s in (surfaces or []) if s]
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        where, args = ["restaurant_id=?", "created_at >= datetime('now', ?)"], [restaurant_id, f"-{int(days)} days"]
        if surfaces:
            where.append(f"surface IN ({','.join('?' for _ in surfaces)})")
            args += surfaces
        rows = conn.execute(
            f"SELECT id, surface, subject, shown_text, meta, created_at, superseded_at FROM ai_reads "
            f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
    except Exception as e:
        log.warning("ai_reads: recent reads unreadable rid=%s: %s", restaurant_id, e)
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        meta = _loads(r["meta"], {}) or {}
        out.append({"id": r["id"], "surface": r["surface"], "subject": r["subject"],
                    "label": SURFACE_LABELS.get(r["surface"], r["surface"].replace("_", " ")),
                    "summary": _one_line(meta.get("summary") or r["shown_text"]),
                    "text": r["shown_text"], "created_at": r["created_at"], "as_of": _mdy(r["created_at"]),
                    "current": r["superseded_at"] is None})
    return out


def latest_read(restaurant_id, surface, subject=None, db_path=None):
    """The current read of one surface and subject (dict) or None."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM ai_reads WHERE restaurant_id=? AND surface=? AND "
            + ("subject=?" if subject is not None else "subject IS NULL")
            + " ORDER BY id DESC LIMIT 1",
            (restaurant_id, surface, subject) if subject is not None else (restaurant_id, surface)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


# ── claims ───────────────────────────────────────────────────────────────────

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3, "several": 4}
_UNIT_DAYS = {"day": 1, "week": 7, "fortnight": 14, "month": 30}
_HORIZON_RE = re.compile(
    r"\b(?:within|in|over|after|by|inside)\s+(?:the\s+)?(?:next\s+)?(?:about\s+|roughly\s+|around\s+|a\s+)?"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few|several)?\s*(?:of\s+)?"
    r"(day|week|fortnight|month)s?\b", re.I)
_NEXT_UNIT_RE = re.compile(r"\bnext\s+(week|month)\b", re.I)
# A number read over a short window is mostly noise: the smallest horizon
# per metric base (complaints need five reviews in the window).
HORIZON_MIN_BY_BASE = {"complaints": 28, "avg_rating": 28, "response_hours": 28}


def horizon_days(expected=None, metric=None) -> int:
    """The claim's horizon in days, from its own words ("within two weeks",
    "over the next month", "by next week") when it says, else the metric's
    default comparison window — held to [HORIZON_MIN_DAYS,
    HORIZON_MAX_DAYS] and never under the metric's own floor
    (HORIZON_MIN_BY_BASE). Pure."""
    days = None
    text = str(expected or "")
    m = _HORIZON_RE.search(text)
    if m:
        n_raw, unit = (m.group(1) or "one").lower(), m.group(2).lower()
        n = int(n_raw) if n_raw.isdigit() else _NUM_WORDS.get(n_raw, 1)
        days = n * _UNIT_DAYS.get(unit, 1)
    else:
        m2 = _NEXT_UNIT_RE.search(text)
        if m2:
            days = _UNIT_DAYS[m2.group(1).lower()]
    base = None
    if metric:
        base = str(metric).split(":", 1)[0]
        if days is None:
            try:
                import metrics
                days = int(metrics.describe(metric)["default_window_days"])
            except Exception:
                days = None
    days = int(days or DEFAULT_HORIZON_DAYS)
    floor = max(HORIZON_MIN_DAYS, HORIZON_MIN_BY_BASE.get(base or "", 0))
    return max(floor, min(HORIZON_MAX_DAYS, days))


def _metric_direction(metric):
    try:
        import metrics
        return "down" if metrics.describe(metric)["lower_is_better"] else "up"
    except Exception:
        return None


def _known_metric(metric):
    if not metric:
        return None
    try:
        import metrics
        return metrics.normalize(metric) if metrics.known(metric) else None
    except Exception:
        return None


def record_claims(restaurant_id, surface, subject, claims, read_id=None, db_path=None, today=None) -> list:
    """Open (or restate) the claims one read made. One open claim per
    (surface, subject, claim_type): a diagnosis restated every morning is
    one claim until its horizon — restated_n counts the restatements and
    last_text keeps the latest words — so a regenerated read never turns
    one cause into thirty pseudo-results. A claim is measured from the day
    it was first made: after = [made, made + horizon), baseline the same
    number of days before. Returns the claim ids. Never raises."""
    if not restaurant_id or not claims:
        return []
    today = _day(today) if today else _local_today(restaurant_id)
    ids = []
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        for c in claims:
            if not isinstance(c, dict):
                continue
            text = _one_line(c.get("text"), 600)
            if not text:
                continue
            ctype = c.get("claim_type") if c.get("claim_type") in CLAIM_TYPES else "cause"
            subj = str(c.get("subject") or subject or c.get("rec_key") or "whole")[:160]
            metric = _known_metric(c.get("metric"))
            direction = c.get("direction") if c.get("direction") in ("down", "up") else _metric_direction(metric)
            L = int(c.get("horizon_days") or horizon_days(c.get("expected"), metric))
            L = max(HORIZON_MIN_DAYS, min(HORIZON_MAX_DAYS, L))
            open_row = conn.execute(
                "SELECT id, horizon_date FROM ai_claims WHERE restaurant_id=? AND surface=? AND subject=? "
                "AND claim_type=? AND verdict IS NULL AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
                (restaurant_id, surface, subj, ctype)).fetchone()
            if open_row is not None and str(open_row["horizon_date"] or "") > today.isoformat():
                conn.execute("UPDATE ai_claims SET restated_n=restated_n+1, restated_at=datetime('now'), "
                             "last_text=?, last_read_id=COALESCE(?, last_read_id) WHERE id=?",
                             (text, read_id, open_row["id"]))
                ids.append(open_row["id"])
                continue
            start = today
            cur = conn.execute(
                "INSERT INTO ai_claims (restaurant_id, read_id, last_read_id, surface, subject, signature, claim_type, "
                "text, last_text, action, expected, rec_key, metric, direction, baseline_start, baseline_end, "
                "after_start, horizon_date, model_band, capped_band) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, read_id, read_id, surface, subj, (c.get("signature") or None), ctype, text, text,
                 _one_line(c.get("action"), 400) or None, _one_line(c.get("expected"), 400) or None,
                 (str(c["rec_key"])[:160] if c.get("rec_key") else None), metric, direction,
                 (start - timedelta(days=L)).isoformat(), (start - timedelta(days=1)).isoformat(),
                 start.isoformat(), (start + timedelta(days=L)).isoformat(),
                 (str(c["model_band"])[:12] if c.get("model_band") else None),
                 (str(c["capped_band"])[:12] if c.get("capped_band") else None)))
            ids.append(cur.lastrowid)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ai_reads: claims not recorded rid=%s %s: %s", restaurant_id, surface, e)
        return []
    finally:
        conn.close()
    return ids


def review_diagnosis_claim(category, result) -> dict:
    """The one claim a review diagnosis makes: its cause, the action it
    recommends and what it expected, settled by the category's complaint
    share (metrics complaints:<category>), which should fall."""
    import rec_ledger
    cat = str(category or "").strip()
    return {"claim_type": "cause", "text": result.get("cause"), "action": result.get("recommended_action"),
            "expected": result.get("expected_outcome"), "rec_key": rec_ledger.rec_key("diag_review", cat),
            "metric": f"complaints:{cat}", "direction": "down", "subject": f"category:{cat}",
            "model_band": result.get("model_confidence"), "capped_band": result.get("confidence"),
            "signature": _signature(f"diag_review:{cat}", result.get("recommended_action"))}


def food_diagnosis_lead(drivers):
    """(lead name, lead driver dict) of a food diagnosis — the name
    client_api.diagnosis_rec_key keys it on."""
    for d in drivers or []:
        if isinstance(d, dict):
            lead = d.get("item") or d.get("label") or d.get("what")
        elif isinstance(d, str):
            lead, d = d, {}
        else:
            continue
        if lead:
            return str(lead).strip(), d
    return None, {}


def food_diagnosis_metric(driver) -> str:
    """The number a food diagnosis's lead driver is settled on: its own
    item's waste for a waste driver when that grain exists (metrics
    item_waste:<item>), else the week's waste; food cost % for a price,
    sourcing, portion or menu driver."""
    d = driver or {}
    kind = d.get("kind")
    if kind == "waste":
        item = d.get("item")
        if item and _known_metric(f"item_waste:{item}"):
            return f"item_waste:{item}"
        return "weekly_waste"
    return "food_cost_pct"


def food_diagnosis_claim(drivers, result) -> dict:
    """The one claim a food cost diagnosis makes, on its lead driver."""
    import rec_ledger
    lead, drv = food_diagnosis_lead(drivers)
    if not lead:
        return None
    key = rec_ledger.rec_key("diag_food", lead.lower()[:80])
    return {"claim_type": "cause", "text": result.get("cause"), "action": result.get("recommended_action"),
            "expected": result.get("expected_outcome"), "rec_key": key, "metric": food_diagnosis_metric(drv),
            "direction": "down", "subject": f"driver:{lead.lower()[:80]}",
            "model_band": result.get("model_confidence"), "capped_band": result.get("confidence"),
            "signature": _signature(key, result.get("recommended_action"))}


def dsr_action_claims(actions) -> list:
    """Claims from a nightly report's actions: each action whose kind
    carries an honest number (outcomes.DSR_ACTION_METRICS — a reorder or a
    talk with the team carries none) is a claim that the number moves the
    better way within its window."""
    out = []
    try:
        import outcomes
    except Exception:
        return out
    for a in actions or []:
        if not isinstance(a, dict) or not a.get("key") or not a.get("text"):
            continue
        parts = str(a["key"]).split(":")
        metric = outcomes.DSR_ACTION_METRICS.get(parts[1] if len(parts) > 1 else "")
        if not metric:
            continue
        out.append({"claim_type": "action", "text": a.get("text"), "action": a.get("text"),
                    "expected": a.get("why"), "rec_key": a["key"], "metric": metric,
                    "subject": ":".join(parts[1:])[:160] or a["key"],
                    "signature": a.get("advice_signature") or _signature(a["key"], a.get("text"))})
    return out


def _signature(key, text):
    try:
        import insight_store
        return insight_store.advice_signature(key, text)
    except Exception:
        return None


# ── scoring claims at their horizon ──────────────────────────────────────────

def _fmt_metric(metric, v):
    if v is None:
        return "unknown"
    try:
        import metrics
        unit = metrics.describe(metric)["unit"]
    except Exception:
        unit = ""
    v = float(v)
    if unit == "$":
        return f"${v:,.0f}"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "★":
        return f"{v:.2f}★"
    if unit == "h":
        return f"{v:.1f}h"
    return f"{v:g}"


def metric_label(metric) -> str:
    try:
        import metrics
        return metrics.describe(metric)["label"]
    except Exception:
        return str(metric or "the number")


_TAKEN = ("accepted", "completed", "implemented")


def _action_state(conn, restaurant_id, rec_key, made_on, horizon):
    """(state, tracker row or None) for the advice a claim carried, as it
    stood at the claim's horizon: taken (answered Track / Done / accepted,
    or the change was made, before the horizon), declined, hidden,
    ignored (left to expire), open, or none (no such recommendation)."""
    if not rec_key:
        return "none", None
    ep = conn.execute(
        "SELECT rec_id, status, implemented_at, tracker_id, silenced_until, closed_at, last_event_at, created_at "
        "FROM rec_instances WHERE restaurant_id=? AND key=? AND created_at <= ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (restaurant_id, rec_key, f"{horizon} 23:59:59")).fetchone()
    if ep is None:
        return "none", None
    tracker = None
    if ep["tracker_id"]:
        try:
            tr = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=? AND restaurant_id=?",
                              (ep["tracker_id"], restaurant_id)).fetchone()
            tracker = dict(tr) if tr else None
        except Exception:
            tracker = None
    ev = conn.execute(
        "SELECT event, at, meta FROM rec_events WHERE rec_id=? AND event IN "
        "('accepted','completed','implemented','dismissed') ORDER BY at, id", (ep["rec_id"],)).fetchall()
    first_taken = next((e["at"] for e in ev if e["event"] in _TAKEN), None) or ep["implemented_at"]
    if first_taken:
        return ("taken" if str(first_taken)[:10] < str(horizon) else "taken_late"), tracker
    dismissed = [e for e in ev if e["event"] == "dismissed"]
    if dismissed:
        kind = (_loads(dismissed[-1]["meta"], {}) or {}).get("kind")
        return ("declined" if kind == "not_for_us" else "hidden"), tracker
    if ep["status"] == "expired":
        return "ignored", tracker
    if ep["status"] == "superseded":
        return "replaced", tracker
    return "open", tracker


def _window_reading(restaurant_id, row, db_path=None):
    """(before, after, compare dict) for a claim's metric over its baseline
    and after windows, read with this restaurant's own noise band."""
    import metrics
    metric = row["metric"]
    b_s, b_e = row["baseline_start"], row["baseline_end"]
    a_s = row["after_start"]
    a_e = (_day(row["horizon_date"]) - timedelta(days=1)).isoformat()
    kw = {"db_path": db_path} if db_path else {}
    before = row["baseline_value"] if row["baseline_value"] is not None else metrics.measure(
        restaurant_id, metric, b_s, b_e, **kw)[0]
    after, _why = metrics.measure(restaurant_id, metric, a_s, a_e, **kw)
    if before is None or after is None:
        return before, after, None
    L = (_day(a_e) - _day(a_s)).days + 1
    try:
        band = metrics.noise_band(restaurant_id, metric, window_days=L, end=b_e, before=before, **kw).get("band")
    except Exception:
        band = None
    return before, after, metrics.compare(metric, before, after, band=band)


def _moved_as_expected(cmp, direction):
    if not cmp or cmp.get("verdict") in (None, "unknown", "no_clear_change"):
        return False
    d = cmp.get("delta") or 0
    return (d < 0) if direction == "down" else (d > 0)


def score_due(restaurant_id, today=None, db_path=None) -> dict:
    """Score every claim whose horizon has passed (the whole after-window is
    in the past). The advice's own tracker decides when it was tracked
    (read through rec_learning.learned_verdict: a disowned, confounded or
    trigger-overlapping result is no verdict); otherwise the claim's
    metric over the days after the read against the same number of days
    before, with this restaurant's noise band:

      held         the advice was taken and the number moved the expected
                   way past the band
      not_held     taken, and it did not
      untested     the advice was never taken (or only after the horizon):
                   the number moving says nothing about the cause
      unmeasurable the number could not be read UNSCORABLE_AFTER_DAYS
                   after the horizon (or the claim carries no number)

    Returns {"scored", "unmeasurable", "waiting"}. Never raises."""
    today = _day(today) if today else _local_today(restaurant_id)
    out = {"scored": 0, "unmeasurable": 0, "waiting": 0}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ai_claims WHERE restaurant_id=? AND verdict IS NULL AND horizon_date <= ? ORDER BY id",
            (restaurant_id, today.isoformat())).fetchall()]
        updates = []
        for r in rows:
            made_on = str(r["after_start"] or r["created_at"])[:10]
            state, tracker = _action_state(conn, restaurant_id, r.get("rec_key"), made_on, r["horizon_date"])
            if not r.get("metric"):
                updates.append((UNMEASURABLE, None, None, "no number settles this claim", "none", state,
                                (tracker or {}).get("id"), r["id"]))
                out["unmeasurable"] += 1
                continue
            verdict = source = None
            if tracker and tracker.get("status") == "evaluated" and state == "taken":
                import rec_learning
                lv = rec_learning.learned_verdict(tracker.get("verdict"), tracker)
                if lv in ("improved", "worsened", "no_clear_change"):
                    verdict, source = (HELD if lv == "improved" else NOT_HELD), "tracker"
            before, after, cmp = _window_reading(restaurant_id, r, db_path=db_path)
            if verdict is None and cmp is None:
                if (today - _day(r["horizon_date"])).days > UNSCORABLE_AFTER_DAYS:
                    updates.append((UNMEASURABLE, before, after,
                                    f"{metric_label(r['metric'])} could not be read for the weeks after the read",
                                    "window", state, (tracker or {}).get("id"), r["id"]))
                    out["unmeasurable"] += 1
                else:
                    out["waiting"] += 1
                continue
            if verdict is None:
                source = "window"
                if state == "taken":
                    verdict = HELD if _moved_as_expected(cmp, r["direction"]) else NOT_HELD
                else:
                    verdict = UNTESTED
            L = (_day(r["horizon_date"]) - _day(r["after_start"])).days
            basis = (f"{metric_label(r['metric'])} {_fmt_metric(r['metric'], before)} → "
                     f"{_fmt_metric(r['metric'], after)} over the {L} days after the read, against the {L} "
                     f"before ({CAVEAT})") if (before is not None and after is not None) else \
                f"the tracker's measured result ({CAVEAT})"
            if verdict == UNTESTED:
                basis += "; the advice wasn't taken, so this says nothing about the cause"
            updates.append((verdict, before, after, basis, source, state, (tracker or {}).get("id"), r["id"]))
            out["scored"] += 1
        for v, before, after, basis, source, state, tid, cid in updates:
            conn.execute(
                "UPDATE ai_claims SET verdict=?, baseline_value=COALESCE(?, baseline_value), verdict_value=?, "
                "verdict_basis=?, verdict_source=?, action_state=?, tracker_id=?, verdict_at=datetime('now') "
                "WHERE id=? AND verdict IS NULL",
                (v, before, after, basis[:400], source, state, tid, cid))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ai_reads: claim scoring failed rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    return out


def restaurants_due(today=None, db_path=None) -> list:
    """Restaurant ids holding an unscored claim whose horizon has passed."""
    today = _day(today) if today else date.today()
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM ai_claims WHERE verdict IS NULL AND horizon_date <= ? "
            "ORDER BY restaurant_id", (today.isoformat(),)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


# ── what a claim's record says ───────────────────────────────────────────────

def claims_record(restaurant_id, kind, days=365, db_path=None) -> dict:
    """This restaurant's scored claims for advice of one recommendation kind
    (rec_key's kind) — Historical Accuracy's addition for the kinds a read
    makes claims for (CLAIM_KINDS): {"measured", "improved", "worsened",
    "no_clear_change", "untested", "unmeasurable"}. Only a claim measured
    over its own window counts in `measured`: one whose advice was tracked
    is counted by its tracker already (rec_learning.kind_record), and one
    whose advice was never taken is untested. Never raises."""
    out = {"measured": 0, "improved": 0, "worsened": 0, "no_clear_change": 0, "untested": 0, "unmeasurable": 0}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT verdict, verdict_source, direction, baseline_value, verdict_value, tracker_id FROM ai_claims "
            "WHERE restaurant_id=? AND rec_key LIKE ? AND verdict IS NOT NULL "
            "AND created_at >= datetime('now', ?)", (restaurant_id, f"{kind}:%", f"-{int(days)} days")).fetchall()
    except Exception:
        return out
    finally:
        conn.close()
    for r in rows:
        if r["verdict"] == UNTESTED:
            out["untested"] += 1
        elif r["verdict"] == UNMEASURABLE:
            out["unmeasurable"] += 1
        elif r["verdict"] in (HELD, NOT_HELD) and r["verdict_source"] == "window" and not r["tracker_id"]:
            out["measured"] += 1
            if r["verdict"] == HELD:
                out["improved"] += 1
            else:
                b, a = r["baseline_value"], r["verdict_value"]
                wrong = (b is not None and a is not None
                         and ((a > b) if r["direction"] == "down" else (a < b)))
                out["worsened" if wrong else "no_clear_change"] += 1
    return out


def confidence_calibration(restaurant_id=None, days=365, db_path=None) -> dict:
    """The admin check of the model's own confidence (review_intelligence
    keeps model_confidence "so it can later be compared with what was
    measured"): for claims whose advice was taken and scored, how often a
    claim the model called high / medium / low held — and the same by the
    capped band the owner was shown. {"by_model_band": [{band, scored,
    held, rate}], "by_capped_band": [...], "scored", "ordered"} — `ordered`
    False when a lower band held more often than a higher one with enough
    of each (5+). Never raises."""
    out = {"by_model_band": [], "by_capped_band": [], "scored": 0, "ordered": None, "days": int(days)}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        where, args = ["verdict IN (?, ?)", "created_at >= datetime('now', ?)"], [HELD, NOT_HELD, f"-{int(days)} days"]
        if restaurant_id:
            where.append("restaurant_id=?")
            args.append(restaurant_id)
        rows = conn.execute(f"SELECT verdict, model_band, capped_band FROM ai_claims WHERE {' AND '.join(where)}",
                            args).fetchall()
    except Exception:
        return out
    finally:
        conn.close()
    out["scored"] = len(rows)
    for col, name in (("model_band", "by_model_band"), ("capped_band", "by_capped_band")):
        by = {}
        for r in rows:
            b = str(r[col] or "unknown").lower()
            s = by.setdefault(b, {"band": b, "scored": 0, "held": 0})
            s["scored"] += 1
            s["held"] += 1 if r["verdict"] == HELD else 0
        order = {"high": 0, "medium": 1, "low": 2}
        lst = sorted(by.values(), key=lambda s: order.get(s["band"], 3))
        for s in lst:
            s["rate"] = round(s["held"] / s["scored"], 3) if s["scored"] else None
        out[name] = lst
    rated = [s for s in out["by_model_band"] if s["band"] in ("high", "medium", "low") and s["scored"] >= 5]
    if len(rated) >= 2:
        rates = [s["rate"] for s in rated]
        out["ordered"] = all(rates[i] >= rates[i + 1] for i in range(len(rates) - 1))
    return out


# ── the memory_context provider ──────────────────────────────────────────────

_STATE_WORDS = {"taken": "the advice was taken", "taken_late": "the advice was taken after the check window",
                "declined": "the owner said not for us", "hidden": "the owner hid it",
                "ignored": "it was left unanswered", "open": "not answered yet", "replaced": "it was replaced",
                "none": None}


def _live_state(conn, restaurant_id, rec_key):
    """The advice's answer as it stands now, in words, with its date."""
    if not rec_key:
        return None
    ep = conn.execute("SELECT rec_id, status, implemented_at, closed_at, last_event_at FROM rec_instances "
                      "WHERE restaurant_id=? AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                      (restaurant_id, rec_key)).fetchone()
    if ep is None:
        return None
    ev = conn.execute("SELECT event, at, meta FROM rec_events WHERE rec_id=? AND event IN "
                      "('accepted','completed','implemented','dismissed') ORDER BY at DESC, id DESC LIMIT 1",
                      (ep["rec_id"],)).fetchone()
    if ev is None:
        return "not answered yet" if ep["status"] in ("open", "superseded") else "left unanswered"
    word = {"accepted": "answered Track", "completed": "answered Done", "implemented": "the change was made",
            "dismissed": "answered Not for us"}.get(ev["event"], ev["event"])
    if ev["event"] == "dismissed" and (_loads(ev["meta"], {}) or {}).get("kind") != "not_for_us":
        word = "hidden"
    return f"{word} ({_mdy(ev['at'])})"


def _interim(restaurant_id, row, db_path=None):
    """"so far" reading of an open claim's number: the days since the read
    against the same number of days before, or None under INTERIM_MIN_DAYS."""
    try:
        import metrics
        start = _day(row["after_start"])
        today = _local_today(restaurant_id)
        n = (today - start).days
        if n < INTERIM_MIN_DAYS or not row.get("metric"):
            return None
        kw = {"db_path": db_path} if db_path else {}
        before = metrics.measure(restaurant_id, row["metric"], (start - timedelta(days=n)).isoformat(),
                                 (start - timedelta(days=1)).isoformat(), **kw)[0]
        after = metrics.measure(restaurant_id, row["metric"], start.isoformat(),
                                (today - timedelta(days=1)).isoformat(), **kw)[0]
        if before is None or after is None:
            return None
        return (f"{metric_label(row['metric'])} {_fmt_metric(row['metric'], before)} → "
                f"{_fmt_metric(row['metric'], after)} so far ({n} days, {CAVEAT})")
    except Exception:
        return None


def claim_lines(req):
    """memory_context provider: the last claim on req.subjects for
    req.surface and its verdict ("LAST READ (9/2/26): ... Answered Done;
    complaints 9 -> 4 since (before and after, not proof)").

    Two lines per claim: the model's own words (fenced — memory_context
    fences every line not marked trusted) and what happened since, which
    Cavnar AI measured (trusted): the answer the advice got and the number
    at the horizon, or so far. A subject is matched on the claim's subject
    ("category:service"), its recommendation key ("diag_review:service") or
    its advice signature ("labor:day:friday"); with no subjects, the
    surface's own latest claims. The nightly report's own actions are
    served by its YESTERDAY'S PRIORITIES block (dsr.narrative), not here."""
    rid = getattr(req, "restaurant_id", None)
    if not rid:
        return []
    surface = getattr(req, "surface", None)
    subjects = [str(s) for s in (getattr(req, "subjects", None) or ()) if s]
    db_path = getattr(req, "db_path", None)
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    lines = []
    try:
        where = ["restaurant_id=?", "created_at >= datetime('now', ?)"]
        args = [rid, f"-{CLAIM_LOOKBACK_DAYS} days"]
        if subjects:
            marks = ",".join("?" for _ in subjects)
            where.append(f"(subject IN ({marks}) OR rec_key IN ({marks}) OR signature IN ({marks}))")
            args += subjects * 3
        elif surface:
            where.append("surface=?")
            args.append(surface)
        else:
            return []
        if surface == "dsr_narrative":
            where.append("surface != 'dsr_narrative'")
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM ai_claims WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 40", args).fetchall()]
        latest = {}
        for r in rows:
            latest.setdefault((r["surface"], r["subject"]), r)
        picked = sorted(latest.values(), key=lambda r: -r["id"])[:4]
        for i, r in enumerate(picked):
            weight = 10.0 - i
            said = f"LAST READ on {r['subject'].replace('_', ' ')} ({SURFACE_LABELS.get(r['surface'], r['surface'])})"
            said += f": {'cause' if r['claim_type'] == 'cause' else r['claim_type']} — {r.get('last_text') or r['text']}"
            if r.get("action") and r["claim_type"] == "cause":
                said += f"; recommended — {r['action']}"
            if r.get("restated_n"):
                said += f" (restated {r['restated_n']} time{'s' if r['restated_n'] != 1 else ''} since)"
            lines.append({"text": said, "date": str(r["created_at"])[:10], "source": "model",
                          "subject": r.get("rec_key") or r["subject"], "weight": weight, "trusted": False})
            since = []
            state = _live_state(conn, rid, r.get("rec_key"))
            if state:
                since.append(state[:1].upper() + state[1:])
            if r.get("verdict"):
                verdict_word = {HELD: "the expected change held", NOT_HELD: "the expected change did not show",
                                UNTESTED: "untested", UNMEASURABLE: "not measurable"}.get(r["verdict"], r["verdict"])
                since.append(f"checked {_mdy(r['horizon_date'])}: {verdict_word} — {r.get('verdict_basis') or ''}".rstrip(" —"))
            else:
                so_far = _interim(rid, r, db_path=db_path)
                since.append(so_far or f"to be checked {_mdy(r['horizon_date'])}")
            lines.append({"text": "Since that read: " + "; ".join(s for s in since if s) + ".",
                          "date": None, "source": "system", "subject": r.get("rec_key") or r["subject"],
                          "weight": weight - 0.5, "trusted": True})
    except Exception as e:
        log.warning("ai_reads: claim lines unavailable rid=%s: %s", rid, e)
        return []
    finally:
        conn.close()
    return lines


# ── the quarterly "what we said, what was done, what happened" ──────────────

def _quarter(d):
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _quarter_bounds(q):
    y, n = int(q[:4]), int(q[-1])
    start = date(y, 3 * (n - 1) + 1, 1)
    end = (date(y + 1, 1, 1) if n == 4 else date(y, 3 * n + 1, 1)) - timedelta(days=1)
    return start, end


# A closed quarter is re-summarised while its claims can still be scored
# (their horizons run up to HORIZON_MAX_DAYS past it, then the scorer's
# grace); after that its summary stands as written.
RESUMMARISE_DAYS = HORIZON_MAX_DAYS + UNSCORABLE_AFTER_DAYS + 30


def summarise_quarters(restaurant_id, today=None, db_path=None) -> int:
    """Write (or refresh) one ai_read_summaries row per closed quarter,
    surface and subject that has reads — the row that outlives the raw
    reads (kept 13 months). said: the last words of the quarter; done: what
    the advice those reads carried got (taken / declined / ignored); what
    happened: the claims' verdicts. Returns rows written. Never raises."""
    today = _day(today) if today else _local_today(restaurant_id)
    this_q_start = _quarter_bounds(_quarter(today))[0]
    try:
        conn = get_conn(db_path)
    except Exception:
        return 0
    n = 0
    try:
        groups = conn.execute(
            "SELECT surface, COALESCE(subject, '') AS subject, MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
            "COUNT(*) AS reads_n FROM ai_reads WHERE restaurant_id=? AND created_at < ? "
            "AND created_at >= ? GROUP BY surface, COALESCE(subject, ''), "
            "substr(created_at, 1, 4) || '-Q' || ((CAST(substr(created_at, 6, 2) AS INTEGER) - 1) / 3 + 1)",
            (restaurant_id, this_q_start.isoformat(),
             (this_q_start - timedelta(days=RAW_KEEP_DAYS)).isoformat())).fetchall()
        for g in groups:
            q = _quarter(_day(g["first_at"]))
            q_start, q_end = _quarter_bounds(q)
            have = conn.execute("SELECT updated_at FROM ai_read_summaries WHERE restaurant_id=? AND quarter=? "
                                "AND surface=? AND subject=?", (restaurant_id, q, g["surface"], g["subject"])).fetchone()
            if have is not None and (today - q_end).days > RESUMMARISE_DAYS:
                continue
            sub_sql = "subject=?" if g["subject"] else "subject IS NULL"
            sub_args = (g["subject"],) if g["subject"] else ()
            last = conn.execute(
                f"SELECT shown_text, rec_keys FROM ai_reads WHERE restaurant_id=? AND surface=? AND {sub_sql} "
                f"AND created_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
                (restaurant_id, g["surface"], *sub_args, q_start.isoformat(), f"{q_end.isoformat()} 23:59:59")).fetchone()
            keys = set()
            for r in conn.execute(
                    f"SELECT rec_keys FROM ai_reads WHERE restaurant_id=? AND surface=? AND {sub_sql} "
                    f"AND created_at BETWEEN ? AND ?",
                    (restaurant_id, g["surface"], *sub_args, q_start.isoformat(), f"{q_end.isoformat()} 23:59:59")):
                keys.update(k for k in (_loads(r["rec_keys"], []) or []) if isinstance(k, str))
            done = {"taken": 0, "declined": 0, "ignored": 0, "open": 0}
            for k in sorted(keys)[:60]:
                st, _tr = _action_state(conn, restaurant_id, k, q_start.isoformat(), q_end.isoformat())
                bucket = ("taken" if st in ("taken", "taken_late") else "declined" if st in ("declined", "hidden")
                          else "ignored" if st in ("ignored", "replaced") else "open" if st == "open" else None)
                if bucket:
                    done[bucket] += 1
            claims = conn.execute(
                f"SELECT verdict, verdict_basis FROM ai_claims WHERE restaurant_id=? AND surface=? "
                f"AND {'subject=?' if g['subject'] else '1=1'} AND created_at BETWEEN ? AND ? ORDER BY id",
                (restaurant_id, g["surface"], *sub_args, q_start.isoformat(),
                 f"{q_end.isoformat()} 23:59:59")).fetchall()
            counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
            happened = next((c["verdict_basis"] for c in reversed(claims) if c["verdict_basis"]), None)
            conn.execute(
                "INSERT INTO ai_read_summaries (restaurant_id, quarter, surface, subject, reads_n, first_at, last_at, "
                "said, done, happened, claims_n, held_n, not_held_n, untested_n, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(restaurant_id, quarter, surface, subject) DO UPDATE SET reads_n=excluded.reads_n, "
                "first_at=excluded.first_at, last_at=excluded.last_at, said=excluded.said, done=excluded.done, "
                "happened=excluded.happened, claims_n=excluded.claims_n, held_n=excluded.held_n, "
                "not_held_n=excluded.not_held_n, untested_n=excluded.untested_n, updated_at=excluded.updated_at",
                (restaurant_id, q, g["surface"], g["subject"], int(g["reads_n"]), g["first_at"], g["last_at"],
                 _one_line(last["shown_text"] if last else "", 500), _json(done), happened, len(claims),
                 counts[HELD], counts[NOT_HELD], counts[UNTESTED]))
            n += 1
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("ai_reads: quarterly summaries failed rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    return n


def summaries(restaurant_id, surfaces=None, db_path=None) -> list:
    """The kept-forever quarterly rows, newest quarter first."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        where, args = ["restaurant_id=?"], [restaurant_id]
        if surfaces:
            where.append(f"surface IN ({','.join('?' for _ in surfaces)})")
            args += list(surfaces)
        rows = conn.execute(f"SELECT * FROM ai_read_summaries WHERE {' AND '.join(where)} "
                            f"ORDER BY quarter DESC, surface, subject", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["done"] = _loads(d.get("done"), {}) or {}
            out.append(d)
        return out
    except Exception:
        return []
    finally:
        conn.close()
