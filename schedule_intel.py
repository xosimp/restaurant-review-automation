"""
schedule_intel.py — what the schedule learns from its own record.

Everything here is per-restaurant, read from first-party rows, and
rendered as facts the prompt and the quality engine can use. Nothing calls
a model; nothing crosses a tenant.

  record_outcomes       what each PUBLISHED week actually did, by date and
                        daypart: hours, sales, labor %, issues, review rating
  outcome_block         "last time this pattern ran" for the prompt
  fairness_ledger       weekends, closes and holidays per person over 8 weeks
  rotation_plan         who is next for a weekend off, a close and a holiday,
                        per role, planned across those weeks
  behaviour_preferences what people keep dropping and claiming
  mentoring             shifts worked beside a closer in a role that is not
                        their own — who could hold a station
  chemistry_suggestions pairs whose shared dayparts ran clean — suggested,
                        never applied
  recommendation events an accept/dismiss ledger, and the kinds nobody takes
  learned-pattern dismissals — the owner's say over what the draft learns
"""
import json
import re
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH
from canonical_facts import FINAL_SQL


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports). A
    bound copy — and a db_path default bound to DB_PATH — sent a test's
    patched models.get_conn to the real database. The module's own DB_PATH
    default means "whatever models uses now"."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

OUTCOME_WEEKS = 12
LEDGER_WEEKS = 8
MENTOR_SHIFTS_TO_HOLD = 8
MENTOR_WINDOW_DAYS = 365          # the shifts that count toward holding a station
SUGGEST_MIN_SHARED = 6
SUGGEST_MIN_CLEAN = 0.8
SUPPRESS_AFTER_SHOWN = 10


def _hours(r):
    try:
        return float(r.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _daypart(r):
    from schedule_rules import daypart_of
    return daypart_of(r.get("shift_start", ""))


# ── outcomes per published week ───────────────────────────────────────────

def record_outcomes(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """For every published week that has ended, one row per date and
    daypart: scheduled hours and people (from the published CSV — the PLAN),
    and beside them what HAPPENED (schedule audit 10/3/26 L-12 — the record
    used to be the plan, so calibration and chemistry learned from planned
    staffing): `actual_hours` and `actual_people` from the punches
    (shift_facts — a shift is the daypart its clock-in falls in, as a
    planned one is; the salaried don't clock in, so their planned shifts
    count as worked), the shifts missed, late, left early and run past their
    end; sales (labor_daily_history's FINAL day, split by this restaurant's
    MEASURED morning share — _morning_share), the DAY's labor % (`labor_pct`
    — labor_daily_history's, the same on both rows: a day-level figure) and
    the DAYPART's (`labor_pct_daypart`, L-13: the punched hours priced at
    each punch's rate, split at the 3pm line the sales split at, over that
    daypart's measured sales — NULL when the split or the punches are not
    there, never the day's copied); coverage and no-show issues on that
    date, and the mean review rating dated that day. What was measured goes
    into the observation log too (schedule_memory: actual_hours,
    coverage_gap_actual, labor_vs_target_daypart). Idempotent: rows are
    keyed by (history_id, date, daypart).

    A day whose morning share was never measured has NULL daypart sales
    and split_basis 'unmeasured' (memory audit 9/29/26, QUALITY-11): the
    split used to fall back to a stated 0.4, so every restaurant without
    intraday data was recorded as doing 40% of its sales before 3pm — a
    dinner-only bar included — and peer benchmarks and the schedule
    prompt's sales per labor hour read it as measured."""
    from schedule_versions import rows_from_csv
    today = today or date.today()
    conn = get_conn(db_path)
    written = 0
    pending = []
    try:
        weeks = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv, labor_target FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
            "AND week_end < ? ORDER BY id DESC LIMIT ?", (restaurant_id, today.isoformat(), OUTCOME_WEEKS)).fetchall()
        if not weeks:
            return {"written": 0}
        share = _morning_share(conn, restaurant_id)
        # Issues are stamped in UTC; the schedule's dates and dayparts are
        # the restaurant's own. A 7pm Central no-show is 00:00 UTC the next
        # day: matched on the UTC date it landed on the wrong day and was
        # counted against both dayparts.
        try:
            from models import get_restaurant as _gr
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI((getattr(_gr(restaurant_id), "timezone", None) or "America/Chicago"))
        except Exception:
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI("America/Chicago")
        issue_at = {}
        try:
            for (created,) in conn.execute(
                    "SELECT created_at FROM ops_issues WHERE restaurant_id=? AND kind IN ('coverage','no_show') "
                    "AND created_at >= date(?, '-2 days')",
                    (restaurant_id, min(w["week_start"] for w in weeks if w["week_start"]))).fetchall():
                try:
                    utc = datetime.fromisoformat(str(created).replace("Z", "")).replace(tzinfo=_ZI("UTC"))
                except ValueError:
                    continue
                local = utc.astimezone(_tz)
                key = (local.strftime("%Y-%m-%d"), "morning" if local.hour < 15 else "night")
                issue_at[key] = issue_at.get(key, 0) + 1
        except Exception as _ix:
            print(f"[outcomes] issues unavailable for restaurant {restaurant_id}: {_ix}")
        actuals = _Actuals(restaurant_id, db_path)
        for w in weeks:
            rows = rows_from_csv(w["schedule_csv"])
            by = {}
            for r in rows:
                part = _daypart(r)
                if part == "unknown" or not r.get("date"):
                    continue
                e = by.setdefault((r["date"], part), {"hours": 0.0, "people": set()})
                e["hours"] += _hours(r)
                e["people"].add(r["employee"])
            placed = _place_reviews(conn, restaurant_id, by, _tz)
            worked = actuals.week(w["week_start"], w["week_end"], rows)
            for (d, part), e in by.items():
                day = conn.execute("SELECT sales, labor_pct, day_of_week FROM labor_daily_history WHERE restaurant_id=? "
                                   f"AND date=? AND {FINAL_SQL}", (restaurant_id, d)).fetchone()
                sales, split_basis = None, None
                if day and day["sales"]:
                    try:
                        wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
                    except ValueError:
                        wd = day["day_of_week"]
                    s = share.get(wd)
                    if s is None:
                        split_basis = "unmeasured"
                    else:
                        split_basis = "measured"
                        sales = round(float(day["sales"]) * (s if part == "morning" else 1 - s), 0)
                a = worked.get((d, part)) or {}
                labor_part = None
                if sales and a.get("labor_cost") is not None:
                    labor_part = round(a["labor_cost"] / float(sales) * 100.0, 1)
                issues = issue_at.get((d, part), 0)
                # Each review on the shift it was about (_place_reviews): the
                # meal the analyser read in it, posted within
                # REVIEW_LAG_DAYS — a Sunday-afternoon review of Saturday's
                # dinner is Saturday night's, not Sunday lunch's — else, for
                # want of anything better, the posting day's busiest daypart.
                got = placed.get((d, part)) or []
                ratings = [g["rating"] for g in got]
                rating = round(sum(ratings) / len(ratings), 2) if ratings else None
                n_reviews = len(ratings)
                attributed = [g["rating"] for g in got if g["how"] == "daypart"]
                kinds = {g["how"] for g in got}
                attribution = (next(iter(kinds)) if len(kinds) == 1 else ("mixed" if kinds else None))
                conn.execute(
                    "INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, sales, labor_pct, issues, "
                    "review_rating, reviews, review_attribution, review_rating_attributed, reviews_attributed, review_lag_days, "
                    "split_basis, actual_hours, actual_people, actual_basis, missed, late, left_early, stayed_late, "
                    "labor_cost_daypart, labor_pct_daypart) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(history_id, date, daypart) DO UPDATE SET "
                    "hours=excluded.hours, people=excluded.people, sales=excluded.sales, labor_pct=excluded.labor_pct, "
                    "issues=excluded.issues, review_rating=excluded.review_rating, reviews=excluded.reviews, "
                    "review_attribution=excluded.review_attribution, "
                    "review_rating_attributed=excluded.review_rating_attributed, "
                    "reviews_attributed=excluded.reviews_attributed, review_lag_days=excluded.review_lag_days, "
                    "split_basis=excluded.split_basis, actual_hours=excluded.actual_hours, "
                    "actual_people=excluded.actual_people, actual_basis=excluded.actual_basis, missed=excluded.missed, "
                    "late=excluded.late, left_early=excluded.left_early, stayed_late=excluded.stayed_late, "
                    "labor_cost_daypart=excluded.labor_cost_daypart, labor_pct_daypart=excluded.labor_pct_daypart, "
                    "recorded_at=datetime('now')",
                    (restaurant_id, w["id"], d, part, round(e["hours"], 1), len(e["people"]), sales,
                     (day["labor_pct"] if day else None), issues, rating, n_reviews, attribution,
                     round(sum(attributed) / len(attributed), 2) if attributed else None, len(attributed),
                     max((g["lag"] for g in got), default=None), split_basis,
                     a.get("hours"), a.get("people"), a.get("basis"), a.get("missed"), a.get("late"),
                     a.get("left_early"), a.get("stayed_late"), a.get("labor_cost"), labor_part))
                written += 1
                if a.get("basis"):
                    pending.append((w, d, part, e, a, labor_part, sales))
        conn.commit()
    finally:
        conn.close()
    # What was measured, into the observation log (schedule_memory — after
    # the outcome rows are committed, on its own connections).
    for w, d, part, e, a, labor_part, sales in pending:
        _observe_actuals(restaurant_id, w, d, part, e, a, labor_part, sales, db_path)
    return {"written": written}


class _Actuals:
    """What each date and daypart of a published week actually had, from
    the punches (schedule audit 10/3/26 L-12, L-13): {(date, daypart):
    {hours, people, basis, missed, late, left_early, stayed_late,
    labor_cost, plan_by_family, worked_by_family}}. A date with no punch at
    all is not a date anybody measured: it is absent (unknown), never 0.

      hours / people   punched hours and people of the shifts whose clock-in
                       falls in the daypart (the way a planned shift is
                       placed), plus the salaried people's planned shifts —
                       they don't clock in, so their plan is their record
      labor_cost       the punched hours priced at each punch's own pay, the
                       person's rate, the role's (labor._shift_rate — the
                       labor analysis's one chain), split at the 3pm line the
                       daypart sales are split at; hourly only, as the day's
                       labor % is (labor_daily_history)
      missed / late / left_early   attendance_events on those shifts
      stayed_late      published shifts whose punch ran
                       schedule_memory.STAYED_LATE_MINUTES or more past
                       their end"""

    def __init__(self, restaurant_id, db_path=DB_PATH):
        import models
        self.rid = restaurant_id
        self.db = None if db_path in (None, DB_PATH) else db_path
        self.restaurant = models.get_restaurant(restaurant_id, db_path or DB_PATH)
        try:
            self.salaried = {models.salaried_name_key(x["name"]) for x in models.salaried_staff(self.restaurant)}
        except Exception:
            self.salaried = set()
        try:
            self.role_rates = models.get_role_rates(restaurant_id) or {}
        except Exception:
            self.role_rates = {}
        self.fallback = float(getattr(self.restaurant, "hourly_rate", None) or self.role_rates.get("_default") or 0)
        try:
            self.person_rates = models.person_rates(self.restaurant)
        except Exception:
            self.person_rates = {}
        try:
            import schedule_rules
            self.families = schedule_rules.role_families(self.restaurant) or {}
        except Exception:
            self.families = {}

    def _fam(self, role):
        from shift_quality import role_family
        return role_family(role, self.families)

    def week(self, week_start, week_end, plan_rows) -> dict:
        import labor
        import shift_facts
        import schedule_memory as _sm
        from schedule_rules import daypart_of
        from models import salaried_name_key
        punches = [p for p in shift_facts.rows(self.rid, since=week_start, until=week_end, db_path=self.db)
                   if p.get("actual_hours") not in (None, "")
                   and salaried_name_key(p.get("employee")) not in self.salaried]
        if not punches:
            return {}
        _people, role_typical = labor.rate_book(punches, self.person_rates)
        measured = {str(p["date"])[:10] for p in punches}
        out = {}

        def slot(d, part):
            return out.setdefault((d, part), {"hours": 0.0, "people": set(), "basis": "punches", "missed": 0,
                                              "late": 0, "left_early": 0, "stayed_late": 0, "labor_cost": 0.0,
                                              "plan_by_family": {}, "worked_by_family": {}})
        for p in punches:
            d = str(p["date"])[:10]
            part = daypart_of(p.get("shift_start") or "")
            try:
                hours = float(p.get("actual_hours") or 0)
            except (TypeError, ValueError):
                continue
            if part == "unknown":
                continue
            e = slot(d, part)
            e["hours"] += hours
            e["people"].add(salaried_name_key(p.get("employee")))
            e["worked_by_family"].setdefault(self._fam(p.get("role")), set()).add(salaried_name_key(p.get("employee")))
            rate = labor._shift_rate(p, self.role_rates, self.fallback, self.person_rates, role_typical)
            s, t = _sm._minutes(p.get("shift_start")), _sm._minutes(p.get("shift_end"))
            split = 15 * 60
            if s is None or t is None or part == "night" and s < split:
                slot(d, part)["labor_cost"] += hours * rate
                continue
            if t <= s:
                t += 24 * 60
            span = float(t - s) or 1.0
            early = max(0, min(t, split) - s) / span
            if early:
                slot(d, "morning")["labor_cost"] += hours * early * rate
            if early < 1:
                slot(d, "night")["labor_cost"] += hours * (1 - early) * rate
        by_person = {}
        for p in punches:
            by_person.setdefault((str(p["date"])[:10], salaried_name_key(p.get("employee"))), []).append(p)
        for r in plan_rows or []:
            d = str(r.get("date") or "")[:10]
            part = daypart_of(r.get("shift_start") or "")
            if d not in measured or part == "unknown":
                continue
            k = salaried_name_key(r.get("employee"))
            e = slot(d, part)
            e["plan_by_family"].setdefault(self._fam(r.get("role")), set()).add(k)
            if k in self.salaried:
                e["hours"] += _hours(r)
                e["people"].add(k)
                e["worked_by_family"].setdefault(self._fam(r.get("role")), set()).add(k)
                continue
            mine = _sm.match_punch(r, by_person.get((d, k)))
            over = _sm.minutes_past_end(r, mine) if mine else None
            if over is not None and over >= _sm.STAYED_LATE_MINUTES:
                e["stayed_late"] += 1
        conn = get_conn(self.db)
        try:
            for a in conn.execute("SELECT business_date, shift_start, outcome FROM attendance_events WHERE "
                                  "restaurant_id=? AND business_date BETWEEN ? AND ?",
                                  (self.rid, week_start, week_end)).fetchall():
                d, part = str(a["business_date"])[:10], daypart_of(a["shift_start"] or "")
                if (d, part) not in out:
                    continue
                if a["outcome"] in ("no_show", "called_out"):
                    out[(d, part)]["missed"] += 1
                elif a["outcome"] in ("late", "left_early"):
                    out[(d, part)][a["outcome"]] += 1
        except Exception as e:
            print(f"[outcomes] attendance unavailable for restaurant {self.rid}: {e}")
        finally:
            conn.close()
        for e in out.values():
            e["hours"] = round(e["hours"], 1)
            e["people"] = len(e["people"])
            e["labor_cost"] = round(e["labor_cost"], 2)
        return out


def _observe_actuals(restaurant_id, w, d, part, e, a, labor_part, sales, db_path=DB_PATH):
    """What one slot measured, into the observation log (schedule_memory,
    phase as_run): the plan beside the punches, each role family the
    punches left short of the plan, the daypart's labor % against the
    week's target. Keyed by the slot, so the Monday re-read replaces it."""
    import schedule_memory as _sm
    db = None if db_path in (None, DB_PATH) else db_path
    common = dict(week_start=w["week_start"], date=d, daypart=part, history_id=w["id"], origin="system",
                  phase="as_run", authority="system", source="punches", db_path=db)
    _sm.observe(restaurant_id, "actual_hours", value={
        "planned_hours": round(e["hours"], 1), "actual_hours": a.get("hours"), "planned_people": len(e["people"]),
        "actual_people": a.get("people"), "missed": a.get("missed"), "late": a.get("late"),
        "left_early": a.get("left_early"), "stayed_late": a.get("stayed_late")},
        fact_key=f"actual_hours|{d}|{part}", **common)
    for fam, planned in (a.get("plan_by_family") or {}).items():
        worked = len((a.get("worked_by_family") or {}).get(fam) or ())
        if fam and worked < len(planned):
            _sm.observe(restaurant_id, "coverage_gap_actual", role=fam,
                        value={"planned": len(planned), "worked": worked}, fact_key=f"coverage_gap|{d}|{part}|{fam}",
                        **common)
    if labor_part is not None:
        _sm.observe(restaurant_id, "labor_vs_target_daypart", value={
            "labor_pct": labor_part, "target": w["labor_target"], "labor_cost": a.get("labor_cost"),
            "sales": sales}, fact_key=f"labor_daypart|{d}|{part}", **common)


MORNING_SPLIT_HOUR = 15        # schedule_rules.daypart_of: a shift starting at 3pm or later is "night"
MORNING_SHARE_MIN_DAYS = 3


# A review is placed on a shift at most this many days before it was posted
# (memory audit 9/29/26, reviews_to_labor).
REVIEW_LAG_DAYS = 2
# When a daypart's service has begun: a dinner review posted at 2pm is about
# an EARLIER dinner.
_SERVICE_STARTS = {"morning": 11 * 60, "night": 17 * 60}


def _place_reviews(conn, restaurant_id, by, tz):
    """{(date, daypart): [{"rating", "how", "lag"}]} — each review posted
    during this week's shifts (or up to REVIEW_LAG_DAYS after) on the one
    shift it was about. With the analyser's daypart: the latest recorded
    shift of that daypart that had begun by the time it was posted, no more
    than REVIEW_LAG_DAYS before ("daypart"). Without one: the posting day's
    busiest daypart, as before ("hours") — which calibration leaves out."""
    import staffing_signals
    from zoneinfo import ZoneInfo as _ZI
    if not by:
        return {}
    dates = sorted({d for d, _p in by})
    lo, hi = dates[0], (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=REVIEW_LAG_DAYS)).strftime("%Y-%m-%d")
    try:
        rows = conn.execute("SELECT rating, review_date, entities FROM reviews WHERE restaurant_id=? AND "
                            "deleted_at IS NULL AND substr(review_date,1,10) BETWEEN ? AND ?",
                            (restaurant_id, lo, hi)).fetchall()
    except Exception as _rx:
        print(f"[outcomes] reviews unavailable for restaurant {restaurant_id}: {_rx}")
        return {}
    main = {}
    for (d, p), e in by.items():
        if d not in main or e["hours"] > by[(d, main[d])]["hours"]:
            main[d] = p
    out = {}
    for r in rows:
        raw = str(r["review_date"] or "")
        try:
            if len(raw) > 10 and ("T" in raw or " " in raw):
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00")[:25])
                if stamp.tzinfo is not None:
                    stamp = stamp.astimezone(tz).replace(tzinfo=None)
                posted, minute = stamp.date(), stamp.hour * 60 + stamp.minute
            else:
                posted, minute = datetime.strptime(raw[:10], "%Y-%m-%d").date(), None
        except ValueError:
            continue
        try:
            ents = json.loads(r["entities"] or "null") or {}
        except (TypeError, ValueError, IndexError, KeyError):
            ents = {}
        part = staffing_signals.schedule_daypart((ents or {}).get("daypart"))
        spot = None
        if part:
            for back in range(0, REVIEW_LAG_DAYS + 1):
                d = (posted - timedelta(days=back)).isoformat()
                if back == 0 and minute is not None and minute < _SERVICE_STARTS[part]:
                    continue               # that meal had not happened yet when they wrote
                if (d, part) in by:
                    spot = ((d, part), "daypart", back)
                    break
        else:
            d = posted.isoformat()
            if d in main:
                spot = ((d, main[d]), "hours", 0)
        if spot:
            out.setdefault(spot[0], []).append({"rating": float(r["rating"] or 0), "how": spot[1], "lag": spot[2]})
    return out


def _morning_share(conn, restaurant_id) -> dict:
    """{weekday: share of the day's sales taken by 3pm}, MEASURED: from ≥3
    captured days of the POS's running total (pos_intraday), else from ≥3
    nights of the nightly report's own hourly split (its sales block's
    hourly nets — the source for a POS that cannot be asked during the day,
    as Restaurant DNA reads it). A weekday with neither is absent: its
    daypart sales are unmeasured, never a stated 0.4."""
    try:
        rows = conn.execute("SELECT weekday, business_date, captured_hour, net_sales FROM pos_intraday WHERE restaurant_id=? "
                            "AND business_date >= date('now','-84 days') ORDER BY business_date, captured_hour", (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    by = {}
    for r in rows:
        by.setdefault((r["weekday"], r["business_date"]), []).append((int(r["captured_hour"]), float(r["net_sales"] or 0)))
    tmp = {}
    for (wd, _), caps in by.items():
        caps.sort()
        total = caps[-1][1]
        at3 = max((s for h, s in caps if h <= MORNING_SPLIT_HOUR), default=None)
        if total > 0 and at3 is not None:
            tmp.setdefault(wd, []).append(min(1.0, at3 / total))
    out = {wd: sorted(v)[len(v) // 2] for wd, v in tmp.items() if len(v) >= MORNING_SHARE_MIN_DAYS}
    for wd, v in _dsr_morning_shares(conn, restaurant_id).items():
        if wd not in out and len(v) >= MORNING_SHARE_MIN_DAYS:
            out[wd] = sorted(v)[len(v) // 2]
    return out


def _dsr_morning_shares(conn, restaurant_id) -> dict:
    """{weekday: [share before 3pm]} from the nightly report's hourly split
    (dsr_reports facts → blocks.sales.detail.hourly), the latest finished
    version of each night in the last 12 weeks. An hour before the business
    day starts (1am) belongs to the night."""
    import json as _json
    from time_utils import BUSINESS_DAY_START_HOUR
    try:
        rows = conn.execute("SELECT business_date, version, facts_json FROM dsr_reports WHERE restaurant_id=? "
                            "AND business_date >= date('now','-84 days') AND status IN ('final','provisional') "
                            "ORDER BY business_date, version", (restaurant_id,)).fetchall()
    except Exception:
        return {}
    nights = {}
    for r in rows:                          # later versions overwrite earlier ones
        try:
            blk = ((_json.loads(r["facts_json"] or "{}") or {}).get("blocks") or {}).get("sales") or {}
        except (TypeError, ValueError):
            continue
        if blk.get("status") != "ready":
            continue
        early = tot = 0.0
        for h in ((blk.get("detail") or {}).get("hourly") or []):
            try:
                hour, net = int(h.get("hour")), float(h.get("net") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if net <= 0:
                continue
            tot += net
            if BUSINESS_DAY_START_HOUR <= hour < MORNING_SPLIT_HOUR:
                early += net
        if tot > 0:
            nights[str(r["business_date"])[:10]] = early / tot
    out = {}
    for d, share in nights.items():
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        out.setdefault(wd, []).append(share)
    return out


def outcomes_by_daypart(restaurant_id, db_path=DB_PATH) -> dict:
    """{weekday: {daypart: {weeks, avg_hours, avg_actual_hours, actual_weeks,
    avg_people, avg_actual_people, missed, stayed_late, avg_sales, splh,
    splh_basis, labor_pct, issues, troubled, rating}}} over the recorded
    weeks — the plan (avg_hours, avg_people) and what the punches say
    happened (avg_actual_hours / avg_actual_people over the `actual_weeks`
    that had punches, missed and stayed-late shifts; schedule audit 10/3/26
    L-12). Sales per labor hour is over the hours WORKED where the weeks
    have them (`splh_basis` "worked"), else over the hours scheduled
    ("scheduled"); `labor_pct` is the daypart's own (L-13), None unless
    measured."""
    conn = get_conn(db_path)
    try:
        # Daypart sales only where the split was measured (split_basis): a
        # row recorded before the column existed divided the day by a stated
        # 0.4, and its sales per labor hour is not a figure to hand the
        # schedule prompt (QUALITY-11).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_outcomes)").fetchall()}
        extra = (", actual_hours, actual_people, missed, stayed_late, labor_pct_daypart, people"
                 if "actual_hours" in cols else
                 ", NULL AS actual_hours, NULL AS actual_people, NULL AS missed, NULL AS stayed_late, "
                 "NULL AS labor_pct_daypart, people")
        rows = conn.execute("SELECT date, daypart, hours, CASE WHEN split_basis='measured' THEN sales END AS sales, "
                            f"issues, review_rating{extra} FROM schedule_outcomes WHERE restaurant_id=? "
                            "ORDER BY date DESC LIMIT 400", (restaurant_id,)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    # "No issues" is a reading only on a night the coverage check watched
    # (watched_dates, A-19; CA1 L20): a daypart whose nights nobody watched
    # said "· no issues" on the web.
    dates = sorted(str(r["date"]) for r in rows if r["date"])
    seen = watched_dates(restaurant_id, dates[0], dates[-1], db_path) if dates else set()
    acc = {}
    for r in rows:
        try:
            wd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        e = acc.setdefault(wd, {}).setdefault(r["daypart"], {"weeks": 0, "hours": 0.0, "sales": 0.0, "sales_n": 0,
                                                             "issues": 0, "ratings": [], "watched": 0,
                                                             "clean_watched": 0, "people": 0, "a_weeks": 0,
                                                             "a_hours": 0.0, "a_people": 0, "missed": 0,
                                                             "stayed_late": 0, "w_sales": 0.0, "w_hours": 0.0,
                                                             "labor": []})
        e["weeks"] += 1
        e["hours"] += float(r["hours"] or 0)
        e["people"] += int(r["people"] or 0)
        if r["sales"] is not None:
            e["sales"] += float(r["sales"]); e["sales_n"] += 1
        if r["actual_hours"] is not None:
            e["a_weeks"] += 1
            e["a_hours"] += float(r["actual_hours"] or 0)
            e["a_people"] += int(r["actual_people"] or 0)
            e["missed"] += int(r["missed"] or 0)
            e["stayed_late"] += int(r["stayed_late"] or 0)
            if r["sales"] is not None and r["actual_hours"]:
                e["w_sales"] += float(r["sales"]); e["w_hours"] += float(r["actual_hours"])
        if r["labor_pct_daypart"] is not None:
            e["labor"].append(float(r["labor_pct_daypart"]))
        e["issues"] += int(r["issues"] or 0)
        if (r["issues"] or 0) or str(r["date"]) in seen:
            e["watched"] += 1
            if not (r["issues"] or 0):
                e["clean_watched"] += 1
        if r["review_rating"] is not None:
            e["ratings"].append(float(r["review_rating"]))
    out = {}
    for wd, parts in acc.items():
        for part, e in parts.items():
            if e["weeks"] < 2:
                continue
            avg_h = e["hours"] / e["weeks"]
            avg_s = (e["sales"] / e["sales_n"]) if e["sales_n"] else None
            if e["issues"]:
                label = f"{e['issues']} issue{'s' if e['issues'] != 1 else ''}"
            elif e["watched"]:
                label = f"no issues on {e['watched']} watched night{'s' if e['watched'] != 1 else ''}"
            else:
                label = "not watched — coverage wasn't checked on these nights"
            worked_splh = round(e["w_sales"] / e["w_hours"], 0) if e["w_hours"] else None
            out.setdefault(wd, {})[part] = {
                "weeks": e["weeks"], "avg_hours": round(avg_h, 1), "avg_sales": round(avg_s, 0) if avg_s else None,
                "avg_people": round(e["people"] / e["weeks"], 1),
                "actual_weeks": e["a_weeks"],
                "avg_actual_hours": round(e["a_hours"] / e["a_weeks"], 1) if e["a_weeks"] else None,
                "avg_actual_people": round(e["a_people"] / e["a_weeks"], 1) if e["a_weeks"] else None,
                "missed": e["missed"] if e["a_weeks"] else None,
                "stayed_late": e["stayed_late"] if e["a_weeks"] else None,
                "labor_pct": round(sum(e["labor"]) / len(e["labor"]), 1) if e["labor"] else None,
                "splh": worked_splh if worked_splh else (round(avg_s / avg_h, 0) if (avg_s and avg_h) else None),
                "splh_basis": "worked" if worked_splh else ("scheduled" if (avg_s and avg_h) else None),
                "issues": e["issues"],
                # `issues` is None when no night was a reading at all, so a
                # client cannot print "no issues" for nights nobody watched.
                "issues_known": bool(e["issues"] or e["watched"]),
                "watched": e["watched"], "clean_watched": e["clean_watched"], "issues_label": label,
                "troubled": e["issues"] >= max(2, e["watched"] // 2) if e["watched"] else False,
                "rating": round(sum(e["ratings"]) / len(e["ratings"]), 2) if e["ratings"] else None,
            }
            if not out[wd][part]["issues_known"]:
                out[wd][part]["issues"] = None
    return out


def outcome_block(outcomes: dict, week_days: list) -> str:
    """The prompt's "last time this pattern ran" facts."""
    if not outcomes:
        return ""
    lines = []
    for wd in week_days or []:
        parts = outcomes.get(wd) or {}
        for part in ("morning", "night"):
            e = parts.get(part)
            if not e:
                continue
            bits = [f"about {e['avg_hours']:g}h scheduled"]
            # What the punches say was worked beside the plan (schedule
            # audit 10/3/26 L-12): the block is called "actually did".
            if e.get("avg_actual_hours") is not None:
                bits.append(f"{e['avg_actual_hours']:g}h worked")
            if e.get("missed"):
                bits.append(f"{e['missed']} shift{'s' if e['missed'] != 1 else ''} missed in {e['actual_weeks']} weeks")
            if e.get("splh"):
                bits.append(f"${e['splh']:,.0f} of sales per labor hour"
                            + (" worked" if e.get("splh_basis") == "worked" else " scheduled"))
            if e["issues"]:
                bits.append(f"{e['issues']} coverage or no-show issue{'s' if e['issues'] != 1 else ''} in {e['weeks']} weeks")
            if e.get("rating") is not None:
                bits.append(f"reviews averaged {e['rating']:g}★")
            tag = " — issues on half or more of its watched nights" if e["troubled"] else ""
            lines.append(f"  {wd} {'lunch/day' if part == 'morning' else 'dinner/night'}: " + ", ".join(bits) + tag)
    if not lines:
        return ""
    # Context, not a lever (schedule audit 10/3/26 PR-7, PR-24): "do not
    # thin it" asked the model to hold a crew up on top of SHIFT
    # REQUIREMENTS, and its hours read as a second target.
    return ("\n\nWHAT PUBLISHED WEEKS ACTUALLY DID (this restaurant's own record by daypart — context: the pattern "
            "that ran and how it went; SHIFT REQUIREMENTS already set this week's numbers):\n" + "\n".join(lines))


# ── the record the ledger and the rotation read ──────────────────────────────
#
# Both read PUBLISHED weeks only, so a restaurant that never published a
# Cavnar AI week (Simple EJ's) drafted with no fairness or rotation memory,
# while its POS punches already said who worked the weekends and who closed
# (schedule audit 10/3/26 D-21). Each week of the window is its live
# published schedule, else what the time clock kept for it (shift_facts) —
# labelled "punches" wherever it is said — so published weeks take over one
# by one as they exist.

def _closes(r, close_times) -> bool:
    """Whether a shift closes its day: ending within 30 minutes of that
    day's close, read across midnight (schedule_rules.end_minutes, and a
    close in the small hours is the next morning — close_minutes). Raw
    minutes made "2:00am" 120, so every shift ending after 1:30am — or any
    shift on a midnight-close Thursday — counted as a close (D-20). With no
    close time set, a shift ending at 10pm or later."""
    from schedule_rules import parse_minutes, end_minutes, _OVERNIGHT_LATEST_BEFORE
    end = end_minutes(r)
    if end is None:
        return False
    try:
        day = datetime.strptime(str(r.get("date") or "")[:10], "%Y-%m-%d").strftime("%A")
    except ValueError:
        day = ""
    close = parse_minutes((close_times or {}).get(day, "")) if day else None
    if close is None:
        return end >= 22 * 60
    if close < _OVERNIGHT_LATEST_BEFORE:
        close += 24 * 60
    return end >= close - 30


def _punch_hours(r) -> str:
    """A punch's hours as a schedule row's scheduled_hours: what was worked."""
    for k in ("actual_hours", "scheduled_hours"):
        v = r.get(k)
        try:
            if v not in (None, "") and float(v) > 0:
                return str(float(v))
        except (TypeError, ValueError):
            continue
    return ""


def record_weeks(restaurant_id, weeks: int = LEDGER_WEEKS, db_path=DB_PATH, today=None) -> list:
    """[(week_start iso, rows, "published"|"punches")], newest first, at
    most `weeks`: each of the last `weeks` weeks before this one as its live
    published schedule, else its punches (shift_facts.person_rows — what was
    worked, the hours being the punch's). Names are each person's one
    spelling (people.canonical_names)."""
    from schedule_versions import rows_from_csv
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        published = _published_weeks(conn, restaurant_id, weeks, today)
    finally:
        conn.close()
    out, covered = [], set()
    for w in published:
        ws = str(w["week_start"] or "")[:10]
        out.append((ws, rows_from_csv(w["schedule_csv"]), "published"))
        try:
            d0, d1 = date.fromisoformat(ws), date.fromisoformat(str(w["week_end"] or ws)[:10])
            covered |= {(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
        except ValueError:
            continue
    monday = today - timedelta(days=today.weekday())
    try:
        import shift_facts
        punches = shift_facts.person_rows(restaurant_id, since=(monday - timedelta(weeks=weeks)).isoformat(),
                                          until=(monday - timedelta(days=1)).isoformat(),
                                          db_path=None if db_path == DB_PATH else db_path)
    except Exception as e:
        print(f"[schedule_intel] punches unread for {restaurant_id}: {e!r}")
        punches = []
    by_week = {}
    for r in punches:
        d = str(r.get("date") or "")[:10]
        if len(d) != 10 or d in covered or not (r.get("employee") or "").strip():
            continue
        try:
            wk = (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
        except ValueError:
            continue
        by_week.setdefault(wk, []).append(dict(r, date=d, scheduled_hours=_punch_hours(r)))
    out += [(wk, rs, "punches") for wk, rs in by_week.items()]
    out.sort(key=lambda t: t[0], reverse=True)
    out = out[:weeks]
    names = sorted({(r.get("employee") or "").strip() for _ws, rs, _s in out for r in rs} - {""})
    try:
        import people
        canon = people.canonical_names(restaurant_id, names, db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        canon = {}
    if canon:
        out = [(ws, [dict(r, employee=canon.get((r.get("employee") or "").strip(), r.get("employee")))
                     for r in rs], src) for ws, rs, src in out]
    return out


def _source_of(hist) -> str:
    kinds = {src for _ws, _rs, src in hist}
    return "published and punches" if kinds == {"published", "punches"} else (next(iter(kinds)) if kinds else "")


# ── fairness ledger ────────────────────────────────────────────────────────

def fairness_ledger(restaurant_id, weeks: int = LEDGER_WEEKS, db_path=DB_PATH, today=None) -> dict:
    """{name: {"weekend": n, "closing": n, "holiday": n, "shifts": n,
    "weeks": w, "from_punches": p}} over the last `weeks` weeks of record
    (record_weeks: published, else punches — D-21) — the rotation memory a
    seven-day window cannot hold. `from_punches`: how many of those weeks
    are the time clock's, said wherever the ledger is."""
    from schedule_economics import _holiday_dates
    today = today or date.today()
    hist = record_weeks(restaurant_id, weeks=weeks, db_path=db_path, today=today)
    if not hist:
        return {}
    close_times = {}
    try:
        from models import get_close_times
        close_times = get_close_times(restaurant_id, db_path) or {}
    except Exception:
        pass
    # Both ends of every week: one starting Dec 28 holds New Year's Day of
    # the next year, which the week_start year alone never looked up (SCHED-33).
    holidays = {}
    years = set()
    for ws, _rs, _src in hist:
        try:
            d0 = date.fromisoformat(ws)
            years |= {d0.year, (d0 + timedelta(days=6)).year}
        except ValueError:
            pass
    for y in years:
        holidays.update(_holiday_dates(y))
    ledger = {}
    for _ws, rows_, _src in hist:
        for r in rows_:
            n = r["employee"]
            e = ledger.setdefault(n, {"weekend": 0, "closing": 0, "holiday": 0, "shifts": 0})
            e["shifts"] += 1
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d")
            except ValueError:
                continue
            # Friday to Sunday, the weekend the scorer's fairness and the
            # prompt count (shift_quality._WEEKEND) — this ledger counted only
            # Saturday and Sunday, so the two disagreed about who had them.
            if d.weekday() >= 4:
                e["weekend"] += 1
            if r["date"] in holidays:
                e["holiday"] += 1
            if _closes(r, close_times):
                e["closing"] += 1
    punched = sum(1 for _ws, _rs, src in hist if src == "punches")
    for e in ledger.values():
        e["weeks"] = len(hist)
        e["from_punches"] = punched
    return ledger


# ── the rotation plan ──────────────────────────────────────────────────────
#
# The ledger says who has carried the weekends and closes; nothing said who
# should get the NEXT one. The plan reads the same published weeks week by
# week and orders each role: who has worked the most weekends in a row (so
# is due one off), who has closed least for their shifts (so is next to
# close) and who is closing far past their share (so should rest from it),
# and who has worked the fewest holidays (so works the next one). The
# generator is handed it as a soft preference and the fairness score judges
# a week against it — over weeks, not inside one.

ROTATION_MIN_SHIFTS = 3          # shifts in the window before somebody is in the rotation
ROTATION_MIN_GROUP = 3           # people in a role before a rotation is planned for it
ROTATION_WEEKEND_DUE = 3         # weekends in a row before a weekend off is due
ROTATION_CLOSE_REST = 1.5        # closing at this multiple of the role's rate, and...
ROTATION_CLOSE_EXCESS = 2        # ...this many closes past their share: rest from closing
ROTATION_HOLIDAY_HORIZON = 42    # days ahead a holiday is planned for
ROTATION_SHOW = 3                # names per queue shown and prompted


def _published_weeks(conn, restaurant_id, weeks, today):
    return conn.execute(
        "SELECT week_start, week_end, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
        "AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id "
        "AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
        "AND week_start >= ? ORDER BY week_start DESC LIMIT ?",
        (restaurant_id, (today - timedelta(weeks=weeks)).isoformat(), weeks)).fetchall()


def rotation_plan(restaurant_id, weeks: int = LEDGER_WEEKS, db_path=DB_PATH, today=None, roster_roles: dict = None) -> dict:
    """{weeks, roles: {role: {...queues}}, holiday, lines, from_punches,
    source} — who is next for a weekend off, a close and a holiday, per
    role, from the last `weeks` weeks of record: each week's published
    schedule, else its punches (record_weeks, D-21 — `from_punches` says
    how many). roster_roles ({name: role}) limits the plan to people still
    on the roster and files them under their roster role; without it each
    person's most-worked role is used. Empty when fewer than two weeks of
    record exist: a rotation needs a history to rotate from."""
    from schedule_economics import _holiday_dates
    today = today or date.today()
    # The weeks of record (published, else punches — D-21), newest first.
    hist = record_weeks(restaurant_id, weeks=weeks, db_path=db_path, today=today)
    close_times = {}
    try:
        from models import get_close_times
        close_times = get_close_times(restaurant_id, db_path) or {}
    except Exception:
        close_times = {}
    if len(hist) < 2:
        return {}
    years = set()
    for ws, _rs, _src in hist:
        try:
            d0 = date.fromisoformat(ws)
            years |= {d0.year, (d0 + timedelta(days=6)).year}
        except ValueError:
            pass
    years |= {today.year, today.year + 1}
    holidays = {}
    for y in years:
        holidays.update(_holiday_dates(y))
    roster = {str(n).strip().lower(): (n, (r or "").strip()) for n, r in (roster_roles or {}).items() if n}
    people = {}       # lower name -> {display, roles{}, shifts, closes, nights, holidays, by_week{ws: weekend?}}
    week_keys = []
    for ws, week_rows, _src in hist:
        week_keys.append(ws)
        for r in week_rows:
            low = r["employee"].strip().lower()
            if roster and low not in roster:
                continue
            p = people.setdefault(low, {"name": r["employee"].strip(), "roles": {}, "shifts": 0, "closes": 0,
                                        "nights": 0, "holidays": 0, "weeks": {}})
            p["shifts"] += 1
            role = (r.get("role") or "").strip()
            if role:
                p["roles"][role] = p["roles"].get(role, 0) + 1
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d")
            except ValueError:
                continue
            p["weeks"][ws] = p["weeks"].get(ws, False) or d.weekday() >= 4
            if r["date"] in holidays:
                p["holidays"] += 1
            if _daypart(r) == "night":
                p["nights"] += 1
            if _closes(r, close_times):
                p["closes"] += 1
    by_role = {}
    for low, p in people.items():
        if p["shifts"] < ROTATION_MIN_SHIFTS:
            continue
        role = (roster.get(low) or (None, ""))[1] or (max(p["roles"].items(), key=lambda kv: (kv[1], kv[0]))[0] if p["roles"] else "")
        if role:
            by_role.setdefault(role, []).append(p)
    roles_out, lines = {}, []
    for role, members in sorted(by_role.items(), key=lambda kv: kv[0].lower()):
        if len(members) < ROTATION_MIN_GROUP:
            continue
        streak = {}
        for p in members:
            s = 0
            for ws in week_keys:                        # newest first
                if p["weeks"].get(ws):
                    s += 1
                else:
                    break                               # off that weekend, or off the whole week
            streak[p["name"]] = s
        weekend_q = sorted((p for p in members if streak[p["name"]] >= 2),
                           key=lambda p: (-streak[p["name"]], -sum(1 for v in p["weeks"].values() if v), p["name"]))
        cap = max(1, (len(members) + 3) // 4)
        due = [p["name"] for p in weekend_q if streak[p["name"]] >= ROTATION_WEEKEND_DUE][:cap]
        closers = [p for p in members if p["nights"] or p["closes"]]
        total_s = sum(p["shifts"] for p in closers)
        rate = (sum(p["closes"] for p in closers) / float(total_s)) if total_s else 0.0
        next_close = [p["name"] for p in sorted(closers, key=lambda p: (p["closes"] / float(p["shifts"]), p["closes"], p["name"]))] if rate else []
        rest = []
        if rate:
            for p in closers:
                share = p["shifts"] * rate
                if p["closes"] >= ROTATION_CLOSE_REST * share and p["closes"] - share >= ROTATION_CLOSE_EXCESS:
                    rest.append((p["closes"] - share, p["name"]))
        rest_names = [n for _x, n in sorted(rest, key=lambda t: (-t[0], t[1]))]
        next_close = [n for n in next_close if n not in rest_names]
        holiday_work = [p["name"] for p in sorted(members, key=lambda p: (p["holidays"], p["name"]))]
        holiday_off = [p["name"] for p in sorted(members, key=lambda p: (-p["holidays"], p["name"])) if p["holidays"]]
        roles_out[role] = {
            "members": len(members),
            "people": sorted(p["name"] for p in members),
            "weekend_off": [p["name"] for p in weekend_q][:ROTATION_SHOW * 2],
            "weekend_due": due,
            "weekend_streak": {p["name"]: streak[p["name"]] for p in weekend_q},
            "next_close": next_close[:ROTATION_SHOW * 2],
            "rest_from_close": rest_names,
            "closes": {p["name"]: {"closes": p["closes"], "shifts": p["shifts"]} for p in closers},
            "holiday_work_first": holiday_work[:ROTATION_SHOW * 2],
            "holiday_off_first": holiday_off[:ROTATION_SHOW * 2],
        }
        bits = []
        if weekend_q:
            q = weekend_q[:ROTATION_SHOW]
            bits.append("next weekend off: " + _then([f"{p['name']} ({streak[p['name']]} in a row)" if i == 0 else p["name"]
                                                        for i, p in enumerate(q)]))
        if next_close:
            bits.append("next close: " + _then(next_close[:ROTATION_SHOW]))
        if rest_names:
            bits.append("rest from closing: " + ", ".join(rest_names[:ROTATION_SHOW]))
        if bits:
            lines.append(f"{_plural_role(role)} — " + "; ".join(bits) + ".")
    holiday = None
    upcoming = sorted((d, n) for d, n in holidays.items()
                      if today.isoformat() < d <= (today + timedelta(days=ROTATION_HOLIDAY_HORIZON)).isoformat())
    if upcoming and roles_out:
        d, n = upcoming[0]
        holiday = {"date": d, "name": n,
                   "work_first": {role: v["holiday_work_first"][:ROTATION_SHOW] for role, v in roles_out.items()},
                   "off_first": {role: v["holiday_off_first"][:ROTATION_SHOW] for role, v in roles_out.items() if v["holiday_off_first"]}}
        from time_utils import mdy
        for role, v in roles_out.items():
            if v["holiday_off_first"]:
                lines.append(f"{n} {mdy(d)}, {_plural_role(role)} — first off: {_then(v['holiday_off_first'][:ROTATION_SHOW])} "
                             f"(worked the most holidays); first to work it: {_then(v['holiday_work_first'][:ROTATION_SHOW])}.")
    if not roles_out:
        return {}
    punched = sum(1 for _ws, _rs, src in hist if src == "punches")
    return {"weeks": len(hist), "roles": roles_out, "holiday": holiday, "lines": lines,
            "from_punches": punched, "source": _source_of(hist)}


def _then(names: list) -> str:
    """'Ana', 'Ana, then Ben', 'Ana, then Ben, Cara' — the head of a queue first."""
    names = [n for n in names if n]
    if not names:
        return ""
    return names[0] if len(names) == 1 else names[0] + ", then " + ", ".join(names[1:])


def _plural_role(role: str) -> str:
    role = (role or "").strip()
    return role if role.lower().endswith("s") else role + "s"


def rotation_block(plan: dict) -> str:
    """The plan for the prompt — a soft preference under the hard rules and
    the shift requirements, never a reason to break either."""
    if not plan or not plan.get("roles"):
        return ""
    lines = []
    for role, v in plan["roles"].items():
        bits = []
        if v.get("weekend_due"):
            bits.append("give these people this weekend off where the rules allow — "
                        + ", ".join(f"{n} ({v['weekend_streak'].get(n)} weekends in a row)" for n in v["weekend_due"]))
        if v.get("next_close"):
            bits.append("hand the week's closes first to " + ", ".join(v["next_close"][:ROTATION_SHOW]))
        if v.get("rest_from_close"):
            bits.append("close " + ", ".join(v["rest_from_close"][:ROTATION_SHOW]) + " less than usual")
        if bits:
            lines.append(f"  {role}: " + "; ".join(bits) + ".")
    h = plan.get("holiday")
    if h and h.get("off_first"):
        from time_utils import mdy
        for role, names in h["off_first"].items():
            lines.append(f"  {h['name']} ({mdy(h['date'])}), {role}: first off {', '.join(names)}; first to work it "
                         f"{', '.join((h.get('work_first') or {}).get(role) or [])}.")
    if not lines:
        return ""
    return ("\n\nROTATION PLAN (over the last " + _weeks_words(plan.get("weeks"), plan.get("from_punches"))
            + " — a preference ranked below the hard rules and the shift requirements; the fairness score checks the "
              "week against it):\n" + "\n".join(lines))


def _weeks_words(weeks, from_punches) -> str:
    """"8 published weeks", "8 weeks (5 from punches — the time clock, not a
    published schedule)" — where the record came from, said (D-21)."""
    weeks, punched = int(weeks or 0), int(from_punches or 0)
    if not punched:
        return f"{weeks} published weeks"
    return (f"{weeks} weeks ({'all' if punched >= weeks else punched} from punches — the time clock, "
            "not a published schedule)")


def ledger_block(ledger: dict) -> str:
    if not ledger:
        return ""
    active = {n: e for n, e in ledger.items() if e["shifts"] >= 3}
    if len(active) < 3:
        return ""
    first = next(iter(active.values()))
    wk = first["weeks"]
    top_w = sorted(active.items(), key=lambda kv: -kv[1]["weekend"])[:3]
    low_w = sorted(active.items(), key=lambda kv: kv[1]["weekend"])[:3]
    top_c = sorted(active.items(), key=lambda kv: -kv[1]["closing"])[:3]
    lines = [f"  Most weekend shifts in the last {_weeks_words(wk, first.get('from_punches'))}: "
             + ", ".join(f"{n} ({e['weekend']})" for n, e in top_w),
             "  Fewest: " + ", ".join(f"{n} ({e['weekend']})" for n, e in low_w),
             "  Most closes: " + ", ".join(f"{n} ({e['closing']})" for n, e in top_c)]
    hol = [(n, e["holiday"]) for n, e in active.items() if e["holiday"]]
    if hol:
        lines.append("  Holidays worked: " + ", ".join(f"{n} ({h})" for n, h in sorted(hol, key=lambda x: -x[1])[:5]))
    return ("\n\nROTATION LEDGER (who has carried the weekends, closes and holidays lately — spread the next ones "
            "toward the people at the bottom of each list where the rules allow):\n" + "\n".join(lines))


# ── behaviour-learned preferences ─────────────────────────────────────────

def behaviour_preferences(restaurant_id, weeks: int = 12, db_path=DB_PATH) -> dict:
    """{name: {"avoids": ["Sunday night", …], "prefers": [...], "drops": n, "claims": n}}
    from drop requests and claims. Two of the same is a pattern."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name, replacement_name, date, shift_start, status, kind FROM shift_change_requests "
                            "WHERE restaurant_id=? AND created_at >= date('now', ?)", (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    from schedule_rules import daypart_of
    tally = {}
    for r in rows:
        try:
            wd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        slot = f"{wd} {'night' if daypart_of(r['shift_start'] or '') == 'night' else 'day'}"
        # A drop the manager DENIED is not a preference the next draft should
        # honour — reading it as "avoids" overrode that decision (SCHED-40).
        # Only a drop that was let go (open, covered) or a swap that went
        # through says the person does not want that slot.
        if (r["kind"] or "drop") in ("drop", "swap") and r["status"] in ("open", "covered"):
            t = tally.setdefault(r["employee_name"], {"avoid": {}, "prefer": {}, "drops": 0, "claims": 0})
            t["avoid"][slot] = t["avoid"].get(slot, 0) + 1
            t["drops"] += 1
        if r["replacement_name"] and r["status"] == "covered":
            t = tally.setdefault(r["replacement_name"], {"avoid": {}, "prefer": {}, "drops": 0, "claims": 0})
            t["prefer"][slot] = t["prefer"].get(slot, 0) + 1
            t["claims"] += 1
    out = {}
    for n, t in tally.items():
        avoids = sorted(s for s, c in t["avoid"].items() if c >= 2)
        prefers = sorted(s for s, c in t["prefer"].items() if c >= 2)
        if avoids or prefers:
            out[n] = {"avoids": avoids, "prefers": prefers, "drops": t["drops"], "claims": t["claims"]}
    return out


def preferences_block(learned: dict, stated: dict) -> str:
    """What staff want — stated (staff_settings.preferred_dayparts /
    desired_hours) and learned from what they drop and claim — is each
    person's ROSTER line in the schedule prompt now (WANTS, schedule audit
    10/3/26 PR-33: one of about nine blocks the model joined by name).
    Nothing is left to say apart from the people, so this is empty.
    Candidate for future cleanup after additional verification: the
    learning workstream's learned-block sections still name it."""
    return ""


def role_shifts(restaurant_id, days: int = MENTOR_WINDOW_DAYS, db_path=DB_PATH) -> dict:
    """{employee: {role: shifts}} over the last `days` of the per-shift
    history — the evidence the schedule prompt's CAN WORK holds a second
    role to (MENTOR_SHIFTS_TO_HOLD shifts, the bar "could hold a station"
    already used; schedule audit 10/3/26 PR-21: one shift ever in a role
    made somebody "cross-trained", flexing them "costs nothing extra")."""
    import shift_facts as _sf
    since = (date.today() - timedelta(days=int(days))).isoformat()
    out = {}
    for r in _sf.person_rows(restaurant_id, since=since, db_path=None if db_path == DB_PATH else db_path) or []:
        n, role = (r.get("employee") or "").strip(), (r.get("role") or "").strip()
        if n and role:
            out.setdefault(n, {})[role] = out.get(n, {}).get(role, 0) + 1
    return out


# ── mentoring / succession ─────────────────────────────────────────────────

def mentoring(restaurant_id, db_path=DB_PATH) -> dict:
    """{name: {role: shifts beside a closer}} — shifts a person worked in a
    role that is not their usual one, on the same date and daypart as
    somebody authorized to close. At MENTOR_SHIFTS_TO_HOLD they could hold
    the station."""
    from models import get_leader_flags
    try:
        closers = {n.lower() for n, v in (get_leader_flags(restaurant_id, db_path) or {}).items() if v}
        # A year of shifts from the per-shift history (memory audit 9/29/26,
        # shift_facts): the stored file was a rolling window an upload could
        # shrink to a fortnight, and the evidence for "could hold the bar"
        # went with it.
        import shift_facts as _sf
        since = (date.today() - timedelta(days=MENTOR_WINDOW_DAYS)).isoformat()
        shifts = _sf.person_rows(restaurant_id, since=since)
    except Exception:
        return {}
    if not closers or not shifts:
        return {}
    usual = {}
    for s in shifts:
        n = (s.get("employee") or "").strip()
        role = (s.get("role") or "").strip()
        if n and role:
            usual.setdefault(n, {})[role] = usual.get(n, {}).get(role, 0) + 1
    usual = {n: max(r.items(), key=lambda kv: kv[1])[0] for n, r in usual.items()}
    by_slot = {}
    for s in shifts:
        by_slot.setdefault((s.get("date"), _daypart(s)), []).append(s)
    out = {}
    for (_d, _p), group in by_slot.items():
        has_closer = any((g.get("employee") or "").strip().lower() in closers for g in group)
        if not has_closer:
            continue
        for g in group:
            n, role = (g.get("employee") or "").strip(), (g.get("role") or "").strip()
            if not n or not role or usual.get(n) == role or n.lower() in closers:
                continue
            out.setdefault(n, {})[role] = out.get(n, {}).get(role, 0) + 1
    return out


def could_hold(mentored: dict) -> dict:
    """{name: [roles]} they have been mentored in enough times."""
    return {n: [r for r, c in roles.items() if c >= MENTOR_SHIFTS_TO_HOLD] for n, roles in (mentored or {}).items()
            if any(c >= MENTOR_SHIFTS_TO_HOLD for c in roles.values())}


# ── was anyone watching? ──────────────────────────────────────────────────
#
# "Clean" here means no coverage or no-show issue was opened. That is only
# evidence when something could have opened one. Nothing creates "no_show"
# issues at all, and a "coverage" issue is opened only by
# strategy_jobs.run_coverage_check, which runs only when ALL of these hold:
#
#   1. the Labor module is on;
#   2. issue routing names a manager (issues.get_routing has "manager");
#   3. the connected POS has a live clock-in feed (its provider module
#      exposes fetch_clock_ins_today — Toast and RPOWER both do: RPOWER's
#      above-store data is fed while the store trades, and
#      rpower.fetch_clock_ins_today reads the business day's punches; this
#      used to say RPOWER could not be watched, which sent anyone asking
#      why Simple EJ's nights were unwatched the wrong way — schedule audit
#      10/3/26 L-32);
#   4. the check actually read the clock-ins during THAT night's service.
#
# 1-3 are read from the current configuration. 4 is per date: the check
# marks each business date it read the clock-ins for (dsr_coverage_runs,
# written only when a published week covered the night and the POS
# answered). Before that mark existed (COVERAGE_RUNS_SINCE) the evidence was
# a pos_intraday sales reading that day — which also counted a night with no
# published week to check against, or one whose clock-in read failed while
# sales still came through (an RPOWER pull refused for unmatched punches),
# as watched. So at an RPOWER restaurant the real gates are the manager
# routing (2), a published week covering the night, and the clock-in read
# itself. Without all four, "8 of 8 shared dayparts ran without an issue",
# an accepted recommendation "improved", and auto-publish's "ran clean"
# weeks were all true of every restaurant by default (re-audit A-19) — so
# those reads are withheld for any date nobody was watching.
COVERAGE_RUNS_SINCE = "2026-09-25"

def coverage_watch_missing(restaurant_id, db_path=DB_PATH) -> list:
    """What stops run_coverage_check from watching this restaurant's nights,
    in the owner's words — empty when it can. Conditions 1-3 above."""
    try:
        from models import get_restaurant
        import issues, pos
        r = get_restaurant(restaurant_id, db_path)
        if not r or not getattr(r, "module_labor", 0):
            return ["Labor isn't turned on"]
        missing = []
        if "manager" not in issues.get_routing(restaurant_id, db_path):
            missing.append("no manager is set to receive issue texts (Account → Issues)")
        _name, mod = pos.connected_provider(restaurant_id)
        if not (mod and getattr(mod, "fetch_clock_ins_today", None) is not None):
            missing.append("no point-of-sale with live clock-ins is connected")
        return missing
    except Exception as e:
        print(f"[schedule_intel] coverage_watch_missing failed for {restaurant_id}: {e}")
        return ["the coverage check could not be read"]


def coverage_check_possible(restaurant_id, db_path=DB_PATH) -> bool:
    """Conditions 1-3 above: whether run_coverage_check can open a coverage
    issue for this restaurant at all."""
    return not coverage_watch_missing(restaurant_id, db_path)


def watched_dates(restaurant_id, start, end, db_path=DB_PATH) -> set:
    """ISO dates in [start, end] on which a clean night means something:
    coverage_check_possible, and the check really read that night's
    clock-ins (4: dsr_coverage_runs) — or, for a night before that mark was
    kept (COVERAGE_RUNS_SINCE), a live POS reading taken that day."""
    if not coverage_check_possible(restaurant_id, db_path):
        return set()
    s, e = str(start)[:10], str(end)[:10]
    out = set()
    conn = get_conn(db_path)
    try:
        out |= {str(r["business_date"])[:10] for r in conn.execute(
            "SELECT business_date FROM dsr_coverage_runs WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
            (restaurant_id, s, e)).fetchall()}
        if s < COVERAGE_RUNS_SINCE:
            out |= {r["business_date"] for r in conn.execute(
                "SELECT DISTINCT business_date FROM pos_intraday WHERE restaurant_id=? AND business_date BETWEEN ? AND ? "
                "AND business_date < ?", (restaurant_id, s, e, COVERAGE_RUNS_SINCE)).fetchall()}
        return out
    except Exception as ex:
        print(f"[schedule_intel] watched_dates failed for {restaurant_id}: {ex}")
        return set()
    finally:
        conn.close()


# ── chemistry suggestions ─────────────────────────────────────────────────

def chemistry_suggestions(restaurant_id, db_path=DB_PATH) -> list:
    """Pairs who shared at least SUGGEST_MIN_SHARED recorded dayparts of
    which SUGGEST_MIN_CLEAN ran without an issue, and are not already a
    pair. Returned as suggestions with their evidence; nothing is written."""
    from schedule_versions import rows_from_csv
    conn = get_conn(db_path)
    try:
        outs = conn.execute("SELECT history_id, date, daypart, issues FROM schedule_outcomes WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        csvs = {r["id"]: r["schedule_csv"] for r in conn.execute(
            "SELECT id, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id)", (restaurant_id,)).fetchall()}
        existing = {frozenset((r["employee_a"].lower(), r["employee_b"].lower())) for r in conn.execute(
            "SELECT employee_a, employee_b FROM staff_pairs WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
    except Exception:
        return []
    finally:
        conn.close()
    if not outs:
        return []
    # Only nights somebody was watching can count as "ran without an issue"
    # (watched_dates, A-19). None watched, no suggestion.
    dates = sorted(str(o["date"]) for o in outs if o["date"])
    seen = watched_dates(restaurant_id, dates[0], dates[-1], db_path) if dates else set()
    outs = [o for o in outs if str(o["date"]) in seen]
    if not outs:
        return []
    people_by_slot = {}
    for hid, csv_text in csvs.items():
        for r in rows_from_csv(csv_text):
            people_by_slot.setdefault((hid, r["date"], _daypart(r)), set()).add(r["employee"])
    # Who actually worked each shift where the punches say (schedule audit
    # 10/3/26 L-12): a pair the schedule put together but one of whom never
    # came did not share that night. The salaried don't clock in — their
    # planned shifts stand.
    try:
        import shift_facts as _sf
        from models import get_restaurant as _gr, salaried_staff, salaried_name_key
        sal = {salaried_name_key(x["name"]) for x in salaried_staff(_gr(restaurant_id))}
        worked = {}
        for p in _sf.rows(restaurant_id, since=dates[0], until=dates[-1],
                          db_path=None if db_path in (None, DB_PATH) else db_path):
            if p.get("actual_hours") in (None, ""):
                continue
            worked.setdefault((str(p["date"])[:10], _daypart(p)), set()).add(p["employee"])
        punched = {d for d, _p in worked}
        for key in list(people_by_slot):
            hid, d, part = key
            if d in punched:
                people_by_slot[key] = worked.get((d, part), set()) | {
                    n for n in people_by_slot[key] if salaried_name_key(n) in sal}
    except Exception as _wx:
        print(f"[schedule_intel] chemistry read the plan only for {restaurant_id}: {_wx}")
    shared, clean = {}, {}
    for o in outs:
        people = sorted(people_by_slot.get((o["history_id"], o["date"], o["daypart"]), set()))
        ok = int(o["issues"] or 0) == 0
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                k = frozenset((people[i].lower(), people[j].lower()))
                shared[k] = shared.get(k, 0) + 1
                clean[k] = clean.get(k, 0) + (1 if ok else 0)
    out = []
    for k, n in shared.items():
        if n < SUGGEST_MIN_SHARED or k in existing:
            continue
        rate = clean[k] / n
        if rate >= SUGGEST_MIN_CLEAN:
            a, b = sorted(k)
            out.append({"a": a.title(), "b": b.title(), "kind": "prefer", "shared": n, "clean_rate": round(rate, 2),
                        "evidence": f"{clean[k]} of {n} shared dayparts ran without a coverage or no-show issue"})
    out.sort(key=lambda x: (-x["shared"], -x["clean_rate"]))
    for x in out:
        x["rec_key"] = pair_rec_key(x["a"], x["b"])
    return out[:8]


def pair_rec_key(a, b) -> str:
    import rec_ledger as _rl
    return _rl.rec_key("suggested_pair", "|".join(sorted((str(a).strip().lower(), str(b).strip().lower()))))


def chemistry_suggestions_shown(restaurant_id, surface="labor", user_id=None, db_path=DB_PATH) -> list:
    """chemistry_suggestions as an owner sees them: without the pairs they
    said "Ignore" to (rec_ledger, on any device), and logged as shown."""
    import rec_ledger as _rl
    # ...and this login's own "Ignore" (PEOPLE-4).
    quiet = _rl.silenced_keys(restaurant_id, db_path=db_path,
                              viewer=user_id if isinstance(user_id, int) else None)
    out = [x for x in chemistry_suggestions(restaurant_id, db_path=db_path) if x["rec_key"] not in quiet]
    _rl.present_many(restaurant_id, [dict(key=x["rec_key"], module="schedule", kind="suggested_pair",
                                          title=f"Pair {x['a']} with {x['b']}", evidence_sources=["schedule"])
                                     for x in out], surface, user_id=user_id, db_path=db_path)
    return out


# ── recommendation ledger ──────────────────────────────────────────────────

def record_recommendation(restaurant_id, kind: str, key: str, action: str, actor=None, db_path=DB_PATH,
                          authority=None) -> None:
    """action: shown | accepted | dismissed | restored (the owner asked for a
    suppressed kind back). `authority` is whose showing or answer it was
    (permissions.answer_authority; None = the owner's, as every row before
    it) — only the owner's count toward suppressing a kind for the owner.
    A restore also lifts the kind's durable suppression."""
    if action not in ("shown", "accepted", "dismissed", "restored") or not kind:
        return
    if authority is None:
        try:
            from permissions import acting_via
            if acting_via():
                authority = "admin"
        except Exception:
            pass
    conn = get_conn(db_path)
    try:
        if action == "restored":
            now = conn.execute("SELECT datetime('now')").fetchone()[0]
            try:
                conn.execute("DELETE FROM rec_kind_states WHERE restaurant_id=? AND family='schedule' AND kind=?",
                             (restaurant_id, str(kind)[:60]))
                conn.execute("INSERT OR REPLACE INTO rec_kind_states (restaurant_id, family, kind, state, since, "
                             "restored_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                             (restaurant_id, "schedule_restore", str(kind)[:60], "restored", now, now, now))
            except Exception as e:
                print(f"[schedule_intel] restore not held durably: {e}")
        # A showing is one per recommendation per day: a manager saving the
        # same week ten times in one sitting was ten "shown, never taken"
        # and switched the advice off for good (SCHED-26), and every rescore
        # grew the table (SCHED-27).
        if action == "shown" and conn.execute(
                "SELECT 1 FROM schedule_recommendation_events WHERE restaurant_id=? AND kind=? AND key=? "
                "AND action='shown' AND created_at >= date('now')",
                (restaurant_id, str(kind)[:60], str(key or "")[:200])).fetchone():
            return
        conn.execute("INSERT INTO schedule_recommendation_events (restaurant_id, kind, key, action, actor, authority) "
                     "VALUES (?,?,?,?,?,?)",
                     (restaurant_id, str(kind)[:60], str(key or "")[:200], action, (actor or "")[:120] or None,
                      authority if authority in ("principal", "delegate", "admin") else None))
        conn.commit()
    finally:
        conn.close()


_REC_WHEN = re.compile(r"\bon (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (morning|night|lunch|dinner)\b")


# A night recommendation's verdict (CA1 L22): one night after accepting used
# to decide "improved" (no issue) or "worsened" (any), with no baseline — a
# single quiet Friday proved the advice. Now it is the SHARE of that weekday
# and daypart's watched nights with a coverage or no-show issue, over at
# least REC_MIN_NIGHTS_AFTER nights after acceptance, against the same
# nights over the REC_BASELINE_WEEKS before it (at least REC_MIN_NIGHTS_BEFORE
# of them), and it moves only past a band: the larger of REC_RATE_FLOOR and
# metrics.BAND_K standard errors of the difference of two proportions — the
# same 10% two-sided false-alarm rule the outcome engine uses.
REC_MIN_NIGHTS_AFTER = 7
REC_MIN_NIGHTS_BEFORE = 4
REC_BASELINE_WEEKS = 12
REC_RATE_FLOOR = 0.15
REC_ACCEPTED_LOOKBACK_DAYS = 180          # long enough for seven weekly nights to land


def _evidence_nights(rows, restaurant_id, db_path):
    """The rows that are evidence: a night with an issue always; a night
    with none only when the coverage check was watching it (A-19)."""
    if not rows:
        return []
    dates = sorted(str(o["date"]) for o in rows)
    seen = watched_dates(restaurant_id, dates[0], dates[-1], db_path)
    return [o for o in rows if (o["issues"] or 0) or str(o["date"]) in seen]


def night_rate_verdict(before_issue_nights, before_n, after_issue_nights, after_n) -> dict:
    """{verdict, before_rate, after_rate, band, before_n, after_n} for an
    issue-night share before and after; verdict "unknown" below the night
    floors. Pure."""
    out = {"before_n": before_n, "after_n": after_n, "before_rate": None, "after_rate": None, "band": None,
           "verdict": "unknown"}
    if before_n < REC_MIN_NIGHTS_BEFORE or after_n < REC_MIN_NIGHTS_AFTER:
        return out
    import metrics
    pb, pa = before_issue_nights / before_n, after_issue_nights / after_n
    pooled = (before_issue_nights + after_issue_nights) / float(before_n + after_n)
    se = (pooled * (1 - pooled) * (1.0 / before_n + 1.0 / after_n)) ** 0.5
    band = max(REC_RATE_FLOOR, metrics.BAND_K * se)
    diff = pb - pa                      # a fall in the issue share is better
    out.update(before_rate=round(pb, 3), after_rate=round(pa, 3), band=round(band, 3),
               verdict="improved" if diff > band else "worsened" if -diff > band else "no_clear_change")
    return out


def measure_accepted_recommendations(restaurant_id, db_path=DB_PATH, today=None) -> int:
    """For accepted schedule recommendations about one night ("Fill the gap
    on Friday night…", "Move somebody … onto Saturday night"), read that
    weekday and daypart's issue share once REC_MIN_NIGHTS_AFTER of its
    nights after acceptance have been recorded in published, finished weeks
    (schedule_outcomes), against the REC_BASELINE_WEEKS before it
    (night_rate_verdict). Only watched nights count (A-19). Recorded ONCE as
    the recommendation's outcome in rec_ledger. Idempotent."""
    import rec_ledger as _rl
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        acc = conn.execute("SELECT kind, key, created_at FROM schedule_recommendation_events WHERE restaurant_id=? "
                           "AND action='accepted' AND created_at >= datetime('now', ?)",
                           (restaurant_id, f"-{REC_ACCEPTED_LOOKBACK_DAYS} days")).fetchall()
        out = conn.execute("SELECT o.date, o.daypart, o.issues, h.week_start, h.week_end FROM schedule_outcomes o "
                           "JOIN schedule_history h ON h.id=o.history_id WHERE o.restaurant_id=? AND h.week_end < ?",
                           (restaurant_id, today.isoformat())).fetchall()
    except Exception as e:
        print(f"[schedule_intel] measure_accepted_recommendations unavailable: {e}")
        return 0
    finally:
        conn.close()
    n = 0
    for a in acc:
        m = _REC_WHEN.search(a["key"] or "")
        if not m:
            continue
        day, part = m.group(1), {"lunch": "morning", "dinner": "night"}.get(m.group(2), m.group(2))
        accepted_on = str(a["created_at"])[:10]
        since = (date.fromisoformat(accepted_on) - timedelta(weeks=REC_BASELINE_WEEKS)).isoformat()
        same = {}
        for o in out:
            if o["daypart"] == part and datetime.strptime(o["date"], "%Y-%m-%d").strftime("%A") == day:
                same.setdefault(o["date"], o)            # one reading per night
        after = _evidence_nights([o for d, o in same.items() if d >= accepted_on], restaurant_id, db_path)
        before = _evidence_nights([o for d, o in same.items() if since <= d < accepted_on], restaurant_id, db_path)
        res = night_rate_verdict(sum(1 for o in before if o["issues"]), len(before),
                                 sum(1 for o in after if o["issues"]), len(after))
        if res["verdict"] == "unknown":
            continue                                     # not enough watched nights yet: read again next week
        if _rl.record(restaurant_id, schedule_rec_key(a["kind"], a["key"]), "outcome",
                      meta={"verdict": res["verdict"], "day": day, "daypart": part,
                            "before_rate": res["before_rate"], "after_rate": res["after_rate"],
                            "band": res["band"], "before_nights": res["before_n"], "after_nights": res["after_n"],
                            "through": max(o["date"] for o in after)},
                      source_ref=f"nights:{accepted_on}:{day}:{part}", db_path=db_path):
            n += 1
    return n


# ── the week as it ran (schedule audit 10/3/26 L-29, L-34) ───────────────
#
# A published week was scored once, as planned. Nothing scored it as it RAN
# — who actually came, when they came and went — so a recommendation the
# manager carried out could only ever be judged on a watched night's issue
# count, and pairing, fatigue, strength and the rest never got an outcome
# (what_worked stayed thin). The week as it ran is the punches (the salaried
# people's planned shifts beside them: they don't clock in), scored by the
# same engine and the same inputs a live rescore uses
# (schedule_engine.quality_inputs_from_db, _quality_signals), and kept per
# shift beside the planned score in the observation log (schedule_memory:
# sq_as_run, sq_planned). A recommendation's outcome is then the dimension it
# named, on the shift it named, as the shift ran — against the same
# dimension in the draft it was made on.

AS_RUN_WEEKS = 4
AS_RUN_BAND = 10.0               # points a dimension must move, as run, to read better or worse
AS_RUN_LOOKBACK_DAYS = REC_ACCEPTED_LOOKBACK_DAYS   # long enough for the week to end and be scored


def as_run_rows(restaurant_id, week_start, week_end, plan_rows, db_path=DB_PATH) -> list:
    """The week as it ran: each punch with its hours (shift_facts, worked
    hours as the row's hours), plus the salaried people's planned rows. []
    when the week has no punch at all — never the plan passed off as what
    happened."""
    import shift_facts
    from models import get_restaurant, salaried_staff, salaried_name_key
    try:
        sal = {salaried_name_key(x["name"]) for x in salaried_staff(get_restaurant(restaurant_id))}
    except Exception:
        sal = set()
    out = []
    for p in shift_facts.rows(restaurant_id, since=week_start, until=week_end,
                              db_path=None if db_path in (None, DB_PATH) else db_path):
        if p.get("actual_hours") in (None, "") or salaried_name_key(p.get("employee")) in sal:
            continue
        d = str(p["date"])[:10]
        out.append({"date": d, "day": datetime.strptime(d, "%Y-%m-%d").strftime("%A"), "employee": p["employee"],
                    "role": p.get("role") or "", "shift_start": p.get("shift_start") or "",
                    "shift_end": p.get("shift_end") or "", "scheduled_hours": str(p.get("actual_hours")), "notes": ""})
    if not out:
        return []
    out += [dict(r) for r in plan_rows or [] if salaried_name_key(r.get("employee")) in sal]
    return out


def _planned_quality(conn, history_id):
    """The quality the published week carried when it was first sent (the
    first `published` version's), else the latest stored for the week."""
    row = conn.execute("SELECT quality_json FROM schedule_versions WHERE history_id=? AND reason='published' AND "
                       "quality_json IS NOT NULL ORDER BY version LIMIT 1", (history_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT quality_json FROM schedule_history WHERE id=?", (history_id,)).fetchone()
    try:
        return json.loads(row["quality_json"] or "null") or {} if row else {}
    except (TypeError, ValueError):
        return {}


def _unrated(s) -> bool:
    """A shift judged without the Operational Scores of some of its people
    (shift_quality's blind spot) — what "Rate the unscored staff" fixes."""
    return any("Operational Score" in str(b) for b in s.get("blind_spots") or [])


def _shift_reading(s) -> dict:
    return {"score": s.get("score"), "capped_by": s.get("capped_by"),
            "dims": {d["key"]: d.get("score") for d in s.get("dimensions") or [] if d.get("key")},
            "hard_breaches": len(s.get("hard_breaches") or []), "unrated": _unrated(s)}


def record_as_run_quality(restaurant_id, db_path=DB_PATH, today=None, weeks=AS_RUN_WEEKS) -> int:
    """Each published week that has ended (the newest `weeks`), scored as it
    ran, beside its planned score — per shift into the observation log
    (sq_as_run, sq_planned; phase as_run; keyed by week, date and daypart,
    so a re-read replaces them), the week-level measures too. Returns the
    shifts recorded; a week with no punch records nothing."""
    import schedule_memory as _sm
    from schedule_versions import rows_from_csv
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history h WHERE restaurant_id=? AND "
            "published_at IS NOT NULL AND superseded_by IS NULL AND week_end < ? AND NOT EXISTS (SELECT 1 FROM "
            "schedule_history n WHERE n.restaurant_id=h.restaurant_id AND n.week_start=h.week_start AND "
            "n.published_at IS NOT NULL AND n.id > h.id) ORDER BY week_start DESC LIMIT ?",
            (restaurant_id, today.isoformat(), int(weeks))).fetchall()
        planned = {h["id"]: _planned_quality(conn, h["id"]) for h in hist}
    finally:
        conn.close()
    n = 0
    db = None if db_path in (None, DB_PATH) else db_path
    for h in hist:
        rows = as_run_rows(restaurant_id, h["week_start"], h["week_end"], rows_from_csv(h["schedule_csv"] or ""),
                           db_path)
        if not rows:
            continue
        quality = score_as_run(restaurant_id, h["id"], rows)
        if not quality.get("checked"):
            continue
        common = dict(week_start=h["week_start"], history_id=h["id"], origin="system", phase="as_run",
                      authority="system", source="as_run_score", db_path=db)
        plan_by = {(s.get("date"), s.get("daypart")): s for s in (planned.get(h["id"]) or {}).get("shifts") or []
                   if s.get("scored")}
        for s in quality.get("shifts") or []:
            if not s.get("scored"):
                continue
            key = (s.get("date"), s.get("daypart"))
            _sm.observe(restaurant_id, "sq_as_run", date=s.get("date"), daypart=s.get("daypart"),
                        value=_shift_reading(s), fact_key=f"sq_as_run|{h['id']}|{key[0]}|{key[1]}", **common)
            if key in plan_by:
                _sm.observe(restaurant_id, "sq_planned", date=s.get("date"), daypart=s.get("daypart"),
                            value=_shift_reading(plan_by[key]), fact_key=f"sq_planned|{h['id']}|{key[0]}|{key[1]}",
                            **common)
            n += 1
        _sm.observe(restaurant_id, "sq_as_run", value={
            "score": quality.get("score"),
            "week_dims": {d.get("key"): d.get("score") for d in quality.get("week_dimensions") or []
                          if isinstance(d, dict) and d.get("key")},
            "confidence": (quality.get("confidence") or {}).get("level") if isinstance(quality.get("confidence"), dict)
            else quality.get("confidence")}, fact_key=f"sq_as_run|{h['id']}|week", **common)
    return n


def score_as_run(restaurant_id, history_id, rows) -> dict:
    """The Shift Quality of `rows` (the week as it ran) on the inputs a live
    rescore of that week uses — never a what-if, never a showing."""
    import shift_quality as _sq
    from schedule_engine import quality_inputs_from_db, stored_daily_targets, _quality_signals
    inputs = quality_inputs_from_db(restaurant_id, daily_target_hours=stored_daily_targets(restaurant_id, history_id),
                                    week_rows=rows)
    signals, weights = _quality_signals(restaurant_id, inputs)
    return _sq.score_rows(rows, profiles=inputs.get("shift_profiles") or None, weights=weights, **signals)


# What each recommendation shape names, and the dimension its outcome is read
# on — the sentences shift_quality.recommendation_details writes.
_REC_DIMENSION = (
    (re.compile(r"^Fill the gap on (?P<where>.+?): "), "coverage"),
    (re.compile(r"^Cover the gap in service on (?P<where>.+?): "), "coverage_curve"),
    (re.compile(r'^Move somebody who clears ".*" onto (?P<where>.+?)\.?$'), "leadership"),
    (re.compile(r"^Put a stronger .+? on (?P<where>.+?): "), "operational_strength"),
    (re.compile(r"^Pair .+? on (?P<where>.+?) with a stronger hand"), "training_balance"),
    (re.compile(r"^Trim about [\d.]+h from (?P<where>.+?) to get back"), "labor_efficiency"),
    (re.compile(r"^Fix the rule breach on (?P<where>.+?): "), "hard_rules"),
    (re.compile(r"^Spread the busy shifts"), "fatigue"),
    (re.compile(r"^Give .+? a day off"), "fatigue"),
    (re.compile(r"^Rate the unscored staff"), "ratings"),
)
# A ratings recommendation's reading: the share of the week's shifts judged
# without some of their people's ratings, as it ran against the draft it
# was made on — this many points of share either way.
AS_RUN_RATED_BAND = 0.25


def _rec_target(text):
    """(dimension, weekday | None, daypart | None) a recommendation sentence
    is about, or None for one no shift measures (a rating to enter)."""
    for rx, dim in _REC_DIMENSION:
        m = rx.match(text or "")
        if not m:
            continue
        where = (m.groupdict().get("where") or "").strip().rstrip(".")
        parts = where.split()
        if len(parts) == 2 and parts[1] in ("morning", "night"):
            return dim, parts[0], parts[1]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", where):
            return dim, where, None
        return dim, None, None
    return None


def measure_recommendations_as_run(restaurant_id, db_path=DB_PATH, today=None) -> int:
    """Every recommendation the manager carried out — by the button or by
    the edit itself (schedule_learning.addressed_recommendations) — read
    against the week it was for, AS IT RAN (schedule audit 10/3/26 L-34:
    only night coverage on watched nights was ever measured). The week is
    the published one whose stored quality carried the sentence before it
    was accepted; the reading is the dimension the sentence names, on the
    shift it names (or the week, for fatigue), in the week as it ran
    (sq_as_run) against the draft it was made on: AS_RUN_BAND points better
    is improved, worse is worsened, else no clear change — and for a rule
    breach, whether the shift ran without one. The punched heads of a
    coverage gap's slot ride along (actual coverage). Recorded once per
    recommendation and week as its rec_ledger outcome (a measured tracker's
    verdict, where there is one, still stands over it). Returns outcomes
    recorded."""
    import rec_ledger as _rl
    import schedule_memory as _sm
    from shift_quality import recommendation_kind
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        acc = conn.execute("SELECT kind, key, MIN(created_at) AS at FROM schedule_recommendation_events WHERE "
                           "restaurant_id=? AND action='accepted' AND created_at >= datetime('now', ?) "
                           "GROUP BY kind, key", (restaurant_id, f"-{AS_RUN_LOOKBACK_DAYS} days")).fetchall()
        if not acc:
            return 0
        vers = conn.execute(
            "SELECT v.history_id, v.created_at, v.quality_json, h.week_start, h.week_end FROM schedule_versions v "
            "JOIN schedule_history h ON h.id=v.history_id WHERE h.restaurant_id=? AND h.published_at IS NOT NULL "
            "AND h.week_end < ? AND v.quality_json IS NOT NULL AND v.created_at >= datetime('now', ?) "
            "ORDER BY v.created_at", (restaurant_id, today.isoformat(), f"-{AS_RUN_LOOKBACK_DAYS + 14} days")).fetchall()
    finally:
        conn.close()
    db = None if db_path in (None, DB_PATH) else db_path
    as_run = {}
    for o in _sm.observations(restaurant_id, kinds=("sq_as_run",), db_path=db):
        as_run[(o.get("history_id"), o.get("date") or "week", o.get("daypart"))] = o.get("value") or {}
    gaps = {}
    for o in _sm.observations(restaurant_id, kinds=("coverage_gap_actual", "actual_hours"), db_path=db):
        gaps.setdefault((o.get("date"), o.get("daypart")), []).append(o)
    n = 0
    for a in acc:
        text = str(a["key"] or "")
        target = _rec_target(text)
        if not target:
            continue
        dim, day, part = target
        made = None
        for v in vers:
            if str(v["created_at"]) > str(a["at"]):
                break
            try:
                q = json.loads(v["quality_json"] or "null") or {}
            except (TypeError, ValueError):
                continue
            if text in [str(r) for r in (q.get("recommendations") or [])]:
                made = (v, q)
        if made is None:
            continue
        v, q = made
        hid = v["history_id"]
        if dim == "ratings":
            planned = [s_ for s_ in q.get("shifts") or [] if s_.get("scored")]
            ran = [v for (h_, d_, _p), v in as_run.items() if h_ == hid and d_ != "week"]
            if not planned or not ran:
                continue
            before = {"ratings": sum(1 for s_ in planned if _unrated(s_)) / float(len(planned))}
            after = {"ratings": sum(1 for v in ran if v.get("unrated")) / float(len(ran))}
            date_s = None
        elif day is None and part is None:
            before = {d.get("key"): d.get("score") for d in q.get("week_dimensions") or [] if isinstance(d, dict)}
            after = (as_run.get((hid, "week", None)) or {}).get("week_dims") or {}
            date_s = None
        else:
            date_s = day if re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)) else next(
                (s.get("date") for s in q.get("shifts") or [] if s.get("day") == day and s.get("daypart") == part), None)
            shift = next((s for s in q.get("shifts") or [] if s.get("date") == date_s
                          and (part is None or s.get("daypart") == part)), None)
            if not shift or not date_s:
                continue
            part = part or shift.get("daypart")
            before = {d["key"]: d.get("score") for d in shift.get("dimensions") or [] if d.get("key")}
            before["hard_rules"] = len(shift.get("hard_breaches") or [])
            reading = as_run.get((hid, date_s, part))
            if not reading:
                continue
            after = dict(reading.get("dims") or {}, hard_rules=reading.get("hard_breaches"))
        if dim not in before or after.get(dim) is None or before.get(dim) is None:
            continue
        b, x = float(before[dim]), float(after[dim])
        if dim == "hard_rules":
            verdict = "improved" if b > 0 and x == 0 else ("worsened" if x > b else "no_clear_change")
        elif dim == "ratings":
            # A falling share of shifts judged without ratings is better.
            verdict = ("improved" if b - x >= AS_RUN_RATED_BAND else "worsened" if x - b >= AS_RUN_RATED_BAND
                       else "no_clear_change")
        else:
            verdict = ("improved" if x - b >= AS_RUN_BAND else "worsened" if b - x >= AS_RUN_BAND
                       else "no_clear_change")
        meta = {"verdict": verdict, "measure": "as_run", "dimension": dim, "before": round(b, 3), "as_run": round(x, 3),
                "band": AS_RUN_RATED_BAND if dim == "ratings" else AS_RUN_BAND, "history_id": hid, "date": date_s,
                "daypart": part}
        if dim in ("coverage", "coverage_curve") and date_s:
            obs = gaps.get((date_s, part)) or []
            heads = next((o.get("value") for o in obs if o.get("kind") == "actual_hours"), None)
            short = [dict(o.get("value") or {}, role=o.get("role")) for o in obs
                     if o.get("kind") == "coverage_gap_actual"]
            meta["actual_coverage"] = {"planned_people": (heads or {}).get("planned_people"),
                                       "worked_people": (heads or {}).get("actual_people"), "short": short}
        if _rl.record(restaurant_id, schedule_rec_key(recommendation_kind(text), text), "outcome", meta=meta,
                      source_ref=f"as_run:{hid}:{date_s or 'week'}:{part or ''}", db_path=db_path):
            n += 1
    return n


def schedule_rec_key(kind, text) -> str:
    """The rec_ledger key of one Shift Quality recommendation."""
    import rec_ledger as _rl
    return _rl.rec_key("schedule_" + (kind or "other"), (text or "")[:120])


# A suppressed schedule kind is re-tested this long after it was suppressed
# (and after each re-test that went unaccepted), for RETEST_WINDOW_DAYS
# (memory audit 9/29/26, "quiet_kinds"): it used to come back by accident a
# year later, when the pruned log forgot why.
SUPPRESS_REVIEW_DAYS = 60
RETEST_WINDOW_DAYS = 14


def suppression_state(restaurant_id, db_path=DB_PATH, now=None, write=True) -> dict:
    """{kind: {state: suppressed | retest, reason, since, review_on (M/D/YY),
    retests}} — the schedule recommendation kinds this owner has plainly
    declined, held as durable state (rec_kind_states, family "schedule")
    instead of recounted from schedule_recommendation_events, which is
    pruned at 365 days. A kind is suppressed when, since the owner last
    asked for it back, it was shown SUPPRESS_AFTER_SHOWN times and never
    accepted, or dismissed "not for us" twice and never accepted — counting
    the OWNER's showings and answers only (a manager's or support's never
    suppress a kind for the owner; memory audit, who_answered). From its
    review date it is re-tested for RETEST_WINDOW_DAYS: accepted, it is
    back; declined again or left unaccepted, it is suppressed to the next
    review date. Kinds about whether a shift is safe to run
    (shift_quality.PROTECTED_REC_KINDS) are never suppressed."""
    from datetime import datetime as _dt, timedelta as _td
    from shift_quality import PROTECTED_REC_KINDS
    now = now or _dt.utcnow()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    out = {}
    try:
        try:
            states = {r["kind"]: dict(r) for r in conn.execute(
                "SELECT * FROM rec_kind_states WHERE restaurant_id=? AND family='schedule'", (restaurant_id,)).fetchall()}
            restores = {r["kind"]: r["restored_at"] for r in conn.execute(
                "SELECT kind, restored_at FROM rec_kind_states WHERE restaurant_id=? AND family='schedule_restore'",
                (restaurant_id,)).fetchall()}
        except Exception:
            states, restores = {}, {}
        try:
            rows = conn.execute("SELECT kind, key, action, authority, created_at FROM schedule_recommendation_events "
                                "WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        except Exception as e:
            print(f"[schedule_intel] suppressed_kinds failed: {e}")
            rows = []
        # Counted since the owner last asked for the kind back — the log's
        # own restore row, or the durable one (the log is pruned at 365 days).
        since = dict(restores)
        for r in rows:
            if r["action"] == "restored":
                since[r["kind"]] = max(since.get(r["kind"], ""), r["created_at"] or "")
        counted = {}
        for r in rows:
            kind = r["kind"]
            if kind in PROTECTED_REC_KINDS or (r["created_at"] or "") <= since.get(kind, ""):
                continue
            c = counted.setdefault(kind, {"shown": set(), "acc": 0, "dis": 0})
            who = r["authority"] or "principal"
            if r["action"] == "shown" and who == "principal":
                c["shown"].add(f"{r['key']}|{str(r['created_at'])[:10]}")
            elif r["action"] == "accepted" and who != "admin":
                c["acc"] += 1
            elif r["action"] == "dismissed" and who == "principal":
                c["dis"] += 1
        for kind, r in counted.items():
            r["shown"] = len(r["shown"])
            if kind in states or r["acc"]:
                continue
            if r["shown"] >= SUPPRESS_AFTER_SHOWN or r["dis"] >= 2:
                reason = (f"declined {int(r['dis'])} times" if (r["dis"] or 0) >= 2
                          else f"shown {int(r['shown'])} times, never accepted")
                review = (now + _td(days=SUPPRESS_REVIEW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                states[kind] = {"kind": kind, "state": "suppressed", "reason": reason, "since": now_s,
                                "review_on": review, "retests": 0}
                if write:
                    conn.execute("INSERT OR REPLACE INTO rec_kind_states (restaurant_id, family, kind, state, reason, "
                                 "since, review_on, retests, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (restaurant_id, "schedule", kind, "suppressed", reason, now_s, review, 0, now_s))
        from time_utils import mdy
        for kind, st in list(states.items()):
            if kind in PROTECTED_REC_KINDS:
                continue
            # An acceptance of the kind since it was suppressed — an edit
            # that carried one out, a button on a week still showing it —
            # brings it back: the strongest sign it is wanted.
            took = conn.execute(
                "SELECT 1 FROM schedule_recommendation_events WHERE restaurant_id=? AND kind=? AND action='accepted' "
                "AND COALESCE(authority,'principal')!='admin' AND created_at >= ? LIMIT 1",
                (restaurant_id, kind, st.get("since") or "")).fetchone()
            if took:
                if write:
                    conn.execute("DELETE FROM rec_kind_states WHERE restaurant_id=? AND family='schedule' AND kind=?",
                                 (restaurant_id, kind))
                continue
            state = "suppressed"
            review = st.get("review_on")
            if review and review <= now_s:
                since_review = conn.execute(
                    "SELECT SUM(action='accepted' AND COALESCE(authority,'principal')!='admin') AS acc, "
                    "SUM(action='dismissed' AND COALESCE(authority,'principal')='principal') AS dis "
                    "FROM schedule_recommendation_events WHERE restaurant_id=? AND kind=? AND created_at >= ?",
                    (restaurant_id, kind, review)).fetchone()
                window_over = (datetime.strptime(review[:19], "%Y-%m-%d %H:%M:%S")
                               + timedelta(days=RETEST_WINDOW_DAYS)) <= now
                if since_review and (since_review["acc"] or 0):
                    if write:
                        conn.execute("DELETE FROM rec_kind_states WHERE restaurant_id=? AND family='schedule' "
                                     "AND kind=?", (restaurant_id, kind))
                    continue                                   # the re-test was taken: back for good
                if (since_review and (since_review["dis"] or 0)) or window_over:
                    review = (now + _td(days=SUPPRESS_REVIEW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                    st = dict(st, review_on=review, retests=int(st.get("retests") or 0) + 1)
                    if write:
                        conn.execute("UPDATE rec_kind_states SET review_on=?, retests=?, updated_at=? "
                                     "WHERE restaurant_id=? AND family='schedule' AND kind=?",
                                     (review, st["retests"], now_s, restaurant_id, kind))
                else:
                    state = "retest"
            out[kind] = {"state": state, "reason": st.get("reason"), "since": mdy(str(st.get("since") or "")[:10])
                         if st.get("since") else None, "review_on": mdy(str(review)[:10]) if review else None,
                         "retests": int(st.get("retests") or 0)}
        if write:
            conn.commit()
    finally:
        conn.close()
    return out


def suppressed_kinds(restaurant_id, db_path=DB_PATH) -> set:
    """Recommendation kinds this owner has plainly declined and that are not
    being re-tested (suppression_state): neither shown nor stored until the
    review date, a restore, or an accepted re-test brings them back.

    "Not for us" used to change nothing (only shown and accepted counted),
    and a suppressed kind could never come back: its recommendations were
    neither shown nor stored, so neither a button nor an edit could accept
    one."""
    try:
        return {k for k, st in suppression_state(restaurant_id, db_path=db_path).items()
                if st["state"] == "suppressed"}
    except Exception as e:
        print(f"[schedule_intel] suppressed_kinds failed: {e}")
        return set()


# ── learned-pattern dismissals ────────────────────────────────────────────

def pattern_key(p: dict) -> str:
    """kind|employee|day|daypart — and, for the kinds that are about a role
    rather than a person (retimes, headcount, role changes, leader swaps),
    the role, the role it was changed from and the time, so two roles on one
    night are dismissed separately. The original moved_off / moved_on keys
    carry none of those and read exactly as they always have."""
    key = f"{p.get('kind')}|{(p.get('employee') or '').lower()}|{p.get('day')}|{p.get('daypart')}"
    extra = [str(p.get(f) or "").strip().lower() for f in ("role", "was_role", "time")]
    if any(extra):
        key += "|" + "|".join(extra)
    return key


# A dismissal counts when it is the restaurant's own word (schedule audit
# 10/3/26 L-10): an account holder's or a manager's — or one stored before
# the authority was (NULL, read as it always was) — and an admin's only once
# the account holder adopted it. An admin's dismissal through view-as used
# to silence the pattern for everyone, forever.
_DISMISSAL_COUNTS_SQL = "(COALESCE(authority, '') <> 'admin' OR adopted_at IS NOT NULL)"


def dismissed_patterns(restaurant_id, db_path=DB_PATH) -> set:
    """The pattern keys the restaurant has dismissed (an admin's only once
    adopted — L-10)."""
    conn = get_conn(db_path)
    try:
        return {r["key"] for r in conn.execute(
            f"SELECT key FROM schedule_pattern_dismissals WHERE restaurant_id=? AND {_DISMISSAL_COUNTS_SQL}",
            (restaurant_id,)).fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def admin_dismissed_patterns(restaurant_id, db_path=DB_PATH) -> dict:
    """{key: who} for the dismissals an admin made that do not count yet —
    the owner sees them and may count them as theirs (adopt_admin_dismissals)."""
    conn = get_conn(db_path)
    try:
        return {r["key"]: r["dismissed_by"] for r in conn.execute(
            "SELECT key, dismissed_by FROM schedule_pattern_dismissals WHERE restaurant_id=? AND authority='admin' "
            "AND adopted_at IS NULL", (restaurant_id,)).fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def dismiss_pattern(restaurant_id, key: str, actor=None, db_path=DB_PATH, authority=None) -> None:
    """Dismiss a learned pattern with whose word it is (`authority`,
    permissions.answer_authority of the login; None from a caller that has
    none, read as before). The restaurant's own word over an admin's
    replaces it: the owner dismissing what support already dismissed counts."""
    conn = get_conn(db_path)
    try:
        # The person the key names (its second field), so a rename or merge
        # re-keys the dismissal with the pattern (people.NAME_STORES).
        parts = str(key).split("|")
        who = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        if who:
            try:
                import people as _people
                who = _people.canonical_names(restaurant_id, [who], db_path=db_path if db_path != DB_PATH else None
                                              ).get(who) or who
            except Exception:
                pass
        auth = authority if authority in ("principal", "delegate", "admin") else None
        conn.execute("INSERT OR IGNORE INTO schedule_pattern_dismissals (restaurant_id, key, dismissed_by, employee, "
                     "authority) VALUES (?,?,?,?,?)",
                     (restaurant_id, str(key)[:200], (actor or "")[:120] or None, who, auth))
        if auth != "admin":
            conn.execute("UPDATE schedule_pattern_dismissals SET authority=?, dismissed_by=?, "
                         "created_at=datetime('now') WHERE restaurant_id=? AND key=? AND authority='admin' "
                         "AND adopted_at IS NULL", (auth, (actor or "")[:120] or None, restaurant_id, str(key)[:200]))
        conn.commit()
    finally:
        conn.close()


def adopt_admin_dismissals(restaurant_id, user, db_path=DB_PATH) -> int:
    """The account holder counts, as their own, the pattern dismissals an
    admin made through view-as or support (L-10, mirroring
    models.adopt_admin_ratings). Only a principal signed in as themselves
    may; returns how many now count."""
    from permissions import answer_authority
    from models import CapabilityError
    if answer_authority(user) != "principal":
        raise CapabilityError("Only the account holder, signed in as themselves, can count these as theirs.")
    who = ((user.get("username") or user.get("email") or "owner") + " (confirmed)")[:120]
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE schedule_pattern_dismissals SET adopted_by=?, adopted_at=datetime('now') "
                         "WHERE restaurant_id=? AND authority='admin' AND adopted_at IS NULL",
                         (who, restaurant_id)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def restore_pattern(restaurant_id, key: str, db_path=DB_PATH) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM schedule_pattern_dismissals WHERE restaurant_id=? AND key=?", (restaurant_id, str(key)[:200]))
        conn.commit()
    finally:
        conn.close()


def init_schedule_intel(db_path: str = DB_PATH):
    """Tables for the outcome record, the recommendation ledger and the
    owner's pattern dismissals — created at boot like every other table."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_outcomes (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        date           TEXT    NOT NULL,
        daypart        TEXT    NOT NULL,
        hours          REAL,
        people         INTEGER,
        sales          REAL,
        labor_pct      REAL,
        issues         INTEGER NOT NULL DEFAULT 0,
        review_rating  REAL,
        reviews        INTEGER NOT NULL DEFAULT 0,
        recorded_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(history_id, date, daypart)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_outcomes_rest ON schedule_outcomes(restaurant_id, date)")
    # How each row's daypart sales were split (memory audit 9/29/26,
    # QUALITY-11): 'measured' (the POS's intraday running total or the
    # nightly report's hourly split) or 'unmeasured' (sales NULL). A row
    # from before the column has NULL: read as unmeasured.
    if "split_basis" not in {r[1] for r in conn.execute("PRAGMA table_info(schedule_outcomes)").fetchall()}:
        conn.execute("ALTER TABLE schedule_outcomes ADD COLUMN split_basis TEXT")
    # Which shift a review's rating was put on, and how sure (memory audit
    # 9/29/26, reviews_to_labor): "daypart" when the analyser read the meal
    # in the review itself and it was posted within REVIEW_LAG_DAYS of that
    # shift; "hours" when it was put on the day's busiest daypart for want
    # of anything better. Calibration reads the first kind only.
    # What happened beside what was planned (schedule audit 10/3/26 L-12,
    # L-13 — record_outcomes): the punched hours and people (actual_basis
    # 'punches'; NULL where the date has no punch — unknown, never 0), the
    # shifts missed, late, left early and run past their end, and the
    # daypart's own labor cost and labor % (NULL unless its sales split was
    # measured). `labor_pct` stays the DAY's figure.
    _oc = {r[1] for r in conn.execute("PRAGMA table_info(schedule_outcomes)").fetchall()}
    for _col, _typ in (("review_attribution", "TEXT"), ("review_rating_attributed", "REAL"),
                       ("reviews_attributed", "INTEGER"), ("review_lag_days", "INTEGER"),
                       ("actual_hours", "REAL"), ("actual_people", "INTEGER"), ("actual_basis", "TEXT"),
                       ("missed", "INTEGER"), ("late", "INTEGER"), ("left_early", "INTEGER"),
                       ("stayed_late", "INTEGER"), ("labor_cost_daypart", "REAL"), ("labor_pct_daypart", "REAL")):
        if _col not in _oc:
            try:
                conn.execute(f"ALTER TABLE schedule_outcomes ADD COLUMN {_col} {_typ}")
            except Exception as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_recommendation_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        kind           TEXT    NOT NULL,
        key            TEXT,
        action         TEXT    NOT NULL,          -- shown | accepted | dismissed
        actor          TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rec_events ON schedule_recommendation_events(restaurant_id, kind)")
    # Whose showing or answer each row is (memory audit 9/29/26,
    # who_answered): only the owner's suppress a kind for the owner.
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_recommendation_events)").fetchall()}
    if "authority" not in _cols:
        conn.execute("ALTER TABLE schedule_recommendation_events ADD COLUMN authority TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rec_events_created "
                 "ON schedule_recommendation_events(created_at)")          # ops.prune_ledgers (DATA-40)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_pattern_dismissals (
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        key            TEXT    NOT NULL,
        dismissed_by   TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, key)
    )""")
    # What the draft has learned and keeps (memory audit 9/29/26,
    # standing_patterns): a move the manager made in enough weeks becomes a
    # row here, confirmed by every published week that keeps it and retired
    # only when a manager reverses it twice — never because nobody had to
    # make the correction again. Kept forever (one row per pattern).
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_standing_patterns (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
        pattern_key      TEXT    NOT NULL,
        kind             TEXT    NOT NULL,
        employee         TEXT,
        role             TEXT,
        day              TEXT,
        daypart          TEXT,
        time             TEXT,
        text             TEXT,
        editors          TEXT,
        first_learned    TEXT    NOT NULL,
        last_confirmed   TEXT    NOT NULL,
        times_applied    INTEGER NOT NULL DEFAULT 0,
        times_overridden INTEGER NOT NULL DEFAULT 0,
        status           TEXT    NOT NULL DEFAULT 'active',
        rule_note        TEXT,
        ruled_by         TEXT,
        checked_through  INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, pattern_key)
    )""")
    # The standing pattern's lifecycle (memory re-audit 9/29/26, LOOPS-2 /
    # QUALITY-2 / FORGET-5 / INVENTORY-2 / QUALITY-3 / QUALITY-14):
    #   person_id        the person a person pattern is about (people.NAME_STORES
    #                    re-points and re-keys it on a rename or merge)
    #   detail           JSON the kinds need beyond the key: was_role, delta, names
    #   times_confirmed  weeks the MANAGER's own hand kept it (the draft had not
    #                    already carried it, or they edited that slot and kept it)
    #                    — times_applied also counts the weeks the draft carried it
    #   last_overridden  when a manager last reversed it: reversals count toward
    #                    retirement only inside STANDING_OVERRIDE_WINDOW_DAYS
    #   retired_at / retired_week  when it retired, and the newest published week
    #                    (schedule_history.id) that retired it — only evidence
    #                    newer than that week brings it back
    #   dormant_at       set while its person has had no shift for
    #                    people.MEMORY_GONE_DAYS or is inactive; cleared on return
    _sp = {r[1] for r in conn.execute("PRAGMA table_info(schedule_standing_patterns)").fetchall()}
    for _col, _typ in (("person_id", "INTEGER"), ("detail", "TEXT"),
                       ("times_confirmed", "INTEGER NOT NULL DEFAULT 0"), ("last_overridden", "TEXT"),
                       ("retired_at", "TEXT"), ("retired_week", "INTEGER"), ("dormant_at", "TEXT")):
        if _col not in _sp:
            try:
                conn.execute(f"ALTER TABLE schedule_standing_patterns ADD COLUMN {_col} {_typ}")
            except Exception as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
    # The standing pattern's evidence and decay (schedule audit 10/3/26 L-6,
    # L-30): opportunities / hits — the published weeks that tested it and
    # the ones that kept it, an unedited week counting as a keep; last_hand
    # — the week (ISO date) a manager's own hand last confirmed it (the
    # weeks that taught it, a week they re-applied it, or the owner's "keep
    # it"); confidence — the Wilson lower bound of hits over opportunities
    # times the half-life decay since last_hand; retest_since — while set
    # (status 'retest') the next draft leaves it out once to see whether the
    # manager puts it back; last_retest_end / retests; retired_reason
    # (reversed | retest | decayed | owner); source 'learned' | 'owner_said'
    # (the owner's one-tap "always", L-35) and authority — whose word made it.
    _sp2 = {r[1] for r in conn.execute("PRAGMA table_info(schedule_standing_patterns)").fetchall()}
    for _col, _typ in (("opportunities", "INTEGER NOT NULL DEFAULT 0"), ("hits", "INTEGER NOT NULL DEFAULT 0"),
                       ("last_hand", "TEXT"), ("confidence", "REAL"), ("retest_since", "TEXT"),
                       ("last_retest_end", "TEXT"), ("retests", "INTEGER NOT NULL DEFAULT 0"),
                       ("retired_reason", "TEXT"), ("source", "TEXT"), ("authority", "TEXT")):
        if _col not in _sp2:
            try:
                conn.execute(f"ALTER TABLE schedule_standing_patterns ADD COLUMN {_col} {_typ}")
            except Exception as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
    # Whose pattern a dismissal is (INVENTORY-2): the key carries the name,
    # so a rename re-keys it (people._repoint_patterns) and a merge folds it.
    # Whose word the dismissal is (schedule audit 10/3/26 L-10): authority
    # (permissions.answer_authority — principal | delegate | admin); an
    # admin's (view-as, support) is kept but does not count until the
    # account holder adopts it (adopted_by / adopted_at).
    _pd = {r[1] for r in conn.execute("PRAGMA table_info(schedule_pattern_dismissals)").fetchall()}
    for _col, _typ in (("employee", "TEXT"), ("person_id", "INTEGER"), ("authority", "TEXT"),
                       ("adopted_by", "TEXT"), ("adopted_at", "TEXT")):
        if _col not in _pd:
            try:
                conn.execute(f"ALTER TABLE schedule_pattern_dismissals ADD COLUMN {_col} {_typ}")
            except Exception as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
    # The owner throwing a draft away (schedule audit 10/3/26 L-26): "redo
    # these days" (kind redo_days, with the owner's optional reason chip and
    # words) and a whole-week regeneration over a draft that was never sent
    # (kind draft_discarded). history_id is the draft rejected; the learners
    # read a redo to link the new draft to the original it replaced, so the
    # earlier edits are not lost, and the edit predictor counts the rejected
    # days' rows as changed.
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_rejections (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        week_start     TEXT,
        kind           TEXT    NOT NULL,          -- redo_days | draft_discarded
        dates_json     TEXT,
        reason_chip    TEXT,
        reason_text    TEXT,
        authority      TEXT,
        requested_by   TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rejections ON schedule_rejections(restaurant_id, history_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rejections_created ON schedule_rejections(created_at)")
    # The owner's one-tap "why" for a big edit in the first weeks (schedule
    # audit 10/3/26 L-35): asked on the save (answer NULL), answered
    # always | this_week | call_off. `keys` are the changed rows' keys
    # (date|employee|start) the answer is about; `subject` the question's
    # facts (kind, person, role, day, daypart, time, delta).
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_edit_answers (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        version        INTEGER,
        question_key   TEXT    NOT NULL,
        kind           TEXT    NOT NULL,
        subject_json   TEXT,
        keys_json      TEXT,
        phase          TEXT,
        answer         TEXT,
        authority      TEXT,
        answered_by    TEXT,
        asked_at       TEXT    NOT NULL DEFAULT (datetime('now')),
        answered_at    TEXT,
        adopted_by     TEXT,
        adopted_at     TEXT,
        UNIQUE(restaurant_id, history_id, question_key)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_edit_answers_asked ON schedule_edit_answers(asked_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_first_seen (
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name  TEXT    NOT NULL,
        first_seen     TEXT    NOT NULL,
        shifts_seen    INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        last_seen      TEXT,
        PRIMARY KEY (restaurant_id, employee_name)
    )""")
    # The newest shift date ever seen for each name (Benchmarking audit
    # BM4-8, Top-50 #26), so retention — who stopped appearing — can be
    # measured (intelligence.dna, dimension B10). A boot ALTER for a table
    # created before the column; NULL until the next upload fills it.
    import sqlite3
    try:
        conn.execute("ALTER TABLE staff_first_seen ADD COLUMN last_seen TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.commit()
    try:
        _normalise_tenure_dates(conn)
    finally:
        conn.close()


_ISO_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"


def _normalise_tenure_dates(conn) -> int:
    """Every first_seen / last_seen as ISO, once, at boot (memory audit
    9/29/26, tenure_date). A legacy "09/01/2026" sorts before every ISO date,
    so it won first_seen=MIN(...) forever and passed dna's "180 days of
    history" string comparison. Only malformed rows are read, so on a clean
    table this is one indexed-free scan of a small table. A date that cannot
    be read at all falls back to the other column (or updated_at's date):
    first_seen is NOT NULL and a guess from the same row beats a sort-order
    accident. Returns the rows rewritten."""
    from labor import _iso_date
    rows = conn.execute(
        f"SELECT restaurant_id, employee_name, first_seen, last_seen, updated_at FROM staff_first_seen "
        f"WHERE first_seen NOT GLOB '{_ISO_GLOB}' OR length(first_seen) != 10 "
        f"OR (last_seen IS NOT NULL AND (last_seen NOT GLOB '{_ISO_GLOB}' OR length(last_seen) != 10))").fetchall()
    fixed = 0
    for r in rows:
        first = _iso_date(r["first_seen"])
        last = _iso_date(r["last_seen"]) if r["last_seen"] else None
        fallback = last or _iso_date(str(r["updated_at"] or "")[:10])
        first = first or fallback
        if not first:
            continue
        if last and last < first:
            first, last = last, first
        conn.execute("UPDATE staff_first_seen SET first_seen=?, last_seen=? WHERE restaurant_id=? AND employee_name=?",
                     (first, last, r["restaurant_id"], r["employee_name"]))
        fixed += 1
    if fixed:
        conn.commit()
    return fixed


def remember_tenure(restaurant_id, shifts: list, db_path=DB_PATH) -> None:
    """Keep the earliest date, the LATEST date and the most shifts ever seen
    for each name, so tenure — and, from last_seen, retention — survives
    the rolling upload window. last_seen only ever moves forward (MAX): an
    older upload re-sent never makes a current employee look departed."""
    if not shifts:
        return
    from labor import _iso_date
    first, last, count = {}, {}, {}
    for s in shifts:
        n = (s.get("employee") or "").strip()
        # ISO or nothing (tenure_date): a "09/01/2026" that reached this
        # table was MIN'd as text and won against every real date forever.
        d = _iso_date(s.get("date")) or ""
        if not n or len(d) != 10:
            continue
        count[n] = count.get(n, 0) + 1
        if n not in first or d < first[n]:
            first[n] = d
        if n not in last or d > last[n]:
            last[n] = d
    conn = get_conn(db_path)
    try:
        for n in count:
            conn.execute(
                "INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen, last_seen) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen), "
                "shifts_seen=MAX(shifts_seen, excluded.shifts_seen), "
                "last_seen=CASE WHEN last_seen IS NULL OR excluded.last_seen > last_seen THEN excluded.last_seen "
                "ELSE last_seen END, updated_at=datetime('now')",
                (restaurant_id, n, first[n], count[n], last[n]))
        conn.commit()
    except Exception as e:
        import ops
        ops.capture(e, job="remember_tenure", context=f"restaurant_id={restaurant_id}")
    finally:
        conn.close()


def forget_stale_names(restaurant_id, window_rows: list, db_path=DB_PATH) -> int:
    """Drop remembered names a POS sync proves were never people, or were
    another spelling of one: last seen INSIDE the synced window, yet absent
    from it - inside its window the POS is the whole record, so anyone who
    worked then is in it. Simple EJ's first RPOWER sync stored payroll codes
    ("0GXXW3") and station logins ("Day Bar"), and tenure / Restaurant DNA
    kept counting them as staff after the names were fixed (9/28/26). A
    person last seen before the window keeps their history. Returns rows
    removed."""
    from staff_settings import name_key
    names = {(r.get("employee") or "").strip() for r in window_rows or [] if (r.get("employee") or "").strip()}
    dates = sorted({(r.get("date") or "")[:10] for r in window_rows or [] if len((r.get("date") or "")[:10]) == 10})
    if not names or not dates:
        return 0
    lo, hi = dates[0], dates[-1]
    # Compared on name_key, and a person's other spelling is folded into the
    # one the sync used before it goes (memory re-audit 9/29/26, FORGET-17):
    # an exact-string match dropped "Dana K."'s earliest first_seen the day
    # the POS started sending "Dana k.", and tenure restarted from the sync.
    by_key = {}
    for n in names:
        by_key.setdefault(name_key(n), n)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name, first_seen, last_seen, shifts_seen FROM staff_first_seen "
                            "WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        have = {r["employee_name"]: r for r in rows}
        stale = []
        for r in rows:
            n = r["employee_name"]
            if n in names or not (r["last_seen"] and lo <= str(r["last_seen"])[:10] <= hi):
                continue
            into = by_key.get(name_key(n))
            if into and into != n:
                # Another spelling of someone the sync names: their earliest
                # day and the most shifts ever counted move to that spelling.
                if into in have:
                    conn.execute("UPDATE staff_first_seen SET first_seen=MIN(first_seen, ?), "
                                 "last_seen=MAX(COALESCE(last_seen, ''), COALESCE(?, '')), "
                                 "shifts_seen=MAX(shifts_seen, ?), updated_at=datetime('now') "
                                 "WHERE restaurant_id=? AND employee_name=?",
                                 (r["first_seen"], r["last_seen"], int(r["shifts_seen"] or 0), restaurant_id, into))
                else:
                    conn.execute("UPDATE staff_first_seen SET employee_name=?, updated_at=datetime('now') "
                                 "WHERE restaurant_id=? AND employee_name=?", (into, restaurant_id, n))
                    have[into] = r
                    continue
            stale.append(n)
        for n in stale:
            conn.execute("DELETE FROM staff_first_seen WHERE restaurant_id=? AND employee_name=?", (restaurant_id, n))
        conn.commit()
        return len(stale)
    except Exception as e:
        import ops
        ops.capture(e, job="forget_stale_names", context=f"restaurant_id={restaurant_id}")
        return 0
    finally:
        conn.close()


def tenure(restaurant_id, from_csv: dict, db_path=DB_PATH) -> dict:
    """{name: shifts} — the larger of what the current upload shows and what
    has been remembered; a person seen since March keeps their tenure when
    the upload window rolls past March."""
    out = dict(from_csv or {})
    conn = get_conn(db_path)
    try:
        for r in conn.execute("SELECT employee_name, shifts_seen, first_seen FROM staff_first_seen WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            n = r["employee_name"]
            seen = int(r["shifts_seen"] or 0)
            # At least as many shifts as were ever counted for them — never
            # a guess upward from a hire date.
            out[n] = max(out.get(n, 0), seen)
    except Exception:
        pass
    finally:
        conn.close()
    return out


def history_weeks(restaurant_id) -> int:
    """How many weeks the shift history spans — an 'unusual day' claim needs a few."""
    from models import _cached_shifts
    try:
        ds = sorted({(s.get("date") or "")[:10] for s in _cached_shifts(restaurant_id) if s.get("date")})
        if len(ds) < 2:
            return 0
        a, b = datetime.strptime(ds[0], "%Y-%m-%d"), datetime.strptime(ds[-1], "%Y-%m-%d")
        return max(0, (b - a).days // 7)
    except Exception:
        return 0
