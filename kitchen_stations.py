"""kitchen_stations — which cook can work which station, and which stations a
shift must have staffed (owner, 9/30/26, from Jim Heflin at Simple EJ's:
"just because a sauté cook works sauté doesn't mean the same cook can be on
grill"; "Kitchen is the umbrella term and the jobs back there aren't equal").

The POS knows one job, "Kitchen". The kitchen knows stations. The owner
sets, in `restaurants.kitchen_stations_json`:

    {"roles":    ["Kitchen"],                 the roster roles that are the kitchen
     "stations": ["Sauté", "Grill", "Fry"],   its stations, in the owner's order
     "needs":    [{"station": "Grill", "daypart": "night", "days": ["Friday"], "count": 1}],
     "skills":   {"Jesus Hernandez": ["Grill", "Sauté"]}}     who is trained on what

A need's daypart is "morning", "night" or "all" (the same 3pm split as the
rest of the scheduler, schedule_rules.daypart_of); no days means every day.

Stations are not a column on a schedule row: a drafted week names the role
(the POS job) as always, and stations are ASSIGNED afterwards by matching
the cooks on each daypart to the stations it needs, each cook to at most one
station and only one they are trained on (`assign`, a bipartite matching).
A station no trained cook on shift can take is a gap; the engine closes it
by adding a shift for a trained cook who is free
(schedule_engine._ensure_station_coverage), the budget trim never removes
the only trained cover (`protects`), and what is left is flagged. Pure —
nothing here reads the database.
"""
import json

DAYPARTS = ("morning", "night")
SPLIT = 15 * 60          # the 3pm split schedule_rules.daypart_of uses
MAX_STATIONS = 20
MAX_NEEDS = 80


def _key(name) -> str:
    return " ".join(str(name or "").lower().split())


def _clean(text, limit=40) -> str:
    return " ".join(str(text or "").split())[:limit]


def normalise(raw) -> dict:
    """The stored blob as a clean config; {} when nothing usable is set.
    Unknown stations in a need or a skill are dropped, so a renamed station
    never leaves a requirement no one can meet."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    stations, seen = [], set()
    for s in raw.get("stations") or []:
        name = _clean(s)
        if name and name.lower() not in seen and len(stations) < MAX_STATIONS:
            stations.append(name)
            seen.add(name.lower())
    by_low = {s.lower(): s for s in stations}
    roles = []
    for r in raw.get("roles") or []:
        name = _clean(r, 60)
        if name and name.lower() not in {x.lower() for x in roles}:
            roles.append(name)
    needs = []
    for n in raw.get("needs") or []:
        if not isinstance(n, dict):
            continue
        st = by_low.get(_clean(n.get("station")).lower())
        part = str(n.get("daypart") or "all").lower()
        if not st or part not in DAYPARTS + ("all",):
            continue
        days = [d for d in (n.get("days") or []) if d in _WEEKDAYS]
        try:
            count = max(1, min(4, int(n.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        needs.append({"station": st, "daypart": part, "days": days, "count": count})
        if len(needs) >= MAX_NEEDS:
            break
    skills = {}
    for person, have in (raw.get("skills") or {}).items():
        name = _clean(person, 80)
        trained = [by_low[s.lower()] for s in (have or []) if isinstance(s, str) and s.lower() in by_low]
        if name and trained:
            skills[name] = sorted(set(trained), key=stations.index)
    if not stations or not roles:
        return {}
    return {"roles": roles, "stations": stations, "needs": needs, "skills": skills}


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def is_kitchen(cfg, role) -> bool:
    return bool(cfg) and _key(role) in {_key(r) for r in cfg.get("roles") or []}


def trained(cfg, name) -> set:
    want = _key(name)
    for person, have in (cfg.get("skills") or {}).items():
        if _key(person) == want:
            return set(have)
    return set()


def required(cfg, day, part) -> list:
    """The stations `day` (Monday…) `part` (morning/night) must have, a
    station listed once per cook it needs, in the owner's station order."""
    out = []
    for n in cfg.get("needs") or []:
        if n["daypart"] not in (part, "all"):
            continue
        if n["days"] and day not in n["days"]:
            continue
        out.extend([n["station"]] * n["count"])
    order = {s: i for i, s in enumerate(cfg.get("stations") or [])}
    return sorted(out, key=lambda s: order.get(s, 99))


def _minutes(t):
    from schedule_rules import parse_minutes
    return parse_minutes(t)


def parts_of(row) -> set:
    """The dayparts a shift covers: the morning when it starts before 3pm,
    the night when it ends after 3pm (a 10am–10pm double is both)."""
    s, e = _minutes(row.get("shift_start", "")), _minutes(row.get("shift_end", ""))
    if s is None:
        return set()
    if e is not None and e <= s:
        e += 24 * 60                      # past midnight
    out = set()
    if s < SPLIT:
        out.add("morning")
    if (e if e is not None else s) > SPLIT or s >= SPLIT:
        out.add("night")
    return out


def assign(cooks, need, cfg) -> tuple:
    """({cook: station}, [unmet station, ...]) — each cook to at most one
    station they are trained on, as many needed stations filled as any
    assignment can (Kuhn's augmenting paths; a kitchen is a handful of
    people, so this is instant)."""
    slots = list(need)
    skill = {c: trained(cfg, c) for c in cooks}
    slot_of = {}                          # cook -> the slot index they fill

    def place(i, seen):
        for c in cooks:
            if c in seen or slots[i] not in skill[c]:
                continue
            seen.add(c)
            if c not in slot_of or place(slot_of[c], seen):
                slot_of[c] = i
                return True
        return False

    for i in range(len(slots)):
        place(i, set())
    filled = set(slot_of.values())
    return ({c: slots[i] for c, i in slot_of.items()},
            [slots[i] for i in range(len(slots)) if i not in filled])


def week(rows, cfg, week_dates=None) -> dict:
    """{"stations": {row index: {daypart: station}}, "gaps": [{date, day,
    daypart, station}]} for a drafted week. Only kitchen rows are matched;
    a cook on a double is matched in each daypart on their own."""
    out = {"stations": {}, "gaps": []}
    if not cfg:
        return out
    by_slot = {}
    for i, r in enumerate(rows or []):
        if not is_kitchen(cfg, r.get("role")) or not r.get("date") or not (r.get("employee") or "").strip():
            continue
        for part in parts_of(r):
            by_slot.setdefault((r["date"], part), []).append(i)
    dates = list(week_dates or sorted({r.get("date") for r in rows or [] if r.get("date")}))
    from datetime import date as _date
    for d in dates:
        try:
            day = _WEEKDAYS[_date.fromisoformat(d).weekday()]
        except (TypeError, ValueError):
            continue
        for part in DAYPARTS:
            need = required(cfg, day, part)
            idx = by_slot.get((d, part), [])
            names = []
            for i in idx:
                n = rows[i]["employee"].strip()
                if n not in names:
                    names.append(n)
            placed, unmet = assign(names, need, cfg)
            for i in idx:
                st = placed.get(rows[i]["employee"].strip())
                if st:
                    out["stations"].setdefault(i, {})[part] = st
            for st in unmet:
                out["gaps"].append({"date": d, "day": day, "daypart": part, "station": st})
    return out


def uncovered(rows, cfg, d, part) -> int:
    """How many of `d` `part`'s required stations no cook on shift can take."""
    if not cfg:
        return 0
    try:
        from datetime import date as _date
        day = _WEEKDAYS[_date.fromisoformat(d).weekday()]
    except (TypeError, ValueError):
        return 0
    need = required(cfg, day, part)
    if not need:
        return 0
    names = []
    for r in rows:
        if r.get("date") == d and is_kitchen(cfg, r.get("role")) and part in parts_of(r):
            n = (r.get("employee") or "").strip()
            if n and n not in names:
                names.append(n)
    return len(assign(names, need, cfg)[1])


def protects(rows, row, cfg) -> bool:
    """True when taking `row` away would leave a required station on its
    date and daypart uncovered — the budget trim keeps it."""
    if not cfg or not is_kitchen(cfg, row.get("role")):
        return False
    rest = [r for r in rows if r is not row]
    return any(uncovered(rest, cfg, row.get("date"), p) > uncovered(rows, cfg, row.get("date"), p)
               for p in parts_of(row))


def prompt_lines(cfg) -> list:
    """The station rules as the schedule prompt reads them."""
    if not cfg:
        return []
    lines = [f"KITCHEN STATIONS ({', '.join(cfg['roles'])} shifts): the stations are "
             f"{', '.join(cfg['stations'])}. A cook can only cover a station they are trained on, "
             "and each cook covers one station per daypart."]
    for n in cfg["needs"]:
        days = ", ".join(n["days"]) if n["days"] else "every day"
        when = {"morning": "before 3pm", "night": "after 3pm", "all": "all day"}[n["daypart"]]
        lines.append(f"- {n['station']}: {n['count']} trained cook{'s' if n['count'] > 1 else ''} {when}, {days}")
    for person, have in sorted(cfg["skills"].items()):
        lines.append(f"- {person} is trained on: {', '.join(have)}")
    return lines


# ── owner edits, one change at a time ──────────────────────────────────────
# (CLAUDE.md / owner, 9/28/26: list edits save on add and remove, never as a
# whole list sent back, so two people editing never wipe each other's work.)

def apply_edit(raw, edit: dict) -> dict:
    """The stored blob after ONE edit. Raises ValueError with an owner-facing
    sentence for an edit that cannot apply.

      {"op": "roles", "roles": [...]}                        the kitchen's roster roles
      {"op": "add_station", "name": "Grill"} / {"op": "remove_station", "name": ...}
      {"op": "add_need", "station", "daypart", "days": [...], "count": 1}
      {"op": "remove_need", "station", "daypart", "days": [...]}
      {"op": "skill", "person": "Jesus Hernandez", "station": "Grill", "on": true}
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    cur = dict(raw or {}) if isinstance(raw, dict) else {}
    cur.setdefault("roles", [])
    cur.setdefault("stations", [])
    cur.setdefault("needs", [])
    cur.setdefault("skills", {})
    op = str((edit or {}).get("op") or "")
    low = {s.lower(): s for s in cur["stations"]}
    if op == "roles":
        roles = [_clean(r, 60) for r in (edit.get("roles") or []) if _clean(r, 60)]
        if not roles:
            raise ValueError("Pick at least one kitchen role.")
        cur["roles"] = roles
    elif op == "add_station":
        name = _clean(edit.get("name"))
        if not name:
            raise ValueError("Name the station.")
        if name.lower() in low:
            raise ValueError(f"{low[name.lower()]} is already a station.")
        if len(cur["stations"]) >= MAX_STATIONS:
            raise ValueError(f"A kitchen can have up to {MAX_STATIONS} stations.")
        cur["stations"] = cur["stations"] + [name]
    elif op == "remove_station":
        name = low.get(_clean(edit.get("name")).lower())
        if not name:
            raise ValueError("That station isn't on the list.")
        cur["stations"] = [s for s in cur["stations"] if s != name]
        cur["needs"] = [n for n in cur["needs"] if (n or {}).get("station") != name]
        cur["skills"] = {p: [s for s in have if s != name] for p, have in cur["skills"].items()}
        cur["skills"] = {p: have for p, have in cur["skills"].items() if have}
    elif op in ("add_need", "remove_need"):
        name = low.get(_clean(edit.get("station")).lower())
        part = str(edit.get("daypart") or "all").lower()
        days = sorted({d for d in (edit.get("days") or []) if d in _WEEKDAYS}, key=_WEEKDAYS.index)
        if not name:
            raise ValueError("Pick a station.")
        if part not in DAYPARTS + ("all",):
            raise ValueError("Pick morning, night or all day.")
        same = lambda n: (n or {}).get("station") == name and str((n or {}).get("daypart") or "all") == part \
            and sorted((n or {}).get("days") or [], key=lambda d: _WEEKDAYS.index(d) if d in _WEEKDAYS else 9) == days
        if op == "add_need":
            try:
                count = max(1, min(4, int(edit.get("count") or 1)))
            except (TypeError, ValueError):
                raise ValueError("How many cooks: a number from 1 to 4.")
            cur["needs"] = [n for n in cur["needs"] if not same(n)] + [
                {"station": name, "daypart": part, "days": days, "count": count}]
            if len(cur["needs"]) > MAX_NEEDS:
                raise ValueError("That's more station rules than a week can use.")
        else:
            before = len(cur["needs"])
            cur["needs"] = [n for n in cur["needs"] if not same(n)]
            if len(cur["needs"]) == before:
                raise ValueError("That rule isn't there any more.")
    elif op == "skill":
        person = _clean(edit.get("person"), 80)
        name = low.get(_clean(edit.get("station")).lower())
        if not person or not name:
            raise ValueError("Pick a cook and a station.")
        key = next((p for p in cur["skills"] if _key(p) == _key(person)), person)
        have = [s for s in cur["skills"].get(key, []) if s != name]
        if edit.get("on"):
            have.append(name)
        if have:
            cur["skills"][key] = have
        else:
            cur["skills"].pop(key, None)
    else:
        raise ValueError("Unknown station change.")
    return cur
