"""
canonical_facts.py — one reader per stored fact, the one every learner reads
through (memory audit 9/29/26: canonical_facts, net_basis, imported_year).

Each learning reader used to re-implement the platform's own data rules, and
several missed one: a half-night synced mid-service stayed in every median
and tracker window, every restaurant without intraday data was recorded as
doing 40% of its sales before 3pm, reviews Google later removed kept
dragging the rating slope, a weather FORECAST was typed as a measurement,
three writers counted reach as reach plus impressions, and the two nightly
"net sales" — the nightly report's own net and the POS sync's daily total —
were compared as one. The rules live here, once:

  final days       FINAL_SQL / final_sql(alias): a night the POS had not
                   closed when it was read (final=0) is provisional, never a
                   data point; an older row with no flag counts as final; a
                   converted demo's seeded day (source 'seed') is none
                   (models.own_history_sql)
  nightly net      net_series(): the ONE nightly net, per business date,
                   with its basis and where it came from. Both raw figures
                   are kept forever where they already live — the report's
                   own net in dsr_metrics (`sales.net`), the POS sync's daily
                   total in labor_daily_history (`sales`, with the provider
                   that built it) — and neither is in the retention registry
                   (tests/test_mem_m5_canonical_facts.py pins it). A figure's
                   BASIS says which way it was counted:
                     dsr_net          the nightly report's net (items after
                                      discounts and comps, no tax), the
                                      owner's imported DSR workbook, or a POS
                                      whose daily total is built the same way
                                      (dsr.store.POS_SYNC_SAME_BASIS: RPOWER)
                     pos_daily_total  any other POS's own daily total (Toast's
                                      businessDay netSales: service charges and
                                      refunds may be counted differently)
                   Two figures are compared only on one basis.
  last year        last_year_net() / sales_history(): the DSR's baseline
                   order — the night's report, then the owner's imported
                   workbook, then the POS sync — each figure naming its
                   source, so an imported year reaches every learner
  live reviews     LIVE_REVIEWS_SQL and REVIEW_AXIS (models.
                   REVIEW_TIME_AXIS_BARE): reviews not removed, on the one
                   time axis
  reach            REACH_MEASURED_SQL (marketing_signals): reach is reach,
                   never reach plus impressions, and an unmeasured post is
                   blank, never 0
  watched nights   watched_nights(): nights a clean staffing record means
                   something — the clock-in check ran
  published weeks  published_weeks(): distinct weeks with a published
                   schedule, and whether a manager edited before publishing
  kinds            KINDS: what a stored figure is — measured, forecast,
                   estimate, assumed, seeded

tests/test_mem_m5_canonical_facts.py holds every learner (metrics,
intelligence/, demand, schedule_intel, schedule_economics, forecast_log,
dsr/) to these rules from the source, in the style of
tests/test_readiness_adoption.py.
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH, REVIEW_TIME_AXIS_BARE


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# ── what a stored figure is ────────────────────────────────────────────────

MEASURED, FORECAST, ESTIMATE, ASSUMED, SEEDED = "measured", "forecast", "estimate", "assumed", "seeded"
KINDS = (MEASURED, FORECAST, ESTIMATE, ASSUMED, SEEDED)


# ── final days ─────────────────────────────────────────────────────────────

# A final day that is the restaurant's OWN: a converted demo's seeded days
# (source 'seed', models.own_history_sql) are no data point for any learner —
# its year-over-year, holiday lift, medians and features read synthetic sales
# forever otherwise (memory fix round INT #21, M7's rule folded into the one
# final-day predicate every daily-history reader uses). A demo keeps its own
# seeded days: they are what the demo shows.
FINAL_SQL = "COALESCE(final, 1) != 0 AND " + _models_mod.own_history_sql()


def final_sql(alias=None) -> str:
    """The final-day predicate, qualified by a table alias in a join."""
    return (f"COALESCE({alias}.final, 1) != 0 AND {_models_mod.own_history_sql(alias)}" if alias
            else FINAL_SQL)


def final_days(restaurant_id, start, end, db_path=None, weekday=None) -> list:
    """[{date, day_of_week, sales, labor_cost, labor_pct, total_hours,
    provider, source}] — labor_daily_history's FINAL days in [start, end],
    oldest first. `weekday` ("Tuesday") narrows it."""
    sql = ("SELECT date, day_of_week, sales, labor_cost, labor_pct, total_hours, provider, source "
           f"FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<=? AND {FINAL_SQL}")
    args = [restaurant_id, str(start)[:10], str(end)[:10]]
    if weekday:
        sql += " AND day_of_week=?"
        args.append(weekday)
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY date", args).fetchall()]
    finally:
        conn.close()


# ── live reviews and reach ─────────────────────────────────────────────────

LIVE_REVIEWS_SQL = "deleted_at IS NULL"
REVIEW_AXIS = REVIEW_TIME_AXIS_BARE


def reach_measured_sql() -> str:
    """marketing_signals.REACH_MEASURED_SQL — the one definition of a post
    whose reach was measured (reach alone; never reach plus impressions)."""
    from marketing_signals import REACH_MEASURED_SQL
    return REACH_MEASURED_SQL


# ── the nightly net, by basis ──────────────────────────────────────────────

BASIS_DSR = "dsr_net"
BASIS_POS = "pos_daily_total"
BASES = (BASIS_DSR, BASIS_POS)


def pos_basis(provider):
    """The basis a POS daily total from `provider` is on: BASIS_DSR for a POS
    whose daily total is built as the report's net (RPOWER), BASIS_POS for
    any other, None when the provider is unknown (an upload with no POS
    behind it): unknown is never called a different figure."""
    if not provider:
        return None
    from dsr.store import pos_sync_same_basis
    return BASIS_DSR if pos_sync_same_basis(provider) else BASIS_POS


def _current_provider(restaurant_id):
    try:
        import pos
        return pos.connected_provider(restaurant_id)[0]
    except Exception:
        return None


# pos: which POS-sync nights net_series may use.
POS_NONE, POS_SAME_BASIS, POS_ALL = "none", "basis", "all"


def net_series(restaurant_id, start=None, end=None, db_path=None, pos=POS_SAME_BASIS, dates=None) -> dict:
    """{iso date: {"net", "basis", "source", "provider"}} for every night in
    [start, end] (or exactly `dates`) that has a figure, in the DSR's
    baseline order:

      dsr      the night's own report (dsr_metrics `sales.net`, the latest
               finished version) — BASIS_DSR
      import   the owner's imported DSR workbook (dsr_history_import) —
               BASIS_DSR: it is the owner's own nightly sheet
      pos_sync the POS sync's daily total (labor_daily_history), FINAL nights
               with a positive figure only; its basis from the provider that
               built it (the row's own, else the restaurant's POS now)

    `pos`: POS_SAME_BASIS (default) takes a POS night only when it is on the
    report's basis or its basis is unknown, so every figure returned can be
    set beside the report's net; POS_ALL takes every final POS night, tagged
    with its basis (the long-span readers — seasonality, last year — which
    name their sources); POS_NONE never. A night none of them has is absent,
    never 0."""
    if dates is not None:
        isos = sorted({(d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]) for d in dates if d})
        if not isos:
            return {}
        where, args = f"IN ({','.join('?' for _ in isos)})", list(isos)
    else:
        where, args = "BETWEEN ? AND ?", [str(start)[:10], str(end)[:10]]
    out = {}
    conn = get_conn(db_path)
    try:
        try:
            dsr = {str(r["business_date"])[:10]: (float(r["value"]), r["source"]) for r in conn.execute(
                "SELECT business_date, value, source FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
                f"AND value IS NOT NULL AND business_date {where}", (restaurant_id, *args)).fetchall()}
        except Exception:
            dsr = {}
        try:
            imported = {str(r["business_date"])[:10]: float(r["net"]) for r in conn.execute(
                "SELECT business_date, net FROM dsr_history_import WHERE restaurant_id=? AND net IS NOT NULL "
                f"AND business_date {where}", (restaurant_id, *args)).fetchall()}
        except Exception:
            imported = {}
        synced = {}
        if pos != POS_NONE:
            try:
                synced = {str(r["date"])[:10]: (float(r["sales"]), r["provider"]) for r in conn.execute(
                    f"SELECT date, sales, provider FROM labor_daily_history WHERE restaurant_id=? AND date {where} "
                    f"AND sales IS NOT NULL AND sales > 0 AND {FINAL_SQL}", (restaurant_id, *args)).fetchall()}
            except Exception:
                synced = {}
    finally:
        conn.close()
    current = _current_provider(restaurant_id) if synced else None
    for d, (v, provider) in dsr.items():
        out[d] = {"net": v, "basis": BASIS_DSR, "source": "dsr", "provider": provider}
    for d, v in imported.items():
        if d not in out:
            out[d] = {"net": v, "basis": BASIS_DSR, "source": "import", "provider": None}
    for d, (v, provider) in synced.items():
        if d in out:
            continue
        prov = provider or current
        # Unknown (no provider on the row or on the restaurant) is never
        # called a different figure.
        basis = pos_basis(prov) or BASIS_DSR
        if pos == POS_SAME_BASIS and basis == BASIS_POS:
            continue
        out[d] = {"net": v, "basis": basis, "source": "pos_sync", "provider": prov}
    return dict(sorted(out.items()))


def sales_history(restaurant_id, start, end, db_path=None) -> dict:
    """net_series with every final POS night (POS_ALL): the long-span reader
    for seasonality, holiday lift and year-over-year context — an imported
    last year and the nights since, each naming its source and basis."""
    return net_series(restaurant_id, start, end, db_path=db_path, pos=POS_ALL)


def one_basis(series) -> dict:
    """The series on ONE basis: where a span holds figures on both, the
    basis with more nights (the report's own net on a tie) — never two ways
    of counting in one comparison."""
    by = {}
    for d, x in (series or {}).items():
        by.setdefault(x.get("basis") or BASIS_DSR, {})[d] = x
    if len(by) <= 1:
        return dict(series or {})
    keep = max(by, key=lambda b: (len(by[b]), b == BASIS_DSR))
    return by[keep]


def last_year_day(restaurant, day):
    """The night a year back that `day` is compared with (dsr.store.
    last_year_day: the same fiscal week and weekday last fiscal year where
    the restaurant keeps a calendar, else the same weekday 364 days back)."""
    try:
        from dsr.store import last_year_day as _ly
        return _ly(restaurant, day)
    except Exception:
        d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        return d - timedelta(days=364)


def last_year_net(restaurant_id, day, restaurant=None, db_path=None):
    """{"date", "net", "source", "basis"} for the night a year back from
    `day`, in the DSR's baseline order (report, import, POS sync) — or None.
    The one last-year reader (imported_year): an owner who imported a year
    of DSR workbooks has last year, whatever the POS archive holds."""
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    ly = last_year_day(restaurant, day)
    if ly is None:
        return None
    got = sales_history(restaurant_id, ly, ly, db_path=db_path).get(ly.isoformat())
    return dict(got, date=ly.isoformat()) if got else None


SOURCE_LABELS = {"dsr": "your nightly reports", "import": "your imported DSR workbooks",
                 "pos_sync": "the POS sync"}


def sources_said(series) -> str:
    """"your imported DSR workbooks (212 nights), the POS sync (60 nights)" —
    what a figure built from a series rests on."""
    n = {}
    for x in (series or {}).values():
        n[x.get("source")] = n.get(x.get("source"), 0) + 1
    order = ("dsr", "import", "pos_sync")
    return ", ".join(f"{SOURCE_LABELS.get(s, s)} ({n[s]} night{'s' if n[s] != 1 else ''})"
                     for s in order if n.get(s))


# ── watched nights and published weeks ─────────────────────────────────────

def watched_nights(restaurant_id, start, end, db_path=None) -> set:
    """ISO dates in [start, end] on which a clean staffing record means
    something: the live clock-in check ran during that night's service
    (dsr_coverage_runs), or — for nights before that record existed — the
    coverage check was possible and a live POS reading was taken that day
    (schedule_intel.watched_dates). A night nobody watched is unknown, never
    clean."""
    s, e = str(start)[:10], str(end)[:10]
    out = set()
    conn = get_conn(db_path)
    try:
        try:
            out |= {str(r["business_date"])[:10] for r in conn.execute(
                "SELECT business_date FROM dsr_coverage_runs WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
                (restaurant_id, s, e)).fetchall()}
        except Exception:
            pass
    finally:
        conn.close()
    try:
        import schedule_intel
        out |= set(schedule_intel.watched_dates(restaurant_id, s, e, db_path=db_path or DB_PATH))
    except Exception:
        pass
    return out


def published_weeks(restaurant_id, since, db_path=None, until=None) -> list:
    """[{"week_start", "history_id", "published_at", "adjusted"}] — one per
    distinct week with a published schedule since `since`, its latest
    published version. `adjusted` is True only when a manager saved an
    EDITED version of that week before it was published: a regenerated
    draft is not an adjustment, and a staff swap after publishing is not a
    manager's edit (QUALITY-20). `until` (a date) reads the record as it
    stood at the end of that day — weeks starting by then, versions
    published by then — for a past week's features."""
    s = str(since)[:10]
    bound, args = "", [restaurant_id, s]
    inner_bound = ""
    if until:
        u = str(until)[:10]
        bound = " AND week_start <= ? AND published_at <= ?"
        inner_bound = " AND nw.published_at <= ?"
        args += [u, u + " 23:59:59"]
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, week_start, published_at FROM schedule_history h WHERE restaurant_id=? "
            "AND published_at IS NOT NULL AND week_start >= ?" + bound + " AND NOT EXISTS (SELECT 1 FROM "
            "schedule_history nw WHERE nw.restaurant_id=h.restaurant_id AND nw.week_start=h.week_start "
            "AND nw.published_at IS NOT NULL AND nw.id > h.id" + inner_bound + ") ORDER BY week_start",
            tuple(args + ([str(until)[:10] + " 23:59:59"] if until else []))).fetchall()
        out = []
        for r in rows:
            edited = False
            try:
                edited = bool(conn.execute(
                    "SELECT 1 FROM schedule_versions WHERE history_id=? AND reason='edited' "
                    "AND (? IS NULL OR created_at <= ?) LIMIT 1",
                    (r["id"], r["published_at"], r["published_at"])).fetchone())
            except Exception:
                edited = False
            out.append({"week_start": str(r["week_start"])[:10], "history_id": r["id"],
                        "published_at": r["published_at"], "adjusted": edited})
        return out
    except Exception:
        return []
    finally:
        conn.close()
