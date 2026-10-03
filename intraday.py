"""
intraday.py — the only part of the product that can see a day happening.

Everything else here reads yesterday: the POS syncs at 3am, labor settles
after the pay period, reviews arrive days late. So between opening and close
an owner got nothing from Cavnar that they couldn't get by looking around
the room — which is exactly the stretch of the day they are working.

Toast can be asked during service (businessDay net sales, and the labor
timeEntries feed), and so can RPOWER: its above-store database is fed while
the store trades (measured on Simple EJ's 9/29/26: a sale posted within
minutes; rpower.fetch_sales_today / fetch_clock_ins_today). A POS that
cannot be read is still reported honestly as not seeing today.

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
        if getattr(e, "not_yet", False):
            # The POS answered with nothing posted yet for the day — not a
            # POS that didn't answer (strategy_jobs.run_intraday_capture
            # decides when "not yet" has gone on too long).
            return {"ok": False, "not_yet": True, "reason": "no sales posted for today yet",
                    "provider": getattr(e, "provider", None)}
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
    """[{employee, role, shift_start, shift_end}] from the PUBLISHED week
    that covers today. The end rides along so the coverage check can name
    an on-shift person whose own shift ends as a gap begins (E-31)."""
    return [{"employee": r["employee"], "role": r["role"], "shift_start": r["shift_start"],
             "shift_end": r.get("shift_end") or ""}
            for r in published_rows(restaurant_id, day, db_path)]


# How a published row reads to the coverage check once a manager let its
# holder off it (an approved drop nobody claimed, or the holder's shift put
# on the open board before it began — shift_requests.shift_release): the
# shift is open, not theirs (schedule audit 10/3/26 E-5).
OPEN_DROPPED = "open (dropped)"


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
    # The person each POS id is (people.person_aliases — memory audit
    # 9/29/26, identity): a clock-in is its person whatever either side is
    # spelled, so a POS rename mid-week is not a no-show.
    by_ext = {}
    try:
        import people as _people
        conn = get_conn(db_path)
        try:
            for r in conn.execute("SELECT a.external_id, p.display_name FROM person_aliases a JOIN people p "
                                  "ON p.id = a.person_id WHERE a.restaurant_id=? AND a.external_id IS NOT NULL "
                                  "AND p.merged_into IS NULL", (restaurant_id,)).fetchall():
                by_ext[str(r["external_id"]).strip()] = r["display_name"]
        finally:
            conn.close()
    except Exception:
        by_ext = {}
    keys = set()
    names = []
    for c in clocked or []:
        n = str(c.get("employee") or "").strip()
        if n:
            keys.add(_ss.name_key(n))
            names.append(n)
        pid = c.get("pos_id") or c.get("employee_id") or c.get("external_id")
        if pid is not None and str(pid).strip() in by_pos:
            keys.add(_ss.name_key(by_pos[str(pid).strip()]))
        if pid is not None and str(pid).strip() in by_ext:
            keys.add(_ss.name_key(by_ext[str(pid).strip()]))
    # Each spelling also as the person it means (an alias → their name now).
    try:
        import people as _people
        keys |= {_ss.name_key(v) for v in _people.canonical_names(restaurant_id, names, db_path=db_path).values()}
    except Exception:
        pass
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


def _released(restaurant_id, day, restaurant, db_path=DB_PATH) -> list:
    """shift_requests.shift_release for the business date, or [] when the
    requests can't be read (then every published row is expected, as
    before — said in the log, never a crash of the check)."""
    try:
        import shift_requests
        return shift_requests.shift_release(restaurant_id, day, restaurant=restaurant, db_path=db_path)
    except Exception as e:
        log.warning("coverage check: shift requests unreadable rid=%s: %s", restaurant_id, e)
        return []


def coverage_gaps(restaurant_id, now_local=None, db_path=DB_PATH, restaurant=None,
                  grace_minutes=COVERAGE_GRACE_MINUTES):
    """Who was on the PUBLISHED schedule for a shift that has already started
    and has not clocked in. {"available": False, "reason"} where the POS has
    no live clock-in feed, or where no published week covers today.
    `arrived_keys` (everyone clocked in, aliases resolved) lets the caller
    close an earlier no-show issue for someone who arrived late.

    Every answer carries `business_date` — the service the clock is in — and
    the caller keys its issues by it, never by the calendar date (schedule
    audit 10/3/26 E-4: after midnight on a 2am close the calendar date made a
    second issue for the same person and never closed the first).

    A published row whose holder a manager let off it (an approved drop
    nobody claimed — shift_requests.shift_release) reads as "open
    (dropped)": it is listed in `released`, never in `missing` (E-5); a
    covered one likewise. Somebody who asked to drop the shift and was never
    approved is still expected, and when missing carries `asked_off` and the
    notice they gave."""
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
        return {"available": False, "reason": "no published schedule covers today",
                "business_date": day.isoformat()}
    try:
        clocked, provider = pos.fetch_clock_ins_today(restaurant_id, day)
    except pos.POSCapabilityError as e:
        return {"available": False, "reason": str(e), "business_date": day.isoformat()}
    except Exception as e:
        log.warning("coverage check failed rid=%s: %s", restaurant_id, e)
        return {"available": False, "reason": "the POS didn't answer", "business_date": day.isoformat()}
    here = arrival_keys(restaurant_id, clocked, db_path)
    # The published week may carry a spelling from before a rename: each
    # scheduled name is read as the person it means now (people).
    try:
        import people as _people
        _canon = _people.canonical_names(restaurant_id, [s["employee"] for s in scheduled], db_path=db_path)
    except Exception:
        _canon = {}
    # `listed_as` keeps the week's own spelling: the row is found by it when
    # a cover is judged against the published week (labor_replacements).
    scheduled = [dict(s, employee=_canon.get(s["employee"], s["employee"]), listed_as=s["employee"])
                 for s in scheduled]
    # Salaried people don't clock in (owner, 9/30/26: "managers don't clock
    # in"): a missing punch is never theirs.
    import attendance as _att
    _sal = _att.salaried_keys(restaurant)
    if _sal:
        from models import salaried_name_key
        scheduled = [s for s in scheduled if salaried_name_key(s["employee"]) not in _sal]
    # What the shift requests say about each row (E-5): a holder a manager
    # let off the shift, or whose shift somebody took, is not expected.
    import shift_requests as _sreq
    releases = _released(restaurant_id, day, restaurant, db_path)
    released, expected = [], []
    for s in scheduled:
        hit = _sreq.released_for(releases, s["employee"], s["shift_start"])
        if hit and hit["outcome"] in ("excused", "covered"):
            released.append(dict(s, status=OPEN_DROPPED if hit["outcome"] == "excused" else "covered",
                                 request_id=hit.get("request_id"), covered_by=hit.get("replacement")))
            continue
        expected.append(dict(s, asked_off=True, notice_minutes=hit.get("notice_minutes"))
                        if hit and hit["outcome"] == "called_out" else s)
    scheduled = expected
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
        return {"available": False, "reason": "clock-in feed incomplete — the POS reported nobody clocked in",
                "business_date": day.isoformat()}
    arrived ={k for k in scheduled_keys if is_here(k, here, scheduled_keys)} | here
    return {"available": True, "provider": provider, "missing": missing, "business_date": day.isoformat(),
            "scheduled": len(scheduled), "scheduled_rows": scheduled, "clocked_in": len(clocked or []),
            "arrived_keys": sorted(arrived), "released": released}


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
    """The phone, when it may be texted here: a consented alert contact (the
    only numbers this product may text — staff_contacts phones carry no SMS
    consent of their own) that has not replied STOP (notify.textable, #107:
    this checked consent alone, so a STOP was ignored)."""
    if not phone:
        return None
    import notify
    return phone if notify.textable(phone, restaurant_id, "alert", db_path) else None


def ask_to_cover(restaurant_id, issue_id, name, user_id=None, surface="labor", db_path=DB_PATH, actor=None):
    """One tap on "Ask Ana to cover": offer Ana the shift in the Cavnar AI
    app (shift_requests.post_open_shift with offer_to — checked by the
    claim's own rules first), told on her own channel (people.tell). Her
    Accept is the claim: the week is written, and her answer lands on this
    issue (mark_cover_answer), a yes resolving it. The ask used to be a text
    saying "Reply to your manager to confirm", on the owner-alert campaign,
    whose replies nobody read (employee audit H2 / COM-06).

    Someone with no app login is still asked the old way — a text to a
    consented number, or email — told to CALL their manager, since a reply
    goes nowhere. Only a person the coverage issue itself suggested can be
    asked, and only while the issue is open.

    Returns {"ok", "via", "name", "offer_id"} or {"ok": False, "error"}.
    `via` is "push" | "sms" | "email" | "app" (waiting in their app, no
    notice delivered) for an offer; "sms" | "email" for the fallback."""
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
    group = issues.is_group_coverage(issue.get("source_key"))
    if group:
        # A role's issue holds several gaps: the suggestion names the one
        # it is for (strategy_jobs.run_coverage_check, E-31), and only a gap
        # still missing can be offered.
        gap = next((p for p in issues.coverage_people(issue)
                    if _ss.name_key(p.get("employee")) == _ss.name_key(suggested[0].get("for"))
                    and (not suggested[0].get("shift_start") or p.get("shift_start") == suggested[0]["shift_start"])),
                   None)
        if not gap or gap.get("status") != "missing":
            return {"ok": False, "error": "That shift is already covered or they've clocked in."}
        missing, role, start = gap["employee"], gap.get("role") or meta.get("role") or "", gap.get("shift_start") or ""
    else:
        missing, role, start = meta.get("missing") or "A teammate", meta.get("role") or "", meta.get("shift_start") or ""
    tail = str(issue.get("source_key") or "").split(":")
    day = (meta.get("business_date") or tail[1]) if len(tail) >= 3 else None
    offer_id = None
    via = None
    if day and (missing if group else meta.get("missing")) and start:
        import shift_requests as _sreq
        try:
            out = _sreq.post_open_shift(restaurant_id, day, start, employee=missing if group else meta["missing"],
                                        actor=actor or "coverage issue", offer_to=who, issue_id=issue_id,
                                        db_path=db_path)
            offer_id, via = out["offer"]["id"], out["offer"].get("via") or "app"
        except _sreq.NotOnApp:
            via = None                    # asked the old way below
        except _sreq.ShiftRequestError as e:
            return {"ok": False, "error": f"Couldn't offer {who} the shift: {e}."}
    text = (f"Cavnar AI · {place}: {missing} can't make today's {role + ' ' if role else ''}shift"
            f"{' (' + start + ')' if start else ''}. Can you cover? Call your manager to say yes or no — "
            "a reply to this message isn't read.")
    contact = {} if via else _staff_contact(restaurant_id, who, db_path)
    phone = _consented_phone(restaurant_id, contact.get("phone"), db_path)
    if phone:
        import notify
        with notify.sms_context(restaurant_id):
            if notify.send_sms(phone, text[:320], use_case="alert"):
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
        return {"ok": False, "error": f"{who} isn't on the Cavnar AI app and has no consented phone or email on file "
                                      "— call them directly."}
    ask = {"name": who, "via": via, "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
    if offer_id:
        ask["offer_id"] = offer_id        # answered in the app (shift_requests.respond_offer)
    if group:
        ask["for"], ask["shift_start"] = missing, start
        # In one write with whatever else is editing the issue (the check
        # marking an arrival, another ask) — a read-then-write lost one.
        issues.update_coverage(restaurant_id, issue_id, lambda m: m.setdefault("asked", []).append(ask) or True,
                               db_path=db_path)
    else:
        meta.setdefault("asked", []).append(ask)
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET meta_json=? WHERE id=? AND restaurant_id=?",
                         (issues.meta_text(meta), issue_id, restaurant_id))
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
    return {"ok": True, "via": via, "name": who, "offer_id": offer_id}


def mark_cover_answer(restaurant_id, issue_id, name, accepted, db_path=None) -> bool:
    """The manager's answer to "Did Zed take it?", kept on the coverage
    issue's own `asked` entry (`answer`: "took" | "declined") so both Home
    clients stop asking once it is answered (memory audit 9/29/26, covers;
    the person's record is people.answer_cover's). False when the issue or
    the ask is not there. Never raises."""
    import json as _json
    import issues
    import models as _models
    import staff_settings as _ss
    db = db_path or _models.DB_PATH     # resolved at call time (CLAUDE.md, bound imports)
    try:
        issue = issues.get_issue(restaurant_id, issue_id, db_path=db)
        if not issue or issue.get("kind") != "coverage":
            return False
        meta = _json.loads(issue.get("meta_json") or "null") or {}
        hit = False
        for a in meta.get("asked") or []:
            if isinstance(a, dict) and _ss.name_key(a.get("name")) == _ss.name_key(name):
                a["answer"] = "took" if accepted else "declined"
                a["answered_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                hit = True
        if not hit:
            return False
        conn = _models.get_conn(db)
        try:
            conn.execute("UPDATE ops_issues SET meta_json=? WHERE id=? AND restaurant_id=?",
                         (issues.meta_text(meta), issue_id, restaurant_id))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("cover answer not kept on the issue rid=%s issue=%s: %s", restaurant_id, issue_id, e)
        return False


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
