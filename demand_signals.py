"""
demand_signals.py — what the owner knows about specific dates.

The schedule read holidays from a fixed list and demand from weekday medians;
nothing let an owner say "the 60-cover party is Saturday" or hand over the
reservation book. Two kinds of signal, both dated:

  event         — a name, an expected cover count and/or a lift in percent
  reservations  — covers booked for that date, entered by hand or pasted as
                  CSV (date,covers) from whatever reservation system they use

No live reservation integration exists yet; the row says `source` so a
future sync writes the same table and the schedule does not change.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

KINDS = ("event", "reservations")
MAX_ROWS = 200
# What an event with no covers and no lift is ASSUMED to add. Carried as
# `assumed_lift_pct` beside `assumed: True` for the prompt and the clients
# to say as an assumption; never written into lift_pct, so it never raises
# a demand level without a figure behind it.
ASSUMED_EVENT_LIFT_PCT = 25


def _d(s):
    return date.fromisoformat(str(s)[:10]).isoformat()


def save(restaurant_id, rows, source="manual", created_by=None, db_path=DB_PATH) -> dict:
    """rows: [{date, kind, label?, covers?, lift_pct?}]. Returns {written, skipped, errors}."""
    written = skipped = 0
    errors = []
    conn = get_conn(db_path)
    try:
        for r in list(rows or [])[:MAX_ROWS]:
            try:
                day = _d(r.get("date"))
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                errors.append(f"{(r or {}).get('date', '?')}: not a date")
                continue
            kind = (r.get("kind") or "reservations").strip().lower()
            if kind not in KINDS:
                skipped += 1
                errors.append(f"{day}: kind must be event or reservations")
                continue
            label = (r.get("label") or ("Reservations" if kind == "reservations" else "")).strip()[:120]
            if not label:
                skipped += 1
                errors.append(f"{day}: an event needs a name")
                continue
            covers = lift = None
            if r.get("covers") not in (None, ""):
                try:
                    covers = int(round(float(r.get("covers"))))
                except (TypeError, ValueError):
                    skipped += 1
                    errors.append(f"{day}: covers is not a number")
                    continue
                if covers < 0 or covers > 100000:
                    skipped += 1
                    errors.append(f"{day}: {covers} is not a cover count")
                    continue
            if r.get("lift_pct") not in (None, ""):
                try:
                    lift = int(round(float(r.get("lift_pct"))))
                except (TypeError, ValueError):
                    skipped += 1
                    errors.append(f"{day}: lift is not a number")
                    continue
                lift = max(-80, min(300, lift))
            # An event with no figure attached used to be stored as a 25% lift,
            # which then raised the day's demand level and headcount exactly
            # like a figure the owner had given. It is stored with no lift
            # now; by_date marks it `assumed` (ASSUMED_EVENT_LIFT_PCT, said as
            # an assumption) and it never moves a demand level (CA1 L29).
            conn.execute(
                "INSERT INTO demand_signals (restaurant_id, date, kind, label, covers, lift_pct, source, created_by) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, date, kind, label) DO UPDATE SET "
                "covers=excluded.covers, lift_pct=excluded.lift_pct, source=excluded.source, created_at=datetime('now')",
                (restaurant_id, day, kind, label, covers, lift, source, (created_by or "").strip()[:120] or None))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "skipped": skipped, "errors": errors[:10]}


# ── marketing's own signals (memory audit 9/29/26, mkt_to_staffing) ─────────
#
# The owner texted 412 guests to fill Tuesday; the auto-draft staffed a slow
# Tuesday, Home kept saying "Trim Tuesday staffing", the DSR's Tomorrow and
# the lineup never mentioned it and the kitchen prepped a normal Tuesday —
# because no marketing module wrote here. A campaign with a target day, or a
# post tagged with an occasion or a dish, is now one row (source "campaign" /
# "post"), and every reader of this table sees it. Its lift is this
# restaurant's MEASURED median campaign lift once CAMPAIGN_LIFT_MIN_CLOSED
# campaigns have closed their measurement windows; before that it is NULL —
# the existing assumed path, which never raises a demand level.
CAMPAIGN_LIFT_MIN_CLOSED = 3
MARKETING_OCCASIONS = ("game_day", "holiday", "event", "offer")


def measured_campaign_lift(restaurant_id, db_path=DB_PATH):
    """{lift_pct, n} — the median measured change in the target weekday's
    sales across this restaurant's closed fill-a-night campaigns
    (outcomes, source slow_day_campaign), or None under
    CAMPAIGN_LIFT_MIN_CLOSED of them. Before and after, not proof — the
    same basis the outcome cards state."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT delta_pct FROM recommendation_outcomes WHERE restaurant_id=? AND "
                            "source='slow_day_campaign' AND status='evaluated' AND delta_pct IS NOT NULL",
                            (restaurant_id,)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    vals = sorted(float(r["delta_pct"]) for r in rows)
    if len(vals) < CAMPAIGN_LIFT_MIN_CLOSED:
        return None
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {"lift_pct": int(round(med)), "n": len(vals)}


def record_marketing(restaurant_id, day, label, source, ref=None, menu_item_id=None, db_path=DB_PATH) -> bool:
    """One marketing signal for a date: `source` "campaign" (a fill-a-night
    text that went out) or "post" (a scheduled post about an occasion or a
    dish). Idempotent per (date, label); the lift is measured or NULL."""
    if source not in ("campaign", "post"):
        raise ValueError(f"record_marketing: {source!r}")
    try:
        d = _d(day)
    except (TypeError, ValueError, AttributeError):
        return False
    label = " ".join(str(label or "").split())[:120]
    if not label:
        return False
    lift = measured_campaign_lift(restaurant_id, db_path=db_path) if source == "campaign" else None
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, covers, lift_pct, source, "
                     "created_by, ref, menu_item_id) VALUES (?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(restaurant_id, date, kind, label) DO UPDATE SET lift_pct=excluded.lift_pct, "
                     "source=excluded.source, ref=excluded.ref, menu_item_id=excluded.menu_item_id",
                     (restaurant_id, d, "event", label, None, (lift or {}).get("lift_pct"), source,
                      "Cavnar AI (marketing)", (str(ref)[:60] if ref else None), menu_item_id))
        conn.commit()
        return True
    finally:
        conn.close()


def record_campaign(restaurant_id, target_day, sent, campaign_id=None, today=None, db_path=DB_PATH) -> bool:
    """A fill-a-night campaign whose texts went out: a signal on the next
    `target_day` (a weekday name) — "Text to 412 guests to fill Tuesday"."""
    wd = str(target_day or "").strip().capitalize()
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    if wd not in days or not int(sent or 0):
        return False
    if today is None:
        try:
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id, naive=True).date()
        except Exception:
            today = date.today()
    night = today + timedelta(days=(days.index(wd) - today.weekday()) % 7)
    return record_marketing(restaurant_id, night, f"Text to {int(sent)} guests to fill {wd}", "campaign",
                            ref=f"campaign:{campaign_id}" if campaign_id else None, db_path=db_path)


def record_post(restaurant_id, scheduled_for, topic, body=None, platform=None, post_id=None, db_path=DB_PATH) -> bool:
    """A scheduled post tagged with an occasion or a dish (marketing_tags.
    infer): a signal on the post's date. A post about nothing in particular
    writes nothing."""
    try:
        import marketing_tags
        tags = marketing_tags.infer(restaurant_id, topic, body, db_path=db_path)
    except Exception:
        return False
    occ, dish = tags.get("occasion"), tags.get("menu_item_name")
    if occ not in MARKETING_OCCASIONS and not dish:
        return False
    what = dish or (marketing_tags.OCCASION_LABELS.get(occ) or occ)
    where = f" on {platform.title()}" if platform else ""
    label = f"Post{where}: {what}" + (f" ({str(topic).strip()[:50]})" if topic and str(topic).strip() and
                                      str(topic).strip().lower() != str(what).lower() else "")
    return record_marketing(restaurant_id, str(scheduled_for)[:10], label, "post",
                            ref=f"post:{post_id}" if post_id else None, menu_item_id=tags.get("menu_item_id"),
                            db_path=db_path)


def parse_reservations_csv(text):
    """'date,covers' lines → rows of kind reservations. Header skipped."""
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        if len(parts) < 2 or not parts[0] or parts[0].lower() in ("date", "day"):
            continue
        rows.append({"date": parts[0], "kind": "reservations", "covers": parts[1]})
    return rows


def delete(restaurant_id, signal_id, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM demand_signals WHERE id=? AND restaurant_id=?", (int(signal_id), restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def upcoming(restaurant_id, start=None, end=None, db_path=DB_PATH) -> list:
    start = _d(start) if start else date.today().isoformat()
    end = _d(end) if end else (date.today() + timedelta(days=60)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM demand_signals WHERE restaurant_id=? AND date BETWEEN ? AND ? "
                            "ORDER BY date, kind, label", (restaurant_id, start, end)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def typical_covers(restaurant_id, db_path=DB_PATH, weeks=8, before=None) -> dict:
    """{weekday: median covers} from the owner's own cover counts, so a
    reservation figure can be read as a lift rather than a raw number.

    `before` (a date): the `weeks` weeks ending the day before it, so one
    night can be read against its typical weekday without counting itself
    (the nightly report). Unset: the weeks ending today."""
    try:
        import covers
        end = (date.fromisoformat(str(before)[:10]) - timedelta(days=1)) if before else date.today()
        hist = covers.by_date(restaurant_id, (end - timedelta(weeks=weeks)).isoformat(), end.isoformat(), db_path=db_path)
    except Exception:
        return {}
    by_day = {}
    for d, n in hist.items():
        try:
            by_day.setdefault(date.fromisoformat(d).strftime("%A"), []).append(int(n))
        except Exception:
            continue
    out = {}
    for day, vals in by_day.items():
        s = sorted(vals)
        if len(s) >= 2:
            out[day] = s[len(s) // 2]
    return out


def by_date(restaurant_id, dates, db_path=DB_PATH) -> dict:
    """{date: {"lift_pct": int, "covers": int|None, "labels": [..]}} for the
    dates asked for. The lift is the strongest signal on the date: an
    explicit lift, else booked covers against that weekday's typical
    covers (when known), else nothing."""
    if not dates:
        return {}
    signals = upcoming(restaurant_id, min(dates), max(dates), db_path=db_path)
    typical = typical_covers(restaurant_id, db_path=db_path)
    out = {}
    for s in signals:
        d = s["date"]
        if d not in dates:
            continue
        entry = out.setdefault(d, {"lift_pct": None, "covers": None, "labels": []})
        entry["labels"].append(s["label"] + (f" ({s['covers']} covers)" if s.get("covers") else ""))
        lift = s.get("lift_pct")
        if s.get("kind") == "event" and lift is None and not s.get("covers"):
            entry["assumed"] = True
            entry["assumed_lift_pct"] = ASSUMED_EVENT_LIFT_PCT
        if lift is None and s.get("covers"):
            try:
                day = date.fromisoformat(d).strftime("%A")
            except Exception:
                day = None
            base = typical.get(day)
            if base:
                lift = int(round((s["covers"] / base - 1) * 100))
        if s.get("covers"):
            entry["covers"] = max(entry["covers"] or 0, int(s["covers"]))
        if lift is not None:
            entry["lift_pct"] = lift if entry["lift_pct"] is None else max(entry["lift_pct"], lift)
    return out


def prompt_block(signals_by_date: dict, week_dates: list) -> str:
    """The dated facts, as the model should read them."""
    lines = []
    for d in week_dates:
        e = (signals_by_date or {}).get(d)
        if not e:
            continue
        try:
            day = date.fromisoformat(d).strftime("%A")
        except Exception:
            day = ""
        what = "; ".join(e["labels"])
        lift = e.get("lift_pct")
        tail = ""
        if lift is not None:
            tail = (f" — expect about {lift}% more than a typical {day}" if lift > 0
                    else f" — expect about {abs(lift)}% less than a typical {day}" if lift < 0
                    else " — about a typical day")
        elif e.get("covers"):
            tail = f" — {e['covers']} covers booked"
        elif e.get("assumed"):
            tail = (f" — no covers or lift given; ASSUMED busier (about {e.get('assumed_lift_pct')}% is an "
                    f"assumption, not a figure) — staff it as a normal busy {day}, not above it")
        # The labels are the owner's words (an event they named, a post's
        # dish) and the date is M/D/YY (memory reaching a prompt, 9/29/26).
        import ai_guard
        from time_utils import mdy
        lines.append(f"  {day} {mdy(d)}: {ai_guard.wrap_untrusted(what)}{tail}")
    if not lines:
        return ""
    return ("\n\nWHAT THE OWNER KNOWS ABOUT SPECIFIC DATES (events and reservations they entered, and the "
            "texts or posts they sent to fill a night — "
            "a stronger signal than the weekday averages above for the date it names; scale that day's "
            "headcount by roughly the lift stated, proportionally across roles, and say so in the summary):\n"
            + "\n".join(lines))
