"""
staffing_curve.py — how many of each role a shift needs, half hour by half
hour, and the one ceiling every requirement is held under.

Two questions the scorer (shift_quality), the requirements table the model
reads (schedule_requirements) and the fill-in passes (schedule_engine) must
answer the same way, so they live here, pure, with no imports from any of
them:

  half_hour_needs  — the restaurant's usual crew for a daypart is what its
                     busiest half hour needs; every other half hour of
                     service needs that crew scaled by its own share of the
                     sales curve. Only the peak hour of lunch and dinner used
                     to carry a requirement, so a draft that had four servers
                     for the rush and one for the climb into it read as
                     fully covered.

  cap_requirement  — the section count is how many front-of-house people
                     can work AT ONCE. A half-hour need that asks for more
                     than that cannot be met by any legal draft, so it is
                     held under the cap; a daypart's count of different
                     people is a different unit and is never capped
                     (staggered starts fit more people than sections —
                     schedule audit 10/10/26 LOGIC-7). A history that runs
                     more on at once than the cap is told to the owner
                     (cap_conflicts: the cap is probably wrong) instead.
                     Roles count by family (capped_role), as the sweep's
                     over_section_cap counts them; role_family is read
                     from shift_quality inside the function.

The intraday capture (pos_intraday) is hourly, so the half-hour curve is
INTERPOLATED from it: each hour's sales are read as a rate at the middle of
that hour, and a half hour takes the straight-line rate at its own middle.
Every surface that shows a half-hour need says so (INTERPOLATED_NOTE).
"""
import math

import schedule_time as _stime

SLOT = 30                    # minutes in one requirement slot
PEAK_SHARE = 0.75            # share of the usual crew the busiest half hour needs
MIN_SLOT_RATIO = 0.3         # a half hour under this share of the peak adds no curve need
INTERPOLATED_NOTE = "interpolated to the half hour from hourly sales"


def _hour_rates(curve: dict) -> dict:
    """{business hour: sales} — each clock hour on the business day's
    timeline (schedule_time.business_hour): a bar's 1am sales are hour 25,
    the night's tail, never the start of the day. Read by clock hour, 0-1am
    sat before the morning, 2am-10am was interpolated from bar sales and the
    12am-2am ramp asked for nobody (BUGSGEN-19)."""
    out = {}
    for h, v in (curve or {}).items():
        try:
            hour, val = int(h), float(v or 0)
        except (TypeError, ValueError):
            continue
        if val > 0 and 0 <= hour <= 23:
            out[_stime.business_hour(hour)] = val
    return out


def half_hour_shares(curve: dict) -> dict:
    """{minute: relative sales rate} for every half hour the hourly curve
    spans, on the business day's timeline (an hour after midnight is past
    1440), linearly interpolated between hour midpoints. Beyond the first
    and last measured hour the edge value holds (no invented ramp)."""
    rates = _hour_rates(curve)
    if not rates:
        return {}
    hours = sorted(rates)
    points = [(h * 60 + 30, rates[h]) for h in hours]      # (minute, rate) at each hour's middle
    out = {}
    for h in range(hours[0], hours[-1] + 1):
        for half in (0, SLOT):
            m = h * 60 + half
            mid = m + SLOT / 2.0
            if mid <= points[0][0]:
                val = points[0][1]
            elif mid >= points[-1][0]:
                val = points[-1][1]
            else:
                val = points[0][1]
                for (m0, v0), (m1, v1) in zip(points, points[1:]):
                    if m0 <= mid <= m1:
                        val = v0 + (v1 - v0) * (mid - m0) / float(m1 - m0)
                        break
            out[m] = val
    return out


def half_hour_needs(curve: dict, typical: dict, lo: int, hi: int, peaks: dict = None, offsets: dict = None,
                    shares: dict = None) -> dict:
    """{minute: {role: people}} for each half hour in [lo, hi) that the
    curve covers. The daypart's busiest half hour needs the role's people
    ON AT ONCE at its usual peak — `peaks` ({role: people}, the staffing
    baseline's concurrent `peak`, demand-scaled: scheduler audit 10/10/26
    LOGIC-11) — and each other half hour that peak scaled by its own sales
    against the busiest. A role with no concurrent figure falls back to
    PEAK_SHARE of its usual different people (the old bar). `offsets`
    ({role: minutes}, staffing_model.lags by family) shifts a role along
    the curve: a cook (+30) is on half an hour before the sales arrive, a
    dishwasher (-30) after. `shares` ({minute: share}) is a curve measured
    at the half hour (schedule_economics.measured_half_hours) used instead
    of the hourly one interpolated. A half hour well off the peak (under
    MIN_SLOT_RATIO) adds nothing — the owner's floors still hold there. {}
    without a curve or a crew."""
    all_shares = dict(shares) if shares else half_hour_shares(curve)
    in_win = {m: v for m, v in all_shares.items() if lo <= m < hi}
    crew = {}
    for role, n in (typical or {}).items():
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0 and (role or "").strip():
            crew[role.strip()] = n
    if not in_win or not crew:
        return {}
    peak = max(in_win.values())
    if peak <= 0:
        return {}
    low_peaks = {str(k).strip().lower(): v for k, v in (peaks or {}).items()}
    low_off = {str(k).strip().lower(): int(v or 0) for k, v in (offsets or {}).items()}
    # Each role read against the busiest half hour of ITS curve (shifted by
    # its offset), so its own peak is its peak.
    role_peak = {}
    for role in crew:
        off = low_off.get(role.lower(), 0)
        role_peak[role] = max(all_shares.get(m + off, 0.0) for m in in_win) if off else peak
    out = {}
    for m in sorted(in_win):
        need = {}
        for role, n in crew.items():
            off = low_off.get(role.lower(), 0)
            if role_peak[role] <= 0:
                continue
            ratio = (all_shares.get(m + off, 0.0) if off else in_win[m]) / role_peak[role]
            if ratio < MIN_SLOT_RATIO:
                continue
            p = low_peaks.get(role.lower())
            top = float(p) if p else PEAK_SHARE * n
            need[role] = max(1, int(math.ceil(top * ratio - 1e-9)))
        if need:
            out[m] = need
    return out


def capped_role(role, cap_roles, families=None) -> bool:
    """Whether the section cap counts `role`: named in `cap_roles` (servers
    when none are), or one of their job codes — "Server AM" and "Server PM"
    are servers, as the sweep's over_section_cap counts them by family
    (D-13). Matching the literal name left the cap off every requirement at
    a restaurant whose codes carry the daypart."""
    from shift_quality import role_family
    names = {str(r).strip().lower() for r in (cap_roles or ()) if str(r).strip()} or {"server"}
    low = str(role or "").strip().lower()
    if low in names:
        return True
    fam = role_family(role, families)
    return fam in {role_family(n, families) for n in names}


def cap_requirement(required: dict, cap, cap_roles, families=None) -> tuple:
    """(required, trimmed) with the roles the section cap counts
    (capped_role) held to at most `cap` people between them. The largest of
    them gives way first, one person at a time, never below one. `trimmed`
    is {role: people removed}; {} when nothing was over. With no cap it is
    unchanged. `required` must be people ON AT ONCE (a half hour's need) —
    the cap's own unit; a daypart's count of different people is never
    capped (schedule audit 10/10/26 LOGIC-7)."""
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    out = dict(required or {})
    if cap <= 0:
        return out, {}
    counted = [r for r in out if capped_role(r, cap_roles, families) and int(out[r] or 0) > 0]
    total = sum(int(out[r]) for r in counted)
    trimmed = {}
    while total > cap:
        bigger = [r for r in counted if int(out[r]) > 1]
        if not bigger:
            break
        r = max(bigger, key=lambda x: (int(out[x]), x))
        out[r] = int(out[r]) - 1
        trimmed[r] = trimmed.get(r, 0) + 1
        total -= 1
    return out, trimmed


def cap_conflicts(concurrent: dict, cap, cap_roles, families=None) -> list:
    """Where the restaurant's own history runs more of the capped roles ON
    AT ONCE than the section count allows: [{day, daypart, typical, cap}],
    busiest first. `concurrent` is the staffing baseline's people-on-at-once
    figure ({(weekday, daypart): {role: {"peak", "slots": {minute: n}}}},
    labor.concurrent_patterns): the capped roles' usual counts added at each
    half hour, the most of them at once. It used to add up the different
    people who worked the daypart — six staggered servers "ran over" five
    sections they never filled at once, and the owner was told the cap was
    probably wrong (schedule audit 10/10/26 LOGIC-7). The half-hour needs
    are held to the cap either way; this is what the owner is told, because
    a history that runs over it every week says the cap is wrong."""
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return []
    out = []
    for key, crew in (concurrent or {}).items():
        try:
            day, part = key
        except (TypeError, ValueError):
            continue
        at = {}
        peaks = 0
        for r, figs in (crew or {}).items():
            if not isinstance(figs, dict) or not capped_role(r, cap_roles, families):
                continue
            for m, v in (figs.get("slots") or {}).items():
                at[m] = at.get(m, 0) + int(v or 0)
            peaks = max(peaks, int(figs.get("peak") or 0))
        n = max(at.values()) if at else peaks
        if n > cap:
            out.append({"day": day, "daypart": part, "typical": n, "cap": cap})
    order = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    out.sort(key=lambda c: (-c["typical"], order.index(c["day"]) if c["day"] in order else 9, c["daypart"]))
    return out


def cap_conflict_line(conflicts: list, role_label: str = "servers") -> str:
    """One sentence for the owner, or "" when the history fits the cap."""
    if not conflicts:
        return ""
    top = conflicts[0]
    part = {"morning": "lunch", "night": "dinner"}.get(top["daypart"], top["daypart"])
    more = len(conflicts) - 1
    return (f"Your history runs {top['typical']} {role_label} on at once on {top['day']} {part}, but the section "
            f"count is {top['cap']}" + (f" (and {more} other shift{'s' if more != 1 else ''} run over it too)" if more else "")
            + f" — no more than {top['cap']} are asked for at once. If you really run {top['typical']} at once, "
              "raise the section count; otherwise the history is the thing to change.")


def _clock(m: int) -> str:
    m %= 24 * 60
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def ramp_runs(needs: dict) -> dict:
    """{role: [(minute, people), ...]} — each role's half-hour needs as the
    points where the number changes, for a compact prompt line."""
    out = {}
    for m in sorted(needs or {}):
        for role, n in needs[m].items():
            runs = out.setdefault(role, [])
            if not runs or runs[-1][1] != n:
                runs.append((m, n))
    return out


def ramp_text(runs: dict, end: int = None) -> str:
    """"Server 2 from 11:00am, 4 from 12:00pm, 3 from 1:30pm; Cook 2 from
    11:00am" — only roles whose need actually changes across the daypart."""
    parts = []
    for role in sorted(runs, key=lambda r: r.lower()):
        pts = runs[role]
        if len(pts) < 2:
            continue
        parts.append(f"{role} " + ", ".join(f"{n} from {_clock(m)}" for m, n in pts)
                     + (f" to {_clock(end)}" if end is not None else ""))
    return "; ".join(parts)
