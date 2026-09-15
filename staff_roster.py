"""staff_roster.py — the names a restaurant's employees actually go by.

One source of truth for "who works here", used by two things that must never
disagree: the list of names an employee may claim at signup, and the job role
that decides which task checklist they see.

The pool is deliberately narrow, and each exclusion is load-bearing:

  - manual_team_members — the roster an owner already maintains for
    Operational Score. This is the authoritative list, and it is why
    self-signup costs the owner nothing extra: they keep it current anyway.
  - the most recently PUBLISHED schedule — the names that came out of the
    POS, which is what the schedule, ratings and availability tables are
    keyed by.

Never labor.load_shifts_for_restaurant: its own docstring says it returns
bundled SAMPLE shifts when a restaurant has no CSV, and a claimable-name list
built from fixtures would let a stranger claim a person who does not exist —
or worse, one who does, at a restaurant that never uploaded anything.
"""


def roster_names_for_restaurant(restaurant_id: int, db_path=None) -> list:
    """[(name, job_role_or_None)] for one restaurant, owner roster first.

    Ordered so the owner-maintained roster wins on job title: a name that
    appears in both places takes the job the owner typed, not whatever the
    last schedule happened to say.
    """
    kw = {"db_path": db_path} if db_path else {}
    out, seen = [], set()

    try:
        from models import get_manual_team_members
        for m in get_manual_team_members(restaurant_id, **kw):
            name = (m.get("name") or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                out.append((name, (m.get("role") or "").strip() or None))
    except Exception:
        pass

    try:
        from models import get_schedule_history, get_schedule_history_detail
        history = get_schedule_history(restaurant_id, **kw) or []
        if history:
            detail = get_schedule_history_detail(history[0]["id"], restaurant_id, **kw) or {}
            for name, job in _names_from_csv(detail.get("schedule_csv") or ""):
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    out.append((name, job))
    except Exception:
        pass

    return out


def _names_from_csv(csv_text: str) -> list:
    """Every distinct employee in a published schedule, with the job title
    from their most recent shift.

    Most recent, rather than first seen, because someone promoted from server
    to bartender mid-schedule should get the checklist they work today.
    """
    import csv
    import io

    if not csv_text.strip():
        return []
    # key -> {"name": as spelled, "date": latest seen, "job": job that day}
    latest = {}
    try:
        for row in csv.DictReader(io.StringIO(csv_text)):
            name = (row.get("employee") or row.get("Employee") or "").strip()
            if not name:
                continue
            date = (row.get("date") or row.get("Date") or "").strip()
            job = (row.get("role") or row.get("Role") or "").strip() or None
            key = name.lower()
            seen = latest.get(key)
            if seen is None or date >= seen["date"]:
                latest[key] = {"name": name, "date": date, "job": job or (seen or {}).get("job")}
    except Exception:
        return []
    return [(v["name"], v["job"]) for v in latest.values()]
