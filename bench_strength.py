"""
bench_strength.py — single points of failure, who to cross-train next and
when to hire (scheduler audit 10/10/26 MARKET-32).

One bartender who can close surfaces as a problem only the night that
person is out. The pieces were all here — the closers the owner chose,
who has worked beside a closer in a role not their own (schedule_intel.
mentoring), the roles each person has held, attendance, the roster's
departures — and nothing put them together.

  strength()   per role family × daypart, over the last WEEKS weeks of
               worked shifts (shift_facts): the people who cover it (MIN_
               SHIFTS or more), the nights a week it runs and the people a
               night it needs, the people that takes when nobody works more
               than NIGHTS_PER_PERSON nights a week — slack = people −
               needed. For a family with chosen closers (schedule_rules.
               chosen_closers), the closers against the nights they cover.
               critical: one person (or nobody) can do it, or slack < 0;
               fragile: slack 0 — one absence from a gap.
  cross_train() for each critical or fragile pool, the next people to
               train, ranked by shifts already worked beside a closer in
               that role, then rating (unrated counts as nothing), then
               shifts worked: "Train Maria to close bar — 6 shifts beside a
               closer".
  hiring()     per family: weekly hours worked over WEEKS weeks (least-
               squares trend), the capacity of the people there (each one's
               own 80th-percentile week, capped at 40) less what departures
               take (the family's departures in the last year, per week ×
               the average person's hours), and the week the trend crosses
               CAPACITY_SHARE of that — "hire by" that week less
               HIRE_LEAD_WEEKS, in M/D/YY. Never projected from fewer than
               MIN_TREND_WEEKS weeks.

Owner-facing (studio_bench 'strength'). Reads only; nothing calls a model.
"""
import logging
import math
from datetime import date, timedelta

log = logging.getLogger(__name__)

WEEKS = 12
MIN_SHIFTS = 3
NIGHTS_PER_PERSON = 4.5
CAPACITY_SHARE = 0.9
HIRE_LEAD_WEEKS = 3
HORIZON_WEEKS = 16
MIN_TREND_WEEKS = 6
MAX_WEEK_HOURS = 40.0


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _hours(r):
    for k in ("actual_hours", "scheduled_hours", "hours"):
        try:
            v = r.get(k)
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _q(vals, q):
    s = sorted(vals)
    if not s:
        return 0.0
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _inputs(restaurant_id, today, db_path=None) -> dict:
    out = {"rows": [], "roster": None, "closers": {}, "leaders": set(), "mentored": {}, "scores": {}, "left": [],
           "families": {}}
    try:
        import shift_facts
        out["rows"] = shift_facts.person_rows(restaurant_id, since=(today - timedelta(weeks=WEEKS)).isoformat(),
                                              until=(today - timedelta(days=1)).isoformat(), db_path=db_path)
    except Exception as e:
        log.warning("bench: shifts unreadable rid=%s: %s", restaurant_id, e)
    try:
        import staff_settings
        out["roster"] = {_key(e["name"]): e["name"] for e in staff_settings.roster(restaurant_id) if e.get("active", True)}
    except Exception:
        out["roster"] = None
    try:
        import models
        import schedule_rules
        r = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
        out["families"] = schedule_rules.role_families(r) or {}
        out["closers"] = {f: {_key(k) for k in v} for f, v in (schedule_rules.chosen_closers(restaurant_id, restaurant=r) or {}).items()}
        out["leaders"] = {_key(n) for n, v in (models.get_leader_flags(restaurant_id) or {}).items() if v}
        out["scores"] = {_key(n): v for n, v in (models.get_operational_scores(restaurant_id) or {}).items()}
    except Exception as e:
        log.warning("bench: rules unreadable rid=%s: %s", restaurant_id, e)
    try:
        import staff_settings
        out["settings"] = {_key(n): v for n, v in (staff_settings.get_all(restaurant_id) or {}).items()}
    except Exception:
        out["settings"] = {}
    try:
        import schedule_intel
        out["mentored"] = {_key(n): v for n, v in (schedule_intel.mentoring(restaurant_id) or {}).items()}
    except Exception:
        out["mentored"] = {}
    try:
        import models
        conn = models.get_conn() if not db_path or db_path == models.DB_PATH else models.get_conn(db_path)
        try:
            out["left"] = [(r["subject"], str(r["changed_at"])[:10]) for r in conn.execute(
                "SELECT subject, changed_at FROM change_log WHERE restaurant_id=? AND entity='roster' AND field='left' "
                "AND changed_at>=?", (restaurant_id, (today - timedelta(days=365)).isoformat())).fetchall()]
        finally:
            conn.close()
    except Exception:
        out["left"] = []
    return out


def _family(role, fams):
    from shift_quality import role_family
    return role_family(role or "", fams)


def strength(restaurant_id, today=None, db_path=None, data=None) -> dict:
    """{pools: [{family, daypart, people, names, nights, need, needed,
    slack, status, closers?, closer_names?, closers_needed?, why}],
    weeks}."""
    from shift_quality import present_dayparts
    today = today or date.today()
    data = data or _inputs(restaurant_id, today, db_path)
    roster = data.get("roster")
    by = {}
    for r in data["rows"]:
        k = _key(r.get("employee"))
        if not k or (roster is not None and k not in roster):
            continue
        part = present_dayparts(r)[0]
        if part not in ("morning", "night"):
            continue
        fam = _family(r.get("role"), data["families"])
        if not fam:
            continue
        e = by.setdefault((fam, part), {"people": {}, "nights": {}})
        e["people"][k] = e["people"].get(k, 0) + 1
        e["nights"].setdefault(str(r.get("date"))[:10], set()).add(k)
    pools = []
    for (fam, part), e in sorted(by.items()):
        members = [k for k, n in e["people"].items() if n >= MIN_SHIFTS]
        if not e["nights"]:
            continue
        nights_per_week = len(e["nights"]) / float(WEEKS)
        need = _q([len(v) for v in e["nights"].values()], 0.5)
        needed = int(math.ceil(need * nights_per_week / NIGHTS_PER_PERSON - 1e-9)) if need else 0
        slack = len(members) - needed
        names = [(roster or {}).get(k, k) for k in members]
        pool = {"family": fam, "daypart": part, "people": len(members), "names": sorted(names),
                "nights": round(nights_per_week, 1), "need": need, "needed": needed, "slack": slack}
        status, why = "ok", None
        if len(members) <= 1 and needed >= 1:
            status, why = "critical", (f"only {names[0] if names else 'nobody'} works {fam} "
                                       f"{'lunch' if part == 'morning' else 'dinner'}")
        elif slack < 0:
            status, why = "critical", f"{len(members)} people for {needed} needed"
        elif slack == 0 and needed:
            status, why = "fragile", "one absence from a gap"
        closers = data["closers"].get(fam)
        if closers is not None and part == "night":
            cl = sorted(k for k in closers if roster is None or k in roster)
            need_cl = int(math.ceil(nights_per_week / NIGHTS_PER_PERSON - 1e-9))
            pool.update({"closers": len(cl), "closer_names": [(roster or {}).get(k, k) for k in cl],
                         "closers_needed": need_cl})
            if len(cl) <= 1:
                status, why = "critical", (f"only {pool['closer_names'][0]} can close {fam}" if cl
                                           else f"nobody is chosen to close {fam}")
            elif len(cl) <= need_cl and status == "ok":
                status, why = "fragile", f"{len(cl)} closers for {pool['nights']:g} nights a week"
        pool["status"], pool["why"] = status, why
        pools.append(pool)
    order = {"critical": 0, "fragile": 1, "ok": 2}
    pools.sort(key=lambda p: (order[p["status"]], p["family"], p["daypart"]))
    return {"pools": pools, "weeks": WEEKS}


def cross_train(restaurant_id, today=None, db_path=None, data=None, pools=None) -> list:
    """[{family, daypart, kind: close|cover, who, because, score}] — the
    next people to train for each critical or fragile pool."""
    today = today or date.today()
    data = data or _inputs(restaurant_id, today, db_path)
    pools = pools if pools is not None else strength(restaurant_id, today, db_path, data)["pools"]
    roster = data.get("roster") or {}
    shifts_in = {}
    for r in data["rows"]:
        k = _key(r.get("employee"))
        fam = _family(r.get("role"), data["families"])
        if k and fam:
            shifts_in.setdefault(k, {}).setdefault(fam, 0)
            shifts_in[k][fam] += 1
    out = []
    for p in pools:
        if p["status"] == "ok":
            continue
        fam = p["family"]
        closing = "closers" in p and len(p.get("closer_names") or []) <= (p.get("closers_needed") or 1)
        have = {_key(n) for n in (p.get("closer_names") if closing else p.get("names")) or []}
        cands = []
        for k, name in roster.items():
            if k in have:
                continue
            beside = sum(n for role, n in (data["mentored"].get(k) or {}).items() if _family(role, data["families"]) == fam)
            worked = (shifts_in.get(k) or {}).get(fam, 0)
            if closing and not worked:
                continue           # a closer is trained from the people already in the role
            if not closing and not beside and not worked:
                continue
            sc = data["scores"].get(k)
            try:
                sc = float(sc) if sc is not None else None
            except (TypeError, ValueError):
                sc = None
            cands.append((beside, sc if sc is not None else 0.0, worked, name))
        cands.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3]))
        for beside, sc, worked, name in cands[:2]:
            bits = []
            if beside:
                bits.append(f"{beside} shift{'s' if beside != 1 else ''} beside a closer in it")
            if worked:
                bits.append(f"{worked} {fam} shift{'s' if worked != 1 else ''} in {WEEKS} weeks")
            if sc:
                bits.append(f"rated {sc:g}")
            out.append({"family": fam, "daypart": p["daypart"], "kind": "close" if closing else "cover",
                        "who": name, "because": ", ".join(bits),
                        "text": (f"Train {name} to close {fam}" if closing else f"Train {name} on {fam} "
                                 f"{'lunch' if p['daypart'] == 'morning' else 'dinner'}")
                        + (f" — {', '.join(bits)}" if bits else "")})
    return out


HEADROOM = 1.2          # with nothing stated, a person is assumed able to work 20% past their busiest week


def _capacity(data, key, weeks) -> tuple:
    """(hours a week this person can work, assumed?): their stated maximum
    (staff_settings max_hours), else their employment type (full 40, part
    29), else their busiest week here + HEADROOM — said as assumed."""
    st = (data.get("settings") or {}).get(key) or {}
    try:
        if st.get("max_hours"):
            return min(MAX_WEEK_HOURS, float(st["max_hours"])), False
    except (TypeError, ValueError):
        pass
    if st.get("employment_type") == "full":
        return MAX_WEEK_HOURS, False
    if st.get("employment_type") == "part":
        return 29.0, False
    busiest = max([v for v in weeks if v > 0] or [0.0])
    return min(MAX_WEEK_HOURS, busiest * HEADROOM), True


def hiring(restaurant_id, today=None, db_path=None, data=None) -> list:
    """[{family, weekly_hours: [..], trend_per_week, capacity, attrition_hours,
    weeks_left, hire_by, text}] — module doc."""
    from time_utils import mdy
    today = today or date.today()
    data = data or _inputs(restaurant_id, today, db_path)
    roster = data.get("roster")
    start = today - timedelta(weeks=WEEKS)
    week_of = lambda d: (date.fromisoformat(d) - start).days // 7       # noqa: E731
    fam_week, person_week, person_fam = {}, {}, {}
    for r in data["rows"]:
        d = str(r.get("date") or "")[:10]
        fam = _family(r.get("role"), data["families"])
        k = _key(r.get("employee"))
        if len(d) != 10 or not fam or not k:
            continue
        w = week_of(d)
        if not 0 <= w < WEEKS:
            continue
        h = _hours(r)
        fam_week.setdefault(fam, [0.0] * WEEKS)[w] += h
        person_week.setdefault((k, fam), [0.0] * WEEKS)[w] += h
        person_fam.setdefault(k, {}).setdefault(fam, 0.0)
        person_fam[k][fam] += h
    left_by_fam = {}
    for name, _d in data["left"]:
        fams = person_fam.get(_key(name)) or {}
        if fams:
            f = max(fams.items(), key=lambda kv: kv[1])[0]
            left_by_fam[f] = left_by_fam.get(f, 0) + 1
    out = []
    for fam, series in sorted(fam_week.items()):
        weeks_with = [i for i, v in enumerate(series) if v > 0]
        if len(weeks_with) < MIN_TREND_WEEKS:
            continue
        xs = list(range(WEEKS))
        mx, my = sum(xs) / WEEKS, sum(series) / WEEKS
        vx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, series)) / vx if vx else 0.0
        level = my + slope * (WEEKS - 1 - mx)
        people = [k for (k, f) in person_week if f == fam and (roster is None or k in roster)]
        cap, assumed = 0.0, 0
        for k in people:
            c, was_assumed = _capacity(data, k, person_week[(k, fam)])
            cap += c
            assumed += 1 if was_assumed else 0
        avg_person = (sum(sum(person_week[(k, fam)]) for k in people) / WEEKS / len(people)) if people else 0.0
        loss_per_week = left_by_fam.get(fam, 0) / 52.0 * avg_person
        weeks_left, hire_by = None, None
        for w in range(0, HORIZON_WEEKS + 1):
            demand_w = level + slope * w
            cap_w = cap - loss_per_week * w
            if demand_w > CAPACITY_SHARE * cap_w:
                weeks_left = w
                break
        if weeks_left is not None:
            hire_by = today + timedelta(weeks=max(0, weeks_left - HIRE_LEAD_WEEKS))
        text = None
        if weeks_left is not None:
            text = (f"{fam.capitalize()}: the hours worked ({level:,.0f} a week, {slope:+.1f} a week) reach "
                    f"{int(CAPACITY_SHARE * 100)}% of what your {len(people)} {fam}s can work"
                    + (f" ({assumed} of them assumed from their busiest week — set their maximum hours to sharpen it)"
                       if assumed else "")
                    + (f" in about {weeks_left} weeks" if weeks_left else " now")
                    + (f", with departures taking about {loss_per_week * 4:,.0f} hours a month" if loss_per_week else "")
                    + f" — hire by {mdy(hire_by.isoformat())}.")
        out.append({"family": fam, "weekly_hours": [round(v, 1) for v in series], "level": round(level, 1),
                    "trend_per_week": round(slope, 2), "capacity": round(cap, 1), "people": len(people),
                    "departures_year": left_by_fam.get(fam, 0), "attrition_hours_week": round(loss_per_week, 2),
                    "weeks_left": weeks_left, "capacity_assumed": assumed, "hire_by": hire_by.isoformat() if hire_by else None,
                    "hire_by_label": mdy(hire_by.isoformat()) if hire_by else None, "text": text})
    out.sort(key=lambda h: (h["weeks_left"] is None, h["weeks_left"] or 0, h["family"]))
    return out


def owner_view(restaurant_id, today=None, db_path=None) -> dict:
    today = today or date.today()
    data = _inputs(restaurant_id, today, db_path)
    st = strength(restaurant_id, today, db_path, data)
    return {"ok": True, "available": bool(st["pools"]), "pools": st["pools"],
            "cross_train": cross_train(restaurant_id, today, db_path, data, st["pools"]),
            "hiring": hiring(restaurant_id, today, db_path, data), "weeks": WEEKS,
            "basis": (f"From the last {WEEKS} weeks of worked shifts: who covers each role and shift, the closers you "
                      f"chose, who has worked beside a closer, and the roster's departures. Nobody is counted for "
                      f"more than {NIGHTS_PER_PERSON:g} nights a week; capacity is each person's stated maximum hours, "
                      f"else full-time 40 / part-time 29, else their busiest week here plus 20% (said as assumed), "
                      f"capped at {MAX_WEEK_HOURS:g}.")}
