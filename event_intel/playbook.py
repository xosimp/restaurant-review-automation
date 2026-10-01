"""
event_intel.playbook — from a game on the calendar to what to do about it
(Event Intelligence phase 2, 10/1/26).

  usual_nights(rid, d)  the ordinary same weekdays before a night: the
                        BASELINE_WEEKS before it, every night event_memory
                        flags (another game, a holiday, a party) left out —
                        the same "usual" every lift here is measured against
  staffing(rid, e)      who worked past games like this one, role by role,
                        against a usual same weekday — from the punches on
                        file (shift_facts). A recommendation ("plan one more
                        Bartender PM") only when SEGMENT_MIN_N games like it
                        were measured, their lift cleared LIFT_FLOOR, and
                        every one of them ran that role above usual; below
                        that it is said as what the last game did
  rush(rid, e)          where past games like it moved the night, hour by
                        hour around kickoff, against a usual same weekday —
                        from the checks on file (pos_tickets, by the hour a
                        check opened). A pattern ("the jump comes the hour
                        before kickoff") needs SEGMENT_MIN_N games that
                        agree within an hour
  game_night(rid, day, net=, ...)  tonight's game against the last one of
                        the same side: what the nightly report says
  alert(rid, today)     the morning brief's line for the next followed game
                        within ALERT_DAYS: the game, what games like it did
                        here (measured, or the last one as a fact), the
                        staffing and rush where measured, and a marketing
                        draft to start

Nothing here calls a model, estimates a number this restaurant has not
measured, or raises into its caller (every public read returns None or []
on failure).
"""
import json
import logging
from datetime import date, timedelta

from event_intel import engine, store

log = logging.getLogger(__name__)

ALERT_DAYS = 3            # the brief names a game this many days ahead
LIFT_FLOOR = 10.0         # a staffing recommendation needs games that ran at least this much above usual
USUAL_MIN = 2             # usual same weekdays a comparison needs
RUSH_AGREE_HOURS = 1      # games "agree" on the rush when their peaks are this close to kickoff


def _d(v):
    return engine._d(v)


def _money(v):
    return f"${float(v):,.0f}"


def _people(n, role):
    return f"{n:g} {role}"


def _same_class(e, g):
    """Same side (home/road), same kickoff class (prime time or not) and
    the same season class (preseason is its own crowd) — never across."""
    pre = lambda x: x.get("season_type") == "preseason"
    return (g.get("home_away") == e.get("home_away")
            and bool(g.get("is_primetime")) == bool(e.get("is_primetime")) and pre(g) == pre(e))


# ── the usual night ─────────────────────────────────────────────────────────

def usual_nights(restaurant_id, day, db_path=store.DB_PATH) -> list:
    """ISO dates of the ordinary same weekdays in the BASELINE_WEEKS before
    `day`: every night event_memory flags is left out."""
    import event_memory
    day = _d(day)
    weeks = getattr(event_memory, "BASELINE_WEEKS", 8)
    same = [day - timedelta(weeks=k) for k in range(1, weeks + 1)]
    try:
        flags = event_memory.flags_for(restaurant_id, same, db_path=db_path)
    except Exception:
        flags = {}
    return [d.isoformat() for d in same if not (flags.get(d.isoformat()) or [])]


def usual_net(restaurant_id, day, db_path=store.DB_PATH):
    """{"median", "n"} — the usual same weekday's final net before `day`
    (canonical_facts, every source), or None below event_memory's floor."""
    import canonical_facts as cf
    import event_memory
    dates = usual_nights(restaurant_id, day, db_path=db_path)
    if not dates:
        return None
    try:
        series = cf.net_series(restaurant_id, db_path=db_path, dates=[_d(x) for x in dates], pos=cf.POS_ALL)
    except Exception:
        return None
    nets = [float(x["net"]) for x in series.values() if x.get("net") and x["net"] > 0]
    if len(nets) < getattr(event_memory, "BASELINE_MIN", 3):
        return None
    return {"median": round(engine._median(nets), 2), "n": len(nets)}


# ── staffing by role ────────────────────────────────────────────────────────

def _roles_on(restaurant_id, iso, db_path):
    """({role: people}, {role: [shift starts]}) for one night from the
    stored punches, or (None, None) with none on file."""
    try:
        import shift_facts
        rows = shift_facts.rows(restaurant_id, since=iso, until=iso, db_path=db_path)
    except Exception:
        return None, None
    people, starts = {}, {}
    for r in rows or []:
        if str(r.get("date") or "")[:10] != iso:
            continue
        who = " ".join(str(r.get("employee") or "").lower().split())
        if not who:
            continue
        role = (r.get("role") or "").strip() or "Unassigned"
        people.setdefault(role, set()).add(who)
        if r.get("shift_start"):
            starts.setdefault(role, []).append(str(r["shift_start"])[:5])
    if not people:
        return None, None
    return {k: len(v) for k, v in people.items()}, starts


def _clock_of(hhmm):
    return engine._clock(hhmm) if hhmm else None


def staffing(restaurant_id, e, db_path=store.DB_PATH):
    """{"games": [{"date", "describe", "roles": {role: n}}], "usual":
    {role: median}, "usual_n", "weekday", "deltas": [{"role", "game",
    "usual", "delta", "every_game", "from"}], "recommend": [...] or [],
    "n", "text", "basis"} for games of the same side and kickoff class at
    this restaurant, or None with no such game on file with punches."""
    try:
        games = [g for g in engine.past_games(restaurant_id, e, db_path=db_path) if _same_class(e, g["event"])]
        if not games:
            return None
        nights, usual_dates = [], set()
        for g in games:
            iso = g["event"]["event_date"]
            roles, starts = _roles_on(restaurant_id, iso, db_path)
            if not roles:
                continue
            nights.append({"date": iso, "describe": engine.describe(g["event"]), "roles": roles, "starts": starts,
                           "lift_pct": g["outcome"].get("lift_pct")})
            usual_dates.update(usual_nights(restaurant_id, iso, db_path=db_path))
        if not nights:
            return None
        usual_rows = []
        for iso in sorted(usual_dates):
            roles, _ = _roles_on(restaurant_id, iso, db_path)
            if roles:
                usual_rows.append(roles)
        if len(usual_rows) < USUAL_MIN:
            return None
        all_roles = sorted({k for n in nights for k in n["roles"]} | {k for u in usual_rows for k in u})
        usual = {k: engine._median([u.get(k, 0) for u in usual_rows]) for k in all_roles}
        deltas = []
        for role in all_roles:
            per = [n["roles"].get(role, 0) - usual[role] for n in nights]
            game_med = engine._median([n["roles"].get(role, 0) for n in nights])
            delta = game_med - usual[role]
            if abs(delta) < 1:
                continue
            starts = sorted(s for n in nights for s in (n["starts"].get(role) or []))
            deltas.append({"role": role, "game": game_med, "usual": usual[role], "delta": delta,
                           "every_game": all(p >= 1 for p in per) if delta > 0 else all(p <= -1 for p in per),
                           "from": _clock_of(starts[len(starts) // 2]) if starts else None})
        deltas.sort(key=lambda x: (-x["delta"], x["role"]))
        weekday = _d(e["event_date"]).strftime("%A") if e.get("event_date") else _d(nights[0]["date"]).strftime("%A")
        eff = engine.effect_for(restaurant_id, e, db_path=db_path)
        recommend = []
        if (len(nights) >= engine.SEGMENT_MIN_N and eff and eff.get("median_lift_pct") is not None
                and float(eff["median_lift_pct"]) >= LIFT_FLOOR):
            recommend = [d for d in deltas if d["delta"] >= 1 and d["every_game"]][:3]
        kind = engine.kind_words(e)
        n = len(nights)
        if recommend:
            plan = "; ".join(f"{int(round(d['delta']))} more {d['role']}" + (f" from about {d['from']}" if d["from"]
                                                                             else "") for d in recommend)
            said = ", ".join(f"{d['game']:g} {d['role']} against a usual {d['usual']:g}" for d in recommend)
            text = (f"Staff above a usual {weekday}: {plan}. On your last {n} {kind} you ran {said}, and sales "
                    f"ran {abs(float(eff['median_lift_pct'])):.0f}% above a usual {weekday}.")
        else:
            ups = [d for d in deltas if d["delta"] > 0][:3]
            text = None
            if ups and n == 1:
                last = nights[0]
                lw = _d(last["date"]).strftime("%A")
                said = ", ".join(f"{last['roles'].get(d['role'], 0):g} {d['role']} against a usual {lw}'s "
                                 f"{d['usual']:g}" for d in ups)
                text = f"On {_mdy(last['date'])} you ran {said} — what was staffed, not yet a pattern to plan on."
            elif ups:
                said = ", ".join(f"{d['game']:g} {d['role']} against a usual {d['usual']:g}" for d in ups)
                text = (f"On your last {n} {kind} you ran a median {said} — what was staffed, not yet a pattern "
                        f"to plan on.")
        return {"games": _games_out(nights), "usual": usual, "usual_n": len(usual_rows), "weekday": weekday,
                "deltas": deltas, "recommend": recommend, "n": n, "text": text,
                "basis": (f"punches on file for {n} {kind} and {len(usual_rows)} ordinary same weekdays before "
                          f"them; each person counted once, in the role they worked")}
    except Exception as ex:
        log.warning("event_intel.playbook staffing failed rid=%s: %s", restaurant_id, ex)
        return None


def _games_out(nights):
    return [{"date": n["date"], "describe": n["describe"], "roles": n["roles"]} for n in nights]


def _mdy(iso):
    from time_utils import mdy
    return mdy(iso)


# ── the rush around kickoff ─────────────────────────────────────────────────

def _service_hour(h):
    from time_utils import BUSINESS_DAY_START_HOUR
    h = int(h)
    return h if h >= BUSINESS_DAY_START_HOUR else h + 24


def _hourly(restaurant_id, iso, db_path):
    """{hour: net} for one business date from the checks on file, by the
    hour each check opened; {} with none."""
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute("SELECT substr(opened_at, 12, 2) AS h, SUM(net_sales) AS net FROM pos_tickets "
                            "WHERE restaurant_id=? AND business_date=? AND cancelled=0 AND opened_at IS NOT NULL "
                            "GROUP BY h", (restaurant_id, iso)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            out[int(r["h"])] = float(r["net"] or 0)
        except (TypeError, ValueError):
            continue
    return out


def _span(h):
    a = engine._clock(f"{h % 24:02d}:00")
    b = engine._clock(f"{(h + 1) % 24:02d}:00")
    return f"{a.rstrip('apm') if a[-2:] == b[-2:] else a}–{b}"


def _offset_words(off):
    if off == 0:
        return "the kickoff hour"
    n = abs(off)
    count = "the hour" if n == 1 else f"{('two', 'three', 'four')[n - 2] if n <= 4 else n} hours"
    return f"{count} {'before' if off < 0 else 'after'} kickoff"


def rush(restaurant_id, e, db_path=store.DB_PATH):
    """{"games": [{"date", "kickoff", "peak_hour", "offset", "game", "usual",
    "hours": [{"hour", "game", "usual"}]}], "n", "pattern_offset" or None,
    "text", "basis"} for games of the same side and kickoff class with
    checks on file, or None."""
    try:
        games = [g for g in engine.past_games(restaurant_id, e, db_path=db_path)
                 if _same_class(e, g["event"]) and g["event"].get("kickoff_local")]
        out = []
        for g in games:
            iso = g["event"]["event_date"]
            tonight = _hourly(restaurant_id, iso, db_path)
            if not tonight:
                continue
            usual_curves = [c for c in (_hourly(restaurant_id, u, db_path)
                                        for u in usual_nights(restaurant_id, iso, db_path=db_path)) if c]
            if len(usual_curves) < USUAL_MIN:
                continue
            hours = sorted({h for c in usual_curves for h in c} | set(tonight), key=_service_hour)
            usual = {h: engine._median([c.get(h, 0.0) for c in usual_curves]) for h in hours}
            peak = max(hours, key=lambda h: (tonight.get(h, 0.0) - usual[h], -_service_hour(h)))
            kick = int(str(g["event"]["kickoff_local"])[:2])
            out.append({"date": iso, "kickoff": g["event"]["kickoff_local"], "peak_hour": peak,
                        "offset": _service_hour(peak) - _service_hour(kick),
                        "game": round(tonight.get(peak, 0.0), 2), "usual": round(usual[peak], 2),
                        "usual_n": len(usual_curves),
                        "hours": [{"hour": h, "game": round(tonight.get(h, 0.0), 2), "usual": round(usual[h], 2)}
                                  for h in hours]})
        if not out:
            return None
        offs = [x["offset"] for x in out]
        mid = engine._median(offs)
        pattern = (int(round(mid)) if len(out) >= engine.SEGMENT_MIN_N
                   and all(abs(o - mid) <= RUSH_AGREE_HOURS for o in offs) else None)
        last = out[0]
        weekday = _d(last["date"]).strftime("%A")
        if last["game"] <= last["usual"]:
            text = None
        elif pattern is not None:
            text = (f"On your last {len(out)} {engine.kind_words(e)} the biggest jump over a usual night came "
                    f"{_offset_words(pattern)}.")
        else:
            text = (f"On {_mdy(last['date'])} the biggest jump over a usual {weekday} came {_span(last['peak_hour'])}, "
                    f"{_offset_words(last['offset'])} ({engine._clock(last['kickoff'])}): {_money(last['game'])} "
                    f"against {_money(last['usual'])}.")
        return {"games": out, "n": len(out), "pattern_offset": pattern, "text": text,
                "basis": "checks on file by the hour they opened, against the median of the same hour on ordinary "
                         "same weekdays before each game"}
    except Exception as ex:
        log.warning("event_intel.playbook rush failed rid=%s: %s", restaurant_id, ex)
        return None


# ── tonight's game against the last one like it (the nightly report) ───────

def game_night(restaurant_id, day, net=None, guests=None, labor_pct=None, db_path=store.DB_PATH):
    """{"event_id", "describe", "tonight": {...}, "last": {...} or None,
    "text", "basis"} for the first followed sports event on `day`, or None.
    `net`, `guests` and `labor_pct` are tonight's own figures from the
    report; the usual night and the last game are read here."""
    try:
        sports = [c for c in engine.context_for(restaurant_id, day, db_path=db_path)
                  if c["event"].get("category") == "sports" and c["event"].get("status") != "cancelled"]
        if not sports:
            return None
        c = sports[0]
        e = c["event"]
        weekday = _d(day).strftime("%A")
        side = "home" if e.get("home_away") == "home" else "road"
        tonight = {"net": net, "guests": guests, "labor_pct": labor_pct}
        heads, _roles = _roles_on(restaurant_id, _d(day).isoformat(), db_path)
        tonight["headcount"] = sum(heads.values()) if heads else None
        u = usual_net(restaurant_id, day, db_path=db_path) if net else None
        if u and net:
            tonight.update(usual=u["median"], usual_n=u["n"], lift_pct=round((float(net) / u["median"] - 1) * 100, 1))
        last = engine.last_like(restaurant_id, e, db_path=db_path)
        last_out = None
        if last and last.get("net"):
            le = last["event"]
            last_out = {"event_id": le["id"], "date": le["event_date"], "describe": last["describe"],
                        "net": last["net"], "usual": last.get("baseline"), "lift_pct": last.get("lift_pct"),
                        "headcount": sum(last["headcount"].values()) if isinstance(last.get("headcount"), dict)
                        else None, "labor_pct": last.get("labor_pct"),
                        "weekday": _d(le["event_date"]).strftime("%A")}
        bits = []
        if net and tonight.get("lift_pct") is not None:
            bits.append(f"Tonight's game sold {_money(net)}, {tonight['lift_pct']:+.0f}% against a usual {weekday} "
                        f"({_money(tonight['usual'])})")
        elif net:
            bits.append(f"Tonight's game sold {_money(net)}; there are too few ordinary {weekday}s on file to say "
                        f"what a usual one is")
        if last_out:
            s = f"your last {side} game, {last_out['describe']}, sold {_money(last_out['net'])}"
            if last_out.get("lift_pct") is not None:
                s += f", {float(last_out['lift_pct']):+.0f}% against a usual {last_out['weekday']}"
            bits.append(s)
        elif net:
            bits.append(f"it's the first {side} game measured here")
        text = (bits[0] + (f"; {bits[1]}" if len(bits) > 1 else "") + ".") if bits else None
        return {"event_id": e["id"], "describe": c["describe"], "side": side, "weekday": weekday,
                "tonight": tonight, "last": last_out,
                "text": text[0].upper() + text[1:] if text else None,
                "basis": ("tonight's net from this report against the median of ordinary same weekdays in the 8 "
                          "weeks before; the last game as event memory measured it the same way")}
    except Exception as ex:
        log.warning("event_intel.playbook game_night failed rid=%s: %s", restaurant_id, ex)
        return None


# ── the morning brief's game alert ──────────────────────────────────────────

def _when(day, today):
    gap = (day - today).days
    if gap == 0:
        return "Today"
    if gap == 1:
        return "Tomorrow"
    return f"{day.strftime('%A')} {_mdy(day.isoformat())}"


def campaign_goal(e) -> str:
    """The Campaign Studio goal a game alert starts a draft from."""
    who = engine.describe(e, with_date=False)
    day = _d(e["event_date"]).strftime("%A") if e.get("event_date") else "game day"
    return f"Bring guests in to watch {who} on {day}"


def alert(restaurant_id, today, sees_sales=True, sees_labor=True, marketing=False, db_path=store.DB_PATH):
    """The brief line for the next followed game in [today, today+ALERT_DAYS]
    — {"key", "tone", "text", "claim_kind", "outside", "ask", "action"?,
    "event_id", "staffing"?, "rush"?} — or None. `sees_sales` /
    `sees_labor` are the viewer's (a manager's brief says the game and the
    marketing draft, never the dollars it may not read)."""
    try:
        today = _d(today)
        followed = store.follows(restaurant_id, db_path=db_path)
        skip = store.dismissed(restaurant_id, db_path=db_path)
        rows = [e for e in store.events_for([f["series_id"] for f in followed], today,
                                            today + timedelta(days=ALERT_DAYS), db_path=db_path)
                if e.get("status") not in ("cancelled", "postponed") and e["id"] not in skip]
        if not rows:
            return None
        e = rows[0]
        day = _d(e["event_date"])
        weekday = day.strftime("%A")
        side = "home" if e.get("home_away") == "home" else "road"
        parts = [f"{_when(day, today)}: {engine.describe(e, with_date=False)}."]
        claim = "context"
        eff = engine.effect_for(restaurant_id, e, db_path=db_path)
        st = rush_out = None
        if sees_sales:
            if eff:
                claim = "measured"
                parts.append(eff["basis"][0].upper() + eff["basis"][1:] + ".")
            else:
                last = engine.last_like(restaurant_id, e, db_path=db_path)
                if last and last.get("net") and last.get("lift_pct") is not None:
                    parts.append(f"No pattern measured yet: your last {side} game, {last['describe']}, sold "
                                 f"{_money(last['net'])}, {float(last['lift_pct']):+.0f}% against a usual "
                                 f"{_d(last['event']['event_date']).strftime('%A')} — one night, not enough to plan on.")
                else:
                    parts.append(f"No {side} game measured here yet, so plan a usual {weekday}.")
        if sees_labor:
            st = staffing(restaurant_id, e, db_path=db_path)
            if st and st.get("recommend"):
                parts.append(st["text"])
            rush_out = rush(restaurant_id, e, db_path=db_path)
            if rush_out and rush_out.get("pattern_offset") is not None and e.get("kickoff_local"):
                kick = int(str(e["kickoff_local"])[:2])
                parts.append(f"Expect the jump around {_span((kick + rush_out['pattern_offset']) % 24)} "
                             f"({_offset_words(rush_out['pattern_offset'])}, as on your last {rush_out['n']}).")
        line = {"key": f"event_ahead:{e['id']}", "event_id": e["id"], "source": "events",
                "tone": "action" if (st and st.get("recommend")) else "neutral",
                # A staffing plan is read from measured nights, not measured
                # itself: the email's footer names it an inference.
                "claim_kind": "inferred" if (st and st.get("recommend")) else claim,
                "outside": True, "text": " ".join(parts),
                "ask": f"How should we get ready for {engine.describe(e)}?"}
        if st and st.get("recommend"):
            line["staffing"] = {k: st[k] for k in ("recommend", "basis", "n")}
        if marketing:
            import nav
            line["action"] = {"label": "Draft a game-day campaign",
                              "nav": nav.path("marketing", "campaigns", goal=campaign_goal(e))}
        return line
    except Exception as ex:
        log.warning("event_intel.playbook alert failed rid=%s: %s", restaurant_id, ex)
        return None
