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

What an event DID is measured afterwards (event_memory, memory audit
9/29/26): a listed event with no figure takes its label's measured median
lift here once EFFECT_MIN_N past nights carry it ("measured 3 times"), and
an owner's own figure stands with the measured record said beside it — the
owner's "Homecoming +30%" used to be the only number there ever was.
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

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


def _measured(restaurant_id, label, db_path=DB_PATH):
    """event_memory.measured_effect for an event's label (the first of its
    labels with a record), or None. Never raises."""
    try:
        import event_memory
        for lab in event_memory.split_labels(label):
            got = event_memory.measured_effect(restaurant_id, lab, db_path=db_path)
            if got:
                return got
    except Exception:
        return None
    return None


def by_date(restaurant_id, dates, db_path=DB_PATH) -> dict:
    """{date: {"lift_pct": int, "covers": int|None, "labels": [..]}} for the
    dates asked for. The lift is the strongest signal on the date: an
    explicit lift, else booked covers against that weekday's typical
    covers (when known), else the event's MEASURED lift here once it has
    event_memory.EFFECT_MIN_N nights (`lift_source` "measured",
    `measured_n`), else nothing — an event with no figure and no record is
    `assumed`. Every event with a record carries it in `measured` so an
    owner's own figure is said beside what was measured."""
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
        label = s["label"] + (f" ({s['covers']} covers)" if s.get("covers") else "")
        lift = s.get("lift_pct")
        measured = _measured(restaurant_id, s["label"], db_path=db_path) if s.get("kind") == "event" else None
        if measured:
            entry.setdefault("measured", []).append({
                "label": s["label"], "median_lift_pct": measured["median_lift_pct"], "n": measured["n"],
                "last": measured["last"].isoformat(), "applies": measured["applies"],
                "owner_lift_pct": lift})
        if s.get("kind") == "event" and lift is None and not s.get("covers"):
            if measured and measured["applies"]:
                lift = int(round(measured["median_lift_pct"]))
                entry["lift_source"] = "measured"
                entry["measured_n"] = max(entry.get("measured_n") or 0, measured["n"])
                label += f" (measured {measured['n']} times here)"
            else:
                entry["assumed"] = True
                entry["assumed_lift_pct"] = ASSUMED_EVENT_LIFT_PCT
        entry["labels"].append(label)
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
            if e.get("lift_source") == "measured":
                tail += (f" (this restaurant's own measured median over {e.get('measured_n')} past nights like it — "
                         "before and after, not proof)")
            else:
                seen = [m for m in e.get("measured") or [] if m.get("owner_lift_pct") is not None]
                if seen:
                    m = seen[0]
                    tail += (f"; the same kind of night measured {m['median_lift_pct']:+.0f}% here over "
                             f"{m['n']} past night{'s' if m['n'] != 1 else ''}")
        elif e.get("covers"):
            tail = f" — {e['covers']} covers booked"
        elif e.get("assumed"):
            tail = (f" — no covers or lift given; ASSUMED busier (about {e.get('assumed_lift_pct')}% is an "
                    f"assumption, not a figure) — staff it as a normal busy {day}, not above it")
        lines.append(f"  {day} {d}: {what}{tail}")
    if not lines:
        return ""
    return ("\n\nWHAT THE OWNER KNOWS ABOUT SPECIFIC DATES (events and reservations, entered by them — "
            "a stronger signal than the weekday averages above for the date it names; scale that day's "
            "headcount by roughly the lift stated, proportionally across roles, and say so in the summary):\n"
            + "\n".join(lines))
