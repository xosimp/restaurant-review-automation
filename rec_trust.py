"""
rec_trust.py — one recommendation's confidence, measured (contract K1).

The glue between the pure arithmetic (confidence_engine) and what it reads:

  Evidence Strength   from the caller: what stands behind the card (how many
                      reviews, weekdays, weeks; the window's coverage; the
                      partial-data flags; a model's own band, which only
                      lowers it). The owner's "don't trust the data" answer
                      on this kind (rec_ledger reason code dont_trust_data)
                      caps it low for DISTRUST_DAYS.
  Historical Accuracy rec_learning.kind_record — this restaurant's taken and
                      shown episodes of the kind, read only through
                      learned_verdict, one result per overlapping window; the
                      anonymous cohort below the floor.
  Data Freshness      data_freshness.source_state over the card's sources.

    assess(rid, key, evidence={...}, sources=("labor", "pos"), ctx=ctx)

`Context` holds what one build reads once (the ledger, each source's state,
each kind's record), so a Home page with seven cards reads the ledger once.
Never raises: on any failure the answer is confidence_engine.unknown() —
every dimension unmeasured and the low band, never "medium".
"""
from datetime import datetime, timedelta

import confidence_engine as ce
from models import DB_PATH

DISTRUST_DAYS = 30
DISTRUST_CAP = 49


class Context:
    """What one build (a Home page, a DSR, one Ask answer) reads once."""

    def __init__(self, restaurant_id, restaurant=None, db_path=None, now=None, freshness_context=None):
        self.rid = restaurant_id
        self._restaurant = restaurant
        self.db_path = db_path or DB_PATH
        self.now = now
        self.freshness_context = freshness_context or {}
        self._episodes = None
        self._records = {}
        self._sources = {}
        self._distrust = None
        self._row = None

    # the restaurant row the freshness readers want (dict of every column)
    def row(self):
        if self._row is None:
            import data_freshness
            conn = data_freshness.get_conn(self.db_path)
            try:
                r = conn.execute("SELECT * FROM restaurants WHERE id=?", (self.rid,)).fetchone()
                self._row = dict(r) if r else {"id": self.rid}
            finally:
                conn.close()
        return self._row

    def restaurant(self):
        if self._restaurant is None:
            try:
                import models
                self._restaurant = models.get_restaurant(self.rid, db_path=self.db_path)
            except Exception:
                self._restaurant = None
        return self._restaurant

    def episodes(self):
        if self._episodes is None:
            import rec_learning
            try:
                conn = rec_learning.get_conn(self.db_path)
                try:
                    since = (datetime.utcnow() - timedelta(days=rec_learning.EFFECT_WINDOW_DAYS))
                    # rec_learning's own loader, lean (no shown rows) — the
                    # input kind_record documents for a caller that reads it once.
                    self._episodes = rec_learning._load(conn, self.rid, since=rec_learning._stamp(since), lean=True)
                finally:
                    conn.close()
            except Exception as e:
                print(f"[rec_trust] ledger unreadable for {self.rid}: {e}")
                self._episodes = None
        return self._episodes

    def record(self, kind):
        if kind not in self._records:
            import rec_learning
            self._records[kind] = rec_learning.kind_record(self.rid, kind, db_path=self.db_path,
                                                           restaurant=self.restaurant(), episodes=self.episodes())
        return self._records[kind]

    def sources(self, keys):
        import data_freshness
        return data_freshness.states(self.row(), keys, db_path=self.db_path, now=self.now,
                                     context=self.freshness_context, cache=self._sources)

    def distrusted(self):
        """{kind: latest ISO date} of the owner's "don't trust the data"
        answers in the last DISTRUST_DAYS."""
        if self._distrust is None:
            self._distrust = {}
            try:
                import json
                import rec_ledger
                conn = rec_ledger.get_conn(self.db_path)
                try:
                    since = (datetime.utcnow() - timedelta(days=DISTRUST_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                    rows = conn.execute("SELECT key, meta, at FROM rec_events WHERE restaurant_id=? "
                                        "AND event IN ('dismissed','snoozed','checkin') AND at >= ? "
                                        "AND meta LIKE '%dont_trust_data%'", (self.rid, since)).fetchall()
                finally:
                    conn.close()
                for r in rows:
                    try:
                        meta = json.loads(r["meta"] or "{}") or {}
                    except (TypeError, ValueError):
                        continue
                    if meta.get("reason_code") != "dont_trust_data":
                        continue
                    k = rec_ledger.kind_of(r["key"])
                    self._distrust[k] = max(self._distrust.get(k, ""), str(r["at"])[:10])
            except Exception as e:
                print(f"[rec_trust] distrust answers unreadable for {self.rid}: {e}")
        return self._distrust


def diagnosis_evidence(dg, n, kind, basis, flags=(), coverage=None) -> dict:
    """Evidence Strength input for a stored model diagnosis (reviews, food,
    campaign): the data count behind it, capped by the model's own band
    (`model_confidence` when the validator kept it apart, else `confidence`)
    — which only ever lowers it — and by any figure that failed
    verification. A diagnosis over a week old is a stale read."""
    dg = dg or {}
    band = dg.get("model_confidence") or dg.get("confidence")
    unverified = (dg.get("unsupported_figures") or dg.get("unverified_figures") or dg.get("unverified") or [])
    fl = tuple(flags or ()) + (("stale_read",) if dg.get("stale") else ())
    return {"n": n, "kind": kind, "basis": basis + ("; written over a week ago" if dg.get("stale") else ""),
            "flags": fl, "coverage": coverage,
            "model_band": band if band in ce.MODEL_CAPS else None,
            "unverified": len(unverified) if isinstance(unverified, (list, tuple)) else int(bool(unverified))}


def verified_evidence_count(dg) -> int:
    """How many operational_evidence entries a diagnosis carries that were
    verified (K6: the validator marks each kept entry `verified: true` and
    drops the rest; an entry from before that marking counts)."""
    return len([e for e in ((dg or {}).get("operational_evidence") or [])
                if isinstance(e, dict) and e.get("verified", True)])


def diagnosis_confidence(restaurant_id, key, dg, n, kind, basis, sources=(), flags=(), db_path=None,
                         ctx=None) -> dict:
    """The K1 object for a stored diagnosis block (K6): evidence from the
    data count, capped by the model's band and by unverified figures."""
    return assess(restaurant_id, key, evidence=diagnosis_evidence(dg, n, kind, basis, flags=flags),
                  sources=sources, db_path=db_path, ctx=ctx)


def _kind(key):
    import rec_ledger
    return rec_ledger.kind_of(key)


def assess(restaurant_id, key, evidence=None, sources=None, restaurant=None, db_path=None, now=None,
           ctx=None) -> dict:
    """The K1 confidence object for recommendation `key`.

    `evidence` is confidence_engine.evidence's keyword arguments: n, kind,
    coverage, flags, model_band, unverified, sample, basis, n_full, cap,
    cap_reason. `sources` are data_freshness keys (default: none — the
    freshness dimension is then not measurable). Deterministic for the same
    rows; never raises; `score` always a number."""
    try:
        ctx = ctx or Context(restaurant_id, restaurant=restaurant, db_path=db_path, now=now)
        ev_in = dict(evidence or {})
        kind = _kind(key)
        seen = ctx.distrusted().get(kind)
        if seen and not ev_in.get("sample"):
            ev_in["cap"] = min(ev_in.get("cap", 100), DISTRUST_CAP)
            ev_in["cap_reason"] = f"you said you don't trust the data behind this ({ce._mdy(seen)})"
        ev = ce.evidence(**ev_in)
        acc = ce.accuracy(ctx.record(kind))
        fr = ce.freshness(ctx.sources(tuple(sources or ())))
        return ce.assemble(ev, acc, fr)
    except Exception as e:
        print(f"[rec_trust] assess failed for {restaurant_id}/{key}: {e}")
        return ce.unknown()


def snapshot_fields(conf) -> dict:
    """What rec_instances and each `shown` meta store (contract K3)."""
    return ce.snapshot(conf)


def band_of(conf) -> str:
    """The band word for an older client's string field."""
    return (conf or {}).get("band") or "low"


def schedule_quality_items(restaurant_id, quality, db_path=None, ctx=None) -> list:
    """Shift Quality's recommendations as [{text, kind, rec_key, confidence}]
    — K1 on each (confidence audit, integration). Evidence Strength is the
    Shift Quality read's own measured completeness (shift_quality.confidence:
    100 less a stated penalty per missing input — unrated staff, no shift
    history, no demand history, no availability), since every suggestion is
    built from that read; accuracy is this restaurant's record for the
    suggestion's kind; freshness is the labor data it rests on. The plain
    `recommendations` strings stay as they are for older builds. Never
    raises: a failure returns the texts with no confidence."""
    recs = [r for r in ((quality or {}).get("recommendations") or []) if isinstance(r, str) and r.strip()]
    if not recs:
        return []
    try:
        import shift_quality
        import schedule_intel
        qc = (quality or {}).get("confidence") or {}
        sc = qc.get("score")
        reasons = [str(x) for x in (qc.get("reasons") or []) if x]
        basis = ("Shift Quality read with every input on file" if not reasons
                 else "Shift Quality read — " + reasons[0].rstrip("."))
        ctx = ctx or Context(restaurant_id, db_path=db_path)
        out = []
        for text in recs[:10]:
            kind = shift_quality.recommendation_kind(text)
            key = schedule_intel.schedule_rec_key(kind, text)
            ev = ({"n": 1, "n_full": 1, "coverage": max(0.0, min(1.0, float(sc) / 100.0)), "basis": basis}
                  if isinstance(sc, (int, float)) else {"n": None, "basis": "The read's completeness wasn't measured"})
            out.append({"text": text, "kind": kind, "rec_key": key,
                        "confidence": assess(restaurant_id, key, evidence=ev, sources=("labor",), ctx=ctx)})
        return out
    except Exception as e:
        print(f"[rec_trust] schedule items unavailable for {restaurant_id}: {e}")
        return [{"text": t} for t in recs[:10]]
