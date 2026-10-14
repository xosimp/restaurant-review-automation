"""
requirement_provenance.py — where a staffing level came from, so Cavnar AI's
own trims never become next month's "usual crew" on their own say-so
(scheduler audit 10/10/26 LEARNING-5).

After go-live the punches the baseline reads are the output of published
Cavnar weeks. The budget trim and the sales-per-labor-hour hold only lower
crews, and the trend detector adopts any level held on 3 of the last 4
weeks — so a week over budget that trimmed Tuesday lunch from 3 servers to 2
became, a month later, "a Tuesday lunch is 2", and nothing raised it back
unless an issue or a review landed on a watched night.

A level is tagged by where it came from:

  pre_cavnar   read from nights before the restaurant's first published
               Cavnar week (go_live) — the manager's own staffing;
  owner        the owner's floor, Crew answer or a manager's own edit;
  validated    a Cavnar-era level whose nights ran no worse (schedule_intel
               slot_readings / ran_well: sales against the forecast, comps,
               turns, issues on watched nights, the meal's reviews);
  trim         a Cavnar-era level lower than before with no such evidence.

hold_down_trend() is the guard the baseline applies: a downward move whose
recent nights are Cavnar-era and not validated keeps the earlier level, and
says so. The measured labor standard (staffing_model) uses cap_unvalidated():
an unvalidated Cavnar-era night may only hold or lower the standard (more
people per unit of work), never raise it (fewer people).

Reads only; never raises (an unreadable record tags nothing and guards
nothing — the baseline then reads as before).
"""
from datetime import date

import models as _models_mod
from models import DB_PATH

VALIDATE_MIN_NIGHTS = 3          # Cavnar-era nights with readings before a lower level is accepted
VALIDATE_MIN_SHARE = 0.5         # of them that ran well (no worse than the restaurant's own nights)


def get_conn(db_path=None):
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def go_live(restaurant_id, db_path=DB_PATH):
    """The first date of the restaurant's first PUBLISHED week that Cavnar AI
    drafted (a schedule_history row carrying the generation's economics), or
    None — every night before it is the manager's own staffing."""
    try:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT MIN(week_start) AS ws FROM schedule_history WHERE restaurant_id=? AND "
                             "published_at IS NOT NULL AND economics_json IS NOT NULL", (restaurant_id,)).fetchone()
        finally:
            conn.close()
        return str(r["ws"])[:10] if r and r["ws"] else None
    except Exception:
        return None


def readings(restaurant_id, pairs, db_path=DB_PATH) -> dict:
    """{(date, daypart): True | False} — whether each Cavnar-era shift ran
    well on the restaurant's one outcome reading (schedule_intel.ran_well);
    a shift with no reading is absent."""
    pairs = [(str(d)[:10], p) for d, p in pairs or ()]
    if not pairs:
        return {}
    try:
        import schedule_intel as _si
        ds = sorted(d for d, _p in pairs)
        got = _si.ran_well(_si.slot_readings(restaurant_id, ds[0], ds[-1], db_path=db_path))
        return {k: v for k, v in got.items() if k in set(pairs)}
    except Exception as e:
        print(f"[provenance] readings unavailable for {restaurant_id}: {e}")
        return {}


def validated(restaurant_id, pairs, db_path=DB_PATH) -> dict:
    """{"ok": bool, "read": n, "well": n, "share"} — whether a Cavnar-era
    level's nights carry outcome evidence: at least VALIDATE_MIN_NIGHTS of
    them read, VALIDATE_MIN_SHARE of those ran well."""
    got = readings(restaurant_id, pairs, db_path=db_path)
    read = len(got)
    well = sum(1 for v in got.values() if v)
    share = (well / float(read)) if read else None
    return {"ok": read >= VALIDATE_MIN_NIGHTS and share is not None and share >= VALIDATE_MIN_SHARE,
            "read": read, "well": well, "share": round(share, 2) if share is not None else None}


def elasticity_hold(trend: dict, ups: dict):
    """The window of a measured +1 (staffing_elasticity's `windows`) an
    upward trend of the role's family on that daypart sits inside, or None
    (blind re-audit 10/11/26 LEARNING-6). Pure."""
    if not ups or not trend:
        return None
    try:
        from intelligence.staffing import family_of
        fam = family_of(trend.get("role"))
    except Exception:
        return None
    since = str(trend.get("since") or "")[:10]
    newest = max([str(d)[:10] for d in (trend.get("recent_dates") or [])] or [since])
    for frm, to in (ups or {}).get((fam, trend.get("part"))) or []:
        if str(frm)[:10] <= newest and (to is None or str(to)[:10] >= since):
            return [frm, to]
    return None


def hold_down_trend(trend: dict, live: str, restaurant_id=None, db_path=DB_PATH, check=None, ups=None) -> dict:
    """{"hold": bool, "provenance": tag, "reason"} for one baseline trend
    ({day, part, role, now, was, since, direction, recent_dates}): a
    downward move whose recent nights are on or after go-live (`live`) and
    not validated is held at `was` (provenance "trim"); validated it is
    "validated"; a move before go-live is "pre_cavnar". An UPWARD move of
    a family while a measured +1 of it bound the drafts on that daypart
    (`ups`: staffing_elasticity.upward_windows) is held at `was` too
    (provenance "elasticity", LEARNING-6): the drafts' own +1 is not the
    restaurant's usual crew, and the measured move applies on top of the
    earlier level as a target, so it never ratchets. `check` (tests)
    replaces validated()."""
    since = str((trend or {}).get("since") or "")
    if not trend or not live or since < live:
        return {"hold": False, "provenance": "pre_cavnar", "reason": None}
    if trend.get("direction") != "down":
        win = elasticity_hold(trend, ups)
        if win is not None:
            from time_utils import mdy
            return {"hold": True, "provenance": "elasticity",
                    "reason": (f"held at {trend.get('was')}: the rise to {trend.get('now')} since {mdy(since)} came "
                               f"from Cavnar AI adding one measured person from {mdy(win[0])}; the measured move "
                               "is applied on its own, so it is not counted twice")}
        # A level the restaurant raised: never a trim, nothing to guard.
        return {"hold": False, "provenance": "cavnar", "reason": None}
    dates = [d for d in (trend.get("recent_dates") or []) if d >= live]
    v = (check or validated)(restaurant_id, [(d, trend.get("part")) for d in dates], db_path=db_path) \
        if restaurant_id is not None else {"ok": False, "read": 0}
    if v.get("ok"):
        return {"hold": False, "provenance": "validated",
                "reason": f"leaner since {since}, and those nights ran no worse ({v['well']} of {v['read']} read)"}
    from time_utils import mdy
    return {"hold": True, "provenance": "trim",
            "reason": (f"held at {trend.get('was')}: the drop to {trend.get('now')} since {mdy(since)} came from "
                       f"Cavnar AI's own drafts, and its nights have no outcome evidence yet "
                       f"({v.get('read', 0)} read; needs {VALIDATE_MIN_NIGHTS} that ran no worse)")}


def is_cavnar_era(iso, live) -> bool:
    return bool(live) and str(iso)[:10] >= str(live)


def cap_unvalidated(values: dict, live, ok_pairs=frozenset(), part=None) -> dict:
    """{date: value} with each Cavnar-era date's value (a per-person
    throughput) held to at most the pre-go-live median, unless the date's
    (date, part) is in `ok_pairs` (validated). Pure."""
    if not live:
        return dict(values or {})
    pre = sorted(v for d, v in (values or {}).items() if str(d) < live and v is not None)
    if not pre:
        return dict(values or {})
    n = len(pre)
    med = pre[n // 2] if n % 2 else (pre[n // 2 - 1] + pre[n // 2]) / 2.0
    out = {}
    for d, v in (values or {}).items():
        if v is not None and str(d) >= live and (str(d), part) not in ok_pairs:
            out[d] = min(v, med)
        else:
            out[d] = v
    return out


def today_iso(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date().isoformat()
    except Exception:
        return date.today().isoformat()
