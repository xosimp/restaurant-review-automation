"""
attendance_risk.py — expected attendance in coverage: who will probably
turn up, shift by shift, and when the crew needs a standby (scheduler audit
10/10/26 RM-18, LEARNING-14).

Attendance was measured well and used badly: weekday rates fed only the
labor screen, the solver read one overall rate and charged it only at 20%
or more and only for someone alone in a role (a cliff: 19% was "reliable"),
and coverage counted every scheduled person as 1.0 present — a crew of five
at 18% each (an expected 0.9 missing) read as fully covered.

  rates()            each person's absence rate by weekday, shrunk in a
                     hierarchy: the person's weekday toward the person
                     (WEEKDAY_SHRINK shifts), the person toward the
                     restaurant (staff_settings.NO_SHOW_PRIOR_SHIFTS), the
                     restaurant toward its cohort (intelligence.
                     schedule_priors.noshow_prior, behind MIN_COHORT;
                     RESTAURANT_SHRINK shifts). A person with no record is
                     the restaurant's rate, never 0.
  p_short()          P(heads < need) for one shift's people — the exact
                     Poisson-binomial over each person's own chance of
                     turning up (independence assumed, and said).
  shift_risks()      per date x daypart x role family: need (the
                     requirement the draft was written to), scheduled,
                     expected heads (the sum of each person's chance of
                     turning up), P(short).
  standby_needed()   a busy shift whose P(short by at least k) is at least
                     STANDBY_RISK needs a standby (or one more person, budget
                     allowing) — k scaled to the crew (short_by_k: one of
                     four matters, one of eight does not; schedule re-audit
                     10/11/26 LEARNING-19), and, where a value of the
                     shortfall and a cost of the standby are known, only
                     when the expected shortfall saved is worth the person.
  exposure()         the graded reliability cost every judge shares: 0
                     below GRADED_FROM, rising to 1 at shift_quality.
                     UNRELIABLE_RATE — never the 20% cliff.

Pure functions over plain data, and one reader (rates). Never raises from a
public function.
"""
from datetime import date

WEEKDAY_SHRINK = 4.0             # shifts of a person's overall rate behind their weekday rate
RESTAURANT_SHRINK = 30.0         # shifts of the cohort's rate behind a restaurant's own base
STANDBY_RISK = 0.35              # P(short by k or more) that asks for a standby
# The shortfall that matters scales with the crew (re-audit 10/11/26
# LEARNING-19): P(short at all) is P(any absence) = 1-(1-p)^n, which reached
# STANDBY_RISK at a 10% base with 4 people, 6.9% with 6, 5.2% with 8 — every
# big busy shift got a paid extra person whether one absence of eight
# mattered or not. k = this share of the need, rounded, at least 1.
STANDBY_SHORT_SHARE = 0.2
GRADED_FROM = 0.10               # a rate under this costs nothing


def exposure(rate) -> float:
    """0-1: how much a person's no-show rate weighs as a reliability risk —
    linear from GRADED_FROM to shift_quality.UNRELIABLE_RATE (1 there and
    above). Replaces the cliff at UNRELIABLE_RATE (LEARNING-14)."""
    try:
        r = float(rate or 0)
    except (TypeError, ValueError):
        return 0.0
    from shift_quality import UNRELIABLE_RATE
    if r <= GRADED_FROM:
        return 0.0
    if r >= UNRELIABLE_RATE:
        return 1.0
    return round((r - GRADED_FROM) / (UNRELIABLE_RATE - GRADED_FROM), 3)


LATE_GRADED_FROM = 0.10          # a late rate under this costs nothing


def late_exposure(rate) -> float:
    """0-1 for lateness, the same shape as exposure(): from LATE_GRADED_FROM
    to staff_settings.LATE_RISK_RATE (1 there and above) — lateness was a
    boolean at 25% (LEARNING-14)."""
    try:
        r = float(rate or 0)
    except (TypeError, ValueError):
        return 0.0
    from staff_settings import LATE_RISK_RATE
    if r <= LATE_GRADED_FROM:
        return 0.0
    if r >= LATE_RISK_RATE:
        return 1.0
    return round((r - LATE_GRADED_FROM) / (LATE_RISK_RATE - LATE_GRADED_FROM), 3)


def shrink(hits, weight, prior, k) -> float:
    """(hits + k x prior) / (weight + k)."""
    try:
        return (float(hits) + float(k) * float(prior)) / (float(weight) + float(k))
    except (TypeError, ValueError, ZeroDivisionError):
        return float(prior or 0.0)


def rates_from_tally(tally: dict, base: float, cohort=None, today=None) -> dict:
    """{"base", "cohort", "people": {name lower: {"all", "days": {weekday:
    rate}, "shifts"}}} from staff_settings.attendance_tally's weighted
    tally (absences, a call-out in full: a body missing either way). Pure."""
    from staff_settings import NO_SHOW_PRIOR_SHIFTS, redemption_shifts
    w_all = sum(float(t.get("w") or 0) for t in (tally or {}).values())
    base_r = float(base or 0.0)
    if cohort is not None:
        base_r = shrink(base_r * w_all, w_all, cohort, RESTAURANT_SHRINK)
    people = {}
    for name, t in (tally or {}).items():
        # Clean weeks since the last absence shrink toward the base too
        # (LEARNING-21), as staff_settings' rates do.
        k = NO_SHOW_PRIOR_SHIFTS + (redemption_shifts(t.get("last_miss"), today) if today else 0.0)
        p_all = shrink(t.get("w_absent") or 0.0, t.get("w") or 0.0, base_r, k)
        days = {}
        for wd, e in (t.get("days") or {}).items():
            days[wd] = round(shrink(e.get("w_absent") or 0.0, e.get("w") or 0.0, p_all, WEEKDAY_SHRINK), 3)
        people[" ".join(str(name).lower().split())] = {"all": round(p_all, 3), "days": days,
                                                       "shifts": int(t.get("shifts") or 0)}
    return {"base": round(base_r, 3), "cohort": cohort, "people": people}


def rates(restaurant_id, db_path=None, today=None) -> dict:
    """rates_from_tally over the restaurant's own attendance record, its
    base shrunk toward the cohort's when one is published. {} on failure."""
    try:
        import staff_settings
        kw = {"db_path": db_path} if db_path else {}
        rec = staff_settings.attendance_record(restaurant_id, today=today, **kw)
        cohort = None
        try:
            from intelligence import schedule_priors as _sp
            cohort = _sp.noshow_prior(restaurant_id, **kw)
        except Exception:
            cohort = None
        return rates_from_tally(rec.get("tally") or {}, (rec.get("base") or {}).get("absence") or 0.0, cohort,
                                today=rec.get("today"))
    except Exception as e:
        print(f"[attendance_risk] rates unavailable for {restaurant_id}: {e}")
        return {}


def p_for(r: dict, name, weekday) -> float:
    """One person's chance of missing a shift on `weekday`."""
    p = ((r or {}).get("people") or {}).get(" ".join(str(name or "").lower().split()))
    if not p:
        return float((r or {}).get("base") or 0.0)
    return float((p.get("days") or {}).get(weekday, p.get("all") or 0.0))


def heads_distribution(miss_probs) -> list:
    """[P(k turn up)] for k = 0..n — the Poisson-binomial over each person's
    chance of turning up (1 - miss). Pure."""
    dist = [1.0]
    for q in miss_probs:
        show = 1.0 - min(1.0, max(0.0, float(q)))
        nxt = [0.0] * (len(dist) + 1)
        for k, pk in enumerate(dist):
            nxt[k] += pk * (1.0 - show)
            nxt[k + 1] += pk * show
        dist = nxt
    return dist


def p_short(miss_probs, need) -> float:
    """P(fewer than `need` of these people turn up)."""
    need = int(need or 0)
    if need <= 0:
        return 0.0
    dist = heads_distribution(miss_probs)
    return round(sum(dist[:need]), 4) if need <= len(dist) else 1.0


def short_by_k(need) -> int:
    """The shortfall a crew of `need` notices (LEARNING-19): STANDBY_SHORT_SHARE
    of it, rounded, at least 1 — 1 for a crew up to 7, 2 from 8, 3 from 13."""
    return max(1, int(round(int(need or 0) * STANDBY_SHORT_SHARE)))


def p_short_by(miss_probs, need, k) -> float:
    """P(the shift is short by `k` or more): P(heads <= need - k)."""
    return p_short(miss_probs, int(need or 0) - int(k or 1) + 1)


def expected_short(miss_probs, need) -> float:
    """E[max(0, need - heads)] — the people the shift is expected to be
    short (Poisson-binomial)."""
    need = int(need or 0)
    dist = heads_distribution(miss_probs)
    return round(sum(pk * max(0, need - k) for k, pk in enumerate(dist)), 4)


def shift_risks(rows: list, requirements: list, r: dict, busy: set = None, families=None) -> list:
    """[{date, day, daypart, role, family, need, scheduled, expected, p_short,
    busy, people: [{employee, miss}]}] for each shift and role family the
    requirements name: the people on the floor for the daypart (shift_quality
    .present_dayparts), each at their own weekday rate. Pure."""
    from shift_quality import present_dayparts, role_family
    on = {}
    for row in rows or []:
        d = str(row.get("date") or "")[:10]
        who = (row.get("employee") or "").strip()
        if not d or not who:
            continue
        fam = role_family(row.get("role"), families)
        for part in present_dayparts(row):
            on.setdefault((d, part, fam), {})[who.lower()] = who
    out = []
    for req in requirements or []:
        d, part = req.get("date"), req.get("daypart")
        if not d or part not in ("morning", "night"):
            continue
        try:
            wd = date.fromisoformat(d).strftime("%A")
        except ValueError:
            continue
        need_by = {}
        for x in req.get("roles") or []:
            fam = role_family(x.get("role"), families)
            n = int(x.get("firm", x.get("required")) or 0)
            if n > 0:
                need_by[fam] = need_by.get(fam, 0) + n
        for fam, need in need_by.items():
            people = sorted((on.get((d, part, fam)) or {}).values())
            miss = [p_for(r, n, wd) for n in people]
            k = short_by_k(need)
            # With one more on, how much of the expected shortfall goes away
            # (the standby's worth, in people).
            saved = round(expected_short(miss, need) - expected_short(miss + [0.0], need), 4)
            out.append({"date": d, "day": wd, "daypart": part, "role": fam, "need": need,
                        "scheduled": len(people), "expected": round(sum(1.0 - q for q in miss), 2),
                        "p_short": p_short(miss, need), "short_by": k, "p_short_by": p_short_by(miss, need, k),
                        "expected_short": expected_short(miss, need), "standby_saves": saved,
                        "busy": (d, part) in (busy or set()),
                        "people": [{"employee": n, "miss": round(q, 3)} for n, q in zip(people, miss)]})
    out.sort(key=lambda x: (-x["p_short"], x["date"], x["daypart"], x["role"]))
    return out


def standby_needed(risks: list, value_of=None, cost_of=None) -> list:
    """The busy shifts whose P(short by short_by_k or more) reaches
    STANDBY_RISK and that are staffed to their need (a shift already short
    needs a person, not a standby): each a standby requirement
    (LEARNING-19). With `value_of(x)` ($ one person short costs that shift)
    and `cost_of(x)` ($ the standby costs), only where the expected
    shortfall the standby saves (standby_saves x value) is worth its cost;
    either unknown, the crew-scaled threshold alone decides."""
    out = []
    for x in risks or []:
        p = x.get("p_short_by", x.get("p_short"))
        if not (x.get("busy") and p is not None and p >= STANDBY_RISK and x["scheduled"] >= x["need"]):
            continue
        if value_of is not None and cost_of is not None:
            try:
                v, c = value_of(x), cost_of(x)
            except Exception:
                v = c = None
            if v is not None and c is not None and float(x.get("standby_saves") or 0) * float(v) < float(c):
                continue
        out.append(x)
    return out


def words(x: dict) -> str:
    """The owner's sentence for one at-risk shift."""
    from time_utils import mdy
    meal = "lunch" if x["daypart"] == "morning" else "dinner"
    k = int(x.get("short_by") or 1)
    p = x.get("p_short_by", x["p_short"])
    return (f"{x['day'][:3]} {mdy(x['date'])} {meal}: about a {int(round(p * 100))}% chance "
            + (f"{k} or more of the " if k > 1 else "fewer than ")
            + f"{x['need']} {x['role'].replace('_', ' ')}{'s' if x['need'] != 1 else ''} "
            + ("don't turn up" if k > 1 else "turn up") + f" (expected "
            f"{x['expected']:g} of {x['scheduled']} scheduled, from each person's own attendance on "
            f"{x['day']}s; assumes one person's absence says nothing about another's)")
