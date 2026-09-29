"""
staffing_signals.py — what the rest of the product knows about the nights a
schedule or a staffing cut is about (memory audit 9/29/26: reviews_to_labor,
dsr_to_schedule, mkt_to_staffing).

Three things each module knew and staffing never read:

  REVIEWS   A "slow service" cluster (7 of 11 reviews on Friday or Saturday
            dinner) sat beside Home's "Trim Saturday staffing", and labor
            advice never checked complaints. `trim_guard` is the check every
            trim makes first — Home's trim_day, the labor read's trims, the
            DSR's control_hours, the pre-dinner cut — and a review diagnosis
            with a staffing cause becomes a soft "+1 server Friday dinner"
            requirement for the draft (`review_requirements`).
  THE DSR   Three Friday reports said "short a dishwasher at 7pm; add one
            Friday" and Thursday's draft repeated the same Friday.
            `last_nights_block` is the schedule input "WHAT THE LAST NIGHTS
            SHOWED" (per weekday and daypart over four weeks of dsr_metrics:
            no-shows, late arrivals, overtime, sales per labor hour, labor
            against target), and an open DSR staffing action about next
            week's days is a soft requirement until the week it concerns has
            passed (`dsr_requirements`).
  MARKETING The owner texted 412 guests to fill Tuesday; the auto-draft
            staffed a slow Tuesday and Home kept saying "Trim Tuesday
            staffing". A campaign with a target day, or a post tagged with
            an occasion or a dish, is a demand_signals row
            (demand_signals.record_marketing) — the one write the schedule
            prompt, demand_by_date, the DSR's Tomorrow and predictions, the
            pre-shift and the prep list already read — and a live fill-a-
            night signal suppresses a trim of that night, saying why.

Soft requirements are exactly that: the prompt says so, and after the draft
`applied` checks which the draft actually honoured, so the review names them
from the rows, not from the model's own account.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

import models as _models_mod

log = logging.getLogger(__name__)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The complaint categories that are about staffing a floor.
SERVICE_CATEGORIES = ("service", "wait_time")
FLOOR_ROLES = ("server", "host", "bartender", "busser", "runner")
# How much a trim's rank drops beside a service complaint cluster on its day.
CLUSTER_RANK_PENALTY = 0.4
# The DSR nights "what the last nights showed" reads.
LAST_NIGHTS_WEEKS = 4
# A marketing signal is live from the day it is written through its night.
MARKETING_SOURCES = ("campaign", "post")

# The analyser's dayparts in the schedule's two.
_ANALYSER_TO_SCHEDULE = {"breakfast": "morning", "brunch": "morning", "lunch": "morning",
                         "happy_hour": "night", "dinner": "night", "late_night": "night"}
_PRETTY_PART = {"morning": "lunch", "night": "dinner"}


def get_conn(db_path=None):
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def schedule_daypart(analyser_daypart):
    """The schedule's daypart (morning / night) for the analyser's, or None."""
    return _ANALYSER_TO_SCHEDULE.get(str(analyser_daypart or "").strip().lower())


def _next_date(weekday, today=None):
    today = today or date.today()
    if weekday not in WEEKDAYS:
        return None
    return today + timedelta(days=(WEEKDAYS.index(weekday) - today.weekday()) % 7)


# ── reviews ─────────────────────────────────────────────────────────────────

def service_clusters(restaurant_id, db_path=None) -> list:
    """The complaint clusters that are about staffing a floor: a service or
    wait-time category, or any cluster whose reviews concentrate on a floor
    role — with the weekdays and the (schedule) daypart they concentrate on.
    [{category, label, mentions, days: [...], daypart: morning|night|None,
    role, text}]."""
    try:
        import review_intelligence as ri
        from analyser import category_label
        clusters = ri.complaint_clusters(restaurant_id, db_path=db_path or _models_mod.DB_PATH)
    except Exception as e:
        log.warning("staffing_signals: clusters unavailable for %s: %s", restaurant_id, e)
        return []
    out = []
    for c in clusters or []:
        role = ((c.get("role") or {}).get("value") or "").strip().lower() or None
        if c.get("category") not in SERVICE_CATEGORIES and role not in FLOOR_ROLES:
            continue
        if c.get("weekday_pair"):
            days = list(c["weekday_pair"]["days"])
        elif c.get("weekday"):
            days = [c["weekday"]["value"]]
        else:
            days = []
        if not days:
            continue
        part = schedule_daypart((c.get("daypart") or {}).get("value"))
        label = category_label(c.get("category"))
        when = " and ".join(f"{d}s" for d in days) + (f" at {_PRETTY_PART[part]}" if part else "")
        out.append({"category": c.get("category"), "label": label, "mentions": int(c.get("mentions") or 0),
                    "days": days, "daypart": part, "role": role,
                    "text": f"guests complain about {label} on {when} ({c.get('mentions')} reviews)"})
    return out


def live_fill_signals(restaurant_id, start, end, db_path=None) -> list:
    """Marketing's fill-a-night signals dated inside [start, end]."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM demand_signals WHERE restaurant_id=? AND date BETWEEN ? AND ? AND "
                            "source IN ('campaign','post') ORDER BY date",
                            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def trim_guard(restaurant_id, weekday, daypart=None, on_date=None, db_path=None) -> dict:
    """The check every staffing cut makes first — Home's trim_day, the labor
    read's trims, the DSR's control_hours, the pre-dinner cut.

    {suppress, why, caution, rank_penalty, campaign, cluster}:
      suppress/why  a live campaign or post aimed at filling that night
                    (the next `weekday`, or `on_date`): no trim is suggested,
                    and the reason says so;
      caution       a service, wait or floor-staff complaint cluster on that
                    weekday (and daypart, when both are known): the trim
                    stands but says it, and ranks lower (rank_penalty)."""
    out = {"suppress": False, "why": None, "caution": None, "rank_penalty": 0.0, "campaign": None, "cluster": None}
    if weekday not in WEEKDAYS:
        return out
    day = on_date if on_date is not None else _next_date(weekday)
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    sig = next((s for s in live_fill_signals(restaurant_id, day, day, db_path=db_path)), None) if day else None
    if sig:
        out.update(suppress=True, campaign={"label": sig.get("label"), "date": sig.get("date"),
                                            "source": sig.get("source")},
                   why=f"Not suggesting a {weekday} trim: {sig.get('label') or 'a campaign to fill it'} "
                       f"is aimed at {weekday} {_mdy(sig.get('date'))}.")
        return out
    for c in service_clusters(restaurant_id, db_path=db_path):
        if weekday not in c["days"]:
            continue
        if daypart and c["daypart"] and c["daypart"] != daypart:
            continue
        out.update(cluster=c, rank_penalty=CLUSTER_RANK_PENALTY,
                   caution=f"Before cutting: {c['text']}.")
        break
    return out


def _mdy(value):
    try:
        from time_utils import mdy
        return mdy(value)
    except Exception:
        return str(value or "")


def review_requirements(restaurant_id, week_dates, db_path=None) -> list:
    """A review diagnosis whose cause is a staffing one, on a weekday and
    daypart its complaints concentrate on, as a SOFT requirement for the
    draft: [{source: "reviews", day, date, daypart, role, delta: +1, text,
    confirm}] — "+1 server Friday dinner", with what would confirm it."""
    try:
        import review_intelligence as ri
        diags = {d["category"]: d for d in ri.get_diagnoses(restaurant_id, db_path=db_path or _models_mod.DB_PATH)}
    except Exception as e:
        log.warning("staffing_signals: diagnoses unavailable for %s: %s", restaurant_id, e)
        return []
    staffing = re.compile(r"\b(understaff\w*|short[- ]staff\w*|short[- ]handed|staffing|headcount|"
                          r"one (?:server|bartender|host|busser|runner)s? short|(?:add|another|extra|more) "
                          r"(?:server|bartender|host|busser|runner|staff)s?)\b", re.I)
    out = []
    by_day = {date.fromisoformat(d).strftime("%A"): d for d in (week_dates or [])}
    for c in service_clusters(restaurant_id, db_path=db_path):
        d = diags.get(c["category"])
        if not d:
            continue
        said = f"{d.get('cause') or ''} {d.get('recommended_action') or ''}"
        if not staffing.search(said):
            continue
        role = (c.get("role") or "server").replace("_", " ")
        part = c.get("daypart") or "night"
        for day in c["days"]:
            if day not in by_day:
                continue
            out.append({"source": "reviews", "day": day, "date": by_day[day], "daypart": part, "role": role,
                        "delta": 1,
                        "text": f"+1 {role} {day} {_PRETTY_PART.get(part, part)} — the reviews diagnosis: "
                                f"{c['text']}",
                        "confirm": d.get("what_would_confirm") or None,
                        "as_of": d.get("as_of")})
    return out


# ── the DSR ─────────────────────────────────────────────────────────────────

_CUT_WORDS = re.compile(r"\b(cut|trim|reduc\w*|fewer|send (?:\w+ )?home|drop|less|shorten|cap)\b", re.I)
_ADD_WORDS = re.compile(r"\b(add|another|extra|more|schedule (?:a|an|one|two)|bring in|call in)\b", re.I)


def _weekday_named(text):
    for d in WEEKDAYS:
        if re.search(rf"\b{d}s?\b", str(text or ""), re.I):
            return d
    return None


def action_direction(action) -> str:
    """"cut" / "add" / None for a DSR staffing action — control_hours is a
    cut by definition; adjust_staffing reads its own words."""
    kind = (action or {}).get("kind")
    text = f"{(action or {}).get('text') or ''} {(action or {}).get('why') or ''}"
    if kind == "control_hours":
        return "cut"
    if kind != "adjust_staffing":
        return None
    if _ADD_WORDS.search(text) and not _CUT_WORDS.search(text):
        return "add"
    if _CUT_WORDS.search(text):
        return "cut"
    return None


def dsr_requirements(restaurant_id, week_dates, today=None, db_path=None) -> list:
    """The open DSR staffing actions about the week being drafted, as soft
    requirements: an adjust_staffing ADD from a report in the last four
    weeks, not answered on any surface (rec_ledger.silenced_keys), about a
    weekday of the week — the weekday it names, else the night after the
    report — and not yet past the week it concerns. [{source: "dsr", day,
    date, daypart, role, delta, text, report_date, expires}]."""
    today = today or date.today()
    since = (today - timedelta(weeks=LAST_NIGHTS_WEEKS)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, narrative_json FROM dsr_reports WHERE restaurant_id=? AND "
                            "business_date >= ? AND narrative_json IS NOT NULL ORDER BY business_date DESC, id DESC",
                            (restaurant_id, since)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    try:
        import rec_ledger
        silenced = set(rec_ledger.silenced_keys(restaurant_id, db_path=db_path or _models_mod.DB_PATH))
    except Exception:
        silenced = set()
    by_day = {date.fromisoformat(d).strftime("%A"): d for d in (week_dates or [])}
    out, seen = [], set()
    for r in rows:
        try:
            n = json.loads(r["narrative_json"] or "null") or {}
        except (TypeError, ValueError):
            continue
        report_day = date.fromisoformat(r["business_date"])
        for a in (n.get("actions_tomorrow") or []):
            if not isinstance(a, dict) or action_direction(a) != "add":
                continue
            if a.get("key") and a["key"] in silenced:
                continue
            day = _weekday_named(a.get("text")) or (report_day + timedelta(days=1)).strftime("%A")
            concerns = _next_date(day, report_day + timedelta(days=1))
            # An action about a night expires after the week it concerns.
            if concerns is None or concerns + timedelta(days=7) < today or day not in by_day:
                continue
            if (day, (a.get("text") or "").lower()) in seen:
                continue
            seen.add((day, (a.get("text") or "").lower()))
            part = "morning" if re.search(r"\b(lunch|brunch|breakfast|morning|am)\b", a.get("text") or "", re.I) \
                else "night"
            role = next((w for w in ("dishwasher", "server", "bartender", "cook", "host", "busser", "runner",
                                     "expo", "prep") if re.search(rf"\b{w}s?\b", a.get("text") or "", re.I)), None)
            out.append({"source": "dsr", "day": day, "date": by_day[day], "daypart": part, "role": role, "delta": 1,
                        "text": f"{a.get('text')} (the {_mdy(r['business_date'])} report)",
                        "report_date": r["business_date"],
                        "expires": (concerns + timedelta(days=7)).isoformat(), "key": a.get("key")})
    return out


def _metric_by_night(conn, restaurant_id, metric, since):
    return {r["business_date"]: r["value"] for r in conn.execute(
        "SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric=? AND business_date>=? "
        "AND value IS NOT NULL", (restaurant_id, metric, since)).fetchall()}


def last_nights(restaurant_id, today=None, db_path=None) -> dict:
    """{weekday: {nights, no_shows, late, overtime_hours, labor_vs_target,
    splh, events}} over the last LAST_NIGHTS_WEEKS weeks of final DSR
    nights — only what the reports measured; an unmeasured figure is
    absent, never 0."""
    today = today or date.today()
    since = (today - timedelta(weeks=LAST_NIGHTS_WEEKS)).isoformat()
    conn = get_conn(db_path)
    try:
        m = {k: _metric_by_night(conn, restaurant_id, k, since) for k in
             ("labor.no_shows", "labor.late_arrivals", "labor.overtime_hours", "labor.vs_target_pts",
              "labor.hours", "sales.net")}
    except Exception:
        return {}
    finally:
        conn.close()
    nights = set().union(*[set(v) for v in m.values()]) if m else set()
    out = {}
    for n in sorted(nights):
        wd = date.fromisoformat(n).strftime("%A")
        e = out.setdefault(wd, {"nights": 0, "no_shows": None, "late": None, "overtime_hours": None,
                                "labor_vs_target": [], "splh": [], "events": []})
        e["nights"] += 1
        for key, field in (("labor.no_shows", "no_shows"), ("labor.late_arrivals", "late"),
                           ("labor.overtime_hours", "overtime_hours")):
            v = m[key].get(n)
            if v is not None:
                e[field] = (e[field] or 0) + float(v)
        if m["labor.vs_target_pts"].get(n) is not None:
            e["labor_vs_target"].append(float(m["labor.vs_target_pts"][n]))
        if m["sales.net"].get(n) and m["labor.hours"].get(n):
            e["splh"].append(float(m["sales.net"][n]) / float(m["labor.hours"][n]))
        # What that night was (event_memory, M5): a game, a holiday, rain.
        try:
            import event_memory
            for f in event_memory.night_facts(restaurant_id, n, db_path=db_path) or []:
                if f.get("label"):
                    e["events"].append(f"{_mdy(n)}: {f['label']}")
        except Exception:
            pass
    return out


def last_nights_block(restaurant_id, week_dates, today=None, db_path=None) -> str:
    """The schedule input "WHAT THE LAST NIGHTS SHOWED": per weekday of the
    week being drafted, what the last four weeks of nightly reports
    measured. "" when the DSR has measured nothing."""
    ln = last_nights(restaurant_id, today=today, db_path=db_path)
    if not ln:
        return ""
    try:
        import schedule_economics
        objective = (schedule_economics.splh_by_daypart(restaurant_id) or {})
    except Exception:
        objective = {}
    lines = []
    for d in week_dates or []:
        wd = date.fromisoformat(d).strftime("%A")
        e = ln.get(wd)
        if not e:
            continue
        bits = []
        if e["no_shows"]:
            bits.append(f"{int(e['no_shows'])} no-show{'s' if e['no_shows'] != 1 else ''}")
        if e["late"]:
            bits.append(f"{int(e['late'])} late arrival{'s' if e['late'] != 1 else ''}")
        if e["overtime_hours"]:
            bits.append(f"{e['overtime_hours']:g} overtime hours")
        if e["labor_vs_target"]:
            avg = sum(e["labor_vs_target"]) / len(e["labor_vs_target"])
            bits.append(f"labor {abs(avg):.1f} pts {'over' if avg > 0 else 'under'} target on average")
        if e["splh"]:
            bits.append(f"about ${sum(e['splh']) / len(e['splh']):,.0f} sales per labor hour")
        if not bits:
            continue
        ev = f" — {'; '.join(e['events'][:2])}" if e["events"] else ""
        lines.append(f"  {wd}s ({e['nights']} night{'s' if e['nights'] != 1 else ''} reported): "
                     + ", ".join(bits) + ev)
    if not lines:
        return ""
    head = ("\n\nWHAT THE LAST NIGHTS SHOWED (the nightly reports of the last four weeks, per weekday — what "
            "actually happened on these nights; a repeated no-show, late arrival or overtime on a weekday is a "
            "reason to staff that day differently, and say so in the summary):\n")
    return head + "\n".join(lines)


# ── the draft's soft requirements ───────────────────────────────────────────

def soft_requirements(restaurant_id, week_dates, today=None, db_path=None) -> list:
    """The reviews' and the DSR's staffing asks for the week being drafted."""
    out = []
    for fn in (review_requirements, dsr_requirements):
        try:
            out += fn(restaurant_id, week_dates, **({"today": today} if fn is dsr_requirements else {}),
                      db_path=db_path)
        except Exception as e:
            log.warning("staffing_signals: %s failed for %s: %s", fn.__name__, restaurant_id, e)
    return out


def soft_block(reqs) -> str:
    if not reqs:
        return ""
    lines = []
    for r in reqs:
        line = f"  {r['day']} {r['date']} {_PRETTY_PART.get(r['daypart'], r['daypart'])}: {r['text']}"
        if r.get("confirm"):
            line += f" (to confirm: {r['confirm']})"
        lines.append(line)
    return ("\n\nSOFT STAFFING REQUIREMENTS (from the reviews diagnosis and the nightly reports — add the person "
            "where the hours budget and the rules allow, never over a hard constraint; in the summary, name each "
            "one you applied and each you could not):\n" + "\n".join(lines))


def applied(reqs, rows, typical=None) -> list:
    """Each soft requirement with `applied`: whether the draft put more of
    its role on that date and daypart than the typical headcount there (or,
    with no typical figure, any of that role at all). Read from the rows —
    the review names what the draft did, not what the model says it did."""
    from shift_quality import present_dayparts
    out = []
    for r in reqs or []:
        role = (r.get("role") or "").lower()
        on = [x for x in rows or [] if (x.get("date") or "")[:10] == r["date"]
              and r["daypart"] in present_dayparts(x)
              and (not role or role in (x.get("role") or "").lower())]
        base = None
        if typical:
            slot = typical.get((r["day"], r["daypart"])) or {}
            base = next((v for k, v in slot.items() if role and role in str(k).lower()), None)
        got = len({(x.get("employee") or "").strip().lower() for x in on})
        out.append(dict(r, scheduled=got, typical=base,
                        applied=(got > base) if base is not None else (got > 0)))
    return out
