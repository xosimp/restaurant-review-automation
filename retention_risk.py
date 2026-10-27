"""
retention_risk.py — retention-aware scheduling: who the schedule may be
pushing out, and a soft "protect" term for the valued ones (scheduler
audit 10/10/26 MARKET-29). OWNER-ONLY: risk is never shown, said or hinted
to the person, nor to a manager below the owner.

Restaurant turnover is the hidden labor cost, and schedules that cut a
regular's hours or keep giving them shifts they do not want come before
quits — nothing warned anyone.

The schedule-side signals, per person, as of a date (`signals`):
  hours_cut        their last 4 weeks' hours against the 8 before (a share
                   cut; only for someone who averaged USUAL_FLOOR_HOURS+)
  unwanted_share   the share of their last 4 weeks' shifts on a slot they
                   keep dropping (schedule_intel.behaviour_preferences
                   avoids) or outside the dayparts they said they want
                   (staff_settings.stated_preferences)
  drops            drops they asked for in the last 8 weeks
  missed_share     late or no-show share of their watched shifts (8 weeks)
  promises_broken  published shifts of theirs the restaurant later changed
                   (commitment_ladder)
The post-shift pulse is NOT a per-person signal: staff were promised it is
read only in aggregate (staff_insights), and a per-person risk would single
an answer out.

The label is the roster: somebody taken off it (change_log roster "left")
in the LABEL_DAYS that start NOTICE_DAYS after a week — the weeks just
before a departure are left out of every example (blind re-audit 10/11/26
LEARNING-25: hours cut and shifts dropped in the weeks before leaving often
FOLLOW the notice, so a model on them learned notice periods, and the
solver "protected" people who had already resigned). With at least
MIN_QUITS such departures and MIN_PERSON_WEEKS person-weeks, a logistic
model is fitted on person-weeks of the last year (standardised, L2) and
kept only if its out-of-fold AUC (folds by person) reaches MIN_AUC AND its
lower 90% bound (Hanley-McNeil, counted in PEOPLE — departures against
people who stayed — not person-weeks) clears 0.5 (6 quits passed noise 7%
of the time) — then `risk` is its probability and the `basis` says so,
with the AUC's range and the departures behind it. Otherwise there is no probability: `level` comes from
stated rules (an hours cut of RULE_CUT or more, RULE_DROPS drops, RULE_
UNWANTED of shifts unwanted), and the basis says it is rules, not a
measured risk.

Valued: rated VALUED_MIN_SCORE or more (unrated counts as nothing), or on
the clock here VALUED_MIN_WEEKS of the last year. Protect: valued and at
"high" level, unless the owner switched it off (`retention_overrides`).
`protect_signal` is what the solver reads (schedule_engine.
_learning_signals 'retention'): moving a protected person's drafted shift
to somebody else, or putting them on a slot they keep dropping, costs
RETENTION_POINTS on preferences — a soft cost under every hard rule.
"""
import logging
import math
import threading
from datetime import date, timedelta

log = logging.getLogger(__name__)

LABEL_DAYS = 28
NOTICE_DAYS = 14                 # the weeks before a departure a notice may already shape
USUAL_FLOOR_HOURS = 8.0          # a week's average before an hours cut is read
MIN_QUITS = 12
MIN_PERSON_WEEKS = 120
MIN_AUC = 0.65
HIGH_PROB = 0.25                 # a fitted probability of leaving within LABEL_DAYS this high is "high"
WATCH_PROB = 0.12
RULE_CUT = 0.3
RULE_DROPS = 3
RULE_UNWANTED = 0.4
VALUED_MIN_SCORE = 4.0
VALUED_MIN_WEEKS = 26
RETENTION_POINTS = 40.0
FEATURES = ("hours_cut", "unwanted_share", "drops", "missed_share", "promises_broken")
# The fitted model per restaurant and day (a per-process cache, safe per
# worker): a generation and the Studio read it without refitting.
_MODEL_CACHE = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("retention_risk._MODEL_CACHE", _MODEL_CACHE)
_MODEL_LOCK = threading.Lock()



def _models_db():
    """The database path as models has it now (CLAUDE.md, bound imports: a
    module's own DB_PATH is the one it was imported with)."""
    import models
    return models.DB_PATH

def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_retention(db_path=None):
    """Boot DDL (models.init_db): the owner's protect switch per person."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS retention_overrides (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            name_key        TEXT    NOT NULL,
            protect         INTEGER NOT NULL,
            updated_by      TEXT,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, name_key)
        )""")
        conn.commit()
    finally:
        conn.close()


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _monday(d):
    return d - timedelta(days=d.weekday())


# ── the data, read once ────────────────────────────────────────────────────

def _load(restaurant_id, today, db_path=None) -> dict:
    """Everything the signals read, for the last 56+365 days."""
    since = (today - timedelta(days=365 + 84)).isoformat()
    out = {"shifts": [], "drops": {}, "attendance": {}, "left": {}, "broken": {}}
    try:
        import shift_facts
        out["shifts"] = shift_facts.person_rows(restaurant_id, since=since, until=today.isoformat(), db_path=db_path)
    except Exception as e:
        log.warning("retention: shifts unreadable rid=%s: %s", restaurant_id, e)
    conn = get_conn(db_path)
    try:
        for r in conn.execute("SELECT employee_name, date FROM shift_change_requests WHERE restaurant_id=? AND "
                              "kind='drop' AND date>=?", (restaurant_id, since)).fetchall():
            out["drops"].setdefault(_key(r["employee_name"]), []).append(str(r["date"])[:10])
        for r in conn.execute("SELECT employee_name, business_date, outcome FROM attendance_events WHERE "
                              "restaurant_id=? AND business_date>=?", (restaurant_id, since)).fetchall():
            out["attendance"].setdefault(_key(r["employee_name"]), []).append(
                (str(r["business_date"])[:10], r["outcome"]))
        for r in conn.execute("SELECT subject, changed_at FROM change_log WHERE restaurant_id=? AND entity='roster' "
                              "AND field='left' AND changed_at>=?", (restaurant_id, since)).fetchall():
            out["left"].setdefault(_key(r["subject"]), []).append(str(r["changed_at"])[:10])
        try:
            for r in conn.execute("SELECT name_key, date FROM shift_commitments WHERE restaurant_id=? AND "
                                  "changed_at IS NOT NULL AND date>=?", (restaurant_id, since)).fetchall():
                out["broken"].setdefault(r["name_key"], []).append(str(r["date"])[:10])
        except Exception:
            pass
    except Exception as e:
        log.warning("retention: records unreadable rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    try:
        import schedule_intel
        out["avoids"] = {_key(n): set(v.get("avoids") or []) for n, v in
                         (schedule_intel.behaviour_preferences(restaurant_id, db_path=db_path or _models_db()) or {}).items()}
    except Exception:
        out["avoids"] = {}
    try:
        import staff_settings
        out["stated"] = {_key(n): set(v.get("preferred_dayparts") or []) for n, v in
                         (staff_settings.stated_preferences(restaurant_id, db_path=db_path or _models_db()) or {}).items()}
    except Exception:
        out["stated"] = {}
    by = {}
    for r in out["shifts"]:
        k = _key(r.get("employee"))
        if k:
            by.setdefault(k, []).append(r)
    out["by_person"] = by
    return out


def _hours(r):
    for k in ("actual_hours", "scheduled_hours", "hours"):
        try:
            v = r.get(k)
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def signals(data, key, as_of) -> dict | None:
    """The person's schedule-side signals as of `as_of` (module doc), or
    None when they had no shift in the 12 weeks before it."""
    from shift_quality import present_dayparts
    rows = data["by_person"].get(key) or []
    a4, a12 = (as_of - timedelta(weeks=4)).isoformat(), (as_of - timedelta(weeks=12)).isoformat()
    a8 = (as_of - timedelta(weeks=8)).isoformat()
    t = as_of.isoformat()
    recent = [r for r in rows if a4 <= str(r.get("date"))[:10] < t]
    before = [r for r in rows if a12 <= str(r.get("date"))[:10] < a4]
    if not recent and not before:
        return None
    h4, h8 = sum(_hours(r) for r in recent), sum(_hours(r) for r in before)
    usual = h8 / 8.0
    cut = max(0.0, 1.0 - (h4 / 4.0) / usual) if usual >= USUAL_FLOOR_HOURS else 0.0
    avoid = data["avoids"].get(key) or set()
    stated = data["stated"].get(key) or set()
    unwanted = 0
    for r in recent:
        try:
            wd = date.fromisoformat(str(r.get("date"))[:10]).strftime("%A")
        except ValueError:
            continue
        part = present_dayparts(r)[0]
        slot = f"{wd} {'night' if part == 'night' else 'day'}"
        if slot in avoid or (stated and part in ("morning", "night") and part not in stated):
            unwanted += 1
    att = [o for d, o in data["attendance"].get(key) or [] if a8 <= d < t]
    return {"hours_cut": round(cut, 3), "unwanted_share": round(unwanted / len(recent), 3) if recent else 0.0,
            "drops": sum(1 for d in data["drops"].get(key) or [] if a8 <= d < t),
            "missed_share": round(sum(1 for o in att if o in ("late", "no_show", "called_out")) / len(att), 3) if att else 0.0,
            "promises_broken": sum(1 for d in data["broken"].get(key) or [] if a8 <= d < t),
            "hours_recent": round(h4 / 4.0, 1), "hours_usual": round(usual, 1)}


# ── the model ──────────────────────────────────────────────────────────────

def _examples(data, today):
    """[(person key, monday, x list, label)] — person-weeks of the last year
    for people on the clock in the 12 weeks before each Monday, labelled
    by a roster departure in [Monday + NOTICE_DAYS, + LABEL_DAYS); a
    person-week with a departure inside its first NOTICE_DAYS is left out
    (its features may already be the notice's — LEARNING-25)."""
    out = []
    mon = _monday(today) - timedelta(weeks=52)
    last = _monday(today) - timedelta(days=LABEL_DAYS + NOTICE_DAYS)
    while mon <= last:
        for key in data["by_person"]:
            left = [d for d in data["left"].get(key) or [] if d >= mon.isoformat()]
            if left and min(left) < mon.isoformat():
                continue
            s = signals(data, key, mon)
            if s is None:
                continue
            if any(d < mon.isoformat() for d in data["left"].get(key) or []):
                continue
            gone = data["left"].get(key) or []
            notice_end = (mon + timedelta(days=NOTICE_DAYS)).isoformat()
            if any(mon.isoformat() <= d < notice_end for d in gone):
                continue
            y = 1 if any(notice_end <= d < (mon + timedelta(days=NOTICE_DAYS + LABEL_DAYS)).isoformat()
                         for d in gone) else 0
            out.append((key, mon, [float(s[f]) for f in FEATURES], y))
        mon += timedelta(weeks=1)
    return out


def _fit(xs, ys, l2=1.0, steps=150, lr=0.5):
    n, k = len(xs), len(xs[0])
    mu = [sum(x[j] for x in xs) / n for j in range(k)]
    sd = [math.sqrt(sum((x[j] - mu[j]) ** 2 for x in xs) / n) or 1.0 for j in range(k)]
    z = [[(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    w, b = [0.0] * k, 0.0
    for _ in range(steps):
        gw, gb = [0.0] * k, 0.0
        for xi, yi in zip(z, ys):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, b + sum(wj * xj for wj, xj in zip(w, xi))))))
            e = p - yi
            gb += e
            for j in range(k):
                gw[j] += e * xi[j]
        b -= lr * gb / n
        w = [wj - lr * (gw[j] / n + l2 * wj / n) for j, wj in enumerate(w)]
    return {"w": w, "b": b, "mu": mu, "sd": sd}


def _prob(m, x):
    z = m["b"] + sum(wj * (xj - mj) / sj for wj, xj, mj, sj in zip(m["w"], x, m["mu"], m["sd"]))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _auc(scores, ys):
    pos = [s for s, y in zip(scores, ys) if y]
    neg = [s for s, y in zip(scores, ys) if not y]
    if not pos or not neg:
        return None
    wins = sum((1.0 if p > q else 0.5 if p == q else 0.0) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def auc_lower(auc, n_pos, n_neg, z=1.645):
    """The lower 90% bound of an AUC (Hanley-McNeil), counted in the units
    that are independent — people, not person-weeks. None without both."""
    import math as _m
    if auc is None or not n_pos or not n_neg:
        return None
    q1, q2 = auc / (2.0 - auc), 2.0 * auc * auc / (1.0 + auc)
    var = (auc * (1 - auc) + (n_pos - 1) * (q1 - auc * auc) + (n_neg - 1) * (q2 - auc * auc)) / float(n_pos * n_neg)
    return round(auc - z * _m.sqrt(max(0.0, var)), 3)


def gate(ex) -> dict:
    """{auc, auc_lo, quits, stayers, ok}: the out-of-fold AUC (folds by
    person) and whether the model is kept — AUC >= MIN_AUC and its lower
    bound over 0.5 (LEARNING-25). Pure over examples."""
    people = sorted({k for k, _m, _x, _y in ex})
    leavers = {k for k, _m, _x, y in ex if y}
    folds = 5
    scores, ys = [], []
    for f in range(folds):
        test = {p for i, p in enumerate(people) if i % folds == f}
        tr = [(x, y) for k, _m, x, y in ex if k not in test]
        te = [(x, y) for k, _m, x, y in ex if k in test]
        if not tr or not te or not any(y for _x, y in tr):
            continue
        m = _fit([x for x, _y in tr], [y for _x, y in tr])
        scores += [_prob(m, x) for x, _y in te]
        ys += [y for _x, y in te]
    auc = _auc(scores, ys)
    lo = auc_lower(auc, len(leavers), len(people) - len(leavers))
    return {"auc": auc, "auc_lo": lo, "quits": len(leavers), "stayers": len(people) - len(leavers),
            "ok": auc is not None and auc >= MIN_AUC and lo is not None and lo > 0.5}


def model(restaurant_id, today=None, db_path=None, data=None) -> dict:
    """{available, model?, auc?, quits, person_weeks, reason?} — cached for
    the restaurant and day."""
    today = today or date.today()
    ck = (restaurant_id, today.isoformat(), db_path or "")
    with _MODEL_LOCK:
        if ck in _MODEL_CACHE:
            return _MODEL_CACHE[ck]
    got = _model(restaurant_id, today, db_path, data)
    with _MODEL_LOCK:
        if len(_MODEL_CACHE) > 256:
            _MODEL_CACHE.clear()
        _MODEL_CACHE[ck] = got
    return got


def _model(restaurant_id, today, db_path=None, data=None) -> dict:
    data = data or _load(restaurant_id, today, db_path)
    ex = _examples(data, today)
    quits = len({k for k, _m, _x, y in ex if y})
    if quits < MIN_QUITS or len(ex) < MIN_PERSON_WEEKS:
        return {"available": False, "quits": quits, "person_weeks": len(ex),
                "reason": (f"{quits} departure{'s' if quits != 1 else ''} on the roster in the last year "
                           f"(a model needs {MIN_QUITS}) — warning signs are shown by stated rules, not as a risk.")}
    g = gate(ex)
    auc = g["auc"]
    if not g["ok"]:
        return {"available": False, "quits": quits, "person_weeks": len(ex), "auc": round(auc, 3) if auc else None,
                "auc_lo": g["auc_lo"],
                "reason": (f"A model on {quits} departures did not predict them well enough out of sample "
                           f"(AUC {auc:.2f}, at least {g['auc_lo']:.2f} at 90%; needs {MIN_AUC} and more than 0.5) — "
                           "warning signs are shown by stated rules."
                           if auc is not None and g["auc_lo"] is not None else
                           "Too few departures in the folds to test a model.")}
    m = _fit([x for _k, _m, x, _y in ex], [y for _k, _m, _x, y in ex])
    return {"available": True, "model": m, "auc": round(auc, 3), "auc_lo": g["auc_lo"], "quits": quits,
            "person_weeks": len(ex)}


# ── who to protect ─────────────────────────────────────────────────────────

def _overrides(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        return {r["name_key"]: bool(r["protect"]) for r in conn.execute(
            "SELECT name_key, protect FROM retention_overrides WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
    finally:
        conn.close()


def set_protect(restaurant_id, name, protect, by=None, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO retention_overrides (restaurant_id, name_key, protect, updated_by, updated_at) "
                     "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, name_key) DO UPDATE SET "
                     "protect=excluded.protect, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (restaurant_id, _key(name), 1 if protect else 0, (by or "")[:120] or None))
        conn.commit()
    finally:
        conn.close()
    return {"name": name, "protect": bool(protect)}


def _valued(restaurant_id, data, key, today, db_path=None):
    try:
        import models
        scores = {_key(n): v for n, v in (models.get_operational_scores(restaurant_id, db_path or _models_db()) or {}).items()}
    except Exception:
        scores = {}
    sc = scores.get(key)
    try:
        sc = float(sc.get("score") if isinstance(sc, dict) else sc) if sc is not None else None
    except (TypeError, ValueError):
        sc = None
    weeks = {(_monday(date.fromisoformat(str(r.get("date"))[:10]))) for r in data["by_person"].get(key) or []
             if str(r.get("date") or "")[:10] >= (today - timedelta(days=365)).isoformat()}
    if sc is not None and sc >= VALUED_MIN_SCORE:
        return True, f"rated {sc:g}"
    if len(weeks) >= VALUED_MIN_WEEKS:
        return True, f"on the clock {len(weeks)} weeks of the last year"
    return False, None


def assess(restaurant_id, today=None, db_path=None) -> dict:
    """{basis, model: {...}, people: [{name, level, risk, signals, reasons,
    valued, protect}]} for everyone active on the roster now."""
    today = today or date.today()
    data = _load(restaurant_id, today, db_path)
    m = model(restaurant_id, today=today, db_path=db_path, data=data)
    try:
        import staff_settings
        roster = [e["name"] for e in staff_settings.roster(restaurant_id, db_path=db_path or _models_db()) if e.get("active", True)]
    except Exception:
        roster = []
    over = _overrides(restaurant_id, db_path)
    people = []
    for name in roster:
        key = _key(name)
        s = signals(data, key, today)
        if s is None:
            continue
        reasons = []
        if s["hours_cut"] >= RULE_CUT:
            reasons.append(f"hours down from about {s['hours_usual']:g} to {s['hours_recent']:g} a week")
        if s["drops"] >= RULE_DROPS:
            reasons.append(f"{s['drops']} shifts given up in 8 weeks")
        if s["unwanted_share"] >= RULE_UNWANTED:
            reasons.append(f"{int(round(s['unwanted_share'] * 100))}% of recent shifts on slots they keep dropping or didn't ask for")
        if s["promises_broken"]:
            reasons.append(f"{s['promises_broken']} published shift{'s' if s['promises_broken'] != 1 else ''} changed on them")
        risk = None
        if m.get("available"):
            risk = round(_prob(m["model"], [float(s[f]) for f in FEATURES]), 3)
            level = "high" if risk >= HIGH_PROB else ("watch" if risk >= WATCH_PROB else "none")
        else:
            level = "high" if len(reasons) >= 2 else ("watch" if reasons else "none")
        valued, why = _valued(restaurant_id, data, key, today, db_path)
        protect = over.get(key, valued and level == "high")
        people.append({"name": name, "level": level, "risk": risk, "signals": s, "reasons": reasons,
                       "valued": valued, "valued_because": why, "protect": bool(protect),
                       "protect_set_by_owner": key in over})
    people.sort(key=lambda p: ({"high": 0, "watch": 1, "none": 2}[p["level"]], -(p["risk"] or 0), p["name"]))
    basis = (f"A model of this restaurant's own departures (out-of-sample AUC {m['auc']:.2f}, at least "
             f"{m['auc_lo']:.2f} at 90%, from {m['quits']} people who left in the last year): the chance of leaving "
             f"{NOTICE_DAYS // 7} to {(NOTICE_DAYS + LABEL_DAYS) // 7} weeks from now, read from the weeks before any "
             "notice."
             if m.get("available") else m.get("reason"))
    return {"ok": True, "basis": basis, "model": {k: v for k, v in m.items() if k != "model"}, "people": people,
            "note": "Only you see this. Nobody is told they were flagged; the schedule just protects their hours and "
                    "the shifts they want, under every rule."}


def protect_signal(restaurant_id, today=None, db_path=None) -> dict:
    """{name: {"protect": True, "avoid": [(weekday, daypart)]}} — the solver's
    'retention' signal: the people to protect and the slots they keep
    dropping."""
    import learning_stats as _ls_gate
    if _ls_gate.learning_off():
        return {}  # learning_v2's off week (LEARNING-14)
    got = assess(restaurant_id, today=today, db_path=db_path)
    data_avoid = {}
    try:
        import schedule_intel
        data_avoid = {n: v.get("avoids") or [] for n, v in (schedule_intel.behaviour_preferences(restaurant_id, db_path=db_path or _models_db()) or {}).items()}
    except Exception:
        pass
    out = {}
    for p in got["people"]:
        if not p["protect"]:
            continue
        avoid = []
        for slot in data_avoid.get(p["name"]) or []:
            bits = str(slot).split()
            if len(bits) == 2:
                avoid.append((bits[0], "night" if bits[1] == "night" else "morning"))
        out[p["name"]] = {"protect": True, "avoid": avoid}
    return out
