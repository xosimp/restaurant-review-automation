"""
staffing_model.py — how many of a role a shift needs, from the work it will
carry: the half-hour workload of the forecast curve divided by what one
person of that role carries at the rush here, plus the crew a meal never
runs without (scheduler audit 10/10/26 RM-17, LEARNING-4, LOGIC-10,
LOGIC-11, LOGIC-12, MARKET-33).

The requirement used to be the usual crew scaled by one clamped ratio: a
restaurant that always ran one server too many on Tuesday lunch was asked
for that crew forever, a +40% game night lifted the line and the dish crew
as much as the bar, and a labor standard was "reported, never applied".
labor_standards measured the work per person-hour over a whole daypart —
an average that, applied to the night's total, understaffs the rush
(LOGIC-12: dinner with 60% of its guests between 6:30 and 8:30 needs more
than the night's average at 7pm).

measure() reads the last MODEL_WEEKS weeks of tickets and punches:

  work slots    per business date and half hour, the work the archive counts
                for each unit (labor_standards.WORK: guests seated for the
                floor, the bar's tickets for the bar, every ticket for the
                line).
  people slots  per date, family and half hour, the people punched in (the
                salaried and training codes out).
  lag           per family, the offset (LAG_STEPS) at which its people curve
                best follows the work curve — cooks on before the rush, dish
                after it (LOGIC-11) — kept only when it explains the curve
                better than no offset by LAG_MIN_GAIN over LAG_MIN_DAYS.
  standard      per family and daypart with a work unit, the work one person
                carried at the date's busiest half hour (aligned by the lag),
                the median over the dates (`own`), shrunk toward a prior —
                the cohort's (intelligence.schedule_priors, MIN_COHORT) or,
                without one, nothing — by STD_SHRINK dates. A Cavnar-era date
                whose outcome is not validated can only lower it
                (requirement_provenance.cap_unvalidated — LEARNING-5). The
                owner's own standard (labor_standards_json) wins.
  fixed         per family and daypart, the fewest people on at once through
                the core window on its quieter dates (FIXED_QUANTILE): the
                crew a meal never runs below whatever the volume.
  work          per weekday and daypart, the typical work of each unit and
                its half-hour shares.

needs() turns a date's demand ratio into people: work(t) = typical work x
ratio x share(t + lag); people(t) = max(fixed, ceil(work(t) / (standard x
0.5))) at each half hour served; the peak is people ON AT ONCE (contract
#2), split across the family's job codes as they usually split; the
different people for the daypart are the peak x the restaurant's own
stagger (usual different people / usual most on at once). A family with
no standard returns nothing: the requirement then stands on the usual crew
(history is the fallback, never the level).

earned() is MARKET-33's nightly read: the hours the work earned at the
standard against the hours scheduled and worked.

Reads only. Never raises from a public function.
"""
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

SLOT = 30
MODEL_WEEKS = 8
PARTS = ("morning", "night")
LAG_STEPS = (-60, -30, 0, 30, 60)
LAG_MIN_DAYS = 6
LAG_MIN_GAIN = 0.05
STD_MIN_DAYS = 6                 # labor_standards.STANDARD_MIN_DAYS
STD_SHRINK = 6.0
FIXED_QUANTILE = 0.25
MIN_SLOT_RATIO = 0.3             # staffing_curve.MIN_SLOT_RATIO
UNIT_FAMILY_TICKETS = "tickets"


def get_conn(db_path=None):
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _median(vals):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantile(vals, q):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _family(role):
    from intelligence.staffing import family_of
    return family_of(role)


def _part_of(minute) -> str:
    import schedule_time as _stime
    return "morning" if minute < _stime.daypart_split() else "night"


def _part_range(part):
    import schedule_time as _stime
    split = _stime.daypart_split()
    return (0, split) if part == "morning" else (split, 48 * 60)


# ── reading the archive and the punches ───────────────────────────────────

def work_slots(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    """{iso date: {unit: {minute: work}}} from the ticket archive, each ticket
    in the half hour it opened on its business day's timeline (an hour after
    midnight is 24+); cancelled tickets out. Units: "guests" (the dining
    room's), "tickets" (every ticket), "bar tickets"."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, substr(replace(opened_at, ' ', 'T'), 1, 13) AS hk, "
            "CASE WHEN CAST(substr(replace(opened_at, ' ', 'T'), 15, 2) AS INTEGER) >= 30 THEN 30 ELSE 0 END AS half, "
            "SUM(CASE WHEN COALESCE(is_bar,0)=0 THEN guest_count ELSE 0 END) AS guests, COUNT(*) AS tickets, "
            "SUM(CASE WHEN COALESCE(is_bar,0)=1 THEN 1 ELSE 0 END) AS bar FROM pos_tickets WHERE restaurant_id=? "
            "AND business_date>=? AND business_date<? AND COALESCE(cancelled,0)=0 AND opened_at IS NOT NULL "
            "GROUP BY business_date, hk, half", (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        bd = str(r["business_date"])[:10]
        try:
            hk = str(r["hk"])
            h = int(hk[11:13]) + 24 * max(0, (date.fromisoformat(hk[:10]) - date.fromisoformat(bd)).days)
        except (TypeError, ValueError):
            continue
        m = h * 60 + int(r["half"] or 0)
        day = out.setdefault(bd, {"guests": {}, "tickets": {}, "bar tickets": {}})
        day["guests"][m] = day["guests"].get(m, 0) + int(r["guests"] or 0)
        day["tickets"][m] = day["tickets"].get(m, 0) + int(r["tickets"] or 0)
        day["bar tickets"][m] = day["bar tickets"].get(m, 0) + int(r["bar"] or 0)
    return out


def people_slots(rows) -> dict:
    """{iso date: {family: {minute: people on}}} from punch rows (a person
    once per half hour; training codes and unreadable times out)."""
    import schedule_time as _stime
    from schedule_rules import is_training_role
    out = {}
    seen = {}
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        role = (r.get("role") or "").strip()
        who = " ".join(str(r.get("employee") or "").lower().split())
        if not d or not role or not who or is_training_role(role):
            continue
        fam = _family(role)
        sp = _stime.span(r)
        if not fam or not sp:
            continue
        s, e = sp
        t = s - (s % SLOT)
        while t < e:
            if s <= t + SLOT // 2 < e:
                key = (d, fam, t)
                if who not in seen.setdefault(key, set()):
                    seen[key].add(who)
                    slot = out.setdefault(d, {}).setdefault(fam, {})
                    slot[t] = slot.get(t, 0) + 1
            t += SLOT
    return out


def _corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def measure_lag(work: dict, people: dict, fam, unit, dates) -> dict:
    """{"lag", "gain", "days"}: the offset s (minutes) at which the family's
    people on at t best follow the work at t + s (positive: people on
    BEFORE the work — the line before the rush; negative: after it — the
    dish pit), as the mean correlation over the dates; 0 unless it beats
    no offset by LAG_MIN_GAIN over LAG_MIN_DAYS dates. Pure."""
    scores = {s: [] for s in LAG_STEPS}
    for d in dates:
        w = (work.get(d) or {}).get(unit) or {}
        p = (people.get(d) or {}).get(fam) or {}
        if not w or not p:
            continue
        ts = sorted(p)
        for s in LAG_STEPS:
            xs = [p[t] for t in ts]
            ys = [w.get(t + s, 0) for t in ts]
            c = _corr(xs, ys)
            if c is not None:
                scores[s].append(c)
    days = len(scores[0])
    if days < LAG_MIN_DAYS:
        return {"lag": 0, "gain": 0.0, "days": days}
    mean = {s: (sum(v) / len(v)) for s, v in scores.items() if len(v) >= LAG_MIN_DAYS}
    best = max(mean, key=lambda s: (mean[s], -abs(s)))
    gain = mean[best] - mean.get(0, 0.0)
    if best == 0 or gain < LAG_MIN_GAIN:
        return {"lag": 0, "gain": round(gain, 3), "days": days}
    return {"lag": best, "gain": round(gain, 3), "days": days}


def _core_slots(part, open_m=None, close_m=None):
    from shift_quality import floor_slots
    return list(floor_slots(part, open_m, close_m) or [])


def measure(restaurant_id, shifts=None, db_path=DB_PATH, today=None, weeks=MODEL_WEEKS, prior=None,
            work=None, people=None, live=None, ok_pairs=None, overrides=None) -> dict:
    """The model (see the module note): {"families": {family: {"lag",
    "lag_gain", "unit", "parts": {part: {"std", "own", "n", "prior",
    "source", "fixed", "text"}}}}, "work": {(weekday, part): {unit: {"total",
    "shares": {minute: share}, "days"}}}, "dates": n}. {} without a ticket
    archive and punches. `prior` — {family: {part: per hour}}, the cohort's;
    `live` / `ok_pairs` — go-live and the validated (date, part) pairs
    (requirement_provenance); `overrides` the owner's standards
    (labor_standards.overrides) — all read here when not given."""
    try:
        import labor_standards as _ls
        if today is None:
            today = _ls._today(restaurant_id)
        start = today - timedelta(weeks=int(weeks))
        if work is None:
            work = work_slots(restaurant_id, start, today, db_path=db_path)
        if not work:
            return {}
        if people is None:
            if shifts is None:
                import labor
                shifts = labor.load_shifts_for_restaurant(restaurant_id) or []
            try:
                import labor
                shifts, _h = labor._without_salaried(restaurant_id, shifts)
            except Exception:
                pass
            lo, hi = start.isoformat(), today.isoformat()
            people = people_slots([s for s in shifts if lo <= str(s.get("date") or "")[:10] < hi])
        dates = sorted(d for d in work if d in people)
        if not dates:
            return {}
        if live is None:
            import requirement_provenance as _prov
            live = _prov.go_live(restaurant_id, db_path=db_path)
            if live and ok_pairs is None:
                cav = [(d, p) for d in dates if d >= live for p in PARTS]
                ok_pairs = {k for k, v in _prov.readings(restaurant_id, cav, db_path=db_path).items() if v}
        if overrides is None:
            try:
                r = _models_mod.get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else \
                    _models_mod.get_restaurant(restaurant_id, db_path)
                overrides = _ls.overrides(r)
            except Exception:
                overrides = {}
        if prior is None:
            try:
                from intelligence import schedule_priors as _sp
                prior = _sp.standard_prior(restaurant_id, db_path=db_path) or {}
            except Exception:
                prior = {}
        return _build(work, people, dates, live, ok_pairs or set(), overrides or {}, prior or {})
    except Exception as e:
        print(f"[staffing_model] unavailable for {restaurant_id}: {e}")
        return {}


def _build(work, people, dates, live, ok_pairs, overrides, prior) -> dict:
    """The model from read slots (pure)."""
    import labor_standards as _ls
    import requirement_provenance as _prov
    fams = sorted({f for d in dates for f in (people.get(d) or {})})
    out = {"families": {}, "work": {}, "dates": len(dates)}
    for fam in fams:
        spec = _ls.WORK.get(fam)
        unit = spec[0] if spec else UNIT_FAMILY_TICKETS
        lag = measure_lag(work, people, fam, unit, dates)
        e = {"lag": lag["lag"], "lag_gain": lag["gain"], "unit": spec[0] if spec else None, "parts": {}}
        for part in PARTS:
            lo, hi = _part_range(part)
            thr, mins = {}, []
            core = set(_core_slots(part))
            for d in dates:
                p = (people.get(d) or {}).get(fam) or {}
                inpart = {t: n for t, n in p.items() if lo <= t < hi}
                if not inpart:
                    continue
                on_core = [inpart.get(t, 0) for t in core] if core else []
                if on_core and max(on_core) > 0:
                    mins.append(min(on_core))
                if not spec:
                    continue
                w = {t: v for t, v in ((work.get(d) or {}).get(unit) or {}).items() if lo <= t < hi and v > 0}
                if not w:
                    continue
                t_star = max(w, key=lambda t: (w[t], -t))
                n_star = inpart.get(t_star - lag["lag"], 0)
                if n_star <= 0:
                    continue
                thr[d] = (w[t_star] * 2.0) / n_star          # work per person-hour at the busiest half hour
            fixed = int(math.floor((_quantile(mins, FIXED_QUANTILE) or 0) + 1e-9)) if mins else 0
            entry = {"fixed": fixed, "fixed_days": len(mins)}
            own_std = (overrides.get(fam) or {}).get(part, (overrides.get(fam) or {}).get("all")) if spec else None
            if spec and own_std is not None:
                entry.update(std=float(own_std), source="yours", n=len(thr),
                             own=round(_median(list(thr.values())), 2) if thr else None)
            elif spec and thr:
                capped = _prov.cap_unvalidated(thr, live, ok_pairs, part=part)
                own = _median(list(capped.values()))
                n = len(capped)
                pr = (prior.get(fam) or {}).get(part)
                if pr is not None:
                    std = (n * own + STD_SHRINK * float(pr)) / (n + STD_SHRINK)
                elif n >= STD_MIN_DAYS:
                    std = own
                else:
                    std = None
                if std:
                    entry.update(std=round(std, 2), own=round(own, 2), n=n, prior=pr, source="measured",
                                 capped=sum(1 for d in thr if capped[d] < thr[d]))
            if entry.get("std"):
                who = _ls.UNIT_WORDS.get(fam, fam)
                meal = "lunch" if part == "morning" else "dinner"
                entry["text"] = (f"{entry['std']:g} {unit} per {who}-hour at {meal}'s busiest half hour"
                                 + (" (your standard)" if entry["source"] == "yours" else
                                    f" (measured here over {entry['n']} {'shift' if entry['n'] == 1 else 'shifts'}"
                                    + (", shrunk toward similar restaurants'" if entry.get("prior") is not None
                                       else "") + ")"))
            e["parts"][part] = entry
        out["families"][fam] = e
    # The typical work of each weekday's meal and its half-hour shares.
    by = {}
    for d in dates:
        wd = date.fromisoformat(d).strftime("%A")
        for unit, slots in (work.get(d) or {}).items():
            for part in PARTS:
                lo, hi = _part_range(part)
                inpart = {t: v for t, v in slots.items() if lo <= t < hi}
                tot = sum(inpart.values())
                if tot <= 0:
                    continue
                e = by.setdefault((wd, part, unit), {"totals": [], "shares": {}})
                e["totals"].append(tot)
                for t, v in inpart.items():
                    e["shares"].setdefault(t, []).append(v / tot)
    for (wd, part, unit), e in by.items():
        n = len(e["totals"])
        if n < 3:
            continue
        shares = {t: sum(v) / n for t, v in e["shares"].items()}       # absent days count 0
        s = sum(shares.values())
        out["work"].setdefault((wd, part), {})[unit] = {
            "total": _median(e["totals"]), "days": n,
            "shares": {t: v / s for t, v in shares.items()} if s > 0 else {}}
    return out


def lags(model) -> dict:
    """{family: minutes} — the measured lead (+) or lag (-) of each family."""
    return {f: e["lag"] for f, e in ((model or {}).get("families") or {}).items() if e.get("lag")}


def _clock(m):
    m = int(m) % (24 * 60)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def needs(model, weekday, part, ratios: dict, typical_roles: dict, concurrent_roles: dict = None,
          window=None) -> dict:
    """{role: {"half": {minute: people on}, "peak", "distinct", "reason",
    "family"}} for the roles of `typical_roles` ({role: usual different
    people}) whose family carries a standard on this weekday's meal.
    `ratios` — {role: demand ratio} (the date's, the meal's, the role's);
    `concurrent_roles` — the baseline's {role: {"peak"}} (labor's
    `concurrent`) for the stagger; `window` (lo, hi) the half hours served.
    Pure over the model."""
    import labor_standards as _ls
    fams = (model or {}).get("families") or {}
    work = ((model or {}).get("work") or {}).get((weekday, part)) or {}
    by_fam = {}
    for role, n in (typical_roles or {}).items():
        fam = _family(role)
        if fam:
            by_fam.setdefault(fam, {})[role] = max(0, int(n or 0))
    out = {}
    for fam, roles in by_fam.items():
        fe = fams.get(fam) or {}
        pe = (fe.get("parts") or {}).get(part) or {}
        unit = fe.get("unit")
        std = pe.get("std")
        w = work.get(unit) if unit else None
        if not std or not w or not w.get("shares"):
            continue
        weights = sum(roles.values()) or len(roles)
        ratio = sum(float(ratios.get(r, ratios.get("_", 1.0))) * (roles[r] or 1) for r in roles) / float(weights)
        lag = int(fe.get("lag") or 0)
        shares = w["shares"]
        peak_share = max(shares.values()) if shares else 0
        if peak_share <= 0:
            continue
        lo, hi = window if window else _part_range(part)
        fixed = int(pe.get("fixed") or 0)
        half = {}
        for t in range(lo - (lo % SLOT), hi, SLOT):
            sh = shares.get(t + lag, 0.0)
            if sh < MIN_SLOT_RATIO * peak_share:
                continue
            wt = float(w["total"]) * ratio * sh
            need = max(fixed, int(math.ceil(wt / (float(std) * 0.5) - 1e-9)), 1)
            half[t] = need
        if not half:
            continue
        peak = max(half.values())
        t_peak = max(half, key=lambda t: (half[t], -t))
        usual_distinct = sum(roles.values())
        usual_peak = sum(int(((concurrent_roles or {}).get(r) or {}).get("peak") or 0) for r in roles)
        stagger = (usual_distinct / float(usual_peak)) if usual_peak and usual_distinct >= usual_peak else 1.0
        distinct = max(peak, int(math.floor(peak * stagger + 0.5)))
        split_d = _ls.split_people(distinct, roles)
        per_half = {t: _ls.split_people(n, roles) for t, n in half.items()}
        who = _ls.UNIT_WORDS.get(fam, fam)
        reason = (f"{pe.get('text') or f'{std:g} {unit} per {who}-hour'}: about "
                  f"{float(w['total']) * ratio * shares.get(t_peak + lag, 0.0):.0f} {unit} expected in the half hour "
                  f"at {_clock(t_peak + lag)} needs {peak} on at once"
                  + (f" ({'on ' + str(abs(lag)) + ' min before' if lag > 0 else str(abs(lag)) + ' min after'} the "
                     "work arrives)" if lag else "")
                  + (f", at least {fixed} all meal" if fixed > 1 else ""))
        for role in roles:
            out[role] = {"half": {t: v[role] for t, v in per_half.items() if v.get(role)},
                         "peak": max((v.get(role, 0) for v in per_half.values()), default=0),
                         "distinct": split_d.get(role, 0), "reason": reason, "family": fam,
                         "std": std, "source": pe.get("source")}
    return out


def earned(model, day_iso, part, actual_work: dict, scheduled_hours: dict, worked_hours: dict) -> dict:
    """MARKET-33: {family: {"earned", "scheduled", "worked", "unit", "work"}}
    for one meal — the hours the night's measured work earned at the
    standard (work per person-hour at the rush, so the hours the rush
    needed, spread over the meal's own work curve: each half hour's work /
    standard, rounded up to a whole person, plus the fixed crew) against
    the hours scheduled and worked. `actual_work` {unit: {minute: work}};
    the hours {family: hours}. Pure."""
    fams = (model or {}).get("families") or {}
    out = {}
    lo, hi = _part_range(part)
    for fam, fe in fams.items():
        pe = (fe.get("parts") or {}).get(part) or {}
        unit, std = fe.get("unit"), pe.get("std")
        if not unit or not std:
            continue
        w = {t: v for t, v in ((actual_work or {}).get(unit) or {}).items() if lo <= t < hi and v > 0}
        if not w:
            continue
        lag = int(fe.get("lag") or 0)
        fixed = int(pe.get("fixed") or 0)
        hours = 0.0
        for t in sorted(w):
            n = max(fixed, int(math.ceil(w[t] / (float(std) * 0.5) - 1e-9)))
            hours += n * SLOT / 60.0
        out[fam] = {"earned": round(hours, 1), "scheduled": round(float((scheduled_hours or {}).get(fam) or 0), 1),
                    "worked": round(float((worked_hours or {}).get(fam) or 0), 1), "unit": unit,
                    "work": int(sum(w.values())), "std": std, "lag": lag}
    return out
