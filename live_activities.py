"""live_activities.py — what the phone's Live Activities and silent widget
pushes are ABOUT (iOS parity audit 10/7/26 #31, #38, #61, #94). push.py
sends them; this module decides who gets one, with what, and when.

Three Live Activities, each a Swift ActivityAttributes type whose JSON shape
the dicts below must match key for key (ios/CavnarAI/Shared/*Activity.swift;
tests/test_live_activities.py holds both sides):

- pending_send (PendingSendAttributes): "Next week's schedule goes out in
  12:04 — Undo". Started on every phone allowed to undo it the moment a send
  is queued (delayed.schedule), wherever it was queued from, and ended when it
  is undone or runs (#61).
- schedule_build (ScheduleBuildAttributes): "Building next week · 3 of 7 days
  drafted". The app starts it when the owner starts a generation; the job's
  progress and its end reach it by push (#38).
- service (ServiceAttributes): "Tonight's service · ▲ 8% vs a typical Fri ·
  Dana hasn't clocked in". Opt-in per phone; started at service open, updated
  from the 20-minute service slot, ended at close (#94).

Nothing here sends, posts or approves anything outward: a Live Activity
only shows, and its one button (Undo) is the safe direction.

Every entry point is called from a job or a write path that must not fail
because of a phone: each catches its own errors and returns a count."""
from datetime import datetime, timedelta, timezone
import json

import push
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _capture(e, job, context=""):
    # A database without these tables is one init_push has not run on (a
    # fixture, a script before boot): nothing to report.
    if "no such table" in str(e):
        return
    try:
        import ops
        ops.capture(e, job=job, context=context)
    except Exception:
        pass


# ── who ──────────────────────────────────────────────────────────────────────

def _viewer(user_id, restaurant_id, db_path=None) -> dict:
    """The login as the permission checks read it at this location — its
    role here and the owner's grants, as the request decorators load them."""
    from auth import get_user_by_id, get_membership
    user = get_user_by_id(int(user_id), db_path or DB_PATH) or {}
    member = get_membership(int(user_id), int(restaurant_id), db_path or DB_PATH) or {}
    viewer = {"id": int(user_id), "restaurant_id": int(restaurant_id),
              "role": member.get("role") or user.get("role"), "is_admin": user.get("is_admin")}
    try:
        from auth import _grants_for
        conn = get_conn(db_path)
        try:
            viewer["grants"] = _grants_for(conn, int(user_id), int(restaurant_id))
        finally:
            conn.close()
    except Exception:
        viewer["grants"] = ()
    return viewer


def _allowed_tokens(tokens, restaurant_id, check, db_path=None) -> list:
    """The tokens whose login passes `check(viewer)` here, each login read once."""
    seen, out = {}, []
    for t in tokens:
        uid = int(t["user_id"])
        if uid not in seen:
            try:
                seen[uid] = bool(check(_viewer(uid, restaurant_id, db_path)))
            except Exception:
                seen[uid] = False
        if seen[uid]:
            out.append(t)
    return out


def may_watch_service(user) -> bool:
    """Tonight's service reads the pulse and who hasn't clocked in — the
    Labor tab's data."""
    from permissions import has_permission, LABOR_VIEW
    return bool((user or {}).get("is_admin")) or has_permission(user, LABOR_VIEW)


def may_watch(user, activity_type) -> bool:
    """Whether this login may hold a push-to-start token for the type. A
    pending send's own check (who may undo THAT kind) runs again per send."""
    if activity_type == "service":
        return may_watch_service(user)
    if activity_type == "pending_send":
        import strategy_routes
        return any(strategy_routes._may_undo(user, k) for k in COUNTDOWN_KINDS)
    if activity_type == "schedule_build":
        import strategy_routes
        return bool((user or {}).get("is_admin")) or strategy_routes._may_publish(user) or _drafts(user)
    return False


def _drafts(user) -> bool:
    from permissions import has_permission, SCHEDULE_DRAFT
    return has_permission(user, SCHEDULE_DRAFT)


# ── runs: started once, updated on change, ended once ────────────────────────

def _claim_start(restaurant_id, activity_type, key, db_path=None) -> bool:
    """True once per (restaurant, type, key): the push-to-start goes out once,
    however many passes see the same send or the same service."""
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO live_activity_runs (restaurant_id, activity_type, activity_key) "
                     "VALUES (?,?,?)", (int(restaurant_id), activity_type, str(key)))
        cur = conn.execute("UPDATE live_activity_runs SET started_at=datetime('now'), updated_at=datetime('now') "
                           "WHERE restaurant_id=? AND activity_type=? AND activity_key=? AND started_at IS NULL "
                           "AND ended_at IS NULL", (int(restaurant_id), activity_type, str(key)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _run(restaurant_id, activity_type, key, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM live_activity_runs WHERE restaurant_id=? AND activity_type=? "
                         "AND activity_key=?", (int(restaurant_id), activity_type, str(key))).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _state_changed(restaurant_id, activity_type, key, state, db_path=None) -> bool:
    """Stores `state` and says whether it differs from the last one sent.
    `updatedAt` is left out of the comparison: the clock alone is no news."""
    probe = json.dumps({k: v for k, v in (state or {}).items() if k != "updatedAt"}, sort_keys=True)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO live_activity_runs (restaurant_id, activity_type, activity_key) "
                     "VALUES (?,?,?)", (int(restaurant_id), activity_type, str(key)))
        cur = conn.execute("UPDATE live_activity_runs SET state_json=?, updated_at=datetime('now') "
                           "WHERE restaurant_id=? AND activity_type=? AND activity_key=? "
                           "AND COALESCE(state_json, '') <> ?",
                           (probe, int(restaurant_id), activity_type, str(key), probe))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _claim_end(restaurant_id, activity_type, key, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO live_activity_runs (restaurant_id, activity_type, activity_key) "
                     "VALUES (?,?,?)", (int(restaurant_id), activity_type, str(key)))
        cur = conn.execute("UPDATE live_activity_runs SET ended_at=datetime('now'), updated_at=datetime('now') "
                           "WHERE restaurant_id=? AND activity_type=? AND activity_key=? AND ended_at IS NULL",
                           (int(restaurant_id), activity_type, str(key)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _unix(dt) -> int:
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_utc(raw):
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00").replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── #61: the queued-send countdown ───────────────────────────────────────────

COUNTDOWN_KINDS = ("schedule_publish", "order_send", "schedule_changes_send")
_KICKER = {"order_send": "Supplier order goes out", "schedule_changes_send": "Schedule changes go out"}
_PLAIN_TITLE = {"order_send": "Supplier order", "schedule_changes_send": "Changes to the sent schedule"}
# How long the finished countdown stays on the Lock Screen.
_END_LINGER = {"stopped": 8, "sent": 15 * 60, "failed": 30 * 60}


def kicker(kind) -> str:
    """PendingSendAttributes.kicker, as the push's alert title."""
    return _KICKER.get(kind, "Schedule goes out")


def plain_title(kind) -> str:
    return _PLAIN_TITLE.get(kind, "Next week's schedule")


def pending_send_state(execute_at, status="pending", note=None) -> dict:
    """PendingSendAttributes.ContentState."""
    out = {"fireAt": push.apple_date(execute_at), "status": status}
    if note:
        out["note"] = str(note)[:160]
    return out


def pending_send_attributes(action) -> dict:
    """PendingSendAttributes: {actionId, kind, title, restaurantId}. The
    restaurant rides along so a tap opens the send at its own location
    (`?loc=`, re-audit 10/8/26 #6)."""
    label = (action.get("label") or "").strip()
    out = {"actionId": int(action["id"]), "kind": action["kind"],
           "title": label or plain_title(action["kind"])}
    if action.get("restaurant_id"):
        out["restaurantId"] = int(action["restaurant_id"])
    return out


def pending_send_queued(action, db_path=None) -> int:
    """delayed.schedule's hook: start the countdown on every phone whose
    login may undo this kind of send (F3-13), without the app being opened.
    Returns how many phones it was sent to."""
    try:
        kind = (action or {}).get("kind")
        if kind not in COUNTDOWN_KINDS or not push.native_push_allowed():
            return 0
        rid = int(action["restaurant_id"])
        tokens = push.live_activity_tokens("pending_send", push.LA_KIND_START, restaurant_id=rid,
                                           db_path=db_path or DB_PATH)
        if not tokens:
            return 0
        import strategy_routes
        tokens = _allowed_tokens(tokens, rid, lambda v: strategy_routes._may_undo(v, kind), db_path)
        if not tokens or not _claim_start(rid, "pending_send", action["id"], db_path):
            return 0
        fire = _parse_utc(action.get("execute_at"))
        attributes = pending_send_attributes(action)
        return push.fire_live_activity(
            tokens, "pending_send", "start", pending_send_state(action.get("execute_at")),
            attributes=attributes,
            alert={"title": kicker(kind), "body": attributes["title"] + " — Undo before it goes, if you'd rather look first."},
            stale_at=_unix(fire + timedelta(seconds=60)) if fire else None, db_path=db_path or DB_PATH)
    except Exception as e:
        _capture(e, "live_activity_pending_send", f"action={(action or {}).get('id')}")
        return 0


def pending_send_finished(restaurant_id, action_id, status, note=None, db_path=None) -> int:
    """delayed.cancel / delayed._run_one's hook: end the countdown on every
    phone running it — "stopped" (undone), "sent" or "failed" (with the
    reason). Returns how many phones were told."""
    try:
        if status not in _END_LINGER or not push.native_push_allowed():
            return 0
        if not _claim_end(restaurant_id, "pending_send", action_id, db_path):
            return 0
        tokens = push.live_activity_tokens("pending_send", push.LA_KIND_UPDATE, restaurant_id=restaurant_id,
                                           activity_key=str(action_id), db_path=db_path or DB_PATH)
        if not tokens:
            return 0
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT execute_at FROM delayed_actions WHERE id=?", (int(action_id),)).fetchone()
        finally:
            conn.close()
        now = datetime.now(timezone.utc)
        n = push.fire_live_activity(
            tokens, "pending_send", "end",
            pending_send_state(row["execute_at"] if row else now, status=status, note=note),
            dismiss_at=_unix(now + timedelta(seconds=_END_LINGER[status])), db_path=db_path or DB_PATH)
        push.remove_live_activity_tokens_for(tokens, db_path=db_path or DB_PATH)
        return n
    except Exception as e:
        _capture(e, "live_activity_pending_send", f"action={action_id} end")
        return 0


# ── #38: building next week ─────────────────────────────────────────────────

def schedule_build_state(progress=None, estimated_end=None, status="building", note=None) -> dict:
    """ScheduleBuildAttributes.ContentState: the days drafted so far (a real
    count, never a percentage), when a typical run would finish, and how it
    ended."""
    p = progress or {}
    out = {"daysDrafted": int(p.get("days_drafted") or 0), "daysTotal": int(p.get("days_total") or 0),
           "status": status}
    end = push.apple_date(estimated_end) if estimated_end is not None else None
    if end is not None:
        out["estimatedEnd"] = end
    if note:
        out["note"] = str(note)[:160]
    return out


def _job_row(job_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT restaurant_id, kind, COALESCE(started_at, created_at) AS began "
                         "FROM async_jobs WHERE job_id=?", (str(job_id),)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def _estimated_end(job, db_path=None):
    """When a typical generation started at the job's start would finish —
    the measured median (schedule_engine.typical_generation_seconds), never
    a guess: None until there is one."""
    if not job or not job.get("restaurant_id"):
        return None
    try:
        import schedule_engine
        typical = schedule_engine.typical_generation_seconds(int(job["restaurant_id"]), db_path)
    except Exception:
        typical = None
    began = _parse_utc(job.get("began"))
    if not typical or not typical.get("seconds") or not began:
        return None
    return began + timedelta(seconds=int(typical["seconds"]))


def job_progress(job_id, progress, db_path=None) -> int:
    """ops.set_async_job_progress's hook: a day newly drafted reaches every
    phone showing this generation. One indexed read when nobody is."""
    try:
        if not push.native_push_allowed():
            return 0
        # The job's own restaurant scopes the tokens (re-audit 10/8/26 #10):
        # a job id alone matched any restaurant's token filed under it.
        job = _job_row(job_id, db_path)
        if not job or not job.get("restaurant_id"):
            return 0
        rid = int(job["restaurant_id"])
        tokens = push.live_activity_tokens("schedule_build", push.LA_KIND_UPDATE, restaurant_id=rid,
                                           activity_key=str(job_id), db_path=db_path or DB_PATH)
        if not tokens:
            return 0
        state = schedule_build_state(progress, _estimated_end(job, db_path))
        if not _state_changed(rid, "schedule_build", job_id, state, db_path):
            return 0
        return push.fire_live_activity(tokens, "schedule_build", "update", state, db_path=db_path or DB_PATH)
    except Exception as e:
        _capture(e, "live_activity_schedule_build", f"job={job_id}")
        return 0


def job_finished(job_id, status, result=None, db_path=None) -> int:
    """ops.finish_async_job's hook: the generation landed ("done") or did not
    ("failed", with the job's own reason). Ends the activity on every phone."""
    try:
        if not push.native_push_allowed():
            return 0
        job = _job_row(job_id, db_path)
        if not job or not job.get("restaurant_id"):
            return 0
        rid = int(job["restaurant_id"])
        tokens = push.live_activity_tokens("schedule_build", push.LA_KIND_UPDATE, restaurant_id=rid,
                                           activity_key=str(job_id), db_path=db_path or DB_PATH)
        if not tokens:
            return 0
        if not _claim_end(rid, "schedule_build", job_id, db_path):
            return 0
        ok = status == "done" and bool((result or {}).get("ok", True))
        note = None if ok else ((result or {}).get("error") or "The schedule didn't finish.")
        progress = None
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT progress_json FROM async_jobs WHERE job_id=?", (str(job_id),)).fetchone()
            progress = json.loads(r["progress_json"]) if r and r["progress_json"] else None
        except Exception:
            progress = None
        finally:
            conn.close()
        if ok and progress and progress.get("days_total"):
            progress = dict(progress, days_drafted=progress["days_total"])
        now = datetime.now(timezone.utc)
        n = push.fire_live_activity(
            tokens, "schedule_build", "end",
            schedule_build_state(progress, status="done" if ok else "failed", note=note),
            dismiss_at=_unix(now + timedelta(minutes=30)), db_path=db_path or DB_PATH)
        push.remove_live_activity_tokens_for(tokens, db_path=db_path or DB_PATH)
        return n
    except Exception as e:
        _capture(e, "live_activity_schedule_build", f"job={job_id} end")
        return 0


# ── #94: tonight's service ───────────────────────────────────────────────────

def _clock(dt) -> str:
    """9:00pm -> "9pm", 21:30 -> "9:30pm"."""
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    return f"{h % 12 or 12}{':%02d' % m if m else ''}{suffix}"


def pulse_line(pulse) -> dict:
    """{"line", "up"} from intraday.pulse — "▲ 8% vs a typical Fri" — or
    {"note"} saying why there is none. A pulse with no measured typical is
    never shown as 0%."""
    p = pulse or {}
    wd = (p.get("weekday") or "")[:3]
    if p.get("available") and p.get("pct") is not None and wd:
        pct = float(p["pct"])
        if abs(pct) < 1:
            return {"line": f"Even with a typical {wd}", "up": None}
        arrow = "▲" if pct > 0 else "▼"
        return {"line": f"{arrow} {abs(pct):.0f}% vs a typical {wd}", "up": pct > 0}
    reason = (p.get("reason") or "No pulse yet").strip()
    return {"note": reason[:1].upper() + reason[1:]}


def _coverage(restaurant_id, business_date, db_path=None):
    run = _run(restaurant_id, "service", business_date, db_path)
    try:
        return json.loads(run["coverage_json"]) if run and run.get("coverage_json") else None
    except (TypeError, ValueError):
        return None


def service_content(restaurant, local=None, db_path=None) -> dict:
    """Tonight's service as the Live Activity draws it, for the read route
    and every push alike — one builder, so the two cannot drift:
    {"in_service", "business_date", "opens_at", "closes_at", "attributes":
    ServiceAttributes, "content_state": ServiceAttributes.ContentState}.
    Reads the database only: the pulse from the hourly capture, who hasn't
    clocked in from the coverage check's last read (note_coverage)."""
    import intraday
    from time_utils import restaurant_now, open_service_day, service_window, business_date, restaurant_tz, mdy
    rid = int(restaurant.id)
    local = local or restaurant_now(restaurant, naive=True)
    day = open_service_day(restaurant, local)
    bday = day or business_date(restaurant, local)
    window = service_window(restaurant, bday)
    tz = restaurant_tz(restaurant)
    closes_utc = window[1].replace(tzinfo=tz).astimezone(timezone.utc) if window else None
    try:
        pulse = intraday.pulse(rid, now_local=local, db_path=db_path or DB_PATH, restaurant=restaurant) \
            if day is not None else None
    except Exception:
        pulse = None
    pl = pulse_line(pulse) if day is not None else {"note": "Closed now"}
    cov = _coverage(rid, bday.isoformat(), db_path)
    state = {"status": "open" if day is not None else "closed",
             "updatedAt": push.apple_date(datetime.now(timezone.utc))}
    if pl.get("line"):
        state["pulseLine"] = pl["line"]
        if pl.get("up") is not None:
            state["pulseUp"] = bool(pl["up"])
    else:
        state["pulseNote"] = pl.get("note")
    if cov and cov.get("available"):
        names = [m.get("employee") for m in (cov.get("missing") or []) if m.get("employee")]
        state["missing"] = names[:4]
        state["missingCount"] = len(names)
    else:
        state["missing"] = []
        state["coverageNote"] = ((cov or {}).get("reason") or "Not checked yet")[:120]
    if closes_utc is not None:
        state["closesAt"] = push.apple_date(closes_utc)
    attributes = {"restaurantId": rid, "restaurantName": getattr(restaurant, "name", "") or "",
                  "businessDate": bday.isoformat(),
                  "dayLabel": f"{bday.strftime('%a')} {mdy(bday.isoformat())}"}
    return {"in_service": day is not None, "business_date": bday.isoformat(),
            "opens_at": _clock(window[0]) if window else None,
            "closes_at": _clock(window[1]) if window else None,
            "closes_at_unix": _unix(closes_utc) if closes_utc else None,
            "attributes": attributes, "content_state": state}


def note_coverage(restaurant, local, gaps, db_path=None) -> int:
    """run_coverage_check's hook: keep the night's latest read of who hasn't
    clocked in (names and roles only, no phone numbers), then let a running
    service activity hear of any change."""
    try:
        from time_utils import business_date
        g = gaps or {}
        bday = str(g.get("business_date") or business_date(restaurant, local).isoformat())[:10]
        stored = {"available": bool(g.get("available")),
                  "reason": None if g.get("available") else (g.get("reason") or "")[:120],
                  "missing": [{"employee": m.get("employee"), "role": m.get("role")}
                              for m in (g.get("missing") or [])][:12]}
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT OR IGNORE INTO live_activity_runs (restaurant_id, activity_type, activity_key) "
                         "VALUES (?,?,?)", (int(restaurant.id), "service", bday))
            conn.execute("UPDATE live_activity_runs SET coverage_json=?, updated_at=datetime('now') "
                         "WHERE restaurant_id=? AND activity_type='service' AND activity_key=?",
                         (json.dumps(stored), int(restaurant.id), bday))
            conn.commit()
        finally:
            conn.close()
        return service_tick(restaurant, local, db_path=db_path)
    except Exception as e:
        _capture(e, "live_activity_service", f"restaurant_id={getattr(restaurant, 'id', None)} coverage")
        return 0


def _open_runs(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [r["activity_key"] for r in conn.execute(
            "SELECT activity_key FROM live_activity_runs WHERE restaurant_id=? AND activity_type='service' "
            "AND started_at IS NOT NULL AND ended_at IS NULL", (int(restaurant_id),)).fetchall()]
    finally:
        conn.close()


def _split_service_watchers(tokens, restaurant_id, db_path=None):
    """(may, may_not): the update tokens whose login may still read Labor
    here, and those that lost it since the activity started. What hasn't
    clocked in is names — only Labor's readers hear them (re-audit 10/8/26
    #10)."""
    may = _allowed_tokens(tokens, restaurant_id, may_watch_service, db_path)
    kept = {int(t["id"]) for t in may}
    return may, [t for t in tokens if int(t["id"]) not in kept]


def _closed_state():
    return {"status": "closed", "missing": [], "updatedAt": push.apple_date(datetime.now(timezone.utc))}


def service_tick(restaurant, local=None, db_path=None) -> int:
    """The service slot's hook (strategy_jobs.run_intraday_capture, every
    pass, open or closed): start tonight's activity on opted-in phones at
    open, update it when what it says changes, end it at close. Two indexed
    reads for a restaurant nobody watches. Returns pushes queued.

    A night counts as running when the server started it OR a phone filed
    a running activity's own token for it — an activity the app started
    itself (iOS 17.0/17.1 has no push-to-start) never had a run claimed, so
    it heard no update and no end (re-audit 10/8/26 #9)."""
    try:
        if not push.native_push_allowed():
            return 0
        rid = int(restaurant.id)
        db = db_path or DB_PATH
        starts = push.live_activity_tokens("service", push.LA_KIND_START, restaurant_id=rid, db_path=db)
        followed = push.live_activity_tokens("service", push.LA_KIND_UPDATE, restaurant_id=rid, db_path=db)
        running = list(dict.fromkeys(_open_runs(rid, db_path) + [t["activity_key"] for t in followed]))
        if not starts and not running:
            return 0
        content = service_content(restaurant, local, db_path)
        bday = content["business_date"]
        sent = 0
        # A service that is over — tonight closed, or an earlier night never
        # ended — ends on every phone still showing it.
        for key in running:
            if content["in_service"] and key == bday:
                continue
            tokens = [t for t in followed if t["activity_key"] == key]
            if not _claim_end(rid, "service", key, db_path):
                # Already ended: a token still filed for it is a leftover.
                push.remove_live_activity_tokens_for(tokens, db_path=db)
                continue
            if key == bday:
                may, may_not = _split_service_watchers(tokens, rid, db_path)
                sent += push.fire_live_activity(may, "service", "end", dict(content["content_state"], status="closed"),
                                                dismiss_at=_unix(datetime.now(timezone.utc) + timedelta(minutes=15)),
                                                db_path=db)
                tokens_plain = may_not
            else:
                tokens_plain = tokens
            sent += push.fire_live_activity(tokens_plain, "service", "end", _closed_state(),
                                            dismiss_at=_unix(datetime.now(timezone.utc) + timedelta(minutes=15)),
                                            db_path=db)
            push.remove_live_activity_tokens_for(tokens, db_path=db)
        if not content["in_service"]:
            return sent
        state = content["content_state"]
        stale = content["closes_at_unix"] + 15 * 60 if content["closes_at_unix"] else None
        updates, lost = _split_service_watchers([t for t in followed if t["activity_key"] == bday], rid, db_path)
        if lost:
            # A login that can no longer read Labor here: its activity ends,
            # with no names on it.
            sent += push.fire_live_activity(lost, "service", "end", _closed_state(),
                                            dismiss_at=_unix(datetime.now(timezone.utc)), db_path=db)
            push.remove_live_activity_tokens_for(lost, db_path=db)
        changed = _state_changed(rid, "service", bday, state, db_path)
        if starts and _claim_start(rid, "service", bday, db_path):
            # Phones already running tonight's activity (started from the
            # app) are not started a second time.
            have = {t["session_hash"] for t in followed if t["activity_key"] == bday}
            fresh = _allowed_tokens([t for t in starts if t["session_hash"] not in have], rid,
                                    may_watch_service, db_path)
            name = content["attributes"]["restaurantName"] or "Tonight"
            sent += push.fire_live_activity(
                fresh, "service", "start", state, attributes=content["attributes"],
                alert={"title": f"{name}: service is open",
                       "body": state.get("pulseLine") or "Tonight's service, live on your Lock Screen."},
                stale_at=stale, db_path=db)
        # Not `elif`: a phone that started tonight itself still hears the
        # change that came with the server's start.
        if changed and updates:
            sent += push.fire_live_activity(updates, "service", "update", state, stale_at=stale, db_path=db)
        return sent
    except Exception as e:
        _capture(e, "live_activity_service", f"restaurant_id={getattr(restaurant, 'id', None)}")
        return 0


def _do_intraday_tonight(u):
    """GET /api/intraday/tonight and its /mobile/api twin: tonight's service
    as the phone's "Tonight's service" Live Activity draws it — the pulse
    against a typical same weekday, who hasn't clocked in, when it closes.
    Labor's readers only."""
    if not may_watch_service(u):
        return {"ok": False, "error": "Your login can't see tonight's service."}, 403
    from models import get_restaurant
    restaurant = get_restaurant(u["restaurant_id"])
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404
    return {"ok": True, **service_content(restaurant)}, 200


# ── #31: the waiting count, for the widget ──────────────────────────────────

def note_waiting_count(restaurant_id, user_id, count, from_web=True, db_path=None) -> int:
    """The action queue's hook: remember the count this login read, and when
    a read on the web finds it changed, wake this login's phones to redraw
    the widget (a silent push). A read from the phone itself is recorded
    only: the phone already has it. Returns devices woken."""
    try:
        if not user_id:
            return 0
        conn = get_conn(db_path)
        try:
            prev = conn.execute("SELECT count FROM widget_waiting_counts WHERE restaurant_id=? AND user_id=?",
                                (int(restaurant_id), int(user_id))).fetchone()
            conn.execute("INSERT INTO widget_waiting_counts (restaurant_id, user_id, count) VALUES (?,?,?) "
                         "ON CONFLICT(restaurant_id, user_id) DO UPDATE SET count=excluded.count, "
                         "updated_at=datetime('now')", (int(restaurant_id), int(user_id), int(count)))
            conn.commit()
        finally:
            conn.close()
        if from_web and prev is not None and int(prev["count"]) != int(count):
            return push.fire_silent(restaurant_id, "waiting", user_ids=[int(user_id)], db_path=db_path or DB_PATH)
        return 0
    except Exception as e:
        _capture(e, "widget_waiting_count", f"restaurant_id={restaurant_id}")
        return 0


# ── the token routes (mobile only: a token is a phone's) ─────────────────────

def _reaches(user, restaurant_id, db_path=None) -> bool:
    """Whether this login may act at `restaurant_id` at all: the location
    its session is on, one it holds an active membership at, or one of an
    owner's group (the switcher's own rule, client_api._do_switch_location)."""
    rid = int(restaurant_id)
    if rid == int(user.get("restaurant_id") or 0):
        return True
    from auth import get_membership
    if get_membership(int(user["id"]), rid, db_path or DB_PATH):
        return True
    try:
        from permissions import has_permission, LOCATION_SWITCH
        if not has_permission(user, LOCATION_SWITCH):
            return False
        from models import get_restaurant, get_location_group
        base = get_restaurant(user.get("base_restaurant_id") or user["restaurant_id"])
        if not base or not base.location_group:
            return False
        return any(int(r["id"]) == rid for r in get_location_group(base.location_group, owner_email=base.owner_email))
    except Exception:
        return False


def _activity_scope(user, activity_type, key, body, db_path=None):
    """What one running activity is about, read from the server's own rows
    — never taken from the phone: (restaurant_id, the delayed kind or None),
    or None when there is no such thing. A countdown's restaurant is its
    delayed action's, a generation's its job's; tonight's service names its
    restaurant in its attributes (ServiceAttributes.restaurantId), which the
    phone sends back as `restaurant_id` — the session's location otherwise."""
    if activity_type == "service":
        try:
            rid = int(body.get("restaurant_id") or user["restaurant_id"])
        except (TypeError, ValueError):
            return None
        return (rid, None) if rid > 0 else None
    conn = get_conn(db_path)
    try:
        if activity_type == "pending_send":
            try:
                aid = int(str(key))
            except ValueError:
                return None
            r = conn.execute("SELECT restaurant_id, kind FROM delayed_actions WHERE id=?", (aid,)).fetchone()
            return (int(r["restaurant_id"]), r["kind"]) if r else None
        if activity_type == "schedule_build":
            r = conn.execute("SELECT restaurant_id FROM async_jobs WHERE job_id=?", (str(key),)).fetchone()
            return (int(r["restaurant_id"]), None) if r and r["restaurant_id"] else None
        return None
    finally:
        conn.close()


def _may_follow(viewer, activity_type) -> bool:
    """Whether this login may hold a running activity's update token: what
    the activity says is that module's. A countdown's status and send time
    are on every login's pending list (strategy_routes._do_delayed_pending);
    a generation is the schedule's drafters'; tonight's service, Labor's
    readers."""
    if activity_type == "pending_send":
        return True
    return may_watch(viewer, activity_type)


def register_token(user, session_hash, body) -> tuple:
    """POST /mobile/api/live-activity-tokens {activity_type, kind, token,
    environment, activity_key, restaurant_id?}. A push-to-start token is
    taken only from a login that may watch that activity, at the session's
    location. An update token is filed at the restaurant the activity is
    about (its own row's), and only for a login that reaches that
    restaurant and may see what the activity shows there (re-audit 10/8/26
    #10): it used to be filed at whatever location the session was on,
    with no check at all."""
    b = body or {}
    activity_type = b.get("activity_type")
    kind = b.get("kind")
    rid = user["restaurant_id"]
    if kind == push.LA_KIND_START and activity_type in push.LIVE_ACTIVITY_TYPES and not may_watch(user, activity_type):
        return {"ok": False, "error": "Your login doesn't get this on the Lock Screen."}, 403
    if kind == push.LA_KIND_UPDATE and activity_type in push.LIVE_ACTIVITY_TYPES and b.get("activity_key"):
        scope = _activity_scope(user, activity_type, b.get("activity_key"), b)
        if scope is None:
            return {"ok": False, "error": "That isn't running here any more."}, 404
        rid = scope[0]
        if int(rid) == int(user["restaurant_id"]):
            viewer = user
        elif _reaches(user, rid):
            viewer = _viewer(user["id"], rid)
        else:
            return {"ok": False, "error": "Your login doesn't get this on the Lock Screen."}, 403
        if not _may_follow(viewer, activity_type):
            return {"ok": False, "error": "Your login doesn't get this on the Lock Screen."}, 403
    try:
        push.register_live_activity_token(user["id"], rid, session_hash, activity_type, kind,
                                          b.get("token"), environment=b.get("environment") or "production",
                                          activity_key=b.get("activity_key") or "")
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True}, 200


def remove_tokens(session_hash, body) -> tuple:
    """DELETE /mobile/api/live-activity-tokens {activity_type?, kind?}: this
    phone's tokens for one type (turning "Tonight's service" off) or all."""
    b = body or {}
    activity_type = b.get("activity_type")
    if activity_type is not None and activity_type not in push.LIVE_ACTIVITY_TYPES:
        return {"ok": False, "error": "Unknown activity_type."}, 400
    kind = b.get("kind")
    if kind is not None and kind not in (push.LA_KIND_START, push.LA_KIND_UPDATE):
        return {"ok": False, "error": "kind must be 'start' or 'update'."}, 400
    n = push.remove_live_activity_tokens(session_hash, activity_type=activity_type, kind=kind)
    return {"ok": True, "removed": n}, 200
