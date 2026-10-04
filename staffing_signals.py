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


def next_draft_date(restaurant_id, weekday, today=None):
    """The date of `weekday` in the week the NEXT schedule draft covers
    (schedule_engine._week_monday: the week starting next Monday, in the
    restaurant's own time) — what advice "on the next schedule" is about
    (re-audit 9/29/26, CROSSMODULE-21). None for a name that is not a
    weekday."""
    if weekday not in WEEKDAYS:
        return None
    if today is None:
        try:
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id).date()
        except Exception:
            today = date.today()
    try:
        from schedule_engine import _week_monday
        monday = _week_monday(today)
        monday = monday.date() if isinstance(monday, datetime) else monday
    except Exception:
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return monday + timedelta(days=WEEKDAYS.index(weekday))


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
    """Marketing's fill-a-night signals dated inside [start, end]: a guest
    text sent to fill the night (source 'campaign'). A scheduled post is not
    one (memory re-audit 9/29/26, CROSSMODULE-7) — a dish post three days a
    week suppressed every trim on those nights, even after it was cancelled."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM demand_signals WHERE restaurant_id=? AND date BETWEEN ? AND ? AND "
                            "source='campaign' ORDER BY date",
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
      suppress/why  a live campaign (a guest text) aimed at filling that night
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
    # A "+1" the owner answered (Not for us on the week's review, or one a
    # published week already carried — CROSSMODULE-10) is not asked again
    # while that answer holds.
    try:
        import rec_ledger
        silenced = set(rec_ledger.silenced_keys(restaurant_id, db_path=db_path or _models_mod.DB_PATH))
    except Exception:
        silenced = set()
    for c in service_clusters(restaurant_id, db_path=db_path):
        d = diags.get(c["category"])
        if not d:
            continue
        said = f"{d.get('cause') or ''} {d.get('recommended_action') or ''}"
        if not staffing.search(said):
            continue
        role = (c.get("role") or "server").replace("_", " ")
        for day in c["days"]:
            if day not in by_day:
                continue
            # The complaints' own daypart; when they name none, the day's
            # busiest daypart in the shifts actually worked here, else the
            # whole day — never a dinner a lunch-only restaurant does not
            # run (re-audit 9/29/26, CROSSMODULE-17).
            part = c.get("daypart") or busiest_daypart(restaurant_id, day, db_path=db_path)
            when = _PRETTY_PART.get(part, part) if part else "(all day)"
            if staff_add_key(day, part, role) in silenced:
                continue
            out.append({"source": "reviews", "day": day, "date": by_day[day], "daypart": part, "role": role,
                        "delta": 1, "category": c.get("category"),
                        "key": staff_add_key(day, part, role),
                        "text": f"+1 {role} {day} {when} — the reviews diagnosis: {c['text']}",
                        "confirm": d.get("what_would_confirm") or None,
                        "as_of": d.get("as_of")})
    return out


# The shifts actually worked that say which daypart a weekday runs heaviest.
BUSIEST_DAYPART_WEEKS = 8


def busiest_daypart(restaurant_id, weekday, today=None, db_path=None):
    """"morning" / "night": the daypart that carried the most worked hours
    on `weekday` over the last BUSIEST_DAYPART_WEEKS weeks of shift_facts
    (scheduled hours where no actual was recorded), or None when the
    record has none — the requirement is then for the whole day. Never
    raises."""
    today = today or date.today()
    since = (today - timedelta(weeks=BUSIEST_DAYPART_WEEKS)).isoformat()
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT business_date, shift_start, COALESCE(actual_hours, scheduled_hours) AS h "
                                "FROM shift_facts WHERE restaurant_id=? AND business_date >= ? AND business_date < ?",
                                (restaurant_id, since, today.isoformat())).fetchall()
        finally:
            conn.close()
        from schedule_rules import daypart_of
    except Exception:
        return None
    hours = {}
    for r in rows:
        try:
            if date.fromisoformat(str(r["business_date"])[:10]).strftime("%A") != weekday:
                continue
        except ValueError:
            continue
        part = daypart_of(r["shift_start"] or "")
        if part in ("morning", "night"):
            hours[part] = hours.get(part, 0.0) + float(r["h"] or 0)
    if not hours or not any(hours.values()):
        return None
    return max(hours.items(), key=lambda kv: kv[1])[0]


def staff_add_key(day, daypart, role) -> str:
    """The recommendation key of one soft "+1": staff_add:<day>:<daypart>:<role>
    (daypart "day" for the whole day) — what the ledger records when a
    published week applied it (CROSSMODULE-10)."""
    import rec_ledger
    return rec_ledger.rec_key("staff_add", f"{str(day).lower()}:{daypart or 'day'}:"
                                           f"{str(role or 'person').strip().lower().replace(' ', '_')}")


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
    out, by_ask = [], {}
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
            part = "morning" if re.search(r"\b(lunch|brunch|breakfast|morning|am)\b", a.get("text") or "", re.I) \
                else "night"
            role = next((w for w in ("dishwasher", "server", "bartender", "cook", "host", "busser", "runner",
                                     "expo", "prep") if re.search(rf"\b{w}s?\b", a.get("text") or "", re.I)), None)
            # One ask per night, daypart, role and action (re-audit 9/29/26,
            # CROSSMODULE-6): the report's key is wording-independent — the
            # same action on another night is the same key — and the model
            # rewords it every night, so three reports asking for one more
            # dishwasher Friday are ONE +1, asked in three reports, never
            # three. The newest report's words are kept (rows run newest
            # first).
            ask = (day, part, role, a.get("key") or (a.get("text") or "").strip().lower())
            if ask in by_ask:
                prev = by_ask[ask]
                prev["reports"] += 1
                prev["report_dates"].append(r["business_date"])
                continue
            req = {"source": "dsr", "day": day, "date": by_day[day], "daypart": part, "role": role, "delta": 1,
                   "text": f"{a.get('text')} (the {_mdy(r['business_date'])} report)",
                   "action_text": a.get("text"),
                   "report_date": r["business_date"], "reports": 1, "report_dates": [r["business_date"]],
                   "expires": (concerns + timedelta(days=7)).isoformat(), "key": a.get("key")}
            by_ask[ask] = req
            out.append(req)
    for req in out:
        if req["reports"] > 1:
            req["text"] = (f"{req['action_text']} (asked in {req['reports']} nightly reports, the latest "
                           f"{_mdy(req['report_date'])})")
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
                words = f.get("display") or f.get("label")
                if words:
                    e["events"].append(f"{_mdy(n)}: {words}")
        except Exception:
            pass
    return out


def day_splh_objective(objective, weekday):
    """The whole night's sales-per-labor-hour objective for `weekday` from
    schedule_economics.splh_objective's per-daypart ones — the day's sales
    over the hours each daypart's objective allows — so a night the DSR
    measured as one figure is set against one figure. None when there is no
    objective for that day."""
    if not (objective or {}).get("available"):
        return None
    parts = (objective.get("by_day") or {}).get(weekday) or {}
    sales = (objective.get("daypart_sales") or {}).get(weekday) or {}
    have = [p for p in parts if parts.get(p) and sales.get(p)]
    allowed = sum(float(sales[p]) / float(parts[p]) for p in have)
    return (sum(float(sales[p]) for p in have) / allowed) if allowed else None


def last_nights_block(restaurant_id, week_dates, today=None, db_path=None) -> str:
    """The schedule input "WHAT THE LAST NIGHTS SHOWED": per weekday of the
    week being drafted, what the last four weeks of nightly reports
    measured. "" when the DSR has measured nothing."""
    ln = last_nights(restaurant_id, today=today, db_path=db_path)
    if not ln:
        return ""
    try:
        import schedule_economics
        objective = schedule_economics.splh_objective(restaurant_id) or {}
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
            got = sum(e["splh"]) / len(e["splh"])
            want = day_splh_objective(objective, wd)
            bits.append(f"about ${got:,.0f} sales per labor hour"
                        + (f" against a ${want:,.0f} objective" if want else ""))
        if not bits:
            continue
        # What the night was is people's words (a closer's note, the
        # owner's name for an event): fenced (SHARED_MEM, memory in prompts).
        ev = f" — {_fence('; '.join(e['events'][:2]))}" if e["events"] else ""
        lines.append(f"  {wd}s ({e['nights']} night{'s' if e['nights'] != 1 else ''} reported): "
                     + ", ".join(bits) + ev)
    if not lines:
        return ""
    # Context (schedule audit 10/3/26 PR-7): "a reason to staff that day
    # differently" was a lever on numbers already made in code; who misses
    # or runs late is each person's ROSTER line and the one rule about them
    # a standing instruction.
    head = ("\n\nWHAT THE LAST NIGHTS SHOWED (the nightly reports of the last four weeks, per weekday — context: "
            "what actually happened on these nights; SHIFT REQUIREMENTS already carry each date's demand, and the "
            "summary may name one of these when it shaped the week's biggest decisions):\n")
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


def _fence(text) -> str:
    """Words a person or a model wrote, as a prompt must carry them."""
    import ai_guard
    return ai_guard.wrap_untrusted(" ".join(str(text or "").split()))


def soft_block(reqs) -> str:
    """The prompt block. Dates M/D/YY; the report's action and the
    diagnosis's "what would confirm it" are a model's own words, fenced —
    the requirement itself (+1 of a role on a night) is ours."""
    if not reqs:
        return ""
    lines = []
    for r in reqs:
        when = _PRETTY_PART.get(r['daypart'], r['daypart']) if r.get("daypart") else "all day"
        line = f"  {r['day']} {_mdy(r['date'])} {when}: "
        if r.get("source") == "dsr" and r.get("action_text"):
            asked = (f"asked in {r['reports']} nightly reports (one ask, one person — the latest, "
                     f"{_mdy(r.get('report_date'))}, in its words): " if (r.get("reports") or 1) > 1 else
                     f"the {_mdy(r.get('report_date'))} report asked, in its words: ")
            line += f"+1 {r['role'] or 'person'} — " + asked + _fence(r["action_text"])
        else:
            line += r["text"]
        if r.get("confirm"):
            line += " (to confirm: " + _fence(r["confirm"]) + ")"
        lines.append(line)
    # Which asks the week carries is read from its rows (applied) and shown
    # to the owner with everything else the week misses (schedule_output.
    # unmet_items) — not asked of a three-bullet summary (schedule audit
    # 10/3/26 PR-11).
    # Each ask is already a number (schedule_requirements, "+1 asked by …"
    # in its SHIFT REQUIREMENTS row, never firm — schedule audit 10/3/26
    # PR-7): "add the person" here asked for it a second time.
    return ("\n\nSOFT STAFFING REQUIREMENTS (the reviews diagnosis's and the nightly reports' asks — context: each is "
            "already counted in that shift's SHIFT REQUIREMENTS as \"+1 …\", a soft ask, never above a hard "
            "constraint; the owner is shown which ones the finished week carries):\n" + "\n".join(lines))


def applied(reqs, rows, typical=None) -> list:
    """Each soft requirement with `applied`: whether the draft put more of
    its role on that date and daypart than the typical headcount there (or,
    with no typical figure, any of that role at all). Read from the rows —
    the review names what the draft did, not what the model says it did."""
    from shift_quality import present_dayparts
    out = []
    for r in reqs or []:
        role = (r.get("role") or "").lower()
        part = r.get("daypart")
        # A whole-day requirement (no daypart known, CROSSMODULE-17) reads
        # every row that day, against the day's busiest daypart's typical.
        on = [x for x in rows or [] if (x.get("date") or "")[:10] == r["date"]
              and (not part or part in present_dayparts(x))
              and (not role or role in (x.get("role") or "").lower())]
        base = None
        if typical:
            slots = [typical.get((r["day"], part)) or {}] if part else \
                [v for (d, _p), v in typical.items() if d == r["day"]]
            vals = [v for slot in slots for k, v in (slot or {}).items() if role and role in str(k).lower()]
            base = max(vals) if vals else None
        got = len({(x.get("employee") or "").strip().lower() for x in on})
        out.append(dict(r, scheduled=got, typical=base,
                        applied=(got > base) if base is not None else (got > 0)))
    return out


# ── closing the loop: what a published week did with the asks ──────────────
#
# Reviews to Labor was open-loop (re-audit 9/29/26, CROSSMODULE-10): the
# draft said which "+1 server Friday dinner" it applied, and nothing kept it
# — whether the published week carried the extra person, or whether Friday's
# service complaints fell afterwards. Each review "+1" is now a
# recommendation in the ledger (staff_add:<day>:<daypart>:<role>, shown on
# the schedule review when the draft is written, carrying the complaint
# theme's share as its number), and a DSR "+1" keeps its report's own key.
# When a week is PUBLISHED, every ask its rows honoured is recorded as
# implemented under the published week (source_ref "schedule:<id>"); the
# ledger then starts the tracker on complaints:<category> (outcomes.
# autostart_implemented, under the slice gate), and the verdict reaches
# what_worked on the schedule and the review diagnosis (rec_learning,
# kept forever in its monthly summaries). Before and after, never proof.

def present_requirements(restaurant_id, reqs, db_path=None) -> dict:
    """Show the draft's review "+1"s on the schedule review
    (rec_ledger.present_many, surface schedule_review): {key: rec_id or
    None}. Never raises."""
    items = []
    for r in reqs or []:
        if r.get("source") != "reviews" or not r.get("key"):
            continue
        metric = None
        if r.get("category"):
            try:
                import metrics
                m = metrics.normalize(f"complaints:{r['category']}")
                metric = m if metrics.known(m) else None
            except Exception:
                metric = None
        items.append({"key": r["key"], "module": "schedule", "title": str(r.get("text") or "")[:200],
                      "evidence_sources": ["reviews", "labor"], "cross_module": True,
                      "expected_metric": metric})
    if not items:
        return {}
    try:
        import rec_ledger
        return rec_ledger.present_many(restaurant_id, items, "schedule_review",
                                       db_path=db_path or _models_mod.DB_PATH) or {}
    except Exception as e:
        log.warning("staffing_signals: requirements not presented for %s: %s", restaurant_id, e)
        return {}


def record_published(restaurant_id, history_id, schedule_csv=None, user_id=None, db_path=None) -> list:
    """A week was published: every soft "+1" its draft was asked for
    (schedule_history.review_json) that the PUBLISHED rows honour is
    recorded as implemented under the week (source_ref "schedule:<id>",
    idempotent) — the review ones under staff_add:…, a DSR one under its
    report's key. Returns the keys recorded. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT review_json, schedule_csv, week_start FROM schedule_history WHERE id=? "
                               "AND restaurant_id=?", (history_id, restaurant_id)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        log.warning("staffing_signals: published week unreadable for %s: %s", restaurant_id, e)
        return []
    if row is None:
        return []
    try:
        review = json.loads(row["review_json"] or "null") or {}
    except (TypeError, ValueError):
        review = {}
    reqs = [r for r in (review.get("soft_requirements") or []) if isinstance(r, dict) and r.get("key")]
    if not reqs:
        return []
    try:
        from schedule_versions import rows_from_csv
        rows = rows_from_csv(schedule_csv if schedule_csv is not None else row["schedule_csv"])
    except Exception as e:
        log.warning("staffing_signals: published rows unreadable for %s: %s", restaurant_id, e)
        return []
    typical = {}
    for r in reqs:
        if r.get("typical") is not None:
            typical.setdefault((r["day"], r.get("daypart")), {})[r.get("role") or ""] = r["typical"]
    done = []
    try:
        import rec_ledger
        for r in applied(reqs, rows, typical or None):
            if not r.get("applied"):
                continue
            meta = {"module": "schedule", "via": "published_week", "source": r.get("source"),
                    "day": r.get("day"), "date": r.get("date"), "daypart": r.get("daypart"),
                    "role": r.get("role"), "category": r.get("category"), "week_start": row["week_start"]}
            if rec_ledger.implemented(restaurant_id, r["key"], "schedule_review", user_id=user_id,
                                      source_ref=f"schedule:{history_id}", meta=meta,
                                      db_path=db_path or _models_mod.DB_PATH):
                done.append(r["key"])
    except Exception as e:
        log.warning("staffing_signals: published asks not recorded for %s: %s", restaurant_id, e)
    return done
