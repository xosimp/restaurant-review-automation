"""
request_autonomy.py — drops and swaps that approve themselves, by rule, once
the manager's own decisions have earned it (schedule audit 10/10/26,
MARKET-14), and the restaurant's staff-request policy that also says whether
a cut in somebody's availability waits on a manager (MARKET-15,
staff_availability_changes.py).

Every drop and swap waited on a manager, even one that was legal, added no
overtime and kept every floor. Here a request approves itself only when ALL
of these hold, and each automatic decision is logged with its reasons on the
request (auto_reasons_json, decided_how='auto'):

  - the owner turned it on for that kind (`auto_approve`, `kinds`) — off by
    default;
  - that kind has EARNED it: at least EARN_APPROVALS of the manager's own
    approvals of that kind in EARN_WINDOW_DAYS since the last reversal
    (automation_trust, scope "shift_request", subject "drop" / "swap" — as
    review replies earn theirs); a manager reversing one automatic decision
    lapses it at once;
  - it is more than `notice_hours` before the shift (both, for a swap);
  - it is legal and leaves the week no worse by any hard rule — a manager on
    the floor every minute, the closer until close, the floors, either
    person's hours, rest, overlaps (shift_requests.drop_warning for a drop,
    _swap_legal for a swap: schedule_rules.judge_change, the one engine);
  - a swap is between two shifts of the same role family and puts neither
    person into new overtime;
  - Shift Quality for the shifts it touches drops by no more than
    `max_quality_drop` points (when the week can be scored at all — a
    restaurant with nobody rated is said to be unmeasured, never scored 0).

A decision a rule refuses stays with the manager, and the refusal is kept
on the request so the manager sees why it did not go through on its own.
"""
import json
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH

SCOPE = "shift_request"
KINDS = ("drop", "swap")
# The manager's approvals of a kind, since the last reversal, that earn it.
EARN_APPROVALS = 8
EARN_WINDOW_DAYS = 60
DEFAULT_NOTICE_HOURS = 24
DEFAULT_MAX_QUALITY_DROP = 5.0
AUTO_ACTOR = "Cavnar AI (automatic)"
DEFAULTS = {"auto_approve": False, "kinds": list(KINDS), "notice_hours": DEFAULT_NOTICE_HOURS,
            "max_quality_drop": DEFAULT_MAX_QUALITY_DROP, "availability_approval": True,
            # An open shift offered by name to its best legal fit first, once
            # earned (auto_offer_open, MARKET-25). Off by default.
            "auto_offer": False}
# The subject the automatic offer earns its trust under (automation_trust
# scope "shift_request"): the managers' own named offers earn it.
OFFER = "offer"
EARN_OFFERS = 5
# How many legal takers are scored for the best fit (each is a week rescore).
OFFER_CANDIDATES = 5
_BOUNDS = {"notice_hours": (0, 336), "max_quality_drop": (0, 100)}


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# ── the policy ───────────────────────────────────────────────────────────────

def policy(restaurant) -> dict:
    """The restaurant's staff-request policy (restaurants.
    staff_request_policy_json) over DEFAULTS. Never raises."""
    out = dict(DEFAULTS, kinds=list(DEFAULTS["kinds"]))
    raw = getattr(restaurant, "staff_request_policy_json", None) if restaurant is not None else None
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        return out
    for k in ("auto_approve", "availability_approval", "auto_offer"):
        if isinstance(stored.get(k), bool):
            out[k] = stored[k]
    if isinstance(stored.get("kinds"), list):
        out["kinds"] = [k for k in KINDS if k in stored["kinds"]]
    for k, (lo, hi) in _BOUNDS.items():
        try:
            if stored.get(k) is not None:
                out[k] = max(lo, min(hi, float(stored[k])))
        except (TypeError, ValueError):
            pass
    return out


def policy_for(restaurant_id, db_path=DB_PATH) -> dict:
    try:
        return policy(_models_mod.get_restaurant(restaurant_id, db_path or _models_mod.DB_PATH))
    except Exception:
        return dict(DEFAULTS, kinds=list(DEFAULTS["kinds"]))


class PolicyError(ValueError):
    pass


def save_policy(restaurant_id, data: dict, user=None, db_path=DB_PATH) -> dict:
    """Merge `data` into the stored policy (only the keys DEFAULTS knows,
    each checked) and record the change. Returns the policy after."""
    if not isinstance(data, dict):
        raise PolicyError("send the settings as an object")
    cur = policy_for(restaurant_id, db_path)
    new = dict(cur)
    for k in ("auto_approve", "availability_approval", "auto_offer"):
        if k in data:
            if not isinstance(data[k], bool):
                raise PolicyError(f"{k} is true or false")
            new[k] = data[k]
    if "kinds" in data:
        if not isinstance(data["kinds"], list) or any(k not in KINDS for k in data["kinds"]):
            raise PolicyError("kinds is a list of drop and swap")
        new["kinds"] = [k for k in KINDS if k in data["kinds"]]
    for k, (lo, hi) in _BOUNDS.items():
        if k in data:
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                raise PolicyError(f"{k} is a number")
            if not lo <= v <= hi:
                raise PolicyError(f"{k} is between {lo:g} and {hi:g}")
            new[k] = v
    # update_restaurant records the change (change_log, a tracked field).
    from models import update_restaurant
    update_restaurant(restaurant_id, {"staff_request_policy_json": json.dumps(new)},
                      **({} if db_path in (None, DB_PATH) else {"db_path": db_path}))
    return new


# ── earned trust ─────────────────────────────────────────────────────────────

def _kind(req) -> str:
    return "swap" if (req.get("kind") or "drop") == "swap" else "drop"


def manager_approvals(restaurant_id, kind, since=None, db_path=DB_PATH) -> int:
    """The manager's own approvals of `kind` in the window, after `since` (the
    last lapse) — never an automatic one, never one later reversed."""
    floor = (datetime.utcnow() - timedelta(days=EARN_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    # Strictly after a lapse: an approval in the same second as the
    # reversal never counts toward earning it back.
    after = since is not None and str(since)[:19] >= floor
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM shift_change_requests WHERE restaurant_id=? AND COALESCE(kind,'drop')=? "
            "AND COALESCE(decided_how,'manager')='manager' AND decided_at IS NOT NULL "
            + ("AND decided_at > ? " if after else "AND decided_at >= ? ") +
            "AND reversed_at IS NULL AND status IN ('open','covered','approved','expired') "
            "AND COALESCE(decided_by,'') <> ''",
            (restaurant_id, kind if kind == "swap" else "drop", str(since)[:19] if after else floor)).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def trust(restaurant_id, kind, db_path=DB_PATH) -> dict:
    """{earned, approvals, needed, lapsed_at, lapse_reason} for one kind: the
    ledger's state, re-measured — earned once the approvals since the last
    lapse reach EARN_APPROVALS. Never raises."""
    import automation_trust
    st = automation_trust.states(restaurant_id, SCOPE, db_path=db_path).get(kind) or {}
    since = st.get("lapsed_at") if st.get("state") == "lapsed" else None
    try:
        n = manager_approvals(restaurant_id, kind, since=since, db_path=db_path)
    except Exception:
        n = 0
    earned = st.get("state") == "earned" or n >= EARN_APPROVALS
    if earned and st.get("state") != "earned":
        automation_trust.transition(restaurant_id, SCOPE, kind, True,
                                    basis={"approvals": n, "window_days": EARN_WINDOW_DAYS}, db_path=db_path)
    return {"kind": kind, "earned": bool(earned), "approvals": n, "needed": EARN_APPROVALS,
            "lapsed_at": st.get("lapsed_at"), "lapse_reason": st.get("lapse_reason")}


def status(restaurant_id, db_path=DB_PATH) -> dict:
    """What the owner's switch shows: the policy and each kind's trust."""
    return {"policy": policy_for(restaurant_id, db_path),
            "trust": dict({k: trust(restaurant_id, k, db_path=db_path) for k in KINDS},
                          **{OFFER: offer_trust(restaurant_id, db_path=db_path)}),
            "earn_approvals": EARN_APPROVALS, "earn_window_days": EARN_WINDOW_DAYS}


def record_manager_decision(restaurant_id, row, approve, how="manager", db_path=DB_PATH):
    """A manager's approval counts toward the kind's trust (re-measured);
    nothing for an automatic one. Never raises."""
    try:
        if approve and (how or "manager") == "manager" and row:
            trust(restaurant_id, _kind(row), db_path=db_path)
    except Exception as e:
        print(f"[request_autonomy] trust not re-measured rid={restaurant_id}: {e!r}")


# ── judging one request ──────────────────────────────────────────────────────

def _hours_to(restaurant_id, day, start, now):
    import shift_requests as _srq
    s, _e = _srq.shift_span(day, start)
    if s is None:
        return None
    return (s - now).total_seconds() / 3600.0


def _quality_drop(restaurant_id, before_rows, after_rows, touched):
    """(points, measured): the largest fall in Shift Quality of the
    (date, daypart) shifts `touched` from before_rows to after_rows —
    (None, False) when the week cannot be scored (nobody rated)."""
    import schedule_engine as _se
    try:
        inputs = _se.quality_inputs_from_db(restaurant_id, week_rows=before_rows)
        q0, _w = _se._score_schedule_quality(restaurant_id, before_rows, inputs, what_if=False)
        q1, _w = _se._score_schedule_quality(restaurant_id, after_rows, inputs, what_if=False)
    except Exception as e:
        print(f"[request_autonomy] quality unread rid={restaurant_id}: {e!r}")
        return None, False
    if not (q0 or {}).get("checked") or not (q1 or {}).get("checked"):
        return None, False

    def _by(q):
        return {(s.get("date"), s.get("daypart")): s.get("score") for s in (q.get("shifts") or [])
                if s.get("scored") and s.get("score") is not None}
    b, a = _by(q0), _by(q1)
    drops = [float(b[k]) - float(a.get(k, 0) or 0) for k in touched if k in b]
    if not drops:
        return None, False
    return round(max(drops), 1), True


def judge(restaurant_id, req, now=None, db_path=DB_PATH) -> dict:
    """{ok, kind, reasons, refusals}: whether `req` (a pending drop or swap)
    approves itself now. Read-only. `reasons` are the checks it passed,
    `refusals` the ones it failed — said in the manager's words."""
    import shift_requests as _srq
    import schedule_rules as _rules
    from time_utils import mdy
    kind = _kind(req)
    out = {"ok": False, "kind": kind, "reasons": [], "refusals": []}
    pol = policy_for(restaurant_id, db_path)
    if not pol["auto_approve"] or kind not in pol["kinds"]:
        out["refusals"].append(f"automatic approval of {kind}s is off")
        return out
    t = trust(restaurant_id, kind, db_path=db_path)
    if not t["earned"]:
        out["refusals"].append(f"not earned yet — {t['approvals']} of the {t['needed']} {kind}s you approve "
                               "yourself before they can approve themselves"
                               + (f" (back to you since {mdy(str(t['lapsed_at'])[:10])}: {t['lapse_reason']})"
                                  if t.get("lapsed_at") and t.get("lapse_reason") else ""))
        return out
    now = _srq._now(restaurant_id, now)
    legs = [(req["date"], req["shift_start"])]
    if kind == "swap":
        legs.append((req["target_date"], req["target_start"]))
    for day, start in legs:
        h = _hours_to(restaurant_id, day, start, now)
        if h is None or h < float(pol["notice_hours"]):
            out["refusals"].append(f"less than {pol['notice_hours']:g} hours before the shift")
            return out
    out["reasons"].append(f"more than {pol['notice_hours']:g} hours' notice")
    hist, rows = _srq._live_week(restaurant_id, req, db_path)
    if not hist:
        out["refusals"].append("the week it belongs to isn't live")
        return out
    try:
        c = _srq._week_constraints(restaurant_id, rows)
    except Exception as e:
        print(f"[request_autonomy] rules unread rid={restaurant_id}: {e!r}")
        out["refusals"].append("the week's rules couldn't be read")
        return out
    if kind == "drop":
        idx = _srq._find(rows, req["employee_name"], req["date"], req["shift_start"])
        if idx is None:
            out["refusals"].append("that shift is no longer on the week")
            return out
        warn, _k = _srq.drop_warning(restaurant_id, req, db_path, rows=rows, constraints=c)
        if warn or _k:
            # A breach, or rules that could not be read (DROP_UNCHECKED):
            # either way a manager decides it, never the automatic approval.
            out["refusals"].append(warn or "the week's rules couldn't be checked")
            return out
        out["reasons"].append("the manager on the floor, the closers and every floor stay as they are")
        after = rows[:idx] + rows[idx + 1:]
        touched = {(rows[idx]["date"], _rules.daypart_of(rows[idx].get("shift_start") or ""))}
    else:
        after, a, b = _srq.swap_rows(rows, req)
        if after is None:
            out["refusals"].append("one of the shifts is no longer on the week")
            return out
        if c.family(rows[a].get("role") or "") != c.family(rows[b].get("role") or ""):
            out["refusals"].append("the two shifts are different roles")
            return out
        out["reasons"].append("both shifts are the same role")
        try:
            _srq._swap_legal(restaurant_id, req, db_path, rows=rows, constraints=c)
        except _srq.ShiftRequestError as e:
            out["refusals"].append(str(e))
            return out
        out["reasons"].append("legal both ways, and the manager on the floor and the closers stay")
        for i, name in ((a, req["target_name"]), (b, req["employee_name"])):
            extra = _rules.overtime_added(c, name, after[i], [r for j, r in enumerate(after) if j != i])
            if extra > 0.05:
                out["refusals"].append(f"it puts {name} {extra:g}h into overtime")
                return out
        out["reasons"].append("no new overtime for either of them")
        touched = {(rows[a]["date"], _rules.daypart_of(rows[a].get("shift_start") or "")),
                   (rows[b]["date"], _rules.daypart_of(rows[b].get("shift_start") or ""))}
    drop, measured = _quality_drop(restaurant_id, rows, after, touched)
    if measured:
        if drop > float(pol["max_quality_drop"]):
            out["refusals"].append(f"Shift Quality falls {drop:g} points (your limit is {pol['max_quality_drop']:g})")
            return out
        out["reasons"].append(f"Shift Quality falls {max(drop, 0):g} points at most")
    else:
        out["reasons"].append("Shift Quality not measured (nobody rated on those shifts)")
    out["ok"] = True
    return out


def _log(restaurant_id, request_id, verdict, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE shift_change_requests SET auto_reasons_json=? WHERE id=? AND restaurant_id=?",
                     (json.dumps(dict(verdict, at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))[:4000],
                      int(request_id), restaurant_id))
        conn.commit()
    finally:
        conn.close()


def try_auto(restaurant_id, req, now=None, db_path=DB_PATH):
    """Approve a just-asked drop or swap on its own when judge() says so: the
    manager's own decide path (decided_how='auto'), the reasons logged on the
    request, the deciders told it was approved automatically and how to
    reverse it. Returns the decided row, or None when it stays with the
    manager (the refusal logged when automatic approval is on and earned).
    Never raises."""
    import shift_requests as _srq
    try:
        verdict = judge(restaurant_id, req, now=now, db_path=db_path)
    except Exception as e:
        print(f"[request_autonomy] judge failed rid={restaurant_id}: {e!r}")
        return None
    pol = policy_for(restaurant_id, db_path)
    if not verdict["ok"]:
        if pol["auto_approve"]:
            try:
                _log(restaurant_id, req["id"], verdict, db_path)
            except Exception:
                pass
        return None
    try:
        _log(restaurant_id, req["id"], verdict, db_path)
        out = _srq.decide(restaurant_id, req["id"], True, decided_by=AUTO_ACTOR, db_path=db_path, now=now,
                          decided_how="auto")
    except _srq.ShiftRequestError as e:
        _log(restaurant_id, req["id"], dict(verdict, ok=False, refusals=[str(e)]), db_path)
        return None
    except Exception as e:
        print(f"[request_autonomy] auto decision failed rid={restaurant_id}: {e!r}")
        return None
    if not out:
        return None
    try:
        what = "swap" if verdict["kind"] == "swap" else "drop"
        _srq._tell_managers(
            restaurant_id, f"{what.capitalize()} approved automatically",
            f"{req.get('employee_name')}'s {what} of {_srq._when(req)}"
            + (f" for {req.get('target_name')}'s {_srq._when(req, 'target_')}" if what == "swap" else "")
            + " was approved automatically: " + "; ".join(verdict["reasons"])
            + ". If that was wrong, reverse it in Labor — automatic approval of "
            + f"{what}s then goes back to you.", db_path)
    except Exception as e:
        print(f"[request_autonomy] notice failed rid={restaurant_id}: {e!r}")
    return dict(out, auto_approved=True, auto_reasons=verdict["reasons"])


# ── a manager reversing an automatic decision ────────────────────────────────

def reverse(restaurant_id, request_id, actor=None, db_path=DB_PATH, now=None) -> dict:
    """The manager's "that shouldn't have gone through": the automatic
    decision undone where it still can be — an open drop goes back to its
    holder (the teammates told it was open hear it's gone), a swap waiting on
    the colleague is closed, a swap that went through is traded back when
    that is still legal — and automatic approval of that kind lapses at once
    (automation_trust), whatever could be undone. Returns {ok, request,
    undone, note}."""
    import automation_trust
    import shift_requests as _srq
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? "
                           "AND decided_how='auto' AND reversed_at IS NULL", (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That wasn't an automatic decision, or it was already reversed."}
    row = dict(row)
    kind = _kind(row)
    who = (actor or "a manager").strip()[:120]
    undone, note = False, None
    if row["status"] == "open" and kind == "drop":
        conn = get_conn(db_path)
        try:
            cur = conn.execute("UPDATE shift_change_requests SET status='denied', reversed_at=datetime('now'), "
                               "reversed_by=?, decision_note=? WHERE id=? AND status='open'",
                               (who, "Your manager reversed the automatic approval — you're on this shift.", row["id"]))
            released = _srq._close_offers(conn, row["id"]) if cur.rowcount == 1 else []
            conn.commit()
            undone = cur.rowcount == 1
        finally:
            conn.close()
        if undone:
            _srq._notify(restaurant_id, "auto_reversed", dict(row, status="denied"), db_path=db_path)
            for o in released:
                _srq._notify_offer(restaurant_id, "offer_withdrawn", o, row, db_path)
    elif row["status"] == "approved" and kind == "swap":
        conn = get_conn(db_path)
        try:
            cur = conn.execute("UPDATE shift_change_requests SET status='denied', reversed_at=datetime('now'), "
                               "reversed_by=?, decision_note=? WHERE id=? AND status='approved'",
                               (who, "Your manager reversed the automatic approval of this swap.", row["id"]))
            conn.commit()
            undone = cur.rowcount == 1
        finally:
            conn.close()
        if undone:
            _srq._notify(restaurant_id, "auto_reversed", dict(row, status="denied"), db_path=db_path)
    elif row["status"] == "covered" and kind == "swap":
        # The shifts as they are now: the holder's old shift (shift_id) is
        # the colleague's, and theirs (target_shift_id) the holder's — so
        # each leg's person is the other's (ARCH-6).
        back = dict(row, employee_name=row["target_name"], date=row["date"], shift_start=row["shift_start"],
                    target_name=row["employee_name"], target_date=row["target_date"],
                    target_start=row["target_start"], person_id=row.get("target_person_id"),
                    target_person_id=row.get("person_id"))
        try:
            undone = bool(_srq.trade_back(restaurant_id, back, actor=who, db_path=db_path, now=now))
        except _srq.ShiftRequestError as e:
            note = f"The swap couldn't be traded back ({e}) — change the week in the Studio."
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE shift_change_requests SET reversed_at=datetime('now'), reversed_by=? WHERE id=?",
                         (who, row["id"]))
            conn.commit()
        finally:
            conn.close()
    else:
        note = "Somebody already took that shift — change the week in the Studio if it should be undone."
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE shift_change_requests SET reversed_at=datetime('now'), reversed_by=? WHERE id=?",
                         (who, row["id"]))
            conn.commit()
        finally:
            conn.close()
    automation_trust.transition(restaurant_id, SCOPE, kind, False,
                                reason=f"you reversed an automatic {kind} approval", db_path=db_path)
    return {"ok": True, "undone": undone, "note": note, "request": _srq._get_row(restaurant_id, row["id"], db_path)}


# ── the open shift offered to its best fit first (MARKET-25) ────────────────
#
# The marketplace broadcast every open shift to every legal taker in turn and
# left the choosing to whoever answered first. Once a restaurant's managers
# have made EARN_OFFERS named offers themselves (since the last reversal), and
# the owner turned `auto_offer` on, Cavnar AI does what they do: the legal
# takers (the claim's own check — shift_requests._who_could_take) are scored
# by what each does to the shift's Shift Quality, and the best fit is offered
# the shift by name before the board hears of it — inside these guardrails:
#   - legal by the one engine (replacement_is_legal → judge_change: a manager
#     on the floor every minute, the floors, closers, rest, hours, minors);
#   - no overtime while a same-role teammate has room (teammates_with_room);
#   - the shift's Shift Quality falls by no more than `max_quality_drop` from
#     the week as published (unmeasured weeks say so, never score 0).
# A manager withdrawing an automatic offer (reverse_offer) lapses it at once.

def manager_offers(restaurant_id, since=None, db_path=DB_PATH) -> int:
    """Named offers the managers made themselves in the window since the
    last lapse — never an automatic one, never a coverage issue's."""
    floor = (datetime.utcnow() - timedelta(days=EARN_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    after = since is not None and str(since)[:19] >= floor
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM shift_offers WHERE restaurant_id=? AND COALESCE(created_by,'') NOT IN "
            "('', ?, 'coverage issue') AND created_at " + ("> ?" if after else ">= ?"),
            (restaurant_id, AUTO_ACTOR, str(since)[:19] if after else floor)).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


def offer_trust(restaurant_id, db_path=DB_PATH) -> dict:
    """{kind: "offer", earned, approvals, needed, lapsed_at, lapse_reason}."""
    import automation_trust
    st = automation_trust.states(restaurant_id, SCOPE, db_path=db_path).get(OFFER) or {}
    since = st.get("lapsed_at") if st.get("state") == "lapsed" else None
    try:
        n = manager_offers(restaurant_id, since=since, db_path=db_path)
    except Exception:
        n = 0
    earned = st.get("state") == "earned" or n >= EARN_OFFERS
    if earned and st.get("state") != "earned":
        automation_trust.transition(restaurant_id, SCOPE, OFFER, True,
                                    basis={"offers": n, "window_days": EARN_WINDOW_DAYS}, db_path=db_path)
    return {"kind": OFFER, "earned": bool(earned), "approvals": n, "needed": EARN_OFFERS,
            "lapsed_at": st.get("lapsed_at"), "lapse_reason": st.get("lapse_reason")}


def _shift_score(restaurant_id, rows, key, inputs):
    """The Shift Quality score of the (date, daypart) shift `key` in `rows`,
    or None when it cannot be scored (nobody rated)."""
    import schedule_engine as _se
    try:
        q, _w = _se._score_schedule_quality(restaurant_id, rows, inputs, what_if=False)
    except Exception:
        return None
    if not (q or {}).get("checked"):
        return None
    for s in q.get("shifts") or []:
        if (s.get("date"), s.get("daypart")) == key and s.get("scored") and s.get("score") is not None:
            return float(s["score"])
    return None


def rank_takers(restaurant_id, req, db_path=DB_PATH, limit=OFFER_CANDIDATES) -> list:
    """The legal takers of an open shift scored for the best fit:
    [{name, score, drop, overtime, ok, why}] best first. `ok` False (with
    `why`) for one a guardrail stops."""
    import schedule_engine as _se
    import schedule_rules as _rules
    import shift_requests as _srq
    import schedule_shifts
    legal = _srq._who_could_take(restaurant_id, req, db_path)[:limit]
    if not legal:
        return []
    conn = get_conn(db_path)
    try:
        hist = _srq._live_hist(conn, restaurant_id, req)
        live_rows = schedule_shifts.rows_of(hist["id"], "live", conn=conn) if hist else []
    finally:
        conn.close()
    if not hist:
        return []
    rows, idx = _srq._locate(live_rows, req)
    if idx is None:
        return []
    c = _srq._week_constraints(restaurant_id, rows)
    key = (rows[idx].get("date"), _rules.daypart_of(rows[idx].get("shift_start") or ""))
    try:
        inputs = _se.quality_inputs_from_db(restaurant_id, week_rows=rows)
    except Exception:
        inputs = None
    base = _shift_score(restaurant_id, rows, key, inputs) if (inputs is not None and not _srq._is_extra(req)) else None
    room = None
    pol = policy_for(restaurant_id, db_path)
    out = []
    for name in legal:
        trial = [dict(r) for r in rows]
        trial[idx] = dict(trial[idx], employee=name)
        ot = _rules.overtime_added(c, name, trial[idx], [r for j, r in enumerate(trial) if j != idx])
        score = _shift_score(restaurant_id, trial, key, inputs) if inputs is not None else None
        drop = round(base - score, 1) if (base is not None and score is not None) else None
        entry = {"name": name, "score": score, "drop": drop, "overtime": round(ot, 2), "ok": True, "why": None}
        if ot > 0.05:
            if room is None:
                room = _srq.teammates_with_room(restaurant_id, rows, idx, c, exclude=(req.get("employee_name"),))
            if room:
                entry.update(ok=False, why=f"puts {name} {ot:g}h into overtime while {room[0]} has room")
        if entry["ok"] and drop is not None and drop > float(pol["max_quality_drop"]):
            entry.update(ok=False, why=f"Shift Quality falls {drop:g} points (your limit is "
                                       f"{pol['max_quality_drop']:g})")
        out.append(entry)
    out.sort(key=lambda e: (not e["ok"], -(e["score"] if e["score"] is not None else -1), e["overtime"],
                            legal.index(e["name"])))
    return out


def auto_offer_open(restaurant_id, req, db_path=DB_PATH):
    """Offer a just-opened shift to its best legal fit by name, when the
    owner turned automatic offers on and the managers' own offers earned
    it. Returns the offer, or None (the reasons kept on the request when it
    is on but refused). Never raises."""
    import shift_requests as _srq
    try:
        pol = policy_for(restaurant_id, db_path)
        if not pol.get("auto_offer") or not req or req.get("status") != "open" or req.get("offer_only"):
            return None
        t = offer_trust(restaurant_id, db_path=db_path)
        verdict = {"ok": False, "kind": OFFER, "reasons": [], "refusals": []}
        if not t["earned"]:
            verdict["refusals"].append(f"not earned yet — {t['approvals']} of the {t['needed']} named offers you "
                                       "make yourself before Cavnar AI offers open shifts on its own")
            _log(restaurant_id, req["id"], verdict, db_path)
            return None
        ranked = rank_takers(restaurant_id, req, db_path=db_path)
        pick = next((e for e in ranked if e["ok"]), None)
        if not pick:
            verdict["refusals"].append("nobody legal clears the guardrails" if ranked else "nobody legal can take it")
            verdict["candidates"] = ranked[:3]
            _log(restaurant_id, req["id"], verdict, db_path)
            return None
        offer = _srq.offer_shift(restaurant_id, req["id"], pick["name"], actor=AUTO_ACTOR,
                                 note="You're the best fit for this one.", db_path=db_path)
        verdict.update(ok=True, offered_to=pick["name"],
                       reasons=["legal by every rule, the manager on the floor and the closers kept",
                                "no overtime while a teammate has room" if pick["overtime"] <= 0.05 else
                                f"{pick['overtime']:g}h of overtime — nobody with room could take it",
                                (f"Shift Quality falls {max(pick['drop'], 0):g} points at most"
                                 if pick["drop"] is not None else "Shift Quality not measured (nobody rated)")])
        _log(restaurant_id, req["id"], verdict, db_path)
        return offer
    except Exception as e:
        print(f"[request_autonomy] automatic offer failed rid={restaurant_id}: {e!r}")
        return None


def reverse_offer(restaurant_id, offer_id, actor=None, db_path=DB_PATH) -> dict:
    """A manager withdrawing an automatic offer: withdrawn where nobody has
    answered yet, and automatic offers lapse at once (automation_trust)."""
    import automation_trust
    import shift_requests as _srq
    conn = get_conn(db_path)
    try:
        o = conn.execute("SELECT * FROM shift_offers WHERE id=? AND restaurant_id=? AND created_by=?",
                         (int(offer_id), restaurant_id, AUTO_ACTOR)).fetchone()
        if not o:
            return {"ok": False, "error": "That wasn't an automatic offer."}
        o = dict(o)
        cur = conn.execute("UPDATE shift_offers SET status='withdrawn', answered_at=datetime('now') WHERE id=? "
                           "AND status='offered'", (o["id"],))
        conn.commit()
        undone = cur.rowcount == 1
    finally:
        conn.close()
    if undone:
        req = _srq._get_row(restaurant_id, o["request_id"], db_path)
        _srq._notify_offer(restaurant_id, "offer_withdrawn", o, req or {}, db_path)
    automation_trust.transition(restaurant_id, SCOPE, OFFER, False,
                                reason="you withdrew an automatic offer", db_path=db_path)
    return {"ok": True, "undone": undone,
            "note": None if undone else "They already answered — change the week in the Studio if it should change."}
