"""Who could cover a shift — the answer that used to live on a different
screen from the question.

coverage_check raises "X hasn't clocked in" as a routed issue; the
replacement engine behind /labor/schedule/replacements ranks who is free
and rated for the role. They were never joined, so the manager got a
problem by text and had to go find the answer. This is the join: the same
inputs (operational scores, staff availability, who is already on today's
schedule), ranked the same way, folded into the issue's detail.

Two kinds of cover (schedule audit 10/3/26 E-31): somebody off today
(for_gap), and somebody already on today whose own shift ends as the gap
begins — the lunch server who could stay for dinner (stay_on). for_gap
leaves everyone on today's schedule out, so the realistic cover, an
extension or a double, was never named. Both are held to the open-shift
claim's own check (schedule_engine.replacement_is_legal).
"""
from models import DB_PATH

# An on-shift person whose own shift ends at most this long before a gap
# starts can be asked to stay on (or come straight back) for it.
COVER_ABUT_MINUTES = 60


def _date_for(restaurant_id, weekday, on_date):
    from datetime import date as _date, timedelta as _td
    if on_date:
        try:
            return _date.fromisoformat(str(on_date)[:10])
        except ValueError:
            return None
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = _date.today()
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if weekday not in names:
        return None
    return today + _td(days=(names.index(weekday) - today.weekday()) % 7)


def _gap_row(restaurant_id, day, role, excluded, shift, db_path):
    """(rows, index) of the published week holding `day` and the row being
    covered — `shift` ({employee, shift_start}) when the caller knows it,
    else the day's row of `role` held by someone in `excluded` (the person
    missing). (None, None) when there is no published row to judge by."""
    try:
        import intraday
        from schedule_versions import rows_from_csv
        rows = rows_from_csv(intraday._published_csv(restaurant_id, day, db_path))
    except Exception:
        return None, None
    iso = day.isoformat()
    want = (role or "").strip().lower()
    for i, r in enumerate(rows):
        if (r.get("date") or "")[:10] != iso:
            continue
        who = (r.get("employee") or "").strip().lower()
        if shift:
            if who == str(shift.get("employee") or "").strip().lower() and \
                    (not shift.get("shift_start") or r.get("shift_start") == shift.get("shift_start")):
                return rows, i
        elif who in excluded and (not want or (r.get("role") or "").strip().lower() == want):
            return rows, i
    return None, None


def for_gap(restaurant_id, role, weekday, exclude=(), db_path=DB_PATH, limit=2, on_date=None, shift=None,
            constraints=None):
    """Best fits for `role` on `weekday` who are not in `exclude` (the person
    missing and everyone already on today's schedule). Rated people first,
    by score; unrated after, by name — never invented, never a score for
    someone who has not been rated (operational score's own rule).

    Only people who work `role` (when their roles are known), and nobody on
    approved time off that day — `on_date`, else the next `weekday` from
    the restaurant's today. Both were ignored, so a dishwasher was offered
    for a bartender gap and someone on vacation was named to cover (SCHED-15).
    `constraints` (the week's schedule_rules.Constraints) saves rebuilding
    them for every candidate's legality check."""
    from models import get_operational_scores, get_unavailability_map, get_staff_contacts
    scores = get_operational_scores(restaurant_id, db_path=db_path) or {}
    unavailable = get_unavailability_map(restaurant_id, db_path=db_path) or {}
    names = {c["employee_name"] for c in (get_staff_contacts(restaurant_id, db_path=db_path) or []) if c.get("employee_name")}
    names |= set(scores.keys())
    # Everyone on the active roster too: an unrated person with no contact
    # row was never suggested at all (memory audit 9/29/26, PEOPLE-11).
    try:
        import staff_settings as _ss_r
        names |= {e["name"] for e in _ss_r.roster(restaurant_id, db_path=db_path)}
    except Exception:
        pass
    # Beside the rating: who took a cover when asked (people.cover_record)
    # and who turns up (the recency-weighted reliability) — the suggestions
    # ignored both, and ranked by rating then alphabetically.
    try:
        import people as _people_r
        import staff_settings as _ss_r2
        covers = _people_r.cover_record(restaurant_id, db_path=None if db_path == DB_PATH else db_path)
        reliab = {_ss_r2.name_key(n): r for n, r in (_ss_r2.reliability(restaurant_id, db_path=db_path) or {}).items()}
    except Exception:
        covers, reliab, _ss_r2 = {}, {}, None
    excluded = {str(x).strip().lower() for x in (exclude or []) if x}
    try:
        import staff_settings as _ss
        excluded |= {n.lower() for n, st in _ss.get_all(restaurant_id, db_path=db_path).items()
                     if not st.get("active", True)}
    except Exception:
        pass
    day = _date_for(restaurant_id, weekday, on_date)
    if day is not None:
        try:
            import time_off as _to
            excluded |= {n.strip().lower() for n in _to.approved_in_window(restaurant_id, day, day, db_path=db_path)}
        except Exception:
            pass
    want = (role or "").strip().lower()
    out = []
    for name in sorted(names):
        key = name.strip()
        if not key or key.lower() in excluded:
            continue
        if weekday and weekday in (unavailable.get(key) or set()):
            continue
        if want:
            try:
                import staff_settings as _ss
                roles = _ss.roles_for(restaurant_id, key, db_path=db_path)
            except Exception:
                roles = set()
            if roles and want not in roles:
                continue
        k = _ss_r2.name_key(key) if _ss_r2 is not None else key.lower()
        cov = covers.get(k) or {}
        rel = reliab.get(k) or {}
        out.append({"name": key, "score": scores.get(key),
                    "covers_taken": int(cov.get("accepted") or 0), "covers_declined": int(cov.get("declined") or 0),
                    "no_show_rate": rel.get("no_show_rate")})
    # The owner's rating first (their judgment), then who takes covers when
    # asked, then who turns up — a known low no-show rate before no record.
    out.sort(key=lambda m: (-(m["score"] or 0), -(m["covers_taken"] - m["covers_declined"]),
                            (m["no_show_rate"] if m["no_show_rate"] is not None else 0.5), m["name"]))
    # Every suggestion is a move an open-shift claim would allow: the same
    # replacement_is_legal check, on the published week with the missing
    # person's row. It named a minor for a shift ending 11:30pm that the
    # claim itself refuses, and "Ask to cover" then texted them (NS5 M9).
    rows, idx = (None, None)
    if day is not None:
        rows, idx = _gap_row(restaurant_id, day, role, excluded, shift, db_path)
    if rows is None:
        return out[:limit]
    import schedule_engine as _se
    legal = []
    for m in out:
        if len(legal) >= limit:
            break
        ok, _why = _se.replacement_is_legal(restaurant_id, rows, idx, m["name"], constraints=constraints)
        if ok:
            legal.append(m)
    return legal


def gap_week(restaurant_id, day, shift, db_path=DB_PATH):
    """(rows, index) of the published week holding `day` and the missing
    person's row (`shift` = {employee, shift_start}); (None, None) without
    one."""
    return _gap_row(restaurant_id, day, None, set(), shift, db_path)


def stay_on(restaurant_id, gap, on_today, exclude=(), db_path=DB_PATH, limit=2, constraints=None, rows=None,
            index=None):
    """People already on today's schedule who could take the gap — `gap`
    ({employee, role, shift_start, date}) — because their own shift ends at,
    or up to COVER_ABUT_MINUTES before, its start: the lunch server who
    could stay for dinner (E-31). `on_today` is today's published rows
    ({employee, role, shift_start, shift_end}). Same role or role family
    (Server AM covers Server PM), or a role they are known to work. Each is
    held to the open-shift claim's own check — the whole gap shift, a double
    allowed — so "Ask them to cover" never offers what the claim would
    refuse. Closest finish first, then the rating. [{"name", "score",
    "kind": "stay", "ends", "how"}]"""
    from datetime import date as _date
    import schedule_engine as _se
    import staff_settings as _ss
    from shift_quality import role_family
    from schedule_rules import parse_minutes
    from models import get_operational_scores
    start = parse_minutes(gap.get("shift_start") or "")
    if start is None:
        return []
    try:
        day = _date.fromisoformat(str(gap.get("date"))[:10])
    except (TypeError, ValueError):
        return []
    if rows is None or index is None:
        rows, index = gap_week(restaurant_id, day, {"employee": gap.get("employee"),
                                                   "shift_start": gap.get("shift_start")}, db_path)
    if rows is None:
        return []
    families = getattr(constraints, "role_families", None) or None
    want = (gap.get("role") or "").strip().lower()
    fam = role_family(want, families)
    excluded = {_ss.name_key(x) for x in (exclude or []) if x}
    scores = get_operational_scores(restaurant_id, db_path=db_path) or {}
    cands = {}
    for r in on_today or []:
        name = (r.get("employee") or "").strip()
        end = parse_minutes(r.get("shift_end") or "")
        if not name or _ss.name_key(name) in excluded or end is None:
            continue
        s = parse_minutes(r.get("shift_start") or "")
        if s is not None and end <= s:
            end += 24 * 60                 # ends after midnight
        if not (start - COVER_ABUT_MINUTES <= end <= start):
            continue                       # still on when it starts, or long gone
        role = (r.get("role") or "").strip().lower()
        if want and role != want and role_family(role, families) != fam:
            try:
                known = {x.strip().lower() for x in _ss.roles_for(restaurant_id, name, db_path=db_path)}
            except Exception:
                known = set()
            if want not in known and fam not in {role_family(x, families) for x in known}:
                continue
        prev = cands.get(_ss.name_key(name))
        if prev is None or end > prev["_end"]:
            cands[_ss.name_key(name)] = {"name": name, "score": scores.get(name), "kind": "stay",
                                         "ends": r.get("shift_end"), "_end": end,
                                         "_listed": (r.get("listed_as") or name).strip()}
    ranked = sorted(cands.values(), key=lambda m: (-m["_end"], -(m["score"] or 0), m["name"]))
    out = []
    for m in ranked:
        if len(out) >= limit:
            break
        # Judged under the week's own spelling, so their own rows are seen.
        ok, _why = _se.replacement_is_legal(restaurant_id, rows, index, m["_listed"], constraints=constraints)
        if ok:
            how = (f"on today until {m['ends']} — could stay on" if m["_end"] == start
                   else f"on today until {m['ends']} — could come back for it")
            out.append({"name": m["name"], "score": m["score"], "kind": "stay", "ends": m["ends"], "how": how})
    return out


def sentence(fits):
    if not fits:
        return ""
    parts = [f"{f['name']}" + (f" ({f['score']:.1f})" if isinstance(f.get("score"), (int, float)) else "")
             for f in fits]
    return " Free today and best placed to cover: " + ", ".join(parts) + "."
