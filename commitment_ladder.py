"""
commitment_ladder.py — how firm each published shift is, said to the person
who works it, and what changing it later costs (scheduler audit 10/10/26
MARKET-27).

Workers value predictable hours (the core Fair Workweek idea), but a
published shift said nothing about how likely it was to change, and the
stability dimension weighed 2 of about 150. The ladder publishes each
shift as one of three:

  firm        needed even at the low end of the night's forecast (the 80%
              range's low, demand.forecast_day) — and every manager's shift
              (the manager-every-minute rule does not move with sales)
  likely      needed at the forecast, not at its low end
  tentative   there only because of a measured lift on the date (an event,
              a holiday, a campaign — the forecast's `effects`): if that
              lift does not come, these go first

Within a role's shifts on one date and daypart the shifts covering the most
of the core service window are the ones kept firmest; the shortest and
latest go first. A date with no forecast range yet is "likely" throughout
(never firm by assumption), with the reason.

  ladder()         the levels for a set of rows, pure apart from the
                   forecast read.
  ensure()         a published week's levels recorded once, the moment
                   it is first read after publishing (`shift_commitments`,
                   first_level kept forever: the promise made), new rows of
                   a re-published week committed when they appear, and a
                   committed row the restaurant later changed or removed
                   marked with when and how (`changed_at`, `change`).
  change_cost()    a change to committed rows priced: stability points per
                   level (COMMIT_POINTS — what the solver adds to the
                   stability dimension when a re-draft moves a committed
                   person) and, where the predictability-pay ledger exists
                   (J1's predictability_pay.preview), the premium the law
                   owes.
  committed_for_week()  {(date, person key, start): level} — the solver's
                   `commitments` signal (schedule_engine._learning_signals).
  stability()      per person over STABILITY_WEEKS: committed shifts, how
                   many the restaurant later changed, by level — the owner's
                   report (studio_bench 'ladder') and each person's own
                   (staff /api/shifts carries each shift's level).

Nothing calls a model.
"""
import logging
import math
from datetime import date, timedelta

log = logging.getLogger(__name__)

LEVELS = ("firm", "likely", "tentative")
COMMIT_POINTS = {"firm": 100.0, "likely": 40.0, "tentative": 0.0}   # stability points (the whole dimension for a firm one)
STABILITY_WEEKS = 8
LEVEL_WORDS = {
    "firm": "Firm — needed even on a slow night. It changes only in an emergency.",
    "likely": "Likely — needed on a normal night. It could change if the night looks slow.",
    "tentative": "Tentative — added for an expected busy night (an event or holiday). It's the first to change "
                 "if that doesn't happen.",
}



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


def init_commitments(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS shift_commitments (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            date            TEXT    NOT NULL,
            name_key        TEXT    NOT NULL,
            shift_start     TEXT    NOT NULL,
            employee_name   TEXT    NOT NULL,
            role            TEXT,
            shift_end       TEXT,
            history_id      INTEGER,
            level           TEXT    NOT NULL,
            first_level     TEXT    NOT NULL,
            reason          TEXT,
            committed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            changed_at      TEXT,
            change          TEXT,
            PRIMARY KEY (restaurant_id, date, name_key, shift_start)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_hist ON shift_commitments(restaurant_id, history_id)")
        conn.commit()
    finally:
        conn.close()


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _minutes(v):
    from schedule_rules import parse_minutes
    return parse_minutes(v or "")


def _core_cover(row) -> int:
    """Minutes of the core service windows the row covers (the firmness
    order within a group: who covers the most of the rush is kept)."""
    import schedule_time as st
    sp = st.span(row)
    if sp is None:
        return 0
    s, e = sp
    return sum(max(0, min(e, hi) - max(s, lo)) for lo, hi in st.CORE_WINDOWS.values())


def _is_manager(restaurant_id, row, managers):
    k = _key(row.get("employee"))
    if k in managers:
        return True
    return "manager" in str(row.get("role") or "").lower() or "owner" in str(row.get("role") or "").lower()


def _managers(restaurant_id, db_path=None) -> set:
    try:
        import models
        r = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
        return {_key(n) for n in (models.salaried_keys(r) or [])}
    except Exception:
        return set()


def ladder(restaurant_id, rows, forecasts=None, db_path=None) -> dict:
    """{(date, person key, start): {"level", "reason"}} for `rows`.
    `forecasts` ({date: demand.forecast_day dict}) saves the reads."""
    import schedule_time as st
    from shift_quality import role_family
    forecasts = dict(forecasts or {})
    managers = _managers(restaurant_id, db_path)
    try:
        import models
        import schedule_rules
        fams = schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
    except Exception:
        fams = {}
    groups = {}
    out = {}
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        if len(d) != 10 or not (r.get("employee") or "").strip():
            continue
        k = (d, _key(r.get("employee")), str(r.get("shift_start") or "").strip())
        if _is_manager(restaurant_id, r, managers):
            out[k] = {"level": "firm", "reason": "a manager's shift — the manager-on-the-floor rule doesn't move with sales"}
            continue
        part = st.present_dayparts(r)[0]
        groups.setdefault((d, part, role_family(r.get("role") or "", fams)), []).append((k, r))
    for (d, part, fam), members in groups.items():
        if d not in forecasts:
            try:
                import demand
                forecasts[d] = demand.forecast_day(restaurant_id, date.fromisoformat(d),
                                                   db_path=db_path or _models_db())
            except Exception as e:
                log.warning("ladder forecast unavailable rid=%s %s: %s", restaurant_id, d, e)
                forecasts[d] = {"available": False}
        f = forecasts[d] or {}
        n = len(members)
        members.sort(key=lambda kr: (-_core_cover(kr[1]), -(_minutes(kr[1].get("shift_end")) or 0)
                                     + (_minutes(kr[1].get("shift_start")) or 0), kr[0][1]))
        if not f.get("available") or not f.get("low") or not f.get("typical_sales"):
            for k, _r in members:
                out[k] = {"level": "likely", "reason": "no forecast range for this date yet, so nothing is called firm"}
            continue
        typical = float(f["typical_sales"])
        low_ratio = min(1.0, float(f["low"]) / typical) if typical > 0 else 1.0
        n_low = max(1, int(math.ceil(n * low_ratio - 1e-9)))
        base = f.get("base_sales")
        n_base = n
        if base and f.get("effect_pct") and float(f["effect_pct"]) > 0 and typical > 0:
            n_base = max(n_low, int(math.ceil(n * float(base) / typical - 1e-9)))
        for i, (k, _r) in enumerate(members):
            if i < n_low:
                out[k] = {"level": "firm", "reason": f"needed even at the low end of the forecast (about ${float(f['low']):,.0f})"}
            elif i < n_base:
                out[k] = {"level": "likely", "reason": f"needed at the forecast (about ${typical:,.0f}), not at its low end"}
            else:
                what = ", ".join(str(x.get("display") or x.get("label") or "") for x in (f.get("effects") or [])
                                 if isinstance(x, dict)) or "a measured lift on the date"
                out[k] = {"level": "tentative", "reason": f"there for {what}; the first to change if it doesn't come"}
    return out


def _published(restaurant_id, history_id, db_path=None):
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT id, week_start, week_end, schedule_csv, published_at FROM schedule_history WHERE "
                            "id=? AND restaurant_id=? AND published_at IS NOT NULL", (int(history_id), restaurant_id)).fetchone()
    finally:
        conn.close()


def ensure(restaurant_id, history_id, today=None, db_path=None) -> dict:
    """Record a published week's commitments (module doc). Returns
    {committed, changed}. Days already past are left as they were."""
    from schedule_versions import rows_from_csv
    today = today or date.today()
    h = _published(restaurant_id, history_id, db_path)
    if not h:
        return {"committed": 0, "changed": 0}
    rows = [r for r in rows_from_csv(h["schedule_csv"] or "") if str(r.get("date") or "")[:10] >= today.isoformat()]
    conn = get_conn(db_path)
    try:
        have = {(r["date"], r["name_key"], r["shift_start"]): dict(r) for r in conn.execute(
            "SELECT * FROM shift_commitments WHERE restaurant_id=? AND date>=? AND date<=?",
            (restaurant_id, max(today.isoformat(), str(h["week_start"])[:10]), str(h["week_end"])[:10])).fetchall()}
    finally:
        conn.close()
    now_keys = {(str(r.get("date"))[:10], _key(r.get("employee")), str(r.get("shift_start") or "").strip()): r
                for r in rows if (r.get("employee") or "").strip()}
    new = {k: r for k, r in now_keys.items() if k not in have}
    levels = ladder(restaurant_id, list(now_keys.values()), db_path=db_path) if new else {}
    changed = []
    by_person_day = {}
    for k in now_keys:
        by_person_day.setdefault((k[0], k[1]), []).append(k)
    for k, c in have.items():
        if c.get("changed_at") or k in now_keys:
            if k in now_keys and not c.get("changed_at"):
                r = now_keys[k]
                if str(r.get("shift_end") or "").strip() != str(c.get("shift_end") or "").strip():
                    changed.append((k, "retimed"))
            continue
        moved = by_person_day.get((k[0], k[1]))
        changed.append((k, "moved" if moved else "removed"))
    conn = get_conn(db_path)
    try:
        for k, r in new.items():
            lv = levels.get(k) or {"level": "likely", "reason": ""}
            conn.execute("INSERT OR IGNORE INTO shift_commitments (restaurant_id, date, name_key, shift_start, "
                         "employee_name, role, shift_end, history_id, level, first_level, reason) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, k[0], k[1], k[2], r.get("employee"), r.get("role"), r.get("shift_end"),
                          int(history_id), lv["level"], lv["level"], lv["reason"][:300]))
        for k, how in changed:
            conn.execute("UPDATE shift_commitments SET changed_at=datetime('now'), change=? WHERE restaurant_id=? AND "
                         "date=? AND name_key=? AND shift_start=? AND changed_at IS NULL", (how, restaurant_id, *k))
        conn.commit()
    finally:
        conn.close()
    return {"committed": len(new), "changed": len(changed)}


def ensure_live(restaurant_id, today=None, db_path=None) -> dict:
    """ensure() for every live published week from today on (the nightly
    pass and the staff read)."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT h.id FROM schedule_history h WHERE h.restaurant_id=? AND h.published_at IS NOT NULL AND "
            "h.superseded_by IS NULL AND h.week_end>=? AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE "
            "nw.restaurant_id=h.restaurant_id AND nw.week_start=h.week_start AND nw.published_at IS NOT NULL AND "
            "nw.id>h.id)", (restaurant_id, today.isoformat())).fetchall()]
    finally:
        conn.close()
    tot = {"weeks": len(ids), "committed": 0, "changed": 0}
    for hid in ids:
        got = ensure(restaurant_id, hid, today=today, db_path=db_path)
        tot["committed"] += got["committed"]
        tot["changed"] += got["changed"]
    return tot


def levels_for(restaurant_id, since, until, db_path=None) -> dict:
    """{(date, person key, start): {level, first_level, reason, changed}}."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM shift_commitments WHERE restaurant_id=? AND date>=? AND date<=?",
                            (restaurant_id, since, until)).fetchall()
    finally:
        conn.close()
    return {(r["date"], r["name_key"], r["shift_start"]): {"level": r["level"], "first_level": r["first_level"],
                                                           "reason": r["reason"], "changed": r["change"]} for r in rows}


def committed_for_week(restaurant_id, week_dates, db_path=None) -> dict:
    """{(date, person key, start): level} of rows still as committed — the
    solver's `commitments` signal for a re-draft of a published week."""
    if not week_dates:
        return {}
    got = levels_for(restaurant_id, min(week_dates), max(week_dates), db_path=db_path)
    return {k: v["first_level"] for k, v in got.items() if not v.get("changed")}


def annotate(restaurant_id, shifts, name, db_path=None) -> list:
    """Each of one person's shifts with its level and the words for it (the
    staff read). A shift with no record yet gets none."""
    dates = [str(s.get("date") or "")[:10] for s in shifts or [] if s.get("date")]
    if not dates:
        return shifts or []
    got = levels_for(restaurant_id, min(dates), max(dates), db_path=db_path)
    k = _key(name)
    for s in shifts:
        start = str(s.get("shift_start") or s.get("start") or "").strip()
        lv = got.get((str(s.get("date") or "")[:10], k, start))
        if lv and not lv.get("changed"):
            s["commitment"] = lv["first_level"]
            s["commitment_text"] = LEVEL_WORDS[lv["first_level"]]
    return shifts


def change_cost(restaurant_id, before_rows, after_rows, db_path=None) -> dict:
    """A change to committed rows priced: {points, rows: [{employee, date,
    start, level, change}], predictability_pay: {...} | None, text}."""
    dates = sorted({str(r.get("date") or "")[:10] for r in (before_rows or []) if r.get("date")})
    if not dates:
        return {"points": 0.0, "rows": [], "predictability_pay": None, "text": None}
    committed = levels_for(restaurant_id, dates[0], dates[-1], db_path=db_path)
    after = {(str(r.get("date"))[:10], _key(r.get("employee")), str(r.get("shift_start") or "").strip()):
             str(r.get("shift_end") or "").strip() for r in after_rows or []}
    out = []
    for r in before_rows or []:
        k = (str(r.get("date"))[:10], _key(r.get("employee")), str(r.get("shift_start") or "").strip())
        lv = committed.get(k)
        if not lv or lv.get("changed"):
            continue
        if k not in after:
            how = "removed or moved"
        elif after[k] != str(r.get("shift_end") or "").strip():
            how = "retimed"
        else:
            continue
        out.append({"employee": r.get("employee"), "date": k[0], "start": k[2], "level": lv["first_level"], "change": how})
    points = sum(COMMIT_POINTS[x["level"]] for x in out)
    pay = None
    try:
        import importlib
        pp = importlib.import_module("predictability_pay")
        pay = pp.preview(restaurant_id, before_rows, after_rows, db_path=db_path)
    except ImportError:
        pay = None
    except Exception as e:
        log.warning("predictability pay preview failed rid=%s: %s", restaurant_id, e)
    firm = [x for x in out if x["level"] == "firm"]
    text = None
    if out:
        text = (f"This changes {len(out)} shift{'s' if len(out) != 1 else ''} already published"
                + (f", {len(firm)} of them firm" if firm else "")
                + (f" — {(pay or {}).get('text')}" if (pay or {}).get("text") else "") + ".")
    return {"points": points, "rows": out, "predictability_pay": pay, "text": text}


def stability(restaurant_id, today=None, weeks=STABILITY_WEEKS, name=None, db_path=None) -> dict:
    """{person: {committed, changed, kept_share, by_level: {level: [n, changed]}}}
    over the last `weeks` weeks of dates already worked (the promise kept or
    not). `name` limits it to one person."""
    today = today or date.today()
    since = (today - timedelta(weeks=int(weeks))).isoformat()
    conn = get_conn(db_path)
    try:
        q, args = "SELECT * FROM shift_commitments WHERE restaurant_id=? AND date>=? AND date<?", [restaurant_id, since,
                                                                                                today.isoformat()]
        if name:
            q += " AND name_key=?"
            args.append(_key(name))
        rows = conn.execute(q, args).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        e = out.setdefault(r["employee_name"], {"committed": 0, "changed": 0, "by_level": {lv: [0, 0] for lv in LEVELS}})
        e["committed"] += 1
        e["by_level"][r["first_level"]][0] += 1
        if r["changed_at"]:
            e["changed"] += 1
            e["by_level"][r["first_level"]][1] += 1
    for e in out.values():
        e["kept_share"] = round(1 - e["changed"] / float(e["committed"]), 3) if e["committed"] else None
    return out


def owner_view(restaurant_id, history_id=None, today=None, db_path=None) -> dict:
    """The Studio's read: the week on screen's ladder (counts per level per
    date) and every person's stability over STABILITY_WEEKS."""
    from time_utils import mdy
    today = today or date.today()
    week = {}
    if history_id:
        ensure(restaurant_id, history_id, today=today, db_path=db_path)
        h = _published(restaurant_id, history_id, db_path)
        if h:
            got = levels_for(restaurant_id, str(h["week_start"])[:10], str(h["week_end"])[:10], db_path=db_path)
            for (d, _k, _s), v in got.items():
                e = week.setdefault(d, {lv: 0 for lv in LEVELS})
                e[v["first_level"]] += 1
    stab = stability(restaurant_id, today=today, db_path=db_path)
    people = sorted(({"name": n, **v} for n, v in stab.items()), key=lambda p: (p["kept_share"] or 0, p["name"]))
    return {"ok": True, "week": [{"date": d, "label": mdy(d), **v} for d, v in sorted(week.items())],
            "people": people, "weeks": STABILITY_WEEKS, "levels": LEVEL_WORDS,
            "basis": "Firm: needed even at the low end of the night's forecast. Likely: needed at the forecast. "
                     "Tentative: there for an expected lift (an event or holiday). Stability: the share of each "
                     f"person's published shifts over the last {STABILITY_WEEKS} weeks the restaurant did not change."}
