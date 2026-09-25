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

# A stored diagnosis's own age is a Data Freshness input (DH3-1): a
# pseudo-source folded into the minimum with the card's real sources,
# recency(age − DIAGNOSIS_GRACE_DAYS) over DIAGNOSIS_HORIZON_DAYS — so a
# cause written 60 days ago no longer reads ~70% because the reviews were
# fetched this morning.
DIAGNOSIS_GRACE_DAYS = 1
DIAGNOSIS_HORIZON_DAYS = 7
# Unattended output (the digest, the weekly plan, Ask's diagnosis tool) may
# anchor a cause on a stale diagnosis only as an association, and on none
# older than this (DH3-9).
STALE_ANCHOR_MAX_DAYS = 14
# Owner changes looked back over for the changed_since flag (DH3-14).
CHANGES_LOOKBACK_DAYS = 30


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
        self._changes = None

    def changes(self):
        """owner_changes, read once per build."""
        if self._changes is None:
            self._changes = owner_changes(self.rid, db_path=self.db_path)
        return self._changes

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
    modules — B4 M2). A diagnosis past its refresh (its `stale` flag — the
    TTL, a day) is a stale read: a partial flag, and the basis says the date
    it was written ("written 9/22/26" — it said "written over a week ago" of
    a read 25 hours old, DH3-1). Its own age rides along as
    read_age_days / read_as_of_iso, which assess() folds into Data
    Freshness as the `diagnosis` pseudo-source."""
    dg = dg or {}
    band = dg.get("confidence")
    unverified = (dg.get("unsupported_figures") or dg.get("unverified_figures") or dg.get("unverified") or [])
    fl = tuple(flags or ()) + (("stale_read",) if dg.get("stale") else ())
    age, iso = diagnosis_age(dg)
    written = ce._mdy(iso) if iso else (dg.get("as_of") or "")
    note = ""
    if dg.get("stale"):
        note = f"; written {written}" if written else "; written on a date that can't be read"
    out = {"n": n, "kind": kind, "basis": basis + note,
           "flags": fl, "coverage": coverage,
           "model_band": band if band in ce.MODEL_CAPS else None,
           "unverified": len(unverified) if isinstance(unverified, (list, tuple)) else int(bool(unverified)),
           "corroborating": verified_evidence_count(dg) if corroborating is None else int(corroborating)}
    if age is not None or dg.get("stale") or dg.get("generated_at"):
        out["read_age_days"] = age
        out["read_as_of_iso"] = iso
    return out


def diagnosis_age(dg) -> tuple:
    """(age in days or None, ISO date written or None) of a stored
    diagnosis — from its generated_at, else its age_hours."""
    from datetime import timezone as _tz
    dg = dg or {}
    iso, age = None, None
    raw = dg.get("generated_at")
    if raw:
        try:
            from time_utils import parse_stamp
            at = parse_stamp(raw, naive_tz="UTC")
        except Exception:
            at = None
        if at is not None:
            iso = at.date().isoformat()
            age = max(0.0, (datetime.now(_tz.utc) - at).total_seconds() / 86400.0)
    if age is None and isinstance(dg.get("age_hours"), (int, float)):
        age = max(0.0, float(dg["age_hours"]) / 24.0)
        if iso is None:
            iso = (datetime.utcnow() - timedelta(days=age)).date().isoformat()
    return (round(age, 2) if age is not None else None), iso


def diagnosis_source(age_days, as_of_iso) -> dict:
    """The `diagnosis` pseudo-source: a data_freshness-shaped state for a
    stored diagnosis's own age, so the freshness minimum weighs it with
    the card's real sources. An age that can't be read is unknown (0)."""
    if age_days is None:
        return {"key": "diagnosis", "label": "Diagnosis", "pct": 0, "as_of": None, "as_of_iso": None,
                "basis": "Diagnosis: when it was written can't be read", "state": "unknown", "error": None}
    pct = int(round(100 * ce.recency(max(0.0, float(age_days)), DIAGNOSIS_GRACE_DAYS, DIAGNOSIS_HORIZON_DAYS)))
    when = ce._mdy(as_of_iso) if as_of_iso else None
    return {"key": "diagnosis", "label": "Diagnosis", "pct": pct, "as_of": when, "as_of_iso": as_of_iso,
            "basis": f"Diagnosis written {when}" if when else "Diagnosis written earlier",
            "state": ce.state(pct), "error": None}


def diagnosis_anchor_strength(dg):
    """How strongly unattended output may lean on a stored diagnosis's cause
    (DH3-9): "likely" while it is within its refresh, "association" once it
    is stale, None (no anchor) past STALE_ANCHOR_MAX_DAYS or when its age
    can't be read."""
    if not dg or not dg.get("cause"):
        return None
    if not dg.get("stale"):
        return "likely"
    age, _iso = diagnosis_age(dg)
    if age is None or age > STALE_ANCHOR_MAX_DAYS:
        return None
    return "association"


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


# What each recorded owner change touches, by the data_freshness source a
# recommendation rests on (DH3-14).
_TARGET_SOURCES = {"labor_target_pct": ("labor", "your labor target"),
                   "food_cost_target": ("inventory", "your food cost target"),
                   "waste_target_pct": ("inventory", "your waste target"),
                   "monthly_revenue_target": ("sales", "your revenue target")}


def owner_changes(restaurant_id, db_path=None, since_days=CHANGES_LOOKBACK_DAYS) -> list:
    """The owner's known changes in the last `since_days`, newest per kind:
    [{what, at (ISO date), sources (data_freshness keys it touches, empty =
    any), kind (a recommendation kind it answers, or None)}] — a published
    schedule (schedule_history.published_at), a reprice (reprice_decisions),
    a changed target (activity_log target_change, models.update_restaurant)
    and a recommendation marked done (rec_events completed/done). Each table
    is read on its own; one that is missing leaves that kind out. Never
    raises."""
    import json
    import data_freshness
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    out = []
    try:
        conn = data_freshness.get_conn(db_path)
    except Exception:
        return out

    def q(sql, args):
        try:
            return conn.execute(sql, args).fetchall()
        except Exception:
            return []
    try:
        for r in q("SELECT MAX(published_at) AS at FROM schedule_history WHERE restaurant_id=? "
                   "AND published_at IS NOT NULL AND published_at >= ?", (restaurant_id, since)):
            if r["at"]:
                out.append({"what": "published a schedule", "at": str(r["at"])[:10], "sources": ("labor",),
                            "kind": None})
        for r in q("SELECT dish, created_at AS at FROM reprice_decisions WHERE restaurant_id=? AND created_at >= ? "
                   "ORDER BY created_at DESC LIMIT 1", (restaurant_id, since)):
            if r["at"]:
                dish = str(r["dish"] or "").strip()
                out.append({"what": f"repriced {dish}" if dish else "repriced a dish", "at": str(r["at"])[:10],
                            "sources": ("inventory",), "kind": None})
        seen = set()
        for r in q("SELECT event_data, created_at AS at FROM activity_log WHERE restaurant_id=? "
                   "AND event_type='target_change' AND created_at >= ? ORDER BY created_at DESC",
                   (restaurant_id, since)):
            try:
                field = (json.loads(r["event_data"] or "{}") or {}).get("field")
            except (TypeError, ValueError):
                field = None
            if field in _TARGET_SOURCES and field not in seen:
                seen.add(field)
                src, label = _TARGET_SOURCES[field]
                out.append({"what": f"changed {label}", "at": str(r["at"])[:10], "sources": (src,), "kind": None})
        for r in q("SELECT key, MAX(at) AS at FROM rec_events WHERE restaurant_id=? AND event='completed' "
                   "AND at >= ? AND meta LIKE '%\"done\"%' GROUP BY key", (restaurant_id, since)):
            if r["at"]:
                out.append({"what": "marked a similar recommendation done", "at": str(r["at"])[:10], "sources": (),
                            "kind": _kind(r["key"]), "key": r["key"]})
    finally:
        conn.close()
    return out


def changed_since(ctx, key, states) -> dict:
    """{what, at, caution} for the newest owner change after this
    recommendation's data window, or None (DH3-14). The window ends on the
    newest date its sources' data covers; a change is relevant when it
    touches one of those sources, or — a recommendation marked done — when
    it answered another recommendation of the same kind. Nothing dates the
    window → None."""
    try:
        live = [s for s in states or () if s and s.get("pct") is not None and s.get("as_of_iso")]
        if not live:
            return None
        through = max(str(s["as_of_iso"])[:10] for s in live)
        keys = {s.get("key") for s in live}
        kind = _kind(key)
        hits = []
        for c in ctx.changes():
            if c["at"] <= through:
                continue
            if c.get("kind"):
                if c["kind"] != kind or c.get("key") == key:
                    continue
            elif not (set(c.get("sources") or ()) & keys):
                continue
            hits.append(c)
        if not hits:
            return None
        c = max(hits, key=lambda c: c["at"])
        when = ce._mdy(c["at"])
        return {"what": c["what"], "at": c["at"], "as_of": when,
                "caution": f"You {c['what']} on {when} — this reads data from before it."}
    except Exception as e:
        print(f"[rec_trust] changed_since unavailable for {getattr(ctx, 'rid', None)}/{key}: {e}")
        return None


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
        # A stored read's own age (diagnosis_evidence): a pseudo-source in
        # the freshness minimum, never evidence (DH3-1).
        has_read_age = "read_age_days" in ev_in
        read_age = ev_in.pop("read_age_days", None)
        read_iso = ev_in.pop("read_as_of_iso", None)
        states = list(ctx.sources(tuple(sources or ())))
        # Something the owner changed after the data window (DH3-14): the
        # figure describes the business before it — a partial flag (capped at
        # PARTIAL_CAP) and a caution naming the change and its date.
        change = None
        if not ev_in.get("sample"):
            change = changed_since(ctx, key, states)
            if change:
                ev_in["flags"] = tuple(dict.fromkeys(tuple(ev_in.get("flags") or ()) + ("changed_since",)))
        ev = ce.evidence(**ev_in)
        acc = ce.accuracy(ctx.record(kind))
        # Folded into the minimum beside a real source only: with none
        # measured, the card stays "freshness unmeasured" (capped) — a
        # diagnosis's own recency never stands in for the data under it.
        if has_read_age and any(s and s.get("pct") is not None for s in states):
            states = states + [diagnosis_source(read_age, read_iso)]
        fr = ce.freshness(states, rests_on=rests_on)
        out = ce.assemble(ev, acc, fr)
        if change:
            out["changed_since"] = change
            cur = str(out.get("caution") or "")
            if not cur or not any(w in cur for w in ("out of date", "failing", "confirms")):
                out["caution"] = change["caution"]
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


# A schedule built on a starting headcount borrowed from other restaurants
# (intelligence.staffing.starting_headcount) is capped on Evidence (BM3-13,
# Top-50 #33): at BORROWED_CAP while none of the staffed slots are the
# restaurant's own, lifting in proportion as its own weeks replace them.
BORROWED_CAP = 49


def _slot_key(key):
    if isinstance(key, (tuple, list)) and len(key) >= 2:
        return str(key[0]).strip().lower(), str(key[1]).strip().lower()
    s = str(key or "")
    if "|" in s:
        a, b = s.split("|", 1)
        return a.strip().lower(), b.strip().lower()
    return None


def borrowed_slots(start, typical=None):
    """{borrowed, total, own} — the (weekday, daypart, role) slots a draft
    staffs from the borrowed starting headcount (`start`, the
    starting_headcount payload) and the slots it staffs from the
    restaurant's own typical headcount — or None when nothing is borrowed.
    A slot the restaurant's own history covers is its own, whatever the
    borrowed figure says (staffing.merge_into_typical's rule)."""
    if not isinstance(start, dict) or not start.get("available"):
        return None
    own = set()
    for key, roles in (typical or {}).items():
        sk = _slot_key(key)
        if not sk or not isinstance(roles, dict):
            continue
        for role, n in roles.items():
            if n:
                own.add((sk[0], sk[1], str(role).strip().lower()))
    borrowed = set()
    for s in start.get("by_slot") or []:
        k = (str(s.get("day") or "").strip().lower(), str(s.get("daypart") or "").strip().lower(),
             str(s.get("role") or "").strip().lower())
        if all(k) and (s.get("people") or 0) > 0 and k not in own:
            borrowed.add(k)
    if not borrowed:
        return None
    return {"borrowed": len(borrowed), "own": len(own), "total": len(borrowed | own)}


def borrowed_cap(slots):
    """(cap, reason) for a schedule with borrowed slots, else (None, None):
    BORROWED_CAP with none of its own, rising linearly with the share of
    slots that are its own — never 100 while any slot is borrowed."""
    if not slots or not slots.get("borrowed"):
        return None, None
    n, total = int(slots["borrowed"]), max(1, int(slots.get("total") or slots["borrowed"]))
    own_share = max(0.0, min(1.0, (total - n) / float(total)))
    cap = min(99.0, float(BORROWED_CAP) + (100.0 - BORROWED_CAP) * own_share)
    shifts = f"{n} shift{'s' if n != 1 else ''}"
    if own_share <= 0:
        return float(BORROWED_CAP), f"{shifts} use other restaurants' staffing, none of yours yet"
    return round(cap, 1), f"{shifts} of {total} use other restaurants' staffing; your own weeks cover the rest"


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
        bcap, breason = borrowed_cap((quality or {}).get("borrowed_slots"))
        if bcap is not None:
            return {"n": 1, "n_full": 1, "kind": "count", "basis": "the Shift Quality read of this schedule",
                    "cap": bcap, "cap_reason": breason}
        return {"n": None, "basis": "The read's completeness wasn't measured"}
    sc = max(0.0, min(100.0, float(sc)))
    basis = "the Shift Quality read of this schedule"
    why = ("every input on file" if not reasons else reasons[0].rstrip(".").rstrip())
    out = {"n": 1, "n_full": 1, "kind": "count", "basis": basis, "cap": sc,
           "cap_reason": f"its inputs are {int(round(sc))}% complete — {why}"}
    # Borrowed staffing (BM3-13): the lower of the two caps binds, and says so.
    bcap, breason = borrowed_cap((quality or {}).get("borrowed_slots"))
    if bcap is not None and bcap < out["cap"]:
        out["cap"], out["cap_reason"] = bcap, breason
    return out


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
