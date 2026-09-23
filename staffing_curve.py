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
                     can work at once. A requirement that asks for more
                     than that cannot be met by any legal draft: the
                     backstop trims it back and the score then marks every
                     such shift short. The requirement is held under the
                     cap, and a history that runs over it is told to the
                     owner (the cap is probably wrong) instead.

The intraday capture (pos_intraday) is hourly, so the half-hour curve is
INTERPOLATED from it: each hour's sales are read as a rate at the middle of
that hour, and a half hour takes the straight-line rate at its own middle.
Every surface that shows a half-hour need says so (INTERPOLATED_NOTE).
"""
import math

SLOT = 30                    # minutes in one requirement slot
PEAK_SHARE = 0.75            # share of the usual crew the busiest half hour needs
MIN_SLOT_RATIO = 0.3         # a half hour under this share of the peak adds no curve need
INTERPOLATED_NOTE = "interpolated to the half hour from hourly sales"


def _hour_rates(curve: dict) -> dict:
    out = {}
    for h, v in (curve or {}).items():
        try:
            hour, val = int(h), float(v or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            out[hour] = val
    return out


def half_hour_shares(curve: dict) -> dict:
    """{minute: relative sales rate} for every half hour the hourly curve
    spans, linearly interpolated between hour midpoints. Beyond the first
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


def half_hour_needs(curve: dict, typical: dict, lo: int, hi: int) -> dict:
    """{minute: {role: people}} for each half hour in [lo, hi) that the
    curve covers. The daypart's busiest half hour needs PEAK_SHARE of the
    usual crew (the same bar the single peak-hour check held), and each
    other half hour that share scaled by its own sales against that peak.
    A half hour well off the peak (under MIN_SLOT_RATIO) adds nothing —
    the owner's floors still hold there. {} without a curve or a crew."""
    shares = {m: v for m, v in half_hour_shares(curve).items() if lo <= m < hi}
    crew = {}
    for role, n in (typical or {}).items():
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0 and (role or "").strip():
            crew[role.strip()] = n
    if not shares or not crew:
        return {}
    peak = max(shares.values())
    if peak <= 0:
        return {}
    out = {}
    for m in sorted(shares):
        ratio = shares[m] / peak
        if ratio < MIN_SLOT_RATIO:
            continue
        need = {role: max(1, int(math.ceil(PEAK_SHARE * n * ratio - 1e-9))) for role, n in crew.items()}
        out[m] = need
    return out


def cap_requirement(required: dict, cap, cap_roles) -> tuple:
    """(required, trimmed) with the roles the section cap counts held to at
    most `cap` people between them. The largest of them gives way first,
    one person at a time, never below one. `trimmed` is {role: people
    removed}; {} when nothing was over. With no cap it is unchanged."""
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    roles = {str(r).strip().lower() for r in (cap_roles or ()) if str(r).strip()} or {"server"}
    out = dict(required or {})
    if cap <= 0:
        return out, {}
    counted = [r for r in out if r.strip().lower() in roles and int(out[r] or 0) > 0]
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


def cap_conflicts(typical_headcount: dict, cap, cap_roles) -> list:
    """Where the restaurant's own history runs more of the capped roles on
    a daypart than the section count allows: [{day, daypart, typical, cap}],
    busiest first. The requirement is held to the cap either way; this is
    what the owner is told, because a history that runs over it every week
    says the cap is wrong, not the history."""
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return []
    roles = {str(r).strip().lower() for r in (cap_roles or ()) if str(r).strip()} or {"server"}
    out = []
    for key, crew in (typical_headcount or {}).items():
        try:
            day, part = key
        except (TypeError, ValueError):
            continue
        n = sum(int(v or 0) for r, v in (crew or {}).items() if str(r).strip().lower() in roles)
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
    return (f"Your history runs {top['typical']} {role_label} on {top['day']} {part}, but the section count "
            f"is {top['cap']}" + (f" (and {more} other shift{'s' if more != 1 else ''} run over it too)" if more else "")
            + f" — the requirement is held to {top['cap']}. If you really run {top['typical']} at once, "
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
