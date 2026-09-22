"""Who could cover a shift — the answer that used to live on a different
screen from the question.

coverage_check raises "X hasn't clocked in" as a routed issue; the
replacement engine behind /labor/schedule/replacements ranks who is free
and rated for the role. They were never joined, so the manager got a
problem by text and had to go find the answer. This is the join: the same
inputs (operational scores, staff availability, who is already on today's
schedule), ranked the same way, folded into the issue's detail.
"""
from models import DB_PATH


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


def for_gap(restaurant_id, role, weekday, exclude=(), db_path=DB_PATH, limit=2, on_date=None):
    """Best fits for `role` on `weekday` who are not in `exclude` (the person
    missing and everyone already on today's schedule). Rated people first,
    by score; unrated after, by name — never invented, never a score for
    someone who has not been rated (operational score's own rule).

    Only people who work `role` (when their roles are known), and nobody on
    approved time off that day — `on_date`, else the next `weekday` from
    the restaurant's today. Both were ignored, so a dishwasher was offered
    for a bartender gap and someone on vacation was named to cover (SCHED-15)."""
    from models import get_operational_scores, get_unavailability_map, get_staff_contacts
    scores = get_operational_scores(restaurant_id, db_path=db_path) or {}
    unavailable = get_unavailability_map(restaurant_id, db_path=db_path) or {}
    names = {c["employee_name"] for c in (get_staff_contacts(restaurant_id, db_path=db_path) or []) if c.get("employee_name")}
    names |= set(scores.keys())
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
        out.append({"name": key, "score": scores.get(key)})
    out.sort(key=lambda m: (-(m["score"] or 0), m["name"]))
    return out[:limit]


def sentence(fits):
    if not fits:
        return ""
    parts = [f"{f['name']}" + (f" ({f['score']:.1f})" if isinstance(f.get("score"), (int, float)) else "")
             for f in fits]
    return " Free today and best placed to cover: " + ", ".join(parts) + "."
