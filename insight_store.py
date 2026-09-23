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


def get(restaurant_id, kind, fp, db_path=DB_PATH):
    """The stored read for exactly this data, or None."""
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
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def latest(restaurant_id, kind, db_path=DB_PATH):
    """(payload, created_at) of the last stored read whatever its data, for
    the "a stale read beats no read" fallback when generation fails."""
    row = _row(restaurant_id, kind, db_path)
    if not row:
        return None, None
    try:
        return json.loads(row["payload"]), row["created_at"]
    except (TypeError, ValueError):
        return None, None


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


def put(restaurant_id, kind, fp, payload, db_path=DB_PATH) -> bool:
    """Store the read. Never raises: a cache write must not fail a page."""
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
                  "evidence_sources": it.get("evidence_sources") or [module],
                  "model_written": it.get("model_written", True),
                  "cavnar_completes": it.get("cavnar_completes", False),
                  "expected_metric": it.get("expected_metric")} for it in items]
        ids = rec_ledger.present_many(restaurant_id, batch, surface, user_id=user_id, db_path=db_path)
    except Exception as e:
        print(f"[insight_store] present failed: {e}")
        ids = {}
    out = []
    for it in items:
        if it["key"] in ids and ids[it["key"]] is None:
            continue          # answered: silenced everywhere
        out.append(dict(it, rec_id=ids.get(it["key"])))
    return out


def answered(restaurant_id, keys, db_path=DB_PATH) -> set:
    """The subset of `keys` the owner has answered (silenced everywhere)."""
    try:
        import rec_ledger
        s = rec_ledger.silenced_keys(restaurant_id, db_path=db_path)
    except Exception:
        return set()
    return {k for k in keys if k in s}
