"""
shift_edits.py — one shift changed, the way the Schedule Studio changes it
(schedule audit 10/10/26 MARKET-20, and the live labor moves' write).

Ask Cavnar could generate and publish a whole week but not "move Maria to
close Friday" or "add a second cook Saturday 5-10", the most common things a
manager asks for; the answer was a full regeneration or a trip to the Studio.
Here one edit — add, move (to another day), retime, remove, reassign — is:

  plan()   read-only: the week as it would be, judged by the one legality
           engine (schedule_rules.judge_change: a manager on the floor every
           minute, the floors, a closer until close, each person's hours,
           rest, overlaps, minors, certificates, windows — new or worse
           breaches refuse it), priced (schedule_economics.cost_delta on the
           week's own pricing), with what a change to a week staff already
           have may owe (predictability_pay where a law covers the
           restaurant, else the notice-window warning) and the version it
           was judged on. What the Ask card shows.
  apply()  the same plan again on the week as it is now (the card is never
           trusted), refused if illegal or if the week changed since the
           version the card named, then saved through save_rows — the
           Studio save's own steps: one write of the week and its version
           row (schedule_versions.write_on), each change's origin
           (step_origins), what the save teaches (schedule_learning.
           capture_save — the manager's own edit, or Cavnar AI's when it was
           Cavnar AI's suggestion), the requests the edit moved told
           (shift_requests.flush_notices), the account log. A week staff
           already have is saved, not sent: Send changes tells them, as in
           the Studio.
"""
import json
from datetime import date, datetime, timedelta

import models as _models_mod

OPS = ("add", "move", "retime", "remove", "reassign")


class EditError(ValueError):
    pass


def get_conn(db_path=None):
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def _iso(value, what="date") -> str:
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        raise EditError(f"give the {what} as YYYY-MM-DD")


def week_for(restaurant_id, on_date, db_path=None):
    """The copy of the week holding `on_date` a one-shift edit lands on —
    {id, week_start, week_end, published_at} — or None: the LIVE copy (the
    week staff have, models.live_week_for) when the date is in a published
    week, else the newest copy nobody replaced (the draft). A regenerated,
    unsent draft of a live week used to outrank the live copy, so "move
    Maria to close Friday" landed in a draft staff never get (schedule
    re-audit 10/11/26 BUGSLIFE-16); a caller that means the draft names
    its history id."""
    iso = _iso(on_date)
    try:
        live = _models_mod.live_week_for(restaurant_id, iso, db_path=db_path or _models_mod.DB_PATH)
    except Exception:
        live = None
    conn = get_conn(db_path)
    try:
        if live:
            r = conn.execute("SELECT id, week_start, week_end, published_at FROM schedule_history WHERE id=? "
                             "AND restaurant_id=? AND superseded_by IS NULL", (live["id"], restaurant_id)).fetchone()
            if r:
                return dict(r)
        r = conn.execute("SELECT id, week_start, week_end, published_at FROM schedule_history WHERE restaurant_id=? "
                         "AND superseded_by IS NULL AND substr(week_start,1,10) <= ? AND substr(week_end,1,10) >= ? "
                         "ORDER BY id DESC LIMIT 1", (restaurant_id, iso, iso)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _current(restaurant_id, history_id, db_path=None):
    """(rows, version, published) of the week as stored now."""
    import schedule_versions as _sv
    conn = get_conn(db_path)
    try:
        own = conn.execute("SELECT id, published_at, superseded_by FROM schedule_history WHERE id=? AND "
                           "restaurant_id=?", (int(history_id), restaurant_id)).fetchone()
        if not own:
            raise EditError("that week's schedule is gone")
        if own["superseded_by"]:
            raise EditError("a newer copy of that week replaced this one — open the newer one")
        return _sv.latest_rows(conn, int(history_id)), _sv.latest_version(conn, int(history_id)), bool(own["published_at"])
    finally:
        conn.close()


def _find(rows, name, day, start=None):
    import staff_settings as _ss
    k = _ss.name_key(name)
    hits = [i for i, r in enumerate(rows) if _ss.name_key(r.get("employee")) == k and (r.get("date") or "")[:10] == day
            and (not start or _same_clock(r.get("shift_start"), start))]
    return hits


def _same_clock(a, b) -> bool:
    import schedule_time as _st
    ma, mb = _st.parse_minutes(a), _st.parse_minutes(b)
    return ma is not None and ma == mb


def _clock(value, what) -> str:
    import schedule_time as _st
    m = _st.parse_minutes(value)
    if m is None:
        raise EditError(f"give the {what} like 5:00pm")
    return _st.fmt_minutes(m)


def _canonical(restaurant_id, name, db_path=None) -> str:
    """The roster spelling of a name, or EditError when nobody here has it."""
    import staff_settings as _ss
    want = _ss.name_key(name)
    if not want:
        raise EditError("say who")
    try:
        roster = _ss.roster(restaurant_id, **({"db_path": db_path} if db_path else {}))
    except Exception:
        roster = []
    for e in roster:
        if _ss.name_key(e.get("name")) == want:
            return e["name"]
    raise EditError(f"there is no {name} on the roster here")


def _weekday(iso):
    return date.fromisoformat(iso).strftime("%A")


def build(restaurant_id, rows, op, args, db_path=None) -> tuple:
    """(after_rows, takers, touched, summary) for one edit of `rows`.
    `takers` are [(index into after_rows, name)] the edit puts on a shift
    (judge_change asks each taker_problem first). Raises EditError."""
    import schedule_time as _st
    if op not in OPS:
        raise EditError("the edit is one of add, move, retime, remove, reassign")
    from time_utils import mdy
    after = [dict(r) for r in rows]
    day = _iso(args.get("date"))
    who = (args.get("employee") or "").strip()
    if op == "add":
        name = _canonical(restaurant_id, who, db_path)
        start, end = _clock(args.get("shift_start"), "start"), _clock(args.get("shift_end"), "end")
        role = " ".join(str(args.get("role") or "").split())[:60]
        if not role:
            raise EditError("say which role the shift is")
        row = {"date": day, "day": _weekday(day), "employee": name, "role": role, "shift_start": start,
               "shift_end": end, "notes": ""}
        _st.retime(row, _st.parse_minutes(start), _st.parse_minutes(end))
        after.append(row)
        return after, [(len(after) - 1, name)], {day}, f"Add {name} as {role} {_weekday(day)} {mdy(day)} {start}-{end}"
    hits = _find(rows, who, day, args.get("shift_start"))
    if not hits:
        raise EditError(f"{who or 'they'} {'have' if not who else 'has'} no shift on {mdy(day)}"
                        + (f" starting {args.get('shift_start')}" if args.get("shift_start") else ""))
    if len(hits) > 1:
        raise EditError(f"{who} has {len(hits)} shifts on {mdy(day)} — say which by its start time")
    i = hits[0]
    old = rows[i]
    label = f"{old['employee']}'s {old.get('role') or ''} shift {_weekday(day)} {mdy(day)} " \
            f"{old.get('shift_start')}-{old.get('shift_end')}".replace("  ", " ")
    if op == "remove":
        after.pop(i)
        return after, [], {day}, f"Take {label} off the schedule"
    if op == "reassign":
        name = _canonical(restaurant_id, args.get("to_employee") or "", db_path)
        after[i] = dict(old, employee=name)
        return after, [(i, name)], {day}, f"Give {label} to {name}"
    new = dict(old)
    if op == "move":
        nd = _iso(args.get("new_date"), "new date")
        new.update(date=nd, day=_weekday(nd))
        touched = {day, nd}
    else:
        touched = {day}
    s = _st.parse_minutes(args.get("new_start")) if args.get("new_start") else _st.parse_minutes(old.get("shift_start"))
    e = _st.parse_minutes(args.get("new_end")) if args.get("new_end") else _st.parse_minutes(old.get("shift_end"))
    if s is None or e is None:
        raise EditError("give the new times like 5:00pm")
    if op == "retime" and not (args.get("new_start") or args.get("new_end")):
        raise EditError("say the new start or end")
    _st.retime(new, s, e)
    after[i] = new
    what = (f"Move {label} to {_weekday(new['date'])} {mdy(new['date'])} {new['shift_start']}-{new['shift_end']}"
            if op == "move" else f"Change {label} to {new['shift_start']}-{new['shift_end']}")
    return after, [(i, new["employee"])], touched, what


def _premium(restaurant_id, before, after, published, touched):
    """What a change to a week staff have may owe: J1's priced ledger
    (predictability_pay.preview) where the module and a covering law exist,
    else the notice-window warning. {text, total} or None."""
    if not published:
        return None
    try:
        import predictability_pay as _pp
        pv = _pp.preview(restaurant_id, before, after)
        if pv.get("covered"):
            return {"text": pv.get("text"), "total": pv.get("total"), "priced": True}
    except ImportError:
        pass
    except Exception as e:
        print(f"[shift_edits] predictability pay not priced rid={restaurant_id}: {e!r}")
    try:
        import schedule_rules as _sr
        from time_utils import restaurant_now_by_id
        w = _sr.late_change_warning(_sr.compliance(restaurant_id), sorted(touched),
                                    restaurant_now_by_id(restaurant_id, naive=True).date())
        return {"text": w, "total": None, "priced": False} if w else None
    except Exception:
        return None


def plan(restaurant_id, op, args, history_id=None, db_path=None) -> dict:
    """The edit judged and priced, nothing written:
    {ok, op, history_id, version, published, summary, legal, problems,
    cost, late_change, before, after (the rows of the dates touched)}.
    `ok` False with `error` when it cannot be built at all."""
    import schedule_rules as _sr
    import schedule_economics as _econ
    import shift_requests as _srq
    try:
        day = _iso((args or {}).get("date"))
        hist = ({"id": int(history_id)} if history_id else week_for(restaurant_id, day, db_path))
        if not hist:
            raise EditError("there is no schedule for that week yet — generate it first")
        rows, version, published = _current(restaurant_id, hist["id"], db_path)
        after, takers, touched, summary = build(restaurant_id, rows, op, args or {}, db_path)
    except EditError as e:
        return {"ok": False, "error": str(e)}
    dates = sorted({r.get("date") for r in rows + after if r.get("date")})
    try:
        c = _srq._week_constraints(restaurant_id, after)
    except Exception as e:
        return {"ok": False, "error": f"the week's rules couldn't be read ({e})"}
    problems = _sr.judge_change(rows, after, c, takers=takers)
    try:
        cost = _econ.cost_delta(rows, after, pricing=_econ.week_pricing(restaurant_id, c))
    except Exception:
        cost = None
    # Priced against what staff were TOLD (the published copy), never the
    # working copy with unsent edits (BUGSLIFE-16).
    told = None
    if published:
        try:
            import schedule_versions as _sv_t
            told = _sv_t.published_rows(int(hist["id"]), restaurant_id, db_path=db_path or _models_mod.DB_PATH)
        except Exception as e:
            print(f"[shift_edits] published copy unread rid={restaurant_id}: {e!r}")
    late = _premium(restaurant_id, told if told is not None else rows, after, published, touched)
    return {"ok": True, "op": op, "args": dict(args or {}), "history_id": int(hist["id"]), "version": version,
            "published": published, "summary": summary, "legal": not problems,
            "problems": [p.get("label") for p in problems], "cost": cost, "late_change": late, "dates": dates,
            "before": [r for r in rows if r.get("date") in touched],
            "after": [r for r in after if r.get("date") in touched], "_rows": after}


def save_rows(restaurant_id, history_id, rows, version, user=None, origin=None, flagged=None, db_path=None) -> dict:
    """The Studio save's own steps for rows built server-side: one write of
    the week and its version row under the version check, the origins of
    its changes, what the save teaches, the requests it moved told, the
    account log. `origin` "cavnar:<source>" marks every changed row as
    Cavnar AI's suggestion (never the manager's habit). Returns {ok,
    version, history_id, unsent_changes} or {ok: False, conflict, error}."""
    import schedule_versions as _sv
    from schedule_engine import _rows_to_csv_text
    try:
        rows = _sv.canonical_rows(restaurant_id, [rows])[0]
    except Exception:
        pass
    csv_text = _rows_to_csv_text(rows)
    who = (user.get("username") or user.get("email")) if isinstance(user, dict) else (user or "Cavnar AI")
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        own = conn.execute("SELECT superseded_by, published_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (int(history_id), restaurant_id)).fetchone()
        if own is None:
            raise EditError("that week's schedule is gone")
        if own["superseded_by"]:
            raise EditError("a newer copy of that week replaced this one")
        changed = _sv.rows_changed_since(conn, int(history_id), version)
        if changed:
            conn.rollback()
            return {"ok": False, "conflict": True, "latest_version": changed,
                    "error": "Somebody changed this week since — the edit was judged on an older copy. Ask again."}
        prev = _sv.latest_rows(conn, int(history_id))
        new_rows = _sv.rows_from_csv(csv_text)
        flags = flagged
        if origin and flags is None:
            before_sigs = {_sv.row_sig(r) for r in prev}
            flags = [dict(r, origin=origin, origin_sig=_sv.row_sig(r)) for r in new_rows
                     if _sv.row_sig(r) not in before_sigs]
        step = _sv.step_origins(prev, new_rows, flags or [])
        new_version = _sv.write_on(conn, restaurant_id, int(history_id), "edited", csv_text, saved_by=who,
                                   expected_version=version,
                                   saved_authority=_sv.authority_of(user) if isinstance(user, dict) else _sv.SYSTEM,
                                   row_origins=step["stored"])
        conn.commit()
        published = bool(own["published_at"])
    except _sv.StaleVersion:
        conn.rollback()
        return {"ok": False, "conflict": True, "error": "Somebody changed this week since. Ask again."}
    except EditError as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    try:
        _log = getattr(_models_mod, "log_account_event", None)
        if _log is None:                     # before the publish service moved it into models
            from client_api import log_account_event as _log
        _log(restaurant_id, "schedule_edited", user if isinstance(user, dict) else None,
             detail=f"history {history_id}: one shift ({'Cavnar AI' if origin else who})")
    except Exception:
        pass
    try:
        import shift_requests as _srq
        _srq.flush_notices(restaurant_id)
    except Exception as e:
        print(f"[shift_edits] request notices not sent rid={restaurant_id}: {e!r}")
    try:
        import schedule_learning as _sl
        _sl.capture_save(restaurant_id, int(history_id), new_version, step,
                         user=user if isinstance(user, dict) else None, published=published)
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="schedule_save_capture", context=f"restaurant_id={restaurant_id} shift edit")
        except Exception:
            pass
    pending = None
    if published:
        try:
            pending = (_sv.unsent_changes(restaurant_id, int(history_id), csv_text) or {}).get("people")
        except Exception:
            pending = None
    return {"ok": True, "version": new_version, "history_id": int(history_id), "published": published,
            "unsent_changes": pending}


def apply(restaurant_id, body: dict, user=None, origin=None, db_path=None) -> dict:
    """The confirmed card: {op, history_id, version, ...args}. Planned again
    on the week as it is; refused when illegal or when the week moved past
    the version the card was built on; saved through save_rows."""
    body = body or {}
    op = str(body.get("op") or "").strip().lower()
    args = {k: body.get(k) for k in ("date", "employee", "shift_start", "shift_end", "role", "new_date", "new_start",
                                     "new_end", "to_employee") if body.get(k) not in (None, "")}
    p = plan(restaurant_id, op, args, history_id=body.get("history_id"), db_path=db_path)
    if not p.get("ok"):
        return p
    if body.get("version") not in (None, "") and str(body.get("version")) != str(p["version"]):
        return {"ok": False, "conflict": True,
                "error": "The week changed since this was suggested — ask again so it is judged on the week now."}
    if not p["legal"]:
        return {"ok": False, "illegal": True, "problems": p["problems"],
                "error": "That change breaks a rule: " + (p["problems"][0] if p["problems"] else "a hard rule")}
    out = save_rows(restaurant_id, p["history_id"], p["_rows"], p["version"], user=user, origin=origin,
                    db_path=db_path)
    if out.get("ok"):
        out.update(summary=p["summary"], cost=p["cost"], late_change=p["late_change"])
    return out


def card(restaurant_id, args) -> tuple:
    """(body, summary, why) for Ask's edit_shift card: the edit planned on
    the stored week, the body carrying the week and version it was judged
    on, or (None, None, why) when it cannot be proposed (illegal, unknown
    person or shift)."""
    op = str((args or {}).get("op") or "").strip().lower()
    a = {k: v for k, v in (args or {}).items() if k != "op" and v not in (None, "")}
    if restaurant_id is None:
        return dict(a, op=op), f"{op.capitalize()} a shift", None
    p = plan(restaurant_id, op, a)
    if not p.get("ok"):
        return None, None, p.get("error")
    if not p["legal"]:
        return None, None, "That change breaks a rule: " + "; ".join(p["problems"][:2])
    body = dict(a, op=op, history_id=p["history_id"], version=p["version"])
    summary = p["summary"]
    cost = p.get("cost") or {}
    if cost.get("dollars_delta"):
        summary += f" ({'+' if cost['dollars_delta'] > 0 else '-'}${abs(cost['dollars_delta']):,.0f} labor)"
    if p["published"]:
        summary += " — saved to the week staff have; Send changes tells them"
    if (p.get("late_change") or {}).get("text"):
        summary += ". " + p["late_change"]["text"]
    return body, summary, None
