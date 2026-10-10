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
  standby_needed()   a busy shift whose P(short) is at least STANDBY_RISK
                     needs a standby (or one more person, budget allowing).
  exposure()         the graded reliability cost every judge shares: 0
                     below GRADED_FROM, rising to 1 at shift_quality.
                     UNRELIABLE_RATE — never the 20% cliff.

Pure functions over plain data, and one reader (rates). Never raises from a
public function.
"""
from datetime import date

WEEKDAY_SHRINK = 4.0             # shifts of a person's overall rate behind their weekday rate
RESTAURANT_SHRINK = 30.0         # shifts of the cohort's rate behind a restaurant's own base
STANDBY_RISK = 0.35              # P(fewer turn up than the shift needs) that asks for a standby
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


def rates_from_tally(tally: dict, base: float, cohort=None) -> dict:
    """{"base", "cohort", "people": {name lower: {"all", "days": {weekday:
    rate}, "shifts"}}} from staff_settings.attendance_tally's weighted
    tally (absences, a call-out in full: a body missing either way). Pure."""
    from staff_settings import NO_SHOW_PRIOR_SHIFTS
    w_all = sum(float(t.get("w") or 0) for t in (tally or {}).values())
    base_r = float(base or 0.0)
    if cohort is not None:
        base_r = shrink(base_r * w_all, w_all, cohort, RESTAURANT_SHRINK)
    people = {}
    for name, t in (tally or {}).items():
        p_all = shrink(t.get("w_absent") or 0.0, t.get("w") or 0.0, base_r, NO_SHOW_PRIOR_SHIFTS)
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
        return rates_from_tally(rec.get("tally") or {}, (rec.get("base") or {}).get("absence") or 0.0, cohort)
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
            out.append({"date": d, "day": wd, "daypart": part, "role": fam, "need": need,
                        "scheduled": len(people), "expected": round(sum(1.0 - q for q in miss), 2),
                        "p_short": p_short(miss, need), "busy": (d, part) in (busy or set()),
                        "people": [{"employee": n, "miss": round(q, 3)} for n, q in zip(people, miss)]})
    out.sort(key=lambda x: (-x["p_short"], x["date"], x["daypart"], x["role"]))
    return out


def standby_needed(risks: list) -> list:
    """The busy shifts whose P(short) reaches STANDBY_RISK and that are
    staffed to their need (a shift already short needs a person, not a
    standby): each a standby requirement."""
    return [x for x in risks or [] if x.get("busy") and x["p_short"] >= STANDBY_RISK
            and x["scheduled"] >= x["need"]]


def words(x: dict) -> str:
    """The owner's sentence for one at-risk shift."""
    from time_utils import mdy
    meal = "lunch" if x["daypart"] == "morning" else "dinner"
    return (f"{x['day'][:3]} {mdy(x['date'])} {meal}: about a {int(round(x['p_short'] * 100))}% chance fewer than "
            f"{x['need']} {x['role'].replace('_', ' ')}{'s' if x['need'] != 1 else ''} turn up (expected "
            f"{x['expected']:g} of {x['scheduled']} scheduled, from each person's own attendance on "
            f"{x['day']}s; assumes one person's absence says nothing about another's)")
