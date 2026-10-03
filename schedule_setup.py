"""
schedule_setup.py — the owner's scheduling setup, said back to them
(schedule audit 10/3/26 F1).

The rules a draft is built and checked to read who runs the floor, who
closes for which role, which job codes are one role, who is training, the
days the managers always work, the leader rules and the floors a role never
goes below. Each of those is the owner's to set, and each was guessed or
silently off: manager status from a role-name pattern (P-7, E-14), closers
role-agnostic with 47 of 64 people marked (D-9), AM/PM job codes read as
separate roles (D-13), leader rules dormant until somebody was rated (D-10)
and never checked when saved (D-11), no floors set at all (D-42).

This module is what the owner sees of that setup: the "Managers: …" line to
confirm, the closer list with its data-quality warning and a suggestion
from the punches, the role families suggested from the POS job names, the
floors suggested from history, a leader rule checked against the team as it
is saved, and the lines a generation's review shows. It only reads; the
routes store what the owner sends back, and nothing here changes data.
"""
import math
from collections import Counter
from datetime import date, datetime, timedelta

import schedule_rules as _sr

# More of the roster than this marked to close reads as a list nobody
# reviewed: a closer is the one person chosen to close for their role (D-9).
CLOSER_SHARE_WARN = 0.30
# The punches a closer suggestion reads, and how often somebody must have
# been the last of their role out in them to be suggested as one.
CLOSER_WINDOW_WEEKS = 8
CLOSER_KEEP_MIN = 2
CLOSER_ADD_MIN = 4
# Floors suggested from history (D-42): the 25th percentile of each role's
# headcount per daypart over the last weeks; a weekday gets its own only
# with enough dates of its own.
FLOOR_WEEKS = 8
FLOOR_PERCENTILE = 25
FLOOR_MIN_DATES = 3
MAX_LEADER_RULES = 40


def _days(dates) -> list:
    return [datetime.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]


def coming_week(restaurant_id) -> list:
    """The week a generation would draft: next Monday to Sunday, in the
    restaurant's own time."""
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = date.today()
    mon = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return [(mon + timedelta(days=i)).isoformat() for i in range(7)]


def _constraints(restaurant_id, week_dates=None, c=None):
    if c is not None:
        return c
    dates = list(week_dates or coming_week(restaurant_id))
    return _sr.build_constraints(restaurant_id, dates, _days(dates))


def _and(names) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _has_availability(c, name) -> bool:
    key = (name or "").strip().lower()
    return bool(c.unavailable_days.get(key) or c.daypart_avail.get(key) or c.time_windows.get(key))


def _date_label(d) -> str:
    from time_utils import mdy
    return f"{datetime.strptime(d, '%Y-%m-%d').strftime('%a')} {mdy(d)}"


def roster_bases(restaurant_id, people) -> dict:
    """{name: schedule_rules.manager_basis} for roster entries
    (staff_settings.roster) — the Team screen's "Floor manager: automatic,
    their role is Manager FOH" — read the way build_constraints reads it,
    without building a week's rules."""
    import people as _people_mod
    import staff_settings as _ss
    held = {}
    try:
        for r in _people_mod.held_roles(restaurant_id):
            held.setdefault(r["key"], []).append(r["role"])
    except Exception:
        held = {}
    try:
        worked = _ss.worked_roles(restaurant_id) or {}
    except Exception:
        worked = {}
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = date.today()
    since = (today - timedelta(weeks=_sr.MANAGER_RECENT_WEEKS)).isoformat()
    out = {}
    for e in people or []:
        nk = _ss.name_key(e["name"])
        st = e.get("settings") or {}
        recent = [r for r, last in sorted((worked.get(nk) or {}).items(), key=lambda kv: kv[1] or "", reverse=True)
                  if (last or "") >= since]
        out[e["name"]] = _sr.manager_basis(e["name"], e.get("role"), st, sorted(held.get(nk) or []), recent,
                                           st.get("certifications") or [])
    return out


# ── who runs the floor (P-7, E-14, E-15, E-13, D-5) ────────────────────────

def manager_status(restaurant_id, week_dates=None, c=None) -> dict:
    """{managers, not_counted, acting, line, ask_standing} — who counts as
    the manager on the floor for the week, and why each one does (their
    role, a held role, a manager role worked lately, the floor manager
    certificate, or the owner's yes); department managers and people the
    owner said no to, named so the owner decides; who stands in on which
    dates; and the managers whose working days are nowhere on file."""
    c = _constraints(restaurant_id, week_dates, c)
    managers, not_counted = [], []
    for name in c.roster_names:
        key = name.strip().lower()
        b = c.manager_basis.get(key) or {}
        if key in c.managers:
            managers.append({"name": name, "role": c.managers[key], "basis": b.get("basis"), "why": b.get("why"),
                             "salaried": c.is_salaried(name), "standing_shifts": list(c.standing_shifts.get(key) or []),
                             "acting_dates": []})
        elif b.get("basis") in ("department", "set_not"):
            not_counted.append({"name": name, "role": b.get("role"), "basis": b["basis"], "why": b.get("why")})
    acting = []
    for key, days in sorted(c.acting_managers.items()):
        if days:
            acting.append({"name": _sr._display_name(c, key), "dates": sorted(days),
                           "label": ", ".join(_date_label(d) for d in sorted(days))})
    line = ("Managers: " + ", ".join(f"{m['name']} ({m['role']})" for m in managers)) if managers else \
        "Managers: nobody yet — set who runs the floor in Team"
    no_days = [m["name"] for m in managers if not m["standing_shifts"] and not _has_availability(c, m["name"])]
    ask = (f"Which days and hours do {_and(no_days)} work? Set {'their' if len(no_days) > 1 else 'the'} standing "
           "shifts in Team so the manager plan starts from the real week.") if no_days else None
    return {"managers": managers, "not_counted": not_counted, "acting": acting, "line": line,
            "ask_standing": ask, "missing_standing": no_days, "week_dates": list(c.week_dates)}


# ── closers, per role (D-9, L-9) ──────────────────────────────────────────

def closer_review(restaurant_id, c=None, weeks=CLOSER_WINDOW_WEEKS, today=None) -> dict:
    """The closers as the rules read them, the share of the roster marked
    (a warning past CLOSER_SHARE_WARN), the flags entered through support
    that do not count yet, and a suggestion per person from the punches:
    keep somebody who is the last of their role out, unmark somebody who
    never is, add somebody who often is. A suggestion only — the owner's
    choices are what the closers route stores."""
    c = _constraints(restaurant_id, c=c)
    from models import admin_leader_flags, get_restaurant
    roster = len(c.roster_names)
    flagged = len(c.closer_flags)
    share = (flagged / roster) if roster else 0.0
    warning = None
    if roster and share > CLOSER_SHARE_WARN:
        warning = (f"{flagged} of {roster} people ({round(share * 100)}%) are marked to close. A closer is the one "
                   "person chosen to close for their role — on until close and the last of their role to leave — so "
                   "a list this long checks nothing. Keep the real closers of the roles that close.")
    by_role = [{"role": _sr._family_label(c, fam), "family": fam,
                "closers": sorted(_sr._display_name(c, k) for k in keys)}
               for fam, keys in sorted(c.closers_by_role.items())]
    placed = set().union(*c.closers_by_role.values()) if c.closers_by_role else set()
    outside = sorted(_sr._display_name(c, k) for k in c.closer_flags - placed)
    try:
        chosen = _sr.closer_roles(get_restaurant(restaurant_id))
    except Exception:
        chosen = []
    try:
        pending = admin_leader_flags(restaurant_id)
    except Exception:
        pending = []
    return {"by_role": by_role, "flagged": flagged, "roster": roster, "share": round(share, 3),
            "warning": warning, "closer_roles": chosen,
            # Which roles the closer rule holds and why: the owner's choice,
            # else the roles their punches show on until close, else (no
            # history) every role somebody is marked to close for.
            "closer_roles_basis": c.closer_roles_basis,
            "closer_roles_in_force": [x["role"] for x in by_role],
            "outside_roles": outside, "pending_admin": pending,
            "suggestions": _closer_suggestions(restaurant_id, c, weeks, today)}


def _history(restaurant_id, weeks, today=None) -> list:
    try:
        import shift_facts
        since = ((today or date.today()) - timedelta(weeks=weeks)).isoformat()
        return shift_facts.person_rows(restaurant_id, since=since) or []
    except Exception:
        return []


def _closer_suggestions(restaurant_id, c, weeks, today=None) -> list:
    """Who was the last of their role out, per day, in the punches."""
    last_out = Counter()
    by_day = {}
    for r in _history(restaurant_id, weeks, today):
        fam = c.family(r.get("role"))
        e = _sr.end_minutes(r)
        if not fam or e is None or _sr.is_training_role(r.get("role")):
            continue
        by_day.setdefault((r.get("date"), fam), []).append((e, (r.get("employee") or "").strip().lower()))
    for (_d, fam), ends in by_day.items():
        last = max(e for e, _n in ends)
        for e, n in ends:
            if n and e >= last - 15:
                last_out[(n, fam)] += 1
    out = []
    for name in c.roster_names:
        key = name.strip().lower()
        mine = {fam: n for (k, fam), n in last_out.items() if k == key}
        fam, n = max(mine.items(), key=lambda kv: kv[1]) if mine else (None, 0)
        label = _sr._family_label(c, fam) if fam else None
        if key in c.closer_flags:
            keep = n >= CLOSER_KEEP_MIN
            out.append({"name": name, "family": fam, "role": label, "closes": n, "action": "keep" if keep else "unmark",
                        "reason": (f"the last {label} out {n} times in {weeks} weeks" if keep else
                                   f"the last of their role out {n} time{'s' if n != 1 else ''} in {weeks} weeks")})
        elif n >= CLOSER_ADD_MIN:
            out.append({"name": name, "family": fam, "role": label, "closes": n, "action": "add",
                        "reason": f"the last {label} out {n} times in {weeks} weeks, not marked to close"})
    order = {"unmark": 0, "add": 1, "keep": 2}
    return sorted(out, key=lambda s: (order[s["action"]], -s["closes"], s["name"].lower()))


# ── role families (D-13) ──────────────────────────────────────────────────

def suggest_role_families(restaurant_id, c=None) -> dict:
    """Every job code in use here — the roster's, the POS job list's
    (people.held_roles, mirrored by pos_archive.sync_roles), the punches' —
    grouped into roles: the owner's map where they set one, else the code
    with its daypart words taken off ("Server AM", "Server PM" → Server).
    A group of two or more codes is a suggestion for the owner to confirm."""
    from shift_quality import role_family
    c = _constraints(restaurant_id, c=c)
    stored = dict(c.role_families)
    roles = sorted(set(c.role_names.values()), key=str.lower)
    groups = {}
    for r in roles:
        groups.setdefault(role_family(r, stored), []).append(r)
    families = [{"family": f, "label": _sr._family_label(c, f), "roles": rs,
                 "source": "yours" if any(r.lower() in stored for r in rs) else "suggested"}
                for f, rs in sorted(groups.items())]
    return {"families": families, "stored": stored, "roles": roles,
            "merged": [f for f in families if len(f["roles"]) > 1]}


def clean_role_families(raw) -> dict:
    """{role: family} from what a client sent, or raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("Send families as {job code: role}.")
    if len(raw) > 200:
        raise ValueError("At most 200 job codes.")
    out = {}
    for k, v in raw.items():
        k, v = " ".join(str(k or "").split())[:60], " ".join(str(v or "").split())[:60]
        if not k:
            continue
        if not v:
            raise ValueError(f"{k}: name the role it belongs to.")
        out[k] = v
    return out


# ── floors from history (D-42) ────────────────────────────────────────────

def _percentile(values, pct=FLOOR_PERCENTILE) -> int:
    vals = sorted(values)
    if not vals:
        return 0
    return int(vals[max(0, math.ceil(pct / 100.0 * len(vals)) - 1)])


def suggest_role_floors(restaurant_id, weeks=FLOOR_WEEKS, c=None, today=None) -> dict:
    """{"floors": {role: {"morning", "night", "days"}}} — a "never below"
    per role family and daypart, pre-filled from history: the 25th
    percentile of how many of the role (any of its job codes — a Server AM
    on at dinner is a server at dinner) were on for that half of the day on
    the dates the restaurant traded it, over the last `weeks` weeks (a day
    the role was missing counts as 0), and a weekday's own figure where it
    differs. Training codes and manager roles (the manager rule covers
    them) are left out. A floor saved on a role is held on the job code
    that works each half of the day (build_constraints). A suggestion only:
    the owner confirms what is saved (the rules route's role_floors) —
    nothing is set from here."""
    from shift_quality import present_dayparts
    c = _constraints(restaurant_id, c=c)
    on = {}            # (date, part, family) -> people
    traded = {"morning": set(), "night": set()}
    fams = set()
    for r in _history(restaurant_id, weeks, today):
        role = " ".join(str(r.get("role") or "").split())
        d = str(r.get("date") or "")[:10]
        who = (r.get("employee") or "").strip().lower()
        if not role or not d or not who:
            continue
        for part in present_dayparts(r):
            if part not in traded:
                continue
            traded[part].add(d)
            if _sr.is_training_role(role) or _sr.manager_role_kind(role) in ("owner", "manager", "lead"):
                continue
            fam = c.family(role)
            c.role_names.setdefault(role.lower(), role)
            fams.add(fam)
            on.setdefault((d, part, fam), set()).add(who)
    floors = {}
    for fam in sorted(fams):
        spec = {"morning": 0, "night": 0, "days": {}}
        for part in ("morning", "night"):
            dates = sorted(traded[part])
            base = _percentile([len(on.get((d, part, fam), ())) for d in dates])
            spec[part] = base
            for wd in _sr.DAYS:
                mine = [d for d in dates if _days([d])[0] == wd]
                if len(mine) < FLOOR_MIN_DATES:
                    continue
                v = _percentile([len(on.get((d, part, fam), ())) for d in mine])
                if v != base:
                    spec["days"].setdefault(wd, {})[part] = v
        if spec["morning"] or spec["night"] or any(v for ds in spec["days"].values() for v in ds.values()):
            floors[_sr._family_label(c, fam)] = spec
    try:
        from models import get_restaurant
        current = _sr.role_floors(get_restaurant(restaurant_id))
    except Exception:
        current = {}
    return {"floors": floors, "current": current, "weeks": weeks, "percentile": FLOOR_PERCENTILE,
            "dates": {p: len(v) for p, v in traded.items()},
            "note": (f"From the last {weeks} weeks: on three of every four days you traded, at least this many were "
                     "on. Check each before you save it — a floor is a minimum the schedule is never built below.")}


# ── leader rules: checked as they are saved (D-11), loaded always (D-10) ───

def _score_of(scores, name):
    low = " ".join(str(name or "").split()).casefold()
    for k, v in (scores or {}).items():
        if " ".join(str(k).split()).casefold() == low:
            return v
    return None


def clean_leader_rules(rules) -> tuple:
    """(rules, errors): each rule as the engine reads it — role, count
    (1-10), days, daypart (morning/night), min_score (1-5), attribute
    (can_close), closing — or the reason it cannot be read."""
    from models import CAPABILITY_ATTRIBUTES
    if not isinstance(rules, list):
        return [], ["leader_rules must be a list"]
    if len(rules) > MAX_LEADER_RULES:
        return [], [f"at most {MAX_LEADER_RULES} leader rules"]
    out, errors = [], []
    for n, x in enumerate(rules, 1):
        if not isinstance(x, dict):
            errors.append(f"rule {n}: not a rule")
            continue
        role = " ".join(str(x.get("role") or "").split())[:60]
        if not role:
            errors.append(f"rule {n}: name the role")
            continue
        rule = {"role": role}
        days = x.get("days") or []
        if not isinstance(days, list) or any(str(d).strip().capitalize() not in _sr.DAYS for d in days):
            errors.append(f"rule {n}: days are weekday names")
            continue
        if days:
            rule["days"] = [d for d in _sr.DAYS if d in {str(x).strip().capitalize() for x in days}]
        part = str(x.get("daypart") or "").strip().lower() or None
        if part not in (None, "morning", "night"):
            errors.append(f"rule {n}: the time of day is morning or night")
            continue
        if part:
            rule["daypart"] = part
        try:
            count = 1 if x.get("count") in (None, "") else int(x.get("count"))
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= 10:
            errors.append(f"rule {n}: how many is 1 to 10")
            continue
        rule["count"] = count
        attr = str(x.get("attribute") or "").strip() or None
        if attr and (CAPABILITY_ATTRIBUTES.get(attr) or {}).get("kind") != "flag":
            errors.append(f"rule {n}: {attr} is not something a person is marked for")
            continue
        if attr:
            rule["attribute"] = attr
        ms = x.get("min_score")
        if ms not in (None, "") and not attr:
            try:
                ms = float(ms)
            except (TypeError, ValueError):
                ms = -1
            if not 1 <= ms <= 5:
                errors.append(f"rule {n}: the minimum score is 1 to 5")
                continue
            rule["min_score"] = int(ms) if ms == int(ms) else ms
        if x.get("closing"):
            rule["closing"] = True
        out.append(rule)
    return out, errors


def leader_rule_warnings(restaurant_id, rules, c=None, scores=None, flags=None) -> list:
    """[{"rule", "text", "unmet", "shifts", "able"}] for each rule the team
    as it stands cannot meet on every shift it covers: "only 1 Bartender AM
    scores 5 or above, so this rule can't be met on 6 of its 7 shifts".
    People are in a rule's role by family (a Bartender PM covers a Bartender
    AM rule's lunch — D-13), by what they hold or have worked; a shift
    counts as meetable when enough of them can work that weekday and half
    of the day, and the week as a whole cannot ask more shifts of them than
    their days in a row allow."""
    from models import get_operational_scores, get_leader_flags, get_restaurant, leader_rule_daypart
    c = _constraints(restaurant_id, c=c)
    scores = get_operational_scores(restaurant_id) if scores is None else scores
    flags = get_leader_flags(restaurant_id) if flags is None else flags
    try:
        closed = set(_sr.closures(get_restaurant(restaurant_id)).get("closed_weekdays") or [])
    except Exception:
        closed = set()
    max_run = int(c.compliance.get("max_consecutive_days") or 6)
    out = []
    for rule in rules or []:
        fam = c.family(rule["role"])
        label = rule["role"]
        part = rule.get("daypart") or leader_rule_daypart(rule["role"])
        days = [d for d in (rule.get("days") or _sr.DAYS) if d not in closed]
        parts = [part] if part else ["morning", "night"]
        slots = [(d, p) for d in days for p in parts]
        if not slots:
            continue
        in_role = [n for n in c.roster_names
                   if fam in {c.family(r) for r in (c.known_roles.get(n.strip().lower()) or ())}]
        if rule.get("attribute"):
            able = [n for n in in_role if _score_of(flags, n)]
            bar = "marked to close"
        elif rule.get("min_score") is not None:
            able = [n for n in in_role if (_score_of(scores, n) or 0) >= float(rule["min_score"])]
            bar = f"scores {float(rule['min_score']):g} or above"
        else:
            able = list(in_role)
            bar = "is in the role"
        need = int(rule.get("count") or 1)

        def _free(n, d, p):
            key = n.strip().lower()
            if d in (c.unavailable_days.get(key) or set()):
                return False
            choice = (c.daypart_avail.get(key) or {}).get(d)
            return choice != "off" and not (choice in ("morning", "night") and choice != p)

        short = [s for s in slots if sum(1 for n in able if _free(n, *s)) < need]
        capacity = sum(min(max_run, len({d for d, p in slots if _free(n, d, p)})) for n in able)
        extra = math.ceil(max(0, need * len(slots) - capacity) / need)
        unmet = min(len(slots), max(len(short), extra))
        if not unmet:
            continue
        n_shifts = len(slots)
        if rule.get("min_score") is not None and not rule.get("attribute") and \
                not any(_score_of(scores, n) is not None for n in in_role):
            text = (f"Nobody in {label} is rated yet, so this rule can't be checked on any of its {n_shifts} shifts "
                    f"until you rate them.")
        elif not able:
            text = (f"Nobody in {label} {bar}, so this rule can't be met on any of its {n_shifts} shifts"
                    + (f" — try {int(float(rule['min_score'])) - 1} or above." if rule.get("min_score") and
                       float(rule["min_score"]) > 1 and not rule.get("attribute") else "."))
        else:
            text = (f"Only {len(able)} {label} {bar}, so this rule can't be met on {unmet} of its {n_shifts} shifts.")
        out.append({"rule": rule, "text": text, "unmet": unmet, "shifts": n_shifts, "able": sorted(able)})
    return out


def leader_rules_status(restaurant_id, roster_names=None, rules=None) -> dict:
    """Whether the leader rules can be judged, said on the generate screen
    (D-10): every rule is loaded whether or not anyone is rated, but a rule
    in scores judges nobody until someone is rated — and ratings entered
    through support count only once the owner takes them as theirs
    (models.adopt_admin_ratings, which takes closer flags too)."""
    from models import (get_shift_leader_rules, get_capabilities, admin_leader_flags, get_leader_flags)
    rules = get_shift_leader_rules(restaurant_id) if rules is None else list(rules)
    score_rules = [r for r in rules if r.get("min_score") is not None and not r.get("attribute")]
    attr_rules = [r for r in rules if r.get("attribute")]
    caps = get_capabilities(restaurant_id, attribute="overall")
    roster = {" ".join(str(n).split()).casefold() for n in (roster_names or [])}

    def _on(n):
        return not roster or " ".join(str(n).split()).casefold() in roster

    rated = sum(1 for n, a in caps.items() if _on(n) and (a.get("overall") or {}).get("score") is not None
                and (a.get("overall") or {}).get("authority") != "admin")
    admin = sum(1 for n, a in caps.items() if _on(n) and (a.get("overall") or {}).get("score") is not None
                and (a.get("overall") or {}).get("authority") == "admin")
    closers = sum(1 for n in get_leader_flags(restaurant_id) if _on(n))
    admin_closers = sum(1 for n in admin_leader_flags(restaurant_id) if _on(n))
    lines = []
    if score_rules and not rated:
        lines.append(f"{len(score_rules)} leader rule{'s' if len(score_rules) != 1 else ''} that need a rating "
                     f"{'are' if len(score_rules) != 1 else 'is'} inactive: nobody on the roster is rated"
                     + (f" ({admin} rating{'s' if admin != 1 else ''} entered through support "
                        f"{'are' if admin != 1 else 'is'} waiting for you to count them as yours)" if admin else
                        " — rate your team in Team")
                     + ".")
    if attr_rules and not closers:
        lines.append("Your closing rule judges nobody: nobody on the roster is marked to close"
                     + (f" ({admin_closers} marked through support {'are' if admin_closers != 1 else 'is'} waiting "
                        "for you to count them as yours)" if admin_closers else "") + ".")
    return {"rules": len(rules), "score_rules": len(score_rules), "attribute_rules": len(attr_rules),
            "rated": rated, "admin_ratings": admin, "closers": closers, "admin_closers": admin_closers,
            "active": bool(rated) or not score_rules, "can_adopt": bool(admin or admin_closers), "lines": lines}


# ── the owner's staffing rules, read back (D-14) ──────────────────────────

def owner_rule_preview(restaurant_id, text, c=None) -> dict:
    """{"checked", "reads_as", "text"} — how a staffing rule the owner just
    wrote will be checked ("at least 2 Server PM on Sat at dinner/night"),
    or that it can't be, said back when it is saved so a rule read wrongly
    is caught then, not after a week drafted against it."""
    c = _constraints(restaurant_id, c=c)
    roles = set(c.role_names.values()) | set(c.role_floors or {})
    try:
        import staff_settings as _ss
        roles |= {e["role"] for e in _ss.roster(restaurant_id) if e.get("role")}
    except Exception:
        pass
    rule = _sr.parse_owner_rule(text, roles, c.role_families)
    if rule is None:
        return {"checked": False, "reads_as": None,
                "text": "Cavnar AI can't check this one automatically — a draft is asked to follow it, and the "
                        "review reminds you to check the week against it."}
    if rule.get("daypart") and rule.get("match", "role") != "role":
        code = _sr.role_for_daypart(rule["role"], rule["daypart"], roles, c.role_families)
        if code:
            rule["floor_role"] = code
    reads = _sr.rule_reads_as(rule)
    return {"checked": True, "reads_as": reads, "text": f"Checked on every draft as: {reads}."}


# ── what a generation's review says about the setup ───────────────────────

def close_times_missing(c) -> list:
    """Weekdays the week trades with no close time on file (D-43): nobody
    at close, the closers and the stays-after-close rule go unchecked on
    them."""
    out = []
    for d in c.week_dates or []:
        if d in (c.closed_dates or set()):
            continue
        day = _days([d])[0]
        if day not in (c.close_times or {}) and day not in out:
            out.append(day)
    return out


def setup_review(c, leader_status=None) -> dict:
    """{"items": [{kind, text, line, ...}], "lines": [text]} — the setup a
    week was drafted against, for the generation's review: who counts as
    the manager (to confirm), who was left out, who stands in, the managers
    with no working days on file, an unreviewed closer list, trading days
    with no close time, leader rules that judge nobody, how each of the
    owner's staffing rules is checked, who is training, and who is
    salaried-style. `line` False keeps an item out of the review's lines
    (shown where the payload puts it)."""
    items = []
    ms = manager_status(c.restaurant_id, c=c)
    items.append({"kind": "managers", "text": ms["line"], "line": False, "managers": ms["managers"]})
    if ms["not_counted"]:
        items.append({"kind": "managers_unconfirmed", "line": True,
                      "text": "Not counted as floor managers: " + "; ".join(
                          f"{m['name']} ({m['role']}) — {m['why']}" for m in ms["not_counted"][:4])})
    if ms["acting"]:
        items.append({"kind": "acting", "line": True, "text": "Standing in as the manager: " + "; ".join(
            f"{a['name']} {a['label']}" for a in ms["acting"])})
    if ms["ask_standing"]:
        items.append({"kind": "standing_missing", "line": True, "text": ms["ask_standing"],
                      "names": ms["missing_standing"]})
    if c.closers_by_role and c.closer_roles_basis == "history":
        roles = [_sr._family_label(c, f) for f in sorted(c.closers_by_role)]
        items.append({"kind": "closer_roles", "line": True, "roles": roles,
                      "text": f"The closer rule holds for {_and(roles)} — the roles your punches show on until close. "
                              "Choose the roles that close in Team → Closers."})
    roster = len(c.roster_names or [])
    if roster and len(c.closer_flags) / roster > CLOSER_SHARE_WARN:
        items.append({"kind": "closers_share", "line": True,
                      "text": f"{len(c.closer_flags)} of {roster} people are marked to close — a closer is the one "
                              "person chosen to close for their role. Review the closers in Team."})
    missing = close_times_missing(c)
    if missing:
        items.append({"kind": "close_times_missing", "line": True, "days": missing,
                      "text": f"No close time for {_and(missing)} — nobody at close, the closers and stays after close "
                              "aren't checked on those days. Set every trading day's close in Hours."})
    for text in (leader_status or {}).get("lines") or []:
        items.append({"kind": "leader_rules_inactive", "line": True, "text": text,
                      "can_adopt": bool((leader_status or {}).get("can_adopt"))})
    for rule in (c.owner_rules or [])[:3]:
        if rule.get("reads_as"):
            items.append({"kind": "owner_rule", "line": True,
                          "text": f"Your rule “{rule['text'][:100]}” is checked as: {rule['reads_as']}"})
    from time_utils import mdy
    for key, t in sorted((c.trainees or {}).items()):
        who = _sr._display_name(c, key)
        items.append({"kind": "trainee", "line": True,
                      "text": f"{who} is training as {t['target_role']}"
                              + (f" until {mdy(t['until'])}" if t.get("until") else "")
                              + (f" with {t['trainer']}" if t.get("trainer") else "")
                              + f" — their {t['target_role']} shifts don't count toward headcount."})
    for key in sorted(c.salaried_style or ()):
        who = _sr._display_name(c, key)
        lim = (c.hours_limits or {}).get(key)
        cap = float(lim[1]) if lim and lim[1] else float(c.salaried_cap or _sr.SALARIED_CAP_DEFAULT)
        items.append({"kind": "owner_salaried", "line": True,
                      "text": f"{who} is scheduled as an owner — no overtime line, up to {cap:g}h a week. Mark them "
                              "paid hourly in Team if they're on the clock."})
    return {"items": items, "lines": [i["text"] for i in items if i.get("line")]}
