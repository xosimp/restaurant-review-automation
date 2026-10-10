"""
event_reactive.py — when an event changes after a week is published, the
exact priced change to the live week, for one-tap approval (scheduler audit
10/10/26 MARKET-28).

A playoff game added, a kickoff moved from noon to 7:20pm, a game
cancelled: the catalog (event_intel) and each restaurant's copy of it in
demand_signals follow the change within a day, the forecast moves with the
measured effect — and the published week stayed as it was, so managers
found out from the guests.

  scan()      every live published week from today on: per date, the
              followed events (series the restaurant follows, games it did
              not dismiss) and the measured effect the forecast applies
              (event_memory.effects_for_day). The first look records them
              as the week's baseline (`published_event_marks`); a later
              difference opens a proposal (`event_week_proposals`) — once
              per change.
  propose()   the change in people: per role family on the event's daypart
              (its start, THE daypart rule), the heads the measured effect
              moves (round(people on × the change in the effect)); an
              effect the restaurant has not measured moves nobody and says
              so — nothing is guessed. More: one shift per head shaped like
              the role's own shift that night, offered first to the best
              legal person off that day (labor_replacements.for_gap — free,
              in the role, not on time off; the claim's full sweep runs
              again when they accept). Fewer: the role's tentative shifts
              first (commitment_ladder), else the latest and shortest.
              Priced: the hours at the person's rate and, where the
              predictability-pay ledger exists (J1's predictability_pay.
              preview), the premium a late change owes.
  approve()   one tap: each added shift posted as an open shift offered to
              its first target (or to everyone legal when they are not on
              the app); a cut is never applied unseen — it opens the week in
              the Studio with the rows named, to Send as an edit (where the
              ledger records it).
  dismiss()   the owner keeps the week as it is.

Owner-facing (studio_bench 'events'); the managers who decide requests are
told when a proposal opens. Nothing calls a model.
"""
import json
import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

MAX_DAYS_AHEAD = 28



def _models_db():
    """The database path as models has it now (CLAUDE.md, bound imports: a
    module's own DB_PATH is the one it was imported with)."""
    import models
    return models.DB_PATH

def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_event_reactive(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS published_event_marks (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            history_id      INTEGER NOT NULL,
            date            TEXT    NOT NULL,
            fingerprint     TEXT    NOT NULL,
            events_json     TEXT,
            effect_pct      REAL,
            seen_at         TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, history_id, date)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS event_week_proposals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            history_id      INTEGER NOT NULL,
            date            TEXT    NOT NULL,
            fingerprint     TEXT    NOT NULL,
            change          TEXT    NOT NULL,
            summary         TEXT    NOT NULL,
            pct_then        REAL,
            pct_now         REAL,
            actions_json    TEXT,
            cost_json       TEXT,
            status          TEXT    NOT NULL DEFAULT 'open',
            result_json     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            decided_at      TEXT,
            decided_by      TEXT,
            UNIQUE(restaurant_id, history_id, date, fingerprint)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_proposals_open ON event_week_proposals(restaurant_id, status)")
        conn.commit()
    finally:
        conn.close()


class ProposalError(ValueError):
    """A refusal the caller shows as it is."""


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def events_on(restaurant_id, day, db_path=None) -> list:
    """The followed, not dismissed, catalog events on `day`:
    [{id, label, kickoff, status, daypart}]."""
    import schedule_time as st
    from event_intel import store
    db = db_path or store.DB_PATH
    try:
        series = [f["series_id"] for f in store.follows(restaurant_id, db_path=db)]
        gone = store.dismissed(restaurant_id, db_path=db)
        rows = store.events_for(series, day, day, db_path=db)
    except Exception as e:
        log.warning("events unreadable rid=%s: %s", restaurant_id, e)
        return []
    out = []
    for e in rows:
        if e["id"] in gone:
            continue
        kick = (e.get("kickoff_local") or "").strip()
        part = st.start_daypart(kick) if kick else "night"
        out.append({"id": e["id"], "label": f"{e.get('short_name') or e.get('series_name')}"
                    + (f" vs {e['opponent']}" if e.get("opponent") else ""),
                    "kickoff": kick or None, "status": e.get("status") or "scheduled",
                    "daypart": part if part in ("morning", "night") else "night"})
    return out


def fingerprint(events) -> str:
    return json.dumps(sorted((e["id"], e.get("kickoff") or "", e.get("status") or "") for e in events))


def _effect_pct(restaurant_id, day, db_path=None):
    try:
        import event_memory
        eff = event_memory.effects_for_day(restaurant_id, date.fromisoformat(day), db_path=db_path)
    except Exception:
        eff = None
    return float(eff["pct"]) if eff and eff.get("pct") else 0.0


def _live_weeks(restaurant_id, today, db_path=None) -> list:
    """The live published copies (models.live_where / live_csv_sql — what
    staff have, never a draft's unsent changes) from today to
    MAX_DAYS_AHEAD."""
    import models
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT h.id, h.week_start, h.week_end, {models.live_csv_sql('h')} AS schedule_csv FROM schedule_history h "
            f"WHERE h.restaurant_id=? AND h.week_end>=? AND h.week_start<=? AND {models.live_where('h')}",
            (restaurant_id, today.isoformat(), (today + timedelta(days=MAX_DAYS_AHEAD)).isoformat())).fetchall()]
    finally:
        conn.close()


def _describe_change(before, after) -> tuple:
    """(kind, sentence) between two event lists of one date."""
    b = {e["id"]: e for e in before}
    a = {e["id"]: e for e in after}
    bits, kind = [], "changed"
    for i, e in a.items():
        if i not in b:
            bits.append(f"{e['label']} was added" + (f" ({e['kickoff']})" if e.get("kickoff") else ""))
            kind = "added"
        elif b[i].get("kickoff") != e.get("kickoff"):
            bits.append(f"{e['label']} moved from {b[i].get('kickoff') or 'no set time'} to {e.get('kickoff') or 'no set time'}")
            kind = "moved"
        elif b[i].get("status") != e.get("status"):
            bits.append(f"{e['label']} is now {e.get('status')}")
            kind = "cancelled" if e.get("status") in ("cancelled", "postponed") else kind
    for i, e in b.items():
        if i not in a:
            bits.append(f"{e['label']} is no longer on")
            kind = "cancelled"
    return kind, "; ".join(bits) or "the events on this date changed"


def _rate(restaurant_id, name, role):
    try:
        import labor
        people, roles = labor.person_rate_book(restaurant_id)
        r = people.get(labor._name_key(name)) if name else None
        r = r or roles.get((role or "").strip().lower())
        return float(r) if r else float(labor.get_hourly_rate(restaurant_id))
    except Exception:
        return None


def _hours(row):
    import schedule_time as st
    try:
        return st.wall_hours(row)
    except Exception:
        return 0.0


def propose(restaurant_id, history_id, day, before_events, after_events, pct_then, pct_now, rows, db_path=None) -> dict:
    """The priced change (module doc): {summary, change, actions: [...],
    cost: {...}, measured: bool}."""
    import schedule_time as st
    from shift_quality import role_family
    kind, what = _describe_change(before_events, after_events)
    d = date.fromisoformat(day)
    delta = (pct_now or 0.0) - (pct_then or 0.0)
    parts = {e["daypart"] for e in (after_events or before_events or [])} or {"night"}
    try:
        import models
        import schedule_rules
        fams = schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
        managers = {_key(n) for n in (models.salaried_keys(models.get_restaurant(restaurant_id, db_path or models.DB_PATH))
                                      or [])}
    except Exception:
        fams, managers = {}, set()
    todays = [r for r in rows if str(r.get("date"))[:10] == day]
    working = {(r.get("employee") or "").strip() for r in todays}
    actions = []
    if abs(delta) < 1e-6:
        summary = (f"{what}. Your own nights haven't measured an effect for it yet, so the week stays as it is — "
                   "nothing is guessed.")
        return {"summary": summary, "change": kind, "actions": [], "cost": {}, "measured": False}
    groups = {}
    for r in todays:
        if _key(r.get("employee")) in managers or "manager" in str(r.get("role") or "").lower():
            continue
        part = st.present_dayparts(r)[0]
        if part not in parts:
            continue
        groups.setdefault(role_family(r.get("role") or "", fams), []).append(r)
    try:
        import commitment_ladder
        lv = commitment_ladder.levels_for(restaurant_id, day, day, db_path=db_path)
    except Exception:
        lv = {}
    for fam, members in sorted(groups.items()):
        n = len(members)
        heads = int(round(n * delta / 100.0))
        if heads == 0:
            continue
        shape = max(members, key=lambda r: (_hours(r), r.get("shift_start") or ""))
        if heads > 0:
            try:
                import labor_replacements
                fits = labor_replacements.for_gap(restaurant_id, shape.get("role"), d.strftime("%A"), exclude=working,
                                                  db_path=db_path or _models_db(), limit=3 + heads,
                                                  on_date=day)
            except Exception as e:
                log.warning("event proposal targets unavailable rid=%s: %s", restaurant_id, e)
                fits = []
            names = [f["name"] for f in fits]
            for i in range(heads):
                who = names[i] if i < len(names) else None
                hrs = _hours(shape)
                rate = _rate(restaurant_id, who, shape.get("role"))
                actions.append({"kind": "add", "role": shape.get("role"), "date": day,
                                "shift_start": shape.get("shift_start"), "shift_end": shape.get("shift_end"),
                                "hours": round(hrs, 2), "offer_to": who, "others": [x for x in names if x != who][:3],
                                "cost": round(hrs * rate, 2) if rate else None})
        else:
            # Tentative first (the ladder told them it could change), then
            # the latest start and the shortest shift.
            order = sorted(members, key=lambda r: (
                0 if (lv.get((day, _key(r.get("employee")), str(r.get("shift_start") or "").strip())) or {}).get(
                    "first_level") == "tentative" else 1,
                -(st.start_of(r) or 0), _hours(r)))
            for r in order[:min(-heads, max(0, n - 1))]:
                hrs = _hours(r)
                rate = _rate(restaurant_id, r.get("employee"), r.get("role"))
                level = (lv.get((day, _key(r.get("employee")), str(r.get("shift_start") or "").strip())) or {}).get(
                    "first_level")
                actions.append({"kind": "cut", "role": r.get("role"), "date": day, "employee": r.get("employee"),
                                "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                                "hours": round(hrs, 2), "level": level,
                                "saves": round(hrs * rate, 2) if rate else None})
    after_rows = [dict(r) for r in rows]
    for a in actions:
        if a["kind"] == "add":
            after_rows.append({"date": day, "day": d.strftime("%A"), "employee": a.get("offer_to") or "Open",
                               "role": a["role"], "shift_start": a["shift_start"], "shift_end": a["shift_end"],
                               "scheduled_hours": str(a["hours"]), "notes": "event"})
        else:
            after_rows = [r for r in after_rows if not (str(r.get("date"))[:10] == day and _key(r.get("employee"))
                                                        == _key(a["employee"]) and r.get("shift_start") == a["shift_start"])]
    pay = None
    try:
        import importlib
        pp = importlib.import_module("predictability_pay")
        pay = pp.preview(restaurant_id, rows, after_rows, db_path=db_path)
    except ImportError:
        pay = None
    except Exception as e:
        log.warning("event proposal predictability pay failed rid=%s: %s", restaurant_id, e)
    added = sum(a.get("cost") or 0 for a in actions if a["kind"] == "add")
    saved = sum(a.get("saves") or 0 for a in actions if a["kind"] == "cut")
    cost = {"added_labor": round(added, 2), "saved_labor": round(saved, 2),
            "predictability_pay": (pay or {}).get("total") if (pay or {}).get("covered") else None,
            "predictability_text": (pay or {}).get("text")}
    if not actions:
        summary = (f"{what}. Your own nights measure it at {delta:+.0f}% on sales, which doesn't move a whole person "
                   "on any role — the week stays as it is.")
    else:
        adds = [a for a in actions if a["kind"] == "add"]
        cuts = [a for a in actions if a["kind"] == "cut"]
        bits = []
        if adds:
            bits.append(f"add {len(adds)} ({', '.join(sorted({a['role'] for a in adds}))})")
        if cuts:
            bits.append(f"take off {len(cuts)} ({', '.join(a['employee'] for a in cuts)})")
        summary = (f"{what}. Your own nights measure it at {delta:+.0f}% on sales: " + " and ".join(bits)
                   + (f" — about ${added:,.0f} more in labor" if added else "")
                   + (f", about ${saved:,.0f} saved" if saved else "")
                   + (f"; {pay['text']}" if (pay or {}).get("text") else "") + ".")
    return {"summary": summary, "change": kind, "actions": actions, "cost": cost, "measured": True}


def scan(restaurant_id, today=None, db_path=None, notify=True) -> dict:
    """The nightly look (module doc). {dates, baselined, proposals}."""
    from schedule_versions import rows_from_csv
    today = today or date.today()
    out = {"dates": 0, "baselined": 0, "proposals": 0}
    for w in _live_weeks(restaurant_id, today, db_path):
        rows = rows_from_csv(w["schedule_csv"] or "")
        try:
            d0, d1 = date.fromisoformat(str(w["week_start"])[:10]), date.fromisoformat(str(w["week_end"])[:10])
        except ValueError:
            continue
        cur = max(d0, today)
        while cur <= d1:
            day = cur.isoformat()
            cur += timedelta(days=1)
            out["dates"] += 1
            evs = events_on(restaurant_id, day, db_path)
            fp = fingerprint(evs)
            pct = _effect_pct(restaurant_id, day, db_path)
            conn = get_conn(db_path)
            try:
                mark = conn.execute("SELECT * FROM published_event_marks WHERE restaurant_id=? AND history_id=? AND "
                                    "date=?", (restaurant_id, w["id"], day)).fetchone()
                if mark is None:
                    conn.execute("INSERT INTO published_event_marks (restaurant_id, history_id, date, fingerprint, "
                                 "events_json, effect_pct) VALUES (?,?,?,?,?,?)",
                                 (restaurant_id, w["id"], day, fp, json.dumps(evs), pct))
                    conn.commit()
                    out["baselined"] += 1
                    continue
                if mark["fingerprint"] == fp:
                    continue
                seen = conn.execute("SELECT 1 FROM event_week_proposals WHERE restaurant_id=? AND history_id=? AND "
                                    "date=? AND fingerprint=?", (restaurant_id, w["id"], day, fp)).fetchone()
                if seen:
                    continue
                before = json.loads(mark["events_json"] or "[]")
                pct_then = mark["effect_pct"]
            finally:
                conn.close()
            p = propose(restaurant_id, w["id"], day, before, evs, pct_then, pct, rows, db_path=db_path)
            conn = get_conn(db_path)
            try:
                cur_ = conn.execute(
                    "INSERT OR IGNORE INTO event_week_proposals (restaurant_id, history_id, date, fingerprint, change, "
                    "summary, pct_then, pct_now, actions_json, cost_json, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (restaurant_id, w["id"], day, fp, p["change"], p["summary"][:1000], pct_then, pct,
                     json.dumps(p["actions"]), json.dumps(p["cost"]), "open" if p["actions"] else "noted"))
                conn.commit()
                pid = cur_.lastrowid if cur_.rowcount else None
            finally:
                conn.close()
            if pid:
                out["proposals"] += 1
                if notify:
                    _tell(restaurant_id, day, p)
    return out


def _tell(restaurant_id, day, p):
    try:
        import people
        from time_utils import mdy
        people.tell_deciders(restaurant_id, f"An event changed on {mdy(day)}", p["summary"][:300],
                             alert_type="shift_request")
    except Exception as e:
        log.warning("event proposal notice failed rid=%s: %s", restaurant_id, e)


def proposals(restaurant_id, status=None, db_path=None, limit=50) -> list:
    conn = get_conn(db_path)
    try:
        q, args = "SELECT * FROM event_week_proposals WHERE restaurant_id=?", [restaurant_id]
        if status:
            q += " AND status=?"
            args.append(status)
        rows = conn.execute(q + " ORDER BY date, id DESC LIMIT ?", args + [int(limit)]).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("actions_json", "cost_json", "result_json"):
            try:
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            except (TypeError, ValueError):
                d[k[:-5]] = None
        out.append(d)
    return out


def approve(restaurant_id, proposal_id, by=None, db_path=None) -> dict:
    """One tap (module doc). Returns the proposal with `result`: per action
    {ok, offered_to | posted | studio, error?}."""
    import shift_requests
    got = [p for p in proposals(restaurant_id, db_path=db_path, limit=500) if p["id"] == int(proposal_id)]
    if not got:
        raise ProposalError("that proposal wasn't found")
    p = got[0]
    if p["status"] != "open":
        raise ProposalError("that proposal is already decided")
    results = []
    for a in p.get("actions") or []:
        if a["kind"] == "add":
            try:
                out = shift_requests.post_open_shift(restaurant_id, a["date"], a["shift_start"], a["shift_end"],
                                                     role=a["role"], actor=by or "event change",
                                                     note="Added for an event change", offer_to=a.get("offer_to"),
                                                     db_path=db_path or _models_db())
                results.append({"ok": True, "kind": "add", "offered_to": a.get("offer_to"),
                                "request_id": (out.get("request") or {}).get("id")})
            except shift_requests.NotOnApp:
                out = shift_requests.post_open_shift(restaurant_id, a["date"], a["shift_start"], a["shift_end"],
                                                     role=a["role"], actor=by or "event change",
                                                     note="Added for an event change",
                                                     db_path=db_path or _models_db())
                results.append({"ok": True, "kind": "add", "posted": True,
                                "request_id": (out.get("request") or {}).get("id")})
            except shift_requests.ShiftRequestError as e:
                results.append({"ok": False, "kind": "add", "error": str(e)})
        else:
            results.append({"ok": True, "kind": "cut", "studio": True, "employee": a.get("employee"),
                            "note": "Open the week in the Studio and remove it there, then Send — the change is "
                                    "recorded with any predictability pay."})
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE event_week_proposals SET status='applied', result_json=?, decided_at=datetime('now'), "
                     "decided_by=? WHERE id=? AND restaurant_id=?",
                     (json.dumps(results), (by or "")[:120] or None, int(proposal_id), restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return dict(p, status="applied", result=results)


def dismiss(restaurant_id, proposal_id, by=None, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE event_week_proposals SET status='dismissed', decided_at=datetime('now'), "
                           "decided_by=? WHERE id=? AND restaurant_id=? AND status='open'",
                           ((by or "")[:120] or None, int(proposal_id), restaurant_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
