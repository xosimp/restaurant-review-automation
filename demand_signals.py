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
# A scheduled post is its own kind (memory re-audit 9/29/26, CROSSMODULE-7):
# written as an "event" it became an event flag in event_memory (and left its
# night out of every baseline), told the schedule the night was "ASSUMED
# busier" and suppressed trims. Never saved through save() — only
# record_post writes it.
POST_KIND = "post"
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
    """{lift_pct, n, source, basis} — THE measured effect of a guest text
    campaign here: the campaign nights' own lift against their typical same
    weekday (event_memory.campaign_effect), once it clears event_memory's
    floor (`applies`); else None, and the assumed path stands. Before and
    after, not proof.

    One measurement (INT PRED-27, memory fix round 9/29/26): this read the
    campaign outcome trackers' weekday_sales change over their multi-week
    windows, which diluted a +30% campaign night to +0.2% — while the
    forecast read event_memory's measurement of the night itself. Staffing,
    the forecast and the campaign's own result now read the one figure. A
    demo, test or internal restaurant's nights teach nothing
    (models.learns_for_itself: its own learning)."""
    try:
        import models as _m_elig
        if not _m_elig.learns_for_itself(_m_elig.get_restaurant(restaurant_id, db_path)):
            return None
    except Exception:
        return None
    try:
        import event_memory
        e = event_memory.campaign_effect(restaurant_id, db_path=db_path)
    except Exception:
        return None
    if not e or not e.get("applies"):
        return None
    return {"lift_pct": int(round(float(e["median_lift_pct"]))), "n": int(e["n"]), "source": "event_memory",
            "basis": e.get("basis")}


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
    # A campaign text is an event the night is aimed at (event_memory's one
    # campaign label); a post is only a post (CROSSMODULE-7).
    kind = "event" if source == "campaign" else POST_KIND
    # No figure is baked into the row: a campaign night's lift is read live
    # from the one measurement (measured_campaign_lift, by_date), so it
    # follows every night event_memory measures after this one.
    lift = None
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, covers, lift_pct, source, "
                     "created_by, ref, menu_item_id) VALUES (?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(restaurant_id, date, kind, label) DO UPDATE SET lift_pct=excluded.lift_pct, "
                     "source=excluded.source, ref=excluded.ref, menu_item_id=excluded.menu_item_id",
                     (restaurant_id, d, kind, label, None, (lift or {}).get("lift_pct"), source,
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


def remove_by_ref(restaurant_id, ref, db_path=DB_PATH) -> int:
    """Take a marketing signal off its night by what it came from ("post:12",
    "campaign:45") — a post cancelled, or one that never went out, no longer
    tells staffing, prep or the lineup anything (CROSSMODULE-7). Returns rows
    removed; never raises."""
    if not ref:
        return 0
    try:
        conn = get_conn(db_path)
        try:
            n = conn.execute("DELETE FROM demand_signals WHERE restaurant_id=? AND ref=?",
                             (restaurant_id, str(ref)[:60])).rowcount
            conn.commit()
        finally:
            conn.close()
        return n or 0
    except Exception:
        return 0


def migrate_post_signals(conn) -> dict:
    """At boot (models.init_demand_signals), idempotent: a post's signal
    written as kind 'event' before CROSSMODULE-7 becomes kind 'post', and the
    signal of a post that was cancelled or failed is taken off its night.
    {"rekinded", "removed"}. Leaves the caller's transaction to commit."""
    out = {"rekinded": 0, "removed": 0}
    out["rekinded"] = conn.execute("UPDATE OR IGNORE demand_signals SET kind=? WHERE source='post' AND kind='event'",
                                   (POST_KIND,)).rowcount or 0
    # The posts table is created later at boot on a new database: nothing to
    # remove yet. Any other failure raises.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='marketing_scheduled_posts'").fetchone():
        out["removed"] = conn.execute(
            "DELETE FROM demand_signals WHERE source='post' AND ref LIKE 'post:%' AND EXISTS ("
            "SELECT 1 FROM marketing_scheduled_posts p WHERE p.restaurant_id = demand_signals.restaurant_id "
            "AND 'post:' || p.id = demand_signals.ref AND p.status IN ('cancelled','failed'))").rowcount or 0
    return out


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
        row = conn.execute("SELECT source, ref FROM demand_signals WHERE id=? AND restaurant_id=?",
                           (int(signal_id), restaurant_id)).fetchone()
        cur = conn.execute("DELETE FROM demand_signals WHERE id=? AND restaurant_id=?", (int(signal_id), restaurant_id))
        conn.commit()
        gone = cur.rowcount > 0
    finally:
        conn.close()
    # A game from the catalog the owner removed stays removed (event_intel:
    # the daily sync would otherwise write it back the next morning).
    if gone and row and str(row["source"] or "") == "events" and str(row["ref"] or "").startswith("event:"):
        try:
            from event_intel import store as _ev_store
            _ev_store.dismiss(restaurant_id, int(str(row["ref"]).split(":", 1)[1]), db_path=db_path)
        except Exception:
            pass
    return gone


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


def _campaign_measured(restaurant_id, db_path=DB_PATH):
    """event_memory.campaign_effect in _measured's shape, gated like
    measured_campaign_lift (a learning-eligible restaurant only), or None."""
    try:
        import models as _m_elig
        if not _m_elig.learns_for_itself(_m_elig.get_restaurant(restaurant_id, db_path)):
            return None
        import event_memory
        return event_memory.campaign_effect(restaurant_id, db_path=db_path)
    except Exception:
        return None


def _post_measured(restaurant_id, signal, db_path=DB_PATH, _cache=None):
    """The marketing module's own measured lift for a post like this one
    (marketing_signals.attribution_summary: sales in the days after each
    post against the same weekdays before, grouped by dish and by occasion)
    — {"lift_pct", "n", "group", "verdict"} for a group of
    MIN_GROUP_POSTS+ measured posts whose own verdicts say it moved sales
    (lifted or dropped), else None: an unmeasured post adds no lift
    (CROSSMODULE-7). A restaurant that does not learn for itself (a demo, or
    an admin's exclusion — models.learns_for_itself) teaches nothing.
    Never raises."""
    try:
        import models as _m_elig
        if not _m_elig.learns_for_itself(_m_elig.get_restaurant(restaurant_id, db_path)):
            return None
        cache = _cache if _cache is not None else {}
        if "summary" not in cache:
            import marketing_signals
            cache["summary"] = marketing_signals.attribution_summary(restaurant_id, db_path=db_path)
        summ = cache["summary"] or {}
        if not summ.get("ok"):
            return None
        import marketing_signals
        import marketing_tags
        want = []
        if signal.get("menu_item_id"):
            conn = get_conn(db_path)
            try:
                r = conn.execute("SELECT name FROM menu_items WHERE id=? AND restaurant_id=?",
                                 (signal["menu_item_id"], restaurant_id)).fetchone()
            finally:
                conn.close()
            if r and r["name"]:
                want.append(("by_dish", str(r["name"])))
        what = str(signal.get("label") or "").split(": ", 1)[-1].split(" (", 1)[0].strip().lower()
        occ = next((k for k, v in marketing_tags.OCCASION_LABELS.items() if v == what or k == what), None)
        if occ:
            want.append(("by_occasion", occ))
        for key, group in want:
            for g in summ.get(key) or []:
                if str(g.get("group") or "").lower() != group.lower():
                    continue
                if int(g.get("posts") or 0) < marketing_signals.MIN_GROUP_POSTS or \
                        g.get("verdict") not in ("lifted", "dropped"):
                    continue
                return {"lift_pct": int(round(float(g["median_lift_pct"]))), "n": int(g["posts"]),
                        "group": group, "verdict": g["verdict"]}
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
    post_cache = {}
    for s in signals:
        d = s["date"]
        if d not in dates:
            continue
        entry = out.setdefault(d, {"lift_pct": None, "covers": None, "labels": []})
        if s.get("kind") == POST_KIND:
            # A post (CROSSMODULE-7): its lift only where marketing measured
            # posts like it; otherwise it is named for the kitchen and the
            # floor, adds nothing, and is never an assumed-busier night.
            pm = _post_measured(restaurant_id, s, db_path=db_path, _cache=post_cache)
            entry.setdefault("posts", []).append({"label": s["label"], "measured": pm})
            if pm:
                entry["labels"].append(f"{s['label']} (posts like it measured {pm['lift_pct']:+d}% here over "
                                       f"{pm['n']} posts)")
                entry["lift_pct"] = pm["lift_pct"] if entry["lift_pct"] is None else max(entry["lift_pct"],
                                                                                        pm["lift_pct"])
                entry.setdefault("lift_source", "post_measured")
            continue
        label = s["label"] + (f" ({s['covers']} covers)" if s.get("covers") else "")
        lift = s.get("lift_pct")
        if s.get("kind") == "event" and str(s.get("source") or "") == "campaign":
            # A fill-a-night text: the one campaign measurement (INT PRED-27).
            measured = _campaign_measured(restaurant_id, db_path=db_path)
        else:
            measured = _measured(restaurant_id, s["label"], db_path=db_path) if s.get("kind") == "event" else None
        # A game or event from the catalog (event_intel, source "events") is
        # not the owner saying the night will be busy: until this restaurant
        # has MEASURED nights like it past the floor it is context only —
        # never an assumed-busier night, never a reason to hold a cut, never
        # a label the schedule reads as the owner's (audit 10/1/26).
        if s.get("kind") == "event" and str(s.get("source") or "") == "events" \
                and not (measured and measured.get("applies")):
            note = (f" (measured {measured['median_lift_pct']:+.0f}% here over {measured['n']} "
                    f"night{'s' if measured['n'] != 1 else ''} so far, not enough to plan on)" if measured
                    else " (no measured effect here yet)")
            entry.setdefault("context", []).append(s["label"] + note)
            continue
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
        if not e.get("labels") and e.get("context"):
            import ai_guard
            from time_utils import mdy
            lines.append(f"  {day} {mdy(d)}: {ai_guard.wrap_untrusted('; '.join(e['context']))} — a game or event "
                         f"nearby, not the owner's; no measured effect to plan on: staff a usual {day}")
            continue
        what = "; ".join(e["labels"])
        if e.get("context"):
            what += " (also nearby: " + "; ".join(e["context"]) + ")"
        lift = e.get("lift_pct")
        tail = ""
        if lift is not None:
            tail = (f" — expect about {lift}% more than a typical {day}" if lift > 0
                    else f" — expect about {abs(lift)}% less than a typical {day}" if lift < 0
                    else " — about a typical day")
            if e.get("lift_source") == "measured":
                tail += (f" (this restaurant's own measured median over {e.get('measured_n')} past nights like it — "
                         "before and after, not proof)")
            elif e.get("lift_source") == "post_measured":
                tail += (" (the sales change measured after this restaurant's own posts like it — before and "
                         "after, not proof)")
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
        # The labels are the owner's words (an event they named, a post's
        # dish) and the date is M/D/YY (memory reaching a prompt, 9/29/26).
        import ai_guard
        from time_utils import mdy
        unmeasured = [p["label"] for p in e.get("posts") or [] if not p.get("measured")]
        if unmeasured:
            post_tail = (" — a post goes out that day with no measured effect on sales here yet: not a reason "
                         "to staff above a usual " + day)
            if what:
                lines.append(f"  {day} {mdy(d)}: {ai_guard.wrap_untrusted(what)}{tail}")
            lines.append(f"  {day} {mdy(d)}: {ai_guard.wrap_untrusted('; '.join(unmeasured))}{post_tail}")
            continue
        lines.append(f"  {day} {mdy(d)}: {ai_guard.wrap_untrusted(what)}{tail}")
    if not lines:
        return ""
    return ("\n\nWHAT THE OWNER KNOWS ABOUT SPECIFIC DATES (events and reservations they entered, and the "
            "texts or posts they sent to fill a night — "
            "a stronger signal than the weekday averages above for the date it names; scale that day's "
            "headcount by roughly the lift stated, proportionally across roles, and say so in the summary):\n"
            + "\n".join(lines))
