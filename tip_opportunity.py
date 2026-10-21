"""
tip_opportunity.py — the money a shift slot is worth to the person who
works it, so the rotation shares it out like weekends and closes
(scheduler audit 10/10/26 MARKET-17).

The fairness ledger counted weekends, closes and holidays per person.
Servers judge fairness by money: two people with the same weekends can earn
twice as much as each other when one always gets Friday dinner. The POS
punches carry each shift's tips, but the scheduler never read them.

What this measures (owner-facing only — never shown to staff):

  slot_values()   per role family × weekday × daypart (THE daypart rule,
                  schedule_time.present_dayparts), the mean tips + auto-
                  gratuity per worked shift and per hour over TIP_WEEKS,
                  from the punch archive (pos_punches; RPOWER today). A slot
                  with fewer than MIN_SLOT_SHIFTS shifts has no value (None,
                  never 0). Where the floor sections are recorded
                  (shift_sections), the same per section, informational: the
                  schedule assigns slots, the manager assigns sections.
  ledger()        per person, the OPPORTUNITY of the slots they worked — the
                  slot's mean, not their own tips, so a better server is not
                  rotated away from money they earned by being better — as
                  a share of their role's mean per shift.
  rotation()      per role family: the rich slots (mean ≥ RICH_RATIO × the
                  family's mean across its slots), who is next for one (the
                  lowest opportunity per shift first) and who should rest
                  from them (≥ REST_RATIO × the role's mean per shift and at
                  least REST_MIN_DOLLARS a shift above it), each with the
                  dollars that say why. Read by schedule_intel.rotation_plan,
                  so the solver, the scorer and the prompt all see it.

Tip payout and pooling are deliberately not built (ROADMAP: partnered).
Reads only; nothing calls a model.
"""
import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

TIP_WEEKS = 8
MIN_SLOT_SHIFTS = 4          # worked shifts of a slot before its value is stated
MIN_PERSON_SHIFTS = 4        # shifts in the window before somebody is in the tip rotation
MIN_GROUP = 2                # tipped people in a family before a rotation is planned
RICH_RATIO = 1.25            # a slot this far above its family's mean is a rich slot
REST_RATIO = 1.2             # opportunity per shift this far above the role's mean...
REST_MIN_DOLLARS = 10.0      # ...and at least this many dollars a shift above it: rest from rich slots
SHOW = 3
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _clock(stamp):
    """'2026-10-02 17:05:00' -> (date, minutes since midnight) or None."""
    try:
        t = datetime.strptime(str(stamp).replace("T", " ")[:16], "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None
    return t.date(), t.hour * 60 + t.minute


def _row_of(p):
    """A punch as a schedule-shaped row (date, shift_start, shift_end) on its
    business date, so THE daypart rule reads it like any shift."""
    import schedule_time as st
    a, b = _clock(p["clock_in"]), _clock(p["clock_out"])
    if a is None or b is None:
        return None
    try:
        bdate = date.fromisoformat(str(p["business_date"])[:10])
    except (TypeError, ValueError):
        return None
    return {"date": bdate.isoformat(), "employee": p["employee_name"], "role": p["role"],
            "shift_start": st.fmt_minutes(a[1]), "shift_end": st.fmt_minutes(b[1]),
            "hours": max(0.0, ((datetime.combine(b[0], datetime.min.time()) + timedelta(minutes=b[1]))
                               - (datetime.combine(a[0], datetime.min.time()) + timedelta(minutes=a[1]))
                               ).total_seconds() / 3600.0)}


def _punches(restaurant_id, start, end, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, employee_name, role, clock_in, clock_out, tips, grats FROM pos_punches "
            "WHERE restaurant_id=? AND business_date>=? AND business_date<? AND COALESCE(is_station,0)=0 "
            "AND clock_in IS NOT NULL AND clock_out IS NOT NULL", (restaurant_id, start, end)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("tip opportunity: punches unreadable rid=%s: %s", restaurant_id, e)
        return []
    finally:
        conn.close()


def _sections(restaurant_id, start, end, db_path=None) -> dict:
    """{(date, person key): section} from shift_sections."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, employee_key, employee_name, section FROM shift_sections WHERE "
                            "restaurant_id=? AND date>=? AND date<?", (restaurant_id, start, end)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {(str(r["date"])[:10], _key(r["employee_name"])): str(r["section"]).strip() for r in rows if r["section"]}


def _families(restaurant_id, db_path=None):
    try:
        import models
        import schedule_rules
        return schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
    except Exception:
        return {}


def _worked(restaurant_id, weeks=TIP_WEEKS, today=None, db_path=None):
    """[{date, day, part, family, employee, tips, hours}] — one per tipped
    punch in the window (a family with no tips at all is not tipped)."""
    import schedule_time as st
    from shift_quality import role_family
    # The restaurant's last complete business date, never the server's
    # calendar date - tomorrow after 7pm Central on a UTC host (audit #23
    # A3-14): the window slid a day and read tonight's half-archived punches.
    if today is None:
        try:
            import pos
            today = pos.local_complete_through(restaurant_id)
        except Exception:
            today = date.today()
    start = (today - timedelta(weeks=int(weeks))).isoformat()
    punches = _punches(restaurant_id, start, today.isoformat(), db_path)
    if not punches:
        return []
    fams = _families(restaurant_id, db_path)
    try:
        import people
        canon = people.canonical_names(restaurant_id, sorted({p["employee_name"] for p in punches if p["employee_name"]}),
                                       db_path=db_path)
    except Exception:
        canon = {}
    out, tipped = [], {}
    for p in punches:
        row = _row_of(p)
        if row is None or not (p["employee_name"] or "").strip():
            continue
        part = st.present_dayparts(row)[0]
        if part not in ("morning", "night"):
            continue
        fam = role_family(p["role"] or "", fams)
        if not fam:
            continue
        money = float(p["tips"] or 0) + float(p["grats"] or 0)
        tipped[fam] = tipped.get(fam, 0.0) + money
        out.append({"date": row["date"], "day": DAY_NAMES[date.fromisoformat(row["date"]).weekday()], "part": part,
                    "family": fam, "employee": canon.get(p["employee_name"], p["employee_name"]).strip(),
                    "tips": money, "hours": row["hours"]})
    return [w for w in out if tipped.get(w["family"], 0) > 0]


def slot_values(restaurant_id, weeks=TIP_WEEKS, today=None, db_path=None, worked=None) -> dict:
    """{available, reason?, weeks, families: {family: {mean, shifts, slots:
    {"Friday|night": {mean, per_hour, shifts}}, sections: {"Friday|night|Patio":
    {mean, shifts}}}}} — module doc."""
    worked = _worked(restaurant_id, weeks, today, db_path) if worked is None else worked
    if not worked:
        return {"available": False, "weeks": weeks,
                "reason": "No tips on the punches yet: tip opportunity needs the POS punch archive (RPOWER today)."}
    secs = {}
    if worked:
        lo = min(w["date"] for w in worked)
        hi = (date.fromisoformat(max(w["date"] for w in worked)) + timedelta(days=1)).isoformat()
        secs = _sections(restaurant_id, lo, hi, db_path)
    fams = {}
    for w in worked:
        f = fams.setdefault(w["family"], {"tips": 0.0, "n": 0, "slots": {}, "sections": {}})
        f["tips"] += w["tips"]
        f["n"] += 1
        s = f["slots"].setdefault(f"{w['day']}|{w['part']}", {"tips": 0.0, "hours": 0.0, "n": 0})
        s["tips"] += w["tips"]
        s["hours"] += w["hours"]
        s["n"] += 1
        sec = secs.get((w["date"], _key(w["employee"])))
        if sec:
            x = f["sections"].setdefault(f"{w['day']}|{w['part']}|{sec}", {"tips": 0.0, "n": 0})
            x["tips"] += w["tips"]
            x["n"] += 1
    out = {}
    for fam, f in fams.items():
        slots = {k: {"mean": round(s["tips"] / s["n"], 2), "shifts": s["n"],
                     "per_hour": round(s["tips"] / s["hours"], 2) if s["hours"] > 0 else None}
                 for k, s in f["slots"].items() if s["n"] >= MIN_SLOT_SHIFTS}
        if not slots:
            continue
        sections = {k: {"mean": round(s["tips"] / s["n"], 2), "shifts": s["n"]}
                    for k, s in f["sections"].items() if s["n"] >= MIN_SLOT_SHIFTS}
        # The family's mean across its measured slots, each slot counted once
        # (a busy slot worked by more people is not weighted up).
        mean = round(sum(s["mean"] for s in slots.values()) / len(slots), 2)
        out[fam] = {"mean": mean, "shifts": f["n"], "slots": slots, "sections": sections}
    if not out:
        return {"available": False, "weeks": weeks,
                "reason": f"Not enough tipped shifts yet: a slot needs {MIN_SLOT_SHIFTS} worked shifts before its tips are stated."}
    return {"available": True, "weeks": weeks, "families": out}


def ledger(restaurant_id, weeks=TIP_WEEKS, today=None, db_path=None, worked=None, values=None) -> dict:
    """{family: {person: {shifts, opportunity, per_shift, share, rich}}}: the
    OPPORTUNITY each person was given (the slot means of the shifts they
    worked, measured slots only), its share of the family's mean per shift,
    and how many of their shifts were rich slots."""
    worked = _worked(restaurant_id, weeks, today, db_path) if worked is None else worked
    values = slot_values(restaurant_id, weeks, today, db_path, worked=worked) if values is None else values
    if not values.get("available"):
        return {}
    out = {}
    for w in worked:
        fam = (values["families"] or {}).get(w["family"])
        if not fam:
            continue
        slot = fam["slots"].get(f"{w['day']}|{w['part']}")
        if not slot:
            continue
        e = out.setdefault(w["family"], {}).setdefault(w["employee"], {"shifts": 0, "opportunity": 0.0, "rich": 0})
        e["shifts"] += 1
        e["opportunity"] += slot["mean"]
        if slot["mean"] >= RICH_RATIO * fam["mean"]:
            e["rich"] += 1
    for fam, people in out.items():
        total_s = sum(p["shifts"] for p in people.values())
        role_mean = (sum(p["opportunity"] for p in people.values()) / total_s) if total_s else 0.0
        for p in people.values():
            p["opportunity"] = round(p["opportunity"], 2)
            p["per_shift"] = round(p["opportunity"] / p["shifts"], 2) if p["shifts"] else None
            p["share"] = round(p["per_shift"] / role_mean, 3) if role_mean and p["per_shift"] is not None else None
            p["role_mean"] = round(role_mean, 2)
    return out


def rotation(restaurant_id, weeks=TIP_WEEKS, today=None, db_path=None, roster=None) -> dict:
    """{family: {rich_slots: [{day, part, mean}], next_rich: [names],
    rest_from_rich: [names], people: {name: {...}}, role_mean, lines}} — who
    is next for a rich slot and who should rest from them. `roster` (a set of
    lower-cased names) limits it to people still on it. Empty when nothing
    is measured."""
    worked = _worked(restaurant_id, weeks, today, db_path)
    values = slot_values(restaurant_id, weeks, today, db_path, worked=worked)
    if not values.get("available"):
        return {}
    book = ledger(restaurant_id, weeks, today, db_path, worked=worked, values=values)
    out = {}
    for fam, info in values["families"].items():
        people = {n: p for n, p in (book.get(fam) or {}).items()
                  if p["shifts"] >= MIN_PERSON_SHIFTS and (not roster or _key(n) in roster)}
        if len(people) < MIN_GROUP:
            continue
        rich = sorted(({"day": k.split("|")[0], "part": k.split("|")[1], "mean": s["mean"], "shifts": s["shifts"]}
                       for k, s in info["slots"].items() if s["mean"] >= RICH_RATIO * info["mean"]),
                      key=lambda s: (-s["mean"], s["day"], s["part"]))
        if not rich:
            continue
        role_mean = next(iter(people.values()))["role_mean"]
        rest = sorted((n for n, p in people.items() if p["per_shift"] is not None
                       and p["per_shift"] >= REST_RATIO * role_mean and p["per_shift"] - role_mean >= REST_MIN_DOLLARS),
                      key=lambda n: (-people[n]["per_shift"], n))
        nxt = [n for n in sorted(people, key=lambda n: (people[n]["per_shift"] or 0, people[n]["rich"], n)) if n not in rest]
        lines = []
        top = rich[0]
        meal = "lunch" if top["part"] == "morning" else "dinner"
        if rest:
            r0 = people[rest[0]]
            lines.append(f"{rest[0]} has averaged ${r0['per_shift']:.0f} a shift in tip opportunity against the "
                         f"{fam}s' ${role_mean:.0f}; {nxt[0] if nxt else 'the next person'} is next for a "
                         f"{top['day']} {meal} (about ${top['mean']:.0f} in tips).")
        out[fam] = {"rich_slots": rich[:6], "next_rich": nxt[:SHOW * 2], "rest_from_rich": rest[:SHOW * 2],
                    "people": people, "role_mean": role_mean, "family_mean": info["mean"], "lines": lines,
                    "weeks": weeks}
    return out


def rich_slot_keys(fam_plan) -> set:
    """{(day, part)} of a family's rich slots — the solver's and scorer's test."""
    return {(s["day"], s["part"]) for s in (fam_plan or {}).get("rich_slots") or []}


def owner_view(restaurant_id, today=None, db_path=None) -> dict:
    """The Studio's read (owner-only): each tipped family's slots in dollars
    (M/D/YY-free — weekday and lunch/dinner only), each person's opportunity
    share, and the rotation's next and rest queues. Sections, where
    recorded, beside them."""
    values = slot_values(restaurant_id, today=today, db_path=db_path)
    if not values.get("available"):
        return {"ok": True, "available": False, "reason": values.get("reason")}
    rot = rotation(restaurant_id, today=today, db_path=db_path)
    fams = []
    for fam, info in sorted(values["families"].items()):
        r = rot.get(fam) or {}
        fams.append({"family": fam, "mean": info["mean"], "shifts": info["shifts"],
                     "slots": [{"day": k.split("|")[0], "part": k.split("|")[1], **s}
                               for k, s in sorted(info["slots"].items(), key=lambda kv: -kv[1]["mean"])],
                     "sections": [{"day": k.split("|")[0], "part": k.split("|")[1], "section": k.split("|")[2], **s}
                                  for k, s in sorted(info["sections"].items(), key=lambda kv: -kv[1]["mean"])][:12],
                     "next_rich": r.get("next_rich") or [], "rest_from_rich": r.get("rest_from_rich") or [],
                     "people": sorted(({"name": n, **p} for n, p in (r.get("people") or {}).items()),
                                      key=lambda p: -(p.get("per_shift") or 0)),
                     "lines": r.get("lines") or []})
    return {"ok": True, "available": True, "weeks": values["weeks"], "families": fams,
            "basis": (f"Tips plus auto-gratuity on the POS punches over the last {values['weeks']} weeks, per worked "
                      f"shift; a slot needs {MIN_SLOT_SHIFTS} shifts before it is stated. A person's opportunity is the "
                      "slot's average, not their own tips, so being good at the job is never rotated away."),
            "note": "Owner-only. Cavnar AI rotates the opportunity; it never pools or pays out tips."}
