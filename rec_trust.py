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
import copy
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
        self._confs = {}

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


def diagnosis_evidence(dg, n, kind, basis, flags=(), coverage=None, corroborating=None) -> dict:
    """Evidence Strength input for a stored model diagnosis (reviews, food,
    campaign): the data count behind it, capped by the model's band as the
    validator CAPPED it (`confidence` — ai_guard.cap_band over its verified
    cross-checks; never `model_confidence`, the raw band that let a
    diagnosis with no cross-check read "high": R9, B5 #9) — which only ever
    lowers it — and by any figure that failed verification; raised,
    boundedly, by the other modules whose verified figures agree
    (`corroborating`, default the diagnosis's own distinct verified
    modules — B4 M2). A diagnosis over a week old is a stale read."""
    dg = dg or {}
    band = dg.get("confidence")
    unverified = (dg.get("unsupported_figures") or dg.get("unverified_figures") or dg.get("unverified") or [])
    fl = tuple(flags or ()) + (("stale_read",) if dg.get("stale") else ())
    return {"n": n, "kind": kind, "basis": basis + ("; written over a week ago" if dg.get("stale") else ""),
            "flags": fl, "coverage": coverage,
            "model_band": band if band in ce.MODEL_CAPS else None,
            "unverified": len(unverified) if isinstance(unverified, (list, tuple)) else int(bool(unverified)),
            "corroborating": verified_evidence_count(dg) if corroborating is None else int(corroborating)}


def verified_evidence_count(dg) -> int:
    """How many distinct MODULES a diagnosis's verified operational_evidence
    comes from (K6: the validator marks each kept entry `verified: true` and
    drops the rest; an entry from before that marking counts). One module's
    figures cited twice are one corroboration, not two (R1 serves one entry
    per module; this makes the count say so)."""
    mods = set()
    for i, e in enumerate((dg or {}).get("operational_evidence") or []):
        if isinstance(e, dict) and e.get("verified", True):
            mods.add(str(e.get("module") or e.get("source") or f"#{i}").strip().lower())
    return len(mods)


def review_diagnosis_input(dg, restaurant_row=None) -> dict:
    """THE Evidence Strength input of a stored review diagnosis — the one
    the Reviews tab, the Home card and the one-thing hero all read, so one
    diagnosis shows one figure everywhere (B1 H3, B4 M1: 73% on the card vs
    87% on the hero). The reviews behind its theme (`mention_count`), the
    Places "sampled" flag (data_freshness.review_evidence_flags), the
    capped band and its corroborating modules."""
    import data_freshness
    dg = dg or {}
    n = int(dg.get("mention_count") or 0)
    flags = data_freshness.review_evidence_flags(restaurant_row) if restaurant_row is not None else ()
    return diagnosis_evidence(dg, n, "reviews",
                              f"{n} reviews on this theme over {dg.get('window_days') or 90} days", flags=flags)


def food_diagnosis_input(restaurant_id, dg, db_path=None) -> dict:
    """THE Evidence Strength input of the stored food cost diagnosis —
    read by the Food Cost card, Home and the one-thing hero alike (B1 H3/
    H4). Its sample is the food data itself — the ISO weeks of inventory
    counts in the last eight (waste_trend.load_waste_history, N_FULL
    "weeks") — raised boundedly by each other module whose verified figure
    agrees, capped by the validator's capped band. A diagnosis with no
    cross-check used to read 0% while its prose said "medium" (B1 H4)."""
    dg = dg or {}
    try:
        from waste_trend import load_waste_history
        weeks, _t = load_waste_history(restaurant_id, 8, db_path=db_path)
        n = len(weeks or [])
    except Exception as e:
        print(f"[rec_trust] food weeks unreadable for {restaurant_id}: {e}")
        n = None
    k = verified_evidence_count(dg)
    basis = (f"{n} week{'s' if n != 1 else ''} of inventory counts" if n is not None
             else "the counts could not be read")
    return diagnosis_evidence(dg, n, "weeks", basis, corroborating=k)


def diagnosis_confidence(restaurant_id, key, dg, n, kind, basis, sources=(), flags=(), db_path=None,
                         ctx=None) -> dict:
    """The K1 object for a stored diagnosis block (K6): evidence from the
    data count, capped by the model's band and by unverified figures. The
    Reviews tab and the Food Cost card now assess through
    review_diagnosis_input / food_diagnosis_input (group P, one input per
    diagnosis); no caller remains in the repo — candidate for future cleanup
    after additional verification."""
    return assess(restaurant_id, key, evidence=diagnosis_evidence(dg, n, kind, basis, flags=flags),
                  sources=sources, db_path=db_path, ctx=ctx)


def _kind(key):
    import rec_ledger
    return rec_ledger.kind_of(key)


def assess(restaurant_id, key, evidence=None, sources=None, restaurant=None, db_path=None, now=None,
           ctx=None, rests_on=None) -> dict:
    """The K1 confidence object for recommendation `key`.

    `evidence` is confidence_engine.evidence's keyword arguments: n, kind,
    coverage, flags, model_band, unverified, sample, basis, n_full, cap,
    cap_reason. `sources` are data_freshness keys (default: none — the
    freshness dimension is then not measurable). `rests_on` names what the
    card rests on when no connected source dates it ("link taps only"), so
    the caution says what is true instead of that nothing dates it. Deterministic for the same
    rows; never raises; `score` always a number."""
    try:
        # One confidence per recommendation key per build (B4 M1, B1 H3):
        # within one Context — a Home page, a cross-module read, a DSR — the
        # second surface to assess a key gets the first one's object, so the
        # Home card, the hero and What connects can't show two figures for
        # one piece of advice. Only a key with a subject ("kind:subject"):
        # a bare fallback key ("dsr_action", "ask_answer") is shared by
        # different items.
        memo = ctx is not None and ":" in str(key or "") and isinstance(getattr(ctx, "_confs", None), dict)
        if memo and key in ctx._confs:
            return copy.deepcopy(ctx._confs[key])
        ctx = ctx or Context(restaurant_id, restaurant=restaurant, db_path=db_path, now=now)
        ev_in = dict(evidence or {})
        kind = _kind(key)
        seen = ctx.distrusted().get(kind)
        if seen and not ev_in.get("sample"):
            ev_in["cap"] = min(ev_in.get("cap", 100), DISTRUST_CAP)
            ev_in["cap_reason"] = f"you said you don't trust the data behind this ({ce._mdy(seen)})"
        ev = ce.evidence(**ev_in)
        acc = ce.accuracy(ctx.record(kind))
        fr = ce.freshness(ctx.sources(tuple(sources or ())), rests_on=rests_on)
        out = ce.assemble(ev, acc, fr)
        if memo:
            ctx._confs[key] = copy.deepcopy(out)
        return out
    except Exception as e:
        print(f"[rec_trust] assess failed for {restaurant_id}/{key}: {e}")
        return ce.unknown()


def snapshot_fields(conf) -> dict:
    """What rec_instances and each `shown` meta store (contract K3)."""
    return ce.snapshot(conf)


def band_of(conf) -> str:
    """The band word for an older client's string field."""
    return (conf or {}).get("band") or "low"


def outbound_label(conf) -> str:
    """The one line an email, push or SMS carries beside a recommendation
    (confidence round 2, T1): the K1 `label` and the date its stalest
    source's data runs through — "72% confidence · data through 9/23/26".
    "" when there is no K1 object; a figure below the floor reads
    "Confidence not yet measurable". Never a band word."""
    if not isinstance(conf, dict) or not conf.get("label"):
        return ""
    as_of = (((conf.get("dimensions") or {}).get("freshness") or {}).get("as_of"))
    return str(conf["label"]) + (f" · data through {as_of}" if as_of else "")



SCHEDULE_PANEL_KEY = "schedule_quality:read"


def schedule_evidence(quality) -> dict:
    """The Evidence Strength input of the Shift Quality read and of every
    suggestion built from it: the one read of this schedule (n 1 of 1),
    held to the read's measured completeness as a documented CAP
    (shift_quality.confidence: 100 less a stated penalty per missing input
    — unrated staff, no shift history, no demand history, no availability).
    Never `coverage`: the score is not a share of a trading window, and
    passing it as one printed "only 62% of the window measured" (B4 H3,
    B1 H2)."""
    qc = (quality or {}).get("confidence") or {}
    sc = qc.get("score")
    reasons = [str(x) for x in (qc.get("reasons") or []) if x]
    if not isinstance(sc, (int, float)) or isinstance(sc, bool):
        return {"n": None, "basis": "The read's completeness wasn't measured"}
    sc = max(0.0, min(100.0, float(sc)))
    basis = "the Shift Quality read of this schedule"
    why = ("every input on file" if not reasons else reasons[0].rstrip(".").rstrip())
    return {"n": 1, "n_full": 1, "kind": "count", "basis": basis, "cap": sc,
            "cap_reason": f"its inputs are {int(round(sc))}% complete — {why}"}


def attach_schedule_confidence(restaurant_id, quality, items, db_path=None, ctx=None) -> list:
    """Put K1 `confidence` on each Shift Quality recommendation item
    ({text, kind, key, rec_key}) in place, and the read's own K1 on the
    panel as `quality["confidence_detail"]` (B4 H3/H4, B1 H2: the pill said
    "High confidence" at 85 while every item below read 70%). Evidence is
    schedule_evidence (the read's completeness as a documented cap);
    accuracy is this restaurant's record of each suggestion's kind (the
    panel's own kind has none, so it is the no-record figure); freshness is
    everything a schedule rests on — shifts, the POS, sales and the weather
    (data_freshness.sources_for(["schedule"])), not labor alone. Never
    raises; an item it can't score keeps no confidence."""
    try:
        import data_freshness
        ev = schedule_evidence(quality)
        srcs = data_freshness.sources_for(["schedule"])
        ctx = ctx or Context(restaurant_id, db_path=db_path)
        if isinstance(quality, dict):
            quality["confidence_detail"] = assess(restaurant_id, SCHEDULE_PANEL_KEY, evidence=dict(ev),
                                                  sources=srcs, ctx=ctx)
        for it in items or []:
            if isinstance(it, dict) and it.get("key"):
                it["confidence"] = assess(restaurant_id, it["key"], evidence=dict(ev), sources=srcs, ctx=ctx)
    except Exception as e:
        print(f"[rec_trust] schedule confidence unavailable for {restaurant_id}: {e}")
    return items
