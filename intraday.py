"""
intraday.py — the only part of the product that can see a day happening.

Everything else here reads yesterday: the POS syncs at 3am, labor settles
after the pay period, reviews arrive days late. So between opening and close
an owner got nothing from Cavnar that they couldn't get by looking around
the room — which is exactly the stretch of the day they are working.

Toast can be asked during service (businessDay net sales, and the labor
timeEntries feed). RPOWER cannot: the vendor confirmed month-at-a-time
extracts, so for those restaurants this module honestly reports that it
can't see today rather than guessing.

TWO THINGS, BOTH ACTIONABLE BEFORE THE NEXT SERVICE:

  pulse()          — how today is tracking against the same weekday at the
                     same hour, once there is enough history to say.
  coverage_gaps()  — who was scheduled and hasn't clocked in.

No hour-level history existed to compare against, so capture() builds one:
each hourly snapshot is this weekday's profile for later weeks. A comparison
is withheld until MIN_PROFILE_SAMPLES same-weekday, same-hour readings exist
— a "you're 30% down" built on one previous Tuesday is noise with a
percentage sign on it.
"""
import logging
from datetime import datetime, timedelta

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

# Same-weekday, same-hour readings needed before a comparison is offered.
MIN_PROFILE_SAMPLES = 3
# ...and before the pulse goes further than the comparison and suggests
# sending somebody home (strategy_jobs.staffing_move). A median of three
# past Fridays is enough to say "you're running behind"; it is not enough
# to cut a shift on (CA1 L28, fix I13).
MIN_SAMPLES_FOR_CUT = 6
# How far off a typical day has to be before it is worth interrupting for.
PULSE_BEHIND_PCT = 15
PULSE_AHEAD_PCT = 20
# How late a scheduled person has to be before it becomes the manager's problem.
COVERAGE_GRACE_MINUTES = 15
# An empty clock-in feed while someone scheduled is this far past due reads
# as a feed that is not reporting — `available: False` — not as a room of
# no-shows (DH2-8).
EMPTY_FEED_UNAVAILABLE_MINUTES = 30


def _median(values):
    s = sorted(values)
    if not s:
        return None
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def capture(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None, business_day=None):
    """Store net sales so far today, under this local hour.

    The reading is filed under the service's BUSINESS date: at 12:30am
    during a Friday that closes at 1:00am it is Friday's figure, stored at
    captured_hour 24 so it still sorts after Friday's 11pm reading — the
    last reading is the night's total (day_total), and the calendar date
    would have started Saturday with Friday's sales (A-1 / A-20).

    Returns {"ok": False, "reason"} for a POS that cannot be read during
    service — a normal state, not an error.
    """
    import pos
    from models import get_restaurant
    from time_utils import restaurant_now, business_date
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    day = business_day or business_date(restaurant, local)
    hour = local.hour + 24 * max(0, (local.date() - day).days)
    try:
        net, provider = pos.fetch_sales_today(restaurant_id, day)
    except pos.POSCapabilityError as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:
        log.warning("intraday capture failed rid=%s: %s", restaurant_id, e)
        return {"ok": False, "reason": "the POS didn't answer"}
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, "
            "net_sales, provider) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, business_date, captured_hour) DO UPDATE SET "
            "net_sales=excluded.net_sales, created_at=datetime('now')",
            (restaurant_id, day.isoformat(), hour, day.strftime("%A"),
             float(net), provider))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "net_sales": float(net), "hour": hour, "provider": provider}


def pulse(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None):
    """How today is tracking at this hour against the same weekday's profile.

    {"available": False, "reason"} until the profile exists — this is the
    honest answer for the first few weeks, and for every POS that can't be
    read during service.
    """
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    day, hour, weekday = local.date(), local.hour, local.strftime("%A")
    conn = get_conn(db_path)
    try:
        today_row = conn.execute(
            "SELECT net_sales, captured_hour FROM pos_intraday WHERE restaurant_id=? AND "
            "business_date=? AND captured_hour<=? ORDER BY captured_hour DESC LIMIT 1",
            (restaurant_id, day.isoformat(), hour)).fetchone()
        if not today_row:
            return {"available": False, "reason": "nothing captured from the POS today"}
        history = [r["net_sales"] for r in conn.execute(
            "SELECT net_sales FROM pos_intraday WHERE restaurant_id=? AND weekday=? "
            "AND captured_hour=? AND business_date<?",
            (restaurant_id, weekday, today_row["captured_hour"], day.isoformat())).fetchall()]
    finally:
        conn.close()
    if len(history) < MIN_PROFILE_SAMPLES:
        return {"available": False, "hour": today_row["captured_hour"],
                "net_sales": today_row["net_sales"], "samples": len(history),
                "reason": f"only {len(history)} past {weekday}s measured at this hour"}
    typical = _median(history)
    pct = round((today_row["net_sales"] / typical - 1) * 100, 1) if typical else None
    return {"available": True, "weekday": weekday, "hour": today_row["captured_hour"],
            "net_sales": round(today_row["net_sales"], 2), "typical": round(typical, 2),
            "samples": len(history), "pct": pct,
            "enough_to_cut": len(history) >= MIN_SAMPLES_FOR_CUT,
            "off": pct is not None and (pct <= -PULSE_BEHIND_PCT or pct >= PULSE_AHEAD_PCT),
            "direction": "behind" if (pct or 0) < 0 else "ahead"}


def _published_csv(restaurant_id, day, db_path=DB_PATH):
    """The schedule staff were actually SENT for `day`: the newest published,
    non-superseded week containing it — or "" when none is.

    This read the newest schedule_history row whose CSV contained today's
    date, whatever it was: a draft nobody published, an auto-draft, or a
    week superseded by a re-publish. So a no-show issue could name someone
    who was never told they were on (#13). A draft is not a commitment."""
    iso = day.isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history h WHERE h.restaurant_id=? "
            "AND h.published_at IS NOT NULL AND h.superseded_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=h.restaurant_id "
            "AND nw.week_start=h.week_start AND nw.published_at IS NOT NULL AND nw.id > h.id) "
            "ORDER BY h.generated_at DESC, h.id DESC LIMIT 8", (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    for r in rows:
        ws, we = str(r["week_start"] or "")[:10], str(r["week_end"] or "")[:10]
        if ws and we and not (ws <= iso <= we):
            continue
        if iso in (r["schedule_csv"] or ""):
            return r["schedule_csv"] or ""
    return ""


def published_rows(restaurant_id, day, db_path=DB_PATH):
    """[{employee, role, shift_start, shift_end, scheduled_hours}] for `day`
    from the published week (see _published_csv)."""
    import csv, io
    out = []
    for row in csv.DictReader(io.StringIO(_published_csv(restaurant_id, day, db_path))):
        if (row.get("date") or "")[:10] != day.isoformat() or not (row.get("employee") or "").strip():
            continue
        out.append({"employee": row["employee"].strip(), "role": (row.get("role") or "").strip(),
                    "shift_start": (row.get("shift_start") or "").strip(),
                    "shift_end": (row.get("shift_end") or "").strip(),
                    "scheduled_hours": (row.get("scheduled_hours") or "").strip()})
    return out


def _todays_scheduled(restaurant_id, day, db_path=DB_PATH):
    """[{employee, role, shift_start}] from the PUBLISHED week that covers
    today."""
    return [{"employee": r["employee"], "role": r["role"], "shift_start": r["shift_start"]}
            for r in published_rows(restaurant_id, day, db_path)]


def _short(key):
    """"maria garcia" / "maria g." -> "maria g": first name and last initial."""
    parts = key.replace(".", " ").split()
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}"


def _abbreviated(key):
    parts = key.replace(".", " ").split()
    return len(parts) >= 2 and len(parts[-1]) == 1


def arrival_keys(restaurant_id, clocked, db_path=DB_PATH) -> set:
    """name_key()s of everyone clocked in — the POS's own name, and the
    staff member a POS id maps to (staff_contacts.pos_id, the alias the
    owner entered under Labor -> Staff contacts)."""
    import staff_settings as _ss
    by_pos = {}
    try:
        from models import get_staff_contacts
        by_pos = {str(c["pos_id"]).strip(): c["employee_name"]
                  for c in get_staff_contacts(restaurant_id, db_path=db_path) if c.get("pos_id")}
    except Exception:
        by_pos = {}
    keys = set()
    for c in clocked or []:
        n = str(c.get("employee") or "").strip()
        if n:
            keys.add(_ss.name_key(n))
        pid = c.get("pos_id") or c.get("employee_id") or c.get("external_id")
        if pid is not None and str(pid).strip() in by_pos:
            keys.add(_ss.name_key(by_pos[str(pid).strip()]))
    return keys


def is_here(name, keys, scheduled_keys=()):
    """Whether a scheduled `name` is among the clocked-in `keys`.

    Exact on staff_settings.name_key (case and spacing never matter — the
    old lowercase-exact match called "Maria  Garcia" a no-show). Then one
    alias: an abbreviated name ("Maria G.") matches a full one ("Maria
    Garcia") on first name and last initial, but only when that is
    unambiguous on BOTH sides — two Maria G.s is a guess, not a match."""
    import staff_settings as _ss
    k = _ss.name_key(name)
    if k in keys:
        return True
    short = _short(k)
    if not short or " " not in short:
        return False
    if sum(1 for s in scheduled_keys if _short(s) == short) > 1:
        return False
    hits = [x for x in keys if _short(x) == short]
    return len(hits) == 1 and (_abbreviated(k) or _abbreviated(hits[0]))


def coverage_gaps(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None,
                  grace_minutes=COVERAGE_GRACE_MINUTES):
    """Who was on the PUBLISHED schedule for a shift that has already started
    and has not clocked in. {"available": False, "reason"} where the POS has
    no live clock-in feed, or where no published week covers today.
    `arrived_keys` (everyone clocked in, aliases resolved) lets the caller
    close an earlier no-show issue for someone who arrived late."""
    import pos
    import staff_settings as _ss
    from models import get_restaurant
    from time_utils import restaurant_now, business_date, BUSINESS_DAY_START_HOUR
    restaurant = restaurant or get_restaurant(restaurant_id)
    local = now_local or restaurant_now(restaurant, naive=True)
    # The service the clock is in, not the calendar day: at 12:30am during a
    # 1am close, tonight's schedule is still the one being worked, and a shift
    # listed for it that starts after midnight is due the next calendar day.
    day = business_date(restaurant, local)
    scheduled = _todays_scheduled(restaurant_id, day, db_path=db_path)
    if not scheduled:
        return {"available": False, "reason": "no published schedule covers today"}
    try:
        clocked, provider = pos.fetch_clock_ins_today(restaurant_id, day)
    except pos.POSCapabilityError as e:
        return {"available": False, "reason": str(e)}
    except Exception as e:
        log.warning("coverage check failed rid=%s: %s", restaurant_id, e)
        return {"available": False, "reason": "the POS didn't answer"}
    here = arrival_keys(restaurant_id, clocked, db_path)
    scheduled_keys = [_ss.name_key(s["employee"]) for s in scheduled]
    missing = []
    for s in scheduled:
        start = None
        for fmt in ("%I:%M%p", "%I:%M %p", "%H:%M"):
            try:
                start = datetime.strptime(s["shift_start"].strip().lower().replace(" ", ""),
                                          fmt.replace(" ", ""))
                break
            except ValueError:
                continue
        if start is None:
            continue
        due = datetime.combine(day, start.time())
        if start.hour < BUSINESS_DAY_START_HOUR:
            due += timedelta(days=1)       # listed for tonight, starts after midnight
        if local < due + timedelta(minutes=grace_minutes):
            continue                      # not late yet
        if is_here(s["employee"], here, scheduled_keys):
            continue                      # clocked in
        missing.append({**s, "minutes_late": int((local - due).total_seconds() // 60)})
    # A feed that answered with NOBODY while people on the published
    # schedule are well past due is a feed that is not reporting, not a
    # restaurant where nobody came in: every one of them would be a no-show
    # issue on the manager's phone (DH2-8).
    if not clocked and any(m["minutes_late"] > EMPTY_FEED_UNAVAILABLE_MINUTES for m in missing):
        return {"available": False, "reason": "clock-in feed incomplete — the POS reported nobody clocked in"}
    arrived ={k for k in scheduled_keys if is_here(k, here, scheduled_keys)} | here
    return {"available": True, "provider": provider, "missing": missing,
            "scheduled": len(scheduled), "scheduled_rows": scheduled, "clocked_in": len(clocked or []),
            "arrived_keys": sorted(arrived)}


# ── asking someone to cover (#13) ─────────────────────────────────────────────

def _staff_contact(restaurant_id, name, db_path=DB_PATH):
    import staff_settings as _ss
    from models import get_staff_contacts
    k = _ss.name_key(name)
    for c in get_staff_contacts(restaurant_id, db_path=db_path) or []:
        if _ss.name_key(c.get("employee_name")) == k:
            return c
    return {}


def _consented_phone(restaurant_id, phone, db_path=DB_PATH):
    """The phone, when that number is a consented alert contact here — the
    only numbers this product may text (staff_contacts phones carry no SMS
    consent of their own)."""
    if not phone:
        return None
    import notify
    want = notify._normalize_phone(phone)
    conn = get_conn(db_path)
    try:
        have = {notify._normalize_phone(r["phone"]) for r in conn.execute(
            "SELECT phone FROM alert_contacts WHERE restaurant_id=? AND COALESCE(sms_consent,0)=1",
            (restaurant_id,)).fetchall() if r["phone"]}
    finally:
        conn.close()
    return phone if want in have else None


def ask_to_cover(restaurant_id, issue_id, name, user_id=None, surface="labor", db_path=DB_PATH):
    """One tap on "Ask Ana to cover": text Ana the cover request — or email
    her when her number carries no SMS consent — and record that the
    suggestion was taken. Only a person the coverage issue itself suggested
    can be asked, and only while the issue is open.

    Returns {"ok", "via", "name"} or {"ok": False, "error"}."""
    import json as _json
    import issues
    import staff_settings as _ss
    issue = issues.get_issue(restaurant_id, issue_id, db_path=db_path)
    if not issue or issue.get("kind") != "coverage":
        return {"ok": False, "error": "That coverage issue wasn't found."}
    if issue.get("status") == "resolved":
        return {"ok": False, "error": "That shift is already covered or closed."}
    try:
        meta = _json.loads(issue.get("meta_json") or "null") or {}
    except (TypeError, ValueError):
        meta = {}
    suggested = [c for c in (meta.get("covers") or []) if _ss.name_key(c.get("name")) == _ss.name_key(name)]
    if not suggested:
        return {"ok": False, "error": "Only someone suggested for this shift can be asked from here."}
    who = suggested[0]["name"]
    if any(_ss.name_key(a.get("name")) == _ss.name_key(who) for a in (meta.get("asked") or [])):
        return {"ok": False, "error": f"{who} has already been asked."}
    from models import get_restaurant
    r = get_restaurant(restaurant_id, db_path)
    place = (getattr(r, "location_name", None) or getattr(r, "name", None) or "the restaurant") if r else "the restaurant"
    missing, role, start = meta.get("missing") or "A teammate", meta.get("role") or "", meta.get("shift_start") or ""
    text = (f"Cavnar AI · {place}: {missing} can't make today's {role + ' ' if role else ''}shift"
            f"{' (' + start + ')' if start else ''}. Can you cover? Reply to your manager to confirm.")
    contact = _staff_contact(restaurant_id, who, db_path)
    via = None
    phone = _consented_phone(restaurant_id, contact.get("phone"), db_path)
    if phone:
        from notify import send_sms
        if send_sms(phone, text[:320], use_case="alert"):
            via = "sms"
    if via is None and contact.get("email"):
        try:
            import emails as _emails
            import html as _h
            html = _emails.report_shell(kicker=place, title="Can you cover a shift today?", subtitle="",
                                        sections=[_emails.report_paragraph(_h.escape(text))])
            res = _emails.deliver(email_type="shift_request", restaurant_id=restaurant_id, payload={
                "from": _emails.sender("client"), "to": [contact["email"]],
                "subject": f"Can you cover today? — {place}", "preheader": text[:120], "html": html})
            if getattr(res, "ok", False):
                via = "email"
        except Exception as e:
            log.warning("cover email failed rid=%s: %s", restaurant_id, e)
    if via is None:
        return {"ok": False, "error": f"{who} has no consented phone or email on file — call them directly."}
    meta.setdefault("asked", []).append({"name": who, "via": via,
                                         "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE ops_issues SET meta_json=? WHERE id=? AND restaurant_id=?",
                     (_json.dumps(meta)[:4000], issue_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    try:
        import rec_ledger
        rec_ledger.record(restaurant_id, cover_key(issue), "accepted", surface=surface, user_id=user_id,
                          meta={"asked": who, "via": via}, source_ref=f"cover:{issue_id}:{_ss.name_key(who)}",
                          db_path=db_path)
    except Exception as e:
        log.warning("cover rec_ledger failed rid=%s: %s", restaurant_id, e)
    return {"ok": True, "via": via, "name": who}


def cover_key(issue) -> str:
    """The recommendation a coverage issue's suggested covers make:
    "cover:<date>:<person missing>"."""
    tail = str((issue or {}).get("source_key") or "").split(":", 1)
    return "cover:" + (tail[1] if len(tail) == 2 else str((issue or {}).get("id")))


def day_total(restaurant_id, day, db_path=DB_PATH):
    """The last net-sales reading captured for a business date, and the hour
    it was taken. That last reading IS the day's total — capture() writes a
    running figure, so the final one is the day."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT net_sales, captured_hour FROM pos_intraday WHERE restaurant_id=? AND "
            "business_date=? ORDER BY captured_hour DESC LIMIT 1",
            (restaurant_id, day.isoformat())).fetchone()
    finally:
        conn.close()
    return (row["net_sales"], row["captured_hour"]) if row else (None, None)


def closing_summary(restaurant_id, day=None, db_path=DB_PATH, restaurant=None):
    """How tonight went, against a typical same weekday.

    The morning brief tells an owner how YESTERDAY went. Nothing told them how
    TODAY went, while they still remember the room — which is the one moment
    the number means something specific rather than being a figure in a table.

    Same honesty rules as pulse(): a comparison is withheld until there are
    MIN_PROFILE_SAMPLES same-weekday closes to compare against, and a POS that
    cannot be read during service says so instead of guessing.
    """
    from models import get_restaurant
    from time_utils import restaurant_now
    restaurant = restaurant or get_restaurant(restaurant_id)
    day = day or restaurant_now(restaurant, naive=True).date()
    weekday = day.strftime("%A")
    net, hour = day_total(restaurant_id, day, db_path)
    if net is None:
        return {"available": False, "reason": "nothing captured from the POS today"}
    conn = get_conn(db_path)
    try:
        # One figure per past same-weekday: that day's own last reading.
        # The bare net_sales alongside MAX() is SQLite's documented
        # min/max-picks-the-row behaviour, not an accident.
        history = [r["net_sales"] for r in conn.execute(
            "SELECT MAX(captured_hour) AS h, net_sales FROM pos_intraday "
            "WHERE restaurant_id=? AND weekday=? AND business_date<? "
            "GROUP BY business_date ORDER BY business_date DESC LIMIT 8",
            (restaurant_id, weekday, day.isoformat())).fetchall()]
    finally:
        conn.close()
    out = {"available": False, "weekday": weekday, "net_sales": round(float(net), 2),
           "hour": hour, "samples": len(history)}
    if len(history) < MIN_PROFILE_SAMPLES:
        out["reason"] = f"only {len(history)} past {weekday}s to compare with"
        return out
    typical = _median(history)
    pct = round((net / typical - 1) * 100, 1) if typical else None
    out.update({"available": True, "typical": round(typical, 2), "pct": pct,
                "direction": "behind" if (pct or 0) < 0 else "ahead"})
    return out
