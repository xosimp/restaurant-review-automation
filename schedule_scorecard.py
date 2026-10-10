"""
schedule_scorecard.py — the Schedule Learning Scorecard: one frozen row per
published week that says whether the drafts are getting better, measured
on what happened rather than on how much of the draft was left untouched
(schedule audit 10/10/26 RM-4, LEARNING-7, AI-29, LEARNING-25).

The only "is it improving" figure used to be the share of drafted rows
that went out unchanged, monthly, two weeks minimum, no interval: a 15-
minute retime and a removed shift weighed the same, Cavnar AI's own kept
fixes lowered it, a manager who stopped looking read as improvement, and
no outcome was tracked over time. Nobody could say whether a fix round, the
memory or the solver made schedules better.

Per published week (`schedule_learning_scores`, computed by the Monday job
`run_scorecard`, re-read while the week's late data lands and FROZEN
FREEZE_AFTER_DAYS after it ended — never recomputed after that):

  drr                 Draft Rework Rate: the manager's own pre-publish
                      edit items per 100 drafted rows, weighted (an add or
                      a removal 1.0, a move 1.0, a role change 0.5, a
                      retime 0.25 per 30 minutes moved), with a 90%
                      interval; Cavnar AI's kept fixes and an admin's hand
                      are not the manager's rework. None for a week nobody
                      of the restaurant looked at (the automatic publish).
  requirement error   per role family × date × daypart, |asked − published|
                      (what the draft was asked for — kept with the
                      generation since 10/10/26 — against the heads that
                      went out) and |published − punched| (against the
                      heads that clocked in), mean absolute
  labor               the week's hourly labor % (labor_daily_history's
                      final days) against the hourly target the budget is
                      held to (the owner's all-in target less the salaried
                      share — schedule_economics._hourly_target_pct)
  overtime            hours past the line worked (the punches, hourly
                      staff) beside those the published week planned
  unmanaged minutes   minutes on the floor with no manager AS RUN — the
                      punches, the salaried managers' published shifts
                      (they don't clock in) — by the owner-facing verdict
                      (schedule_rules.violations no_manager, with the
                      owner's opening and closing allowances)
  shortfall           person-hours the punches left short of the asked
                      requirement
  uncovered no-shows  a no-show or call-off nobody covered
  issues / reviews    coverage and no-show issues on watched nights; the
                      mean review rating put on a shift by its meal
  forecast            the frozen week projection's error beside the naive
                      8-week mean's (forecast_log revenue_week), and the skill
  attribution         the code commit and experiment arms that made the
                      draft; per row ORIGIN (AI-29: the model, the managers'
                      plan, a repair stage that added or changed it, a pass
                      after the loop) and how many of each survived to the
                      published week untouched; the generation's model cost;
                      the owner's rules the draft honoured; and the LEARNING
                      ABLATION — the same repair loop run again over the
                      same model answer with the learned memory switched off
                      (signals["learned"] empty), and both drafts' rework
                      distance to the week the manager settled on: the
                      learned memory's effect, with no customer exposure.

A measurement that is not there is None, never 0 (a restaurant with no
POS has no unmanaged minutes as run, not zero of them).

Readouts: `owner_view` (the restaurant's own weeks, in plain words — the
labor intel payload and GET labor/learning-scorecard) and `admin_view`
(internal: every eligible restaurant's latest weeks, the within-restaurant
trend of each figure over weeks since onboarding with a restaurant-cluster
90% interval — "is it improving" as a slope with an interval, not a
monthly mean — the ablation's mean effect, origin survival, the cost per
accepted week and the instruction-honoured rate).
"""
import json
import logging
import os
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

SCORECARD_VERSION = 1
FREEZE_AFTER_DAYS = 14
SCORE_WEEKS = 12                    # the Monday job reads each restaurant's published weeks this far back
EDIT_WEIGHTS = {"added": 1.0, "removed": 1.0, "moved": 1.0, "role_changed": 0.5}
RETIME_WEIGHT_PER_30 = 0.25
DEFAULT_SLOT_HOURS = 4.0
# The learning ablation (run at generation, schedule_engine): bounded, and
# switched off with SCHEDULE_LEARNING_ABLATION=0.
ABLATION_ENV = "SCHEDULE_LEARNING_ABLATION"
ABLATION_MAX_SECONDS = 20.0
ABLATION_MIN_CLOCK_SECONDS = 120.0
# How far back a week's generation tags are read (the job's window and the
# freeze after it) — the retention registry's reader window (ops).
TAGS_READ_DAYS = SCORE_WEEKS * 7 + FREEZE_AFTER_DAYS
RUN_MAX_SECONDS = 15 * 60
CURSOR_KEY = "schedule_scorecard_cursor"
ADMIN_MAX_RESTAURANTS = 500


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_schedule_scorecard(db_path=None):
    """Boot DDL (models.init_db): the generation's attribution and the
    per-week scorecard rows."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_generation_tags (
            history_id      INTEGER PRIMARY KEY,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            generation_id   TEXT,
            commit_sha      TEXT,
            arms_json       TEXT,
            origins_json    TEXT,
            ablation_json   TEXT,
            rules_json      TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_gen_tags_rid ON schedule_generation_tags(restaurant_id)")
        # The retention sweep deletes by created_at (ops._RETENTION_DAYS):
        # without its own index that delete scans the whole table.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_gen_tags_created "
                     "ON schedule_generation_tags(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_learning_scores (
            restaurant_id      INTEGER NOT NULL REFERENCES restaurants(id),
            history_id         INTEGER NOT NULL,
            week_start         TEXT,
            week_end           TEXT,
            status             TEXT    NOT NULL DEFAULT 'open',
            version            INTEGER NOT NULL DEFAULT 1,
            looked_at          INTEGER,
            rows_drafted       INTEGER,
            drr                REAL,
            drr_lo             REAL,
            drr_hi             REAL,
            req_error_asked    REAL,
            req_error_punched  REAL,
            labor_pct          REAL,
            hourly_target      REAL,
            labor_vs_target    REAL,
            ot_hours           REAL,
            ot_hours_planned   REAL,
            unmanaged_minutes  REAL,
            shortfall_hours    REAL,
            uncovered_no_shows INTEGER,
            issues             INTEGER,
            review_rating      REAL,
            forecast_error     REAL,
            naive_error        REAL,
            forecast_skill     REAL,
            ablation_effect    REAL,
            metrics_json       TEXT,
            computed_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            frozen_at          TEXT,
            PRIMARY KEY (restaurant_id, history_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_scores_week ON schedule_learning_scores(restaurant_id, "
                     "week_start)")
        conn.commit()
    finally:
        conn.close()


# ── the drafts' distance to a week (the rework a draft needed) ─────────────

def _minutes(value):
    from schedule_rules import parse_minutes
    return parse_minutes(value or "")


def edit_weight(kind, item) -> float:
    """One edit item's weight in the Draft Rework Rate."""
    if kind == "retimed":
        moved = 0
        for a, b in (("old_start", "new_start"), ("old_end", "new_end")):
            x, y = _minutes((item or {}).get(a)), _minutes((item or {}).get(b))
            if x is not None and y is not None:
                d = abs(y - x)
                moved += min(d, 1440 - d)
        return max(RETIME_WEIGHT_PER_30, RETIME_WEIGHT_PER_30 * round(moved / 30.0))
    return EDIT_WEIGHTS.get(kind, 1.0)


def distance(draft_rows, final_rows) -> dict:
    """The rework `draft_rows` would need to become `final_rows`: weighted
    edit items per 100 drafted rows with a 90% interval
    (learning_stats.weighted_rate_ci) — the DRR of any draft, the learning
    ablation's two drafts included."""
    import learning_stats
    import schedule_versions as sv
    weights = [edit_weight(kind, item) for kind, _keys, item in sv.change_items(sv.diff(draft_rows or [],
                                                                                      final_rows or []))]
    return learning_stats.weighted_rate_ci(weights, len(draft_rows or []))


def drr(rec) -> dict:
    """The Draft Rework Rate of one week's learning record (schedule_versions.
    learning_weeks): the manager's own pre-publish edit items — Cavnar AI's
    kept fixes and an admin's hand left out; a change the owner called a
    one-off is still rework the draft needed — per 100 drafted rows."""
    import learning_stats
    weights = [edit_weight(e["kind"], e["item"]) for e in (rec or {}).get("items") or []
               if e.get("origin") == "manager" and e.get("excluded") in (None, "answered")]
    out = learning_stats.weighted_rate_ci(weights, len((rec or {}).get("base") or []))
    out["kinds"] = {}
    for e in (rec or {}).get("items") or []:
        if e.get("origin") == "manager" and e.get("excluded") in (None, "answered"):
            out["kinds"][e["kind"]] = out["kinds"].get(e["kind"], 0) + 1
    return out


# ── the row origins (AI-29) ─────────────────────────────────────────────────

def _sig(r) -> str:
    import schedule_versions as sv
    return sv.row_sig(r)


def _person_day(r):
    return (str(r.get("date") or "")[:10], " ".join(str(r.get("employee") or "").split()).lower())


def loop_info(pre_rows, loop_rows, loop_report, x_origin) -> dict:
    """{row signature: {"added": stage | None, "changed": [stages]}} for the
    repair loop's output rows: a row a stage added (RepairContext.origin),
    a model row a stage changed (its fix lines, trims and staggers name the
    stage; a search that moved it is "search"), or neither."""
    pre = {r.get("_rid"): r for r in pre_rows or [] if r.get("_rid")}
    touched = {}
    for f in (loop_report or {}).get("fixes") or []:
        touched.setdefault(f.get("row_id"), []).append(f.get("stage") or "fix")
    for t in (loop_report or {}).get("trimmed") or []:
        touched.setdefault(t.get("row_id"), []).append(t.get("stage") or "budget")
    for s_ in (loop_report or {}).get("staggered") or []:
        touched.setdefault(s_.get("row_id"), []).append("stagger")
    out = {}
    for r in loop_rows or []:
        rid = r.get("_rid")
        added = (x_origin or {}).get(rid)
        changed = list(dict.fromkeys(touched.get(rid) or []))
        if not added and rid in pre and _sig(pre[rid]) != _sig(r) and not changed:
            changed = ["search"]
        out[_sig(r)] = {"added": added, "changed": changed}
    return out


def classify_rows(pre_rows, final_rows, info=None) -> dict:
    """{row signature: origin} for the rows of the week the owner gets:
      manager_plan        the managers' planned shifts (schedule_skeleton)
      model               exactly as the model wrote it
      model_edited:<st>   the model's person-day, changed by a repair stage
      added:<st>          a row a repair stage added
      post_loop           added or changed after the repair loop (the gate,
                          a restore) — unattributed
    (schedule audit 10/10/26 AI-29: nobody could say whether owners edit
    what the model wrote or what code added)."""
    import schedule_skeleton
    model, plan, model_days = set(), set(), set()
    for r in pre_rows or []:
        if schedule_skeleton.is_plan_note(r.get("notes")):
            plan.add(_sig(r))
        else:
            model.add(_sig(r))
            model_days.add(_person_day(r))
    out = {}
    for r in final_rows or []:
        s_ = _sig(r)
        if schedule_skeleton.is_plan_note(r.get("notes")) or s_ in plan:
            out[s_] = "manager_plan"
        elif s_ in model:
            out[s_] = "model"
        elif info and s_ in info:
            i = info[s_]
            if i.get("added") and _person_day(r) not in model_days:
                out[s_] = f"added:{i['added']}"
            else:
                st = (i.get("changed") or [i.get("added") or "search"])[-1]
                out[s_] = f"model_edited:{st}"
        else:
            out[s_] = "post_loop"
    return out


def origin_family(origin) -> str:
    """The origin's family for the readout: model | manager_plan |
    model_edited | added | post_loop."""
    return str(origin or "post_loop").split(":", 1)[0]


def survival(origins, base_rows, published_rows) -> dict:
    """{origin family: {rows, kept, share}} — how many drafted rows of each
    origin went out untouched (same date, person, start, end and role)."""
    pub = {}
    for r in published_rows or []:
        pub[_sig(r)] = pub.get(_sig(r), 0) + 1
    out = {}
    for r in base_rows or []:
        s_ = _sig(r)
        fam = origin_family((origins or {}).get(s_, "post_loop"))
        e = out.setdefault(fam, {"rows": 0, "kept": 0})
        e["rows"] += 1
        if pub.get(s_):
            pub[s_] -= 1
            e["kept"] += 1
    for e in out.values():
        e["share"] = round(e["kept"] / float(e["rows"]), 3) if e["rows"] else None
    return out


def ablation_wanted(learned, clock=None) -> bool:
    """Whether the generation runs the learning ablation: something learned
    binds this week, the switch is not off, and the job's clock has room."""
    if not learned or (os.environ.get(ABLATION_ENV) or "1").strip() in ("0", "off", "false"):
        return False
    try:
        return clock is None or clock.seconds_left() > ABLATION_MIN_CLOCK_SECONDS
    except Exception:
        return False


def compact(rows) -> list:
    return [{k: r.get(k) for k in ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours",
                                   "notes")} for r in rows or []]


def tag_generation(restaurant_id, history_id, generation_id=None, arms=None, origins=None, ablation=None,
                   rules=None, db_path=None) -> bool:
    """Keep what made a saved draft: the code commit, the experiment arms,
    each row's origin, the learning ablation, the owner's rules it honoured.
    Never raises into the generation."""
    if not history_id:
        return False
    try:
        commit = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "")[:12] or None
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO schedule_generation_tags (history_id, restaurant_id, generation_id, commit_sha, arms_json, "
                "origins_json, ablation_json, rules_json) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(history_id) DO UPDATE "
                "SET generation_id=excluded.generation_id, commit_sha=excluded.commit_sha, arms_json=excluded.arms_json, "
                "origins_json=excluded.origins_json, ablation_json=excluded.ablation_json, "
                "rules_json=excluded.rules_json",
                (int(history_id), restaurant_id, generation_id, commit,
                 json.dumps([{"experiment": a.get("experiment"), "arm": a.get("arm"), "pinned": bool(a.get("pinned"))}
                             for a in arms or []]),
                 json.dumps(origins or {}), json.dumps(ablation, default=str) if ablation else None,
                 json.dumps(rules, default=str) if rules else None))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log.warning("generation tags not kept for history %s: %s", history_id, e)
        return False


def generation_tags(history_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM schedule_generation_tags WHERE history_id=?", (int(history_id),)).fetchone()
    except Exception:
        return {}
    finally:
        conn.close()
    if r is None:
        return {}
    d = dict(r)
    for k in ("arms_json", "origins_json", "ablation_json", "rules_json"):
        try:
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        except (TypeError, ValueError):
            d[k[:-5]] = None
    return d


def rules_honoured(constraints, violations) -> dict:
    """{rules, broken, rate}: the owner's checked rules the draft kept — a
    rule is broken when the final sweep has an owner_rule breach of it."""
    rules = list(getattr(constraints, "owner_rules", None) or [])
    if not rules:
        return {"rules": 0, "broken": 0, "rate": None,
                "unchecked": len(getattr(constraints, "owner_rules_unchecked", None) or [])}
    hit = {str(v.get("floor_role") or "").strip().lower() for v in violations or [] if v.get("kind") == "owner_rule"}
    n_broken = sum(1 for r in rules if str(r.get("family") or r.get("role") or "").strip().lower() in hit)
    return {"rules": len(rules), "broken": n_broken, "rate": round((len(rules) - n_broken) / float(len(rules)), 3),
            "unchecked": len(getattr(constraints, "owner_rules_unchecked", None) or [])}


# ── one week's figures ──────────────────────────────────────────────────────

def _week_dates(week_start, week_end):
    try:
        d0 = date.fromisoformat(str(week_start)[:10])
        d1 = date.fromisoformat(str(week_end or week_start)[:10])
    except (TypeError, ValueError):
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range(max(0, min(13, (d1 - d0).days)) + 1)]


def _heads_by_family(rows, families):
    from shift_quality import present_dayparts, role_family
    out = {}
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        who = " ".join(str(r.get("employee") or "").split()).lower()
        if not d or not who:
            continue
        fam = role_family(r.get("role"), families)
        for part in present_dayparts(r):
            if part in ("morning", "night", "late"):
                out.setdefault((d, part, fam), set()).add(who)
    return {k: len(v) for k, v in out.items()}


def _slot_hours(rows, families):
    from shift_quality import present_dayparts, role_family
    by = {}
    for r in rows or []:
        try:
            h = float(r.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            continue
        if h <= 0:
            continue
        fam = role_family(r.get("role"), families)
        for part in present_dayparts(r):
            by.setdefault((str(r.get("date") or "")[:10], part, fam), []).append(h)
    return {k: sorted(v)[len(v) // 2] for k, v in by.items()}


def compute_week(restaurant_id, history_id, today=None, db_path=None) -> dict:
    """Every figure of one published week (module doc). None where the week
    has no measurement of it."""
    import learning_stats
    import models
    import schedule_versions as sv
    from shift_quality import role_family
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        h = conn.execute("SELECT id, week_start, week_end, schedule_csv, published_at, economics_json FROM "
                         "schedule_history WHERE id=? AND restaurant_id=?", (int(history_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if h is None or not h["published_at"]:
        return {}
    restaurant = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
    try:
        import schedule_rules
        families = schedule_rules.role_families(restaurant) or {}
    except Exception:
        families = {}
    dates = _week_dates(h["week_start"], h["week_end"])
    out = {"history_id": int(history_id), "week_start": str(h["week_start"])[:10],
           "week_end": str(h["week_end"] or h["week_start"])[:10], "version": SCORECARD_VERSION}
    kw = {"db_path": db_path} if db_path else {}
    recs = sv.learning_weeks(restaurant_id, history_ids=[int(history_id)], include_unlooked=True, today=today, **kw)
    rec = recs[0] if recs else None
    published = sv.rows_from_csv(h["schedule_csv"] or "")
    out["looked_at"] = bool(rec and rec["looked_at"])
    out["generated"] = rec is not None
    if rec is not None:
        out["rows_drafted"] = len(rec["base"])
        d = drr(rec) if rec["looked_at"] else None
        out["drr"] = d
        # Requirement error against what the draft was ASKED for.
        asked = {}
        for (dd, part, role), n in (rec.get("asked") or {}).items():
            k = (dd, part, role_family(role, families))
            asked[k] = asked.get(k, 0) + int(n or 0)
        pub_heads = _heads_by_family(rec.get("final_all") or published, families)
        if asked:
            errs = [abs(n - pub_heads.get(k, 0)) for k, n in asked.items()]
            out["req_error_asked"] = round(sum(errs) / len(errs), 3)
        out["asked_slots"] = len(asked)
    else:
        out["rows_drafted"] = None
        out["drr"] = None
        asked, pub_heads = {}, _heads_by_family(published, families)
    # The punches for the week (hourly staff), and the salaried people's
    # published shifts (they don't clock in).
    try:
        salaried = set(models.salaried_keys(restaurant))
    except Exception:
        salaried = set()
    punches = []
    if dates:
        try:
            import shift_facts
            punches = [p for p in shift_facts.rows(restaurant_id, since=dates[0], until=dates[-1], db_path=db_path)
                       if " ".join(str(p.get("employee") or "").split()).casefold() not in salaried]
        except Exception as e:
            log.warning("scorecard punches unreadable rid=%s: %s", restaurant_id, e)
    punched_dates = {str(p.get("date") or "")[:10] for p in punches}
    week_over = bool(dates) and date.fromisoformat(dates[-1]) < today
    if punches:
        punched_heads = _heads_by_family(punches, families)
        if asked:
            keys = [k for k in asked if k[0] in punched_dates]
            if keys:
                out["req_error_punched"] = round(sum(abs(pub_heads.get(k, 0) - punched_heads.get(k, 0)) for k in keys)
                                                 / len(keys), 3)
                hours = _slot_hours(published, families)
                out["shortfall_hours"] = round(sum(max(0, asked[k] - punched_heads.get(k, 0))
                                                   * hours.get(k, DEFAULT_SLOT_HOURS) for k in keys), 1)
        # Overtime worked against overtime planned (the hourly staff).
        try:
            from labor import OVERTIME_THRESHOLD_HOURS as LINE
        except Exception:
            LINE = 40.0
        worked, planned = {}, {}
        for p in punches:
            try:
                hrs = float(p.get("actual_hours") if p.get("actual_hours") is not None else p.get("scheduled_hours"))
            except (TypeError, ValueError):
                continue
            k = " ".join(str(p.get("employee") or "").split()).casefold()
            worked[k] = worked.get(k, 0.0) + hrs
        for r in published:
            k = " ".join(str(r.get("employee") or "").split()).casefold()
            if k in salaried:
                continue
            try:
                planned[k] = planned.get(k, 0.0) + float(r.get("scheduled_hours") or 0)
            except (TypeError, ValueError):
                continue
        # Overtime worked is read only once the punches cover the days the
        # week was scheduled on (one may be missing): a POS sync that
        # stopped mid-week is not a week of no overtime.
        pub_dates = {str(r.get("date") or "")[:10] for r in published}
        if week_over and len(pub_dates - punched_dates) <= 1:
            out["ot_hours"] = round(sum(max(0.0, v - LINE) for v in worked.values()), 2)
        out["ot_hours_planned"] = round(sum(max(0.0, v - LINE) for v in planned.values()), 2)
        # Minutes with no manager on, as the week ran (the owner-facing
        # verdict: the opening prep and closing-stay allowances apply).
        if week_over:
            try:
                import schedule_rules as _rules
                days = [datetime.strptime(x, "%Y-%m-%d").strftime("%A") for x in dates]
                c = _rules.build_constraints(restaurant_id, dates, days, restaurant=restaurant)
                ran = [dict(p) for p in punches if str(p.get("date") or "")[:10] in punched_dates]
                ran += [dict(r) for r in published if " ".join(str(r.get("employee") or "").split()).casefold()
                        in salaried and str(r.get("date") or "")[:10] in punched_dates]
                viols = _rules.violations(ran, c)
                # Nobody on the roster counts as a manager: the minutes are
                # not measurable, not zero.
                if not any(v.get("kind") == "no_manager_roster" for v in viols) and \
                        (getattr(c, "managers", None) or getattr(c, "acting_managers", None)):
                    out["unmanaged_minutes"] = float(sum(int(v.get("minutes") or 0) for v in viols
                                                         if v.get("kind") == "no_manager"))
            except Exception as e:
                log.warning("scorecard unmanaged minutes unreadable rid=%s: %s", restaurant_id, e)
    # Hourly labor % against the hourly target.
    conn = get_conn(db_path)
    try:
        if dates:
            from canonical_facts import FINAL_SQL
            row = conn.execute(f"SELECT SUM(labor_pct * sales) AS w, SUM(sales) AS s, COUNT(*) AS n FROM "
                               f"labor_daily_history WHERE restaurant_id=? AND sales > 0 AND labor_pct IS NOT NULL "
                               f"AND date >= ? AND date <= ? AND {FINAL_SQL}", (restaurant_id, dates[0], dates[-1])
                               ).fetchone()
            if row and row["s"]:
                out["labor_pct"] = round(float(row["w"]) / float(row["s"]), 2)
                try:
                    # The ONE read of the target (thresholds.target_for,
                    # re-audit #10): the owner's goal first, then the setting.
                    import thresholds as _thr
                    import schedule_economics as _econ
                    _t = _thr.target_for(restaurant, "labor")
                    tgt = _t.get("pct")
                    if tgt:
                        ht, _sal = _econ._hourly_target_pct(restaurant_id, float(tgt), int(row["n"] or 0),
                                                            float(row["s"]), **kw)
                        out["hourly_target"] = round(float(ht), 2)
                        out["labor_vs_target"] = round(out["labor_pct"] - out["hourly_target"], 2)
                except Exception as e:
                    log.warning("scorecard hourly target unreadable rid=%s: %s", restaurant_id, e)
            # Missed shifts nobody covered.
            try:
                n = conn.execute("SELECT COUNT(*) FROM attendance_events WHERE restaurant_id=? AND business_date >= ? "
                                 "AND business_date <= ? AND outcome IN ('no_show', 'called_out') AND "
                                 "COALESCE(TRIM(covered_by), '') = ''", (restaurant_id, dates[0], dates[-1])).fetchone()[0]
                if week_over:
                    out["uncovered_no_shows"] = int(n or 0)
            except Exception:
                pass
            # Issues on watched nights, and the reviews put on a shift.
            try:
                outs = conn.execute("SELECT date, issues, review_rating_attributed AS rating, review_attribution AS ra "
                                    "FROM schedule_outcomes WHERE restaurant_id=? AND history_id=?",
                                    (restaurant_id, int(history_id))).fetchall()
            except Exception:
                outs = []
            if outs:
                try:
                    import schedule_intel
                    watched = schedule_intel.watched_dates(restaurant_id, dates[0], dates[-1], **kw)
                except Exception:
                    watched = set()
                if watched:
                    out["issues"] = int(sum(int(o["issues"] or 0) for o in outs if str(o["date"])[:10] in watched))
                rated = [float(o["rating"]) for o in outs if o["rating"] is not None and o["ra"] == "daypart"]
                if rated:
                    out["review_rating"] = round(sum(rated) / len(rated), 2)
            # The frozen week projection against the naive mean.
            try:
                f = conn.execute("SELECT predicted, actual, error_pct, naive_mean FROM forecast_log WHERE "
                                 "restaurant_id=? AND kind='revenue_week' AND horizon_end >= ? AND horizon_end <= ? "
                                 "AND actual IS NOT NULL ORDER BY id DESC LIMIT 1",
                                 (restaurant_id, dates[0], dates[-1])).fetchone()
            except Exception:
                f = None
            if f and f["actual"]:
                actual = float(f["actual"])
                out["forecast_error"] = round(abs(float(f["predicted"]) - actual) / actual * 100.0, 2)
                if f["naive_mean"] is not None:
                    out["naive_error"] = round(abs(float(f["naive_mean"]) - actual) / actual * 100.0, 2)
                    if out["naive_error"] > 0:
                        out["forecast_skill"] = round(1.0 - out["forecast_error"] / out["naive_error"], 3)
            # The generation's model cost.
            try:
                calls = conn.execute("SELECT model, usage_json FROM schedule_model_calls WHERE history_id=?",
                                     (int(history_id),)).fetchall()
            except Exception:
                calls = []
    finally:
        conn.close()
    cost = None
    for c_ in calls if dates else []:
        try:
            from ai_utils import _estimate_cost
            u = json.loads(c_["usage_json"] or "{}") or {}
            cost = (cost or 0.0) + float(_estimate_cost(c_["model"], u.get("input_tokens"), u.get("output_tokens"),
                                                        u.get("cache_write_tokens"), u.get("cache_read_tokens")) or 0)
        except Exception:
            continue
    out["model_cost_usd"] = round(cost, 4) if cost is not None else None
    # Did the week run the way the draft said (LEARNING-16: the planned and
    # as-run scores the outcomes job keeps, sq_planned / sq_as_run, read
    # here): the mean as-run minus planned Shift Quality over the shifts with
    # both — a plan-adherence check, never an outcome.
    try:
        import schedule_memory as _sm
        obs = _sm.observations(restaurant_id, kinds=("sq_as_run", "sq_planned"), db_path=db_path)
        ran, plan = {}, {}
        for o in obs:
            if o.get("history_id") != int(history_id) or not o.get("date"):
                continue
            sc = (o.get("value") or {}).get("score")
            if sc is None:
                continue
            (ran if o["kind"] == "sq_as_run" else plan)[(o["date"], o.get("daypart"))] = float(sc)
        both = [ran[k] - plan[k] for k in ran if k in plan]
        if both:
            out["plan_adherence"] = {"shifts": len(both), "mean_delta": round(sum(both) / len(both), 2)}
    except Exception as e:
        log.warning("scorecard plan adherence unreadable rid=%s: %s", restaurant_id, e)
    # Attribution: the commit and arms, each row's origin and its survival,
    # the owner's rules honoured, and the learning ablation.
    tags = generation_tags(history_id, db_path=db_path)
    out["commit"] = tags.get("commit_sha")
    out["arms"] = tags.get("arms") or []
    out["rules"] = tags.get("rules")
    if rec is not None and tags.get("origins"):
        pub = rec.get("final_owner") or published
        out["origin_survival"] = survival(tags["origins"], rec["base"], pub)
    ab = tags.get("ablation") or {}
    if rec is not None and rec["looked_at"] and ab.get("ran") and ab.get("rows_on") is not None:
        final = rec.get("final") or []
        on, off = distance(ab["rows_on"], final), distance(ab.get("rows_off") or [], final)
        out["ablation"] = {"learned": ab.get("learned"), "drr_on": on, "drr_off": off,
                           "effect": (round(off["rate"] - on["rate"], 2)
                                      if on.get("rate") is not None and off.get("rate") is not None else None),
                           "seconds": ab.get("seconds")}
    return out


_COLS = ("looked_at", "rows_drafted", "req_error_asked", "req_error_punched", "labor_pct", "hourly_target",
         "labor_vs_target", "ot_hours", "ot_hours_planned", "unmanaged_minutes", "shortfall_hours",
         "uncovered_no_shows", "issues", "review_rating", "forecast_error", "naive_error", "forecast_skill")


def store_week(restaurant_id, m, today=None, db_path=None) -> str:
    """Upsert one week's figures; freeze it once FREEZE_AFTER_DAYS have
    passed since it ended. A frozen row is never written again. Returns
    'frozen', 'open' or 'kept' (already frozen)."""
    today = today or date.today()
    if not m:
        return "skipped"
    try:
        frozen = date.fromisoformat(m["week_end"]) + timedelta(days=FREEZE_AFTER_DAYS) <= today
    except (TypeError, ValueError, KeyError):
        frozen = False
    d = m.get("drr") or {}
    ab = m.get("ablation") or {}
    vals = {c: m.get(c) for c in _COLS}
    vals["looked_at"] = 1 if m.get("looked_at") else 0
    conn = get_conn(db_path)
    try:
        cur = conn.execute("SELECT status FROM schedule_learning_scores WHERE restaurant_id=? AND history_id=?",
                           (restaurant_id, m["history_id"])).fetchone()
        if cur is not None and cur["status"] == "frozen":
            return "kept"
        cols = ["restaurant_id", "history_id", "week_start", "week_end", "status", "version", "drr", "drr_lo", "drr_hi",
                "ablation_effect", "metrics_json", "frozen_at"] + list(_COLS)
        row = [restaurant_id, m["history_id"], m.get("week_start"), m.get("week_end"), "frozen" if frozen else "open",
               SCORECARD_VERSION, d.get("rate"), d.get("lo"), d.get("hi"), ab.get("effect"),
               json.dumps(m, default=str), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") if frozen else None] + \
            [vals[c] for c in _COLS]
        conn.execute(f"INSERT INTO schedule_learning_scores ({', '.join(cols)}, computed_at) VALUES "
                     f"({', '.join('?' for _ in cols)}, datetime('now')) ON CONFLICT(restaurant_id, history_id) DO UPDATE SET "
                     + ", ".join(f"{c}=excluded.{c}" for c in cols[2:]) + ", computed_at=excluded.computed_at", row)
        conn.commit()
    finally:
        conn.close()
    return "frozen" if frozen else "open"


def score_restaurant(restaurant_id, today=None, db_path=None, weeks=SCORE_WEEKS) -> dict:
    """Every published week of the last `weeks` that has ended and is not
    frozen yet, (re)computed and stored. {weeks, frozen, open}."""
    today = today or date.today()
    since = (today - timedelta(weeks=int(weeks))).isoformat()
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT h.id FROM schedule_history h WHERE h.restaurant_id=? AND h.published_at IS NOT NULL AND "
            "h.superseded_by IS NULL AND h.week_end < ? AND h.week_start >= ? AND NOT EXISTS (SELECT 1 FROM "
            "schedule_learning_scores s WHERE s.restaurant_id=h.restaurant_id AND s.history_id=h.id AND "
            "s.status='frozen') ORDER BY h.week_start", (restaurant_id, today.isoformat(), since)).fetchall()
    finally:
        conn.close()
    out = {"weeks": 0, "frozen": 0, "open": 0}
    for h in hist:
        m = compute_week(restaurant_id, h["id"], today=today, db_path=db_path)
        st = store_week(restaurant_id, m, today=today, db_path=db_path)
        out["weeks"] += 1
        if st in ("frozen", "open"):
            out[st] += 1
    return out


def run_scorecard(db_path=None, today=None) -> dict:
    """The Monday job (after schedule_outcomes and the memory): every Labor
    restaurant that may teach, bounded by RUN_MAX_SECONDS and resumable
    from CURSOR_KEY (scheduler.resumable_sweep). The standard counts."""
    import models
    import scheduler as _sched
    ids = []
    for r in models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants():
        if not getattr(r, "module_labor", 0):
            continue
        try:
            if not models.learns_for_itself(r, db_path=db_path):
                continue
        except Exception:
            continue
        ids.append(r.id)
    tally = {"attempted": 0, "failed": 0, "weeks": 0, "frozen": 0}

    def _one(rid):
        tally["attempted"] += 1
        try:
            got = score_restaurant(rid, today=today, db_path=db_path)
            tally["weeks"] += got["weeks"]
            tally["frozen"] += got["frozen"]
        except Exception as e:
            tally["failed"] += 1
            import ops
            ops.capture(e, job="schedule_scorecard", context=f"restaurant_id={rid}", restaurant_id=rid)
    _done, ran_out = _sched.resumable_sweep(CURSOR_KEY, sorted(ids), _one, RUN_MAX_SECONDS, workers=1,
                                            job="schedule_scorecard")
    return {"attempted": tally["attempted"], "ok": tally["attempted"] - tally["failed"], "failed": tally["failed"],
            "skipped": 0, "hit_bound": bool(ran_out), "weeks": tally["weeks"], "frozen": tally["frozen"]}


# ── the readouts ────────────────────────────────────────────────────────────

def _rows(where, args, db_path=None, limit=5000) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM schedule_learning_scores WHERE " + where + " ORDER BY week_start DESC LIMIT ?",
                            (*args, int(limit))).fetchall()
    except Exception as e:
        log.warning("scorecard unreadable: %s", e)
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}") or {}
        except (TypeError, ValueError):
            d["metrics"] = {}
        out.append(d)
    return out


def _fmt(v, unit="", digits=1):
    if v is None:
        return None
    return f"{round(float(v), digits):g}{unit}"


def owner_view(restaurant_id, weeks=12, db_path=None) -> dict:
    """The restaurant's own scorecard: per published week (newest first),
    the figures with their week in M/D/YY and plain lines; and the trend of
    the rework rate (a slope with its interval, weeks oldest first) once
    there are enough weeks. Absent figures are absent."""
    from time_utils import mdy
    import learning_stats
    rows = _rows("restaurant_id=?", (restaurant_id,), db_path=db_path, limit=weeks)
    out_weeks = []
    for r in rows:
        lines = []
        if r["drr"] is not None:
            lines.append(f"{_fmt(r['drr'])} edits per 100 drafted shifts before it went out"
                         + (f" (90% range {_fmt(r['drr_lo'])}–{_fmt(r['drr_hi'])})" if r["drr_hi"] is not None else ""))
        if r["labor_pct"] is not None:
            lines.append(f"Hourly labor ran {_fmt(r['labor_pct'], '%')}"
                         + (f" against a {_fmt(r['hourly_target'], '%')} hourly target" if r["hourly_target"] is not None
                            else ""))
        if r["ot_hours"] is not None:
            lines.append(f"{_fmt(r['ot_hours'], 'h', 2)} of overtime worked"
                         + (f" ({_fmt(r['ot_hours_planned'], 'h', 2)} planned)" if r["ot_hours_planned"] else ""))
        if r["unmanaged_minutes"] is not None:
            lines.append(f"{int(r['unmanaged_minutes'])} minutes on the floor with no manager, as the week ran")
        if r["shortfall_hours"] is not None:
            lines.append(f"{_fmt(r['shortfall_hours'], 'h')} short of the staffing the draft was asked for")
        if r["uncovered_no_shows"] is not None:
            lines.append(f"{r['uncovered_no_shows']} no-show{'s' if r['uncovered_no_shows'] != 1 else ''} nobody covered")
        if r["forecast_error"] is not None:
            lines.append(f"Sales forecast off by {_fmt(r['forecast_error'], '%')}"
                         + (f" (a plain 8-week average was off by {_fmt(r['naive_error'], '%')})"
                            if r["naive_error"] is not None else ""))
        out_weeks.append({"history_id": r["history_id"], "week": mdy(r["week_start"]), "week_start": r["week_start"],
                          "status": r["status"], "looked_at": bool(r["looked_at"]),
                          "drr": r["drr"], "drr_lo": r["drr_lo"], "drr_hi": r["drr_hi"],
                          "labor_pct": r["labor_pct"], "hourly_target": r["hourly_target"],
                          "labor_vs_target": r["labor_vs_target"], "ot_hours": r["ot_hours"],
                          "ot_hours_planned": r["ot_hours_planned"], "unmanaged_minutes": r["unmanaged_minutes"],
                          "shortfall_hours": r["shortfall_hours"], "uncovered_no_shows": r["uncovered_no_shows"],
                          "issues": r["issues"], "review_rating": r["review_rating"],
                          "forecast_error": r["forecast_error"], "naive_error": r["naive_error"],
                          "req_error_asked": r["req_error_asked"], "req_error_punched": r["req_error_punched"],
                          "lines": lines})
    pts = [(restaurant_id, i, w["drr"]) for i, w in enumerate(reversed(out_weeks)) if w["drr"] is not None]
    trend = learning_stats.fe_slope_ci(pts) if len(pts) >= 4 else None
    sentence = None
    if trend and trend.get("slope") is not None:
        if trend["hi"] < 0:
            sentence = "Your drafts are needing fewer edits week over week."
        elif trend["lo"] > 0:
            sentence = "Your drafts are needing more edits lately — tell Cavnar AI what keeps changing."
        else:
            sentence = "No clear change yet in how much each draft needs."
    return {"ok": True, "available": bool(out_weeks), "weeks": out_weeks, "trend": trend, "trend_sentence": sentence,
            "frozen_after_days": FREEZE_AFTER_DAYS}


TREND_FIGURES = ("drr", "labor_vs_target", "ot_hours", "unmanaged_minutes", "shortfall_hours", "uncovered_no_shows",
                 "req_error_asked", "forecast_error")


def admin_view(db_path=None) -> dict:
    """Internal: the platform scorecard (module doc). Eligible restaurants
    only (intelligence.jobs.excluded_learning_ids)."""
    import learning_stats
    try:
        from intelligence.jobs import excluded_learning_ids
        excluded = set(excluded_learning_ids(db_path=db_path) if db_path else excluded_learning_ids())
    except Exception as e:
        log.warning("scorecard: eligibility unreadable: %s", e)
        return {"ok": False, "error": "Which restaurants may teach could not be read."}
    rows = [r for r in _rows("1=1", (), db_path=db_path, limit=20000) if r["restaurant_id"] not in excluded]
    by = {}
    for r in rows:
        by.setdefault(r["restaurant_id"], []).append(r)
    rids = sorted(by)[:ADMIN_MAX_RESTAURANTS]
    trend = {}
    for fig in TREND_FIGURES:
        pts = []
        for rid in rids:
            mine = sorted(by[rid], key=lambda r: r["week_start"] or "")
            for i, r in enumerate(mine):
                if r.get(fig) is not None:
                    pts.append((rid, i, float(r[fig])))
        trend[fig] = learning_stats.fe_slope_ci(pts)
        trend[fig]["mean"] = learning_stats.mean_ci([p[2] for p in pts])["mean"] if pts else None
    abl = [(r["restaurant_id"], r["ablation_effect"]) for rid in rids for r in by[rid] if r["ablation_effect"] is not None]
    import schedule_experiments as sx
    ablation = sx.cluster_mean_ci(abl) if abl else {"n": 0, "mean": None, "ci90": None}
    surv, cost, accepted, rules_n, rules_ok = {}, 0.0, 0, 0, 0
    for rid in rids:
        for r in by[rid]:
            m = r["metrics"]
            for fam, e in (m.get("origin_survival") or {}).items():
                s_ = surv.setdefault(fam, {"rows": 0, "kept": 0})
                s_["rows"] += int(e.get("rows") or 0)
                s_["kept"] += int(e.get("kept") or 0)
            if m.get("model_cost_usd") is not None:
                cost += float(m["model_cost_usd"])
            if r["drr"] is not None and float(r["drr"]) == 0.0:
                accepted += 1
            rl = m.get("rules") or {}
            if rl.get("rules"):
                rules_n += int(rl["rules"])
                rules_ok += int(rl["rules"]) - int(rl.get("broken") or 0)
    for e in surv.values():
        e["share"] = round(e["kept"] / float(e["rows"]), 3) if e["rows"] else None
    latest = []
    for rid in rids:
        r = max(by[rid], key=lambda r: r["week_start"] or "")
        latest.append({k: r.get(k) for k in ("restaurant_id", "history_id", "week_start", "status", "drr", "drr_lo",
                                              "drr_hi", "labor_vs_target", "ot_hours", "unmanaged_minutes",
                                              "shortfall_hours", "uncovered_no_shows", "forecast_skill",
                                              "ablation_effect")})
    return {"ok": True, "restaurants": len(rids), "weeks": sum(len(by[r]) for r in rids), "trend": trend,
            "ablation": ablation, "origin_survival": surv,
            "cost_per_accepted_week": round(cost / accepted, 4) if accepted else None,
            "accepted_weeks": accepted, "model_cost_usd": round(cost, 4),
            "instruction_honoured_rate": round(rules_ok / float(rules_n), 3) if rules_n else None,
            "latest": latest, "frozen_after_days": FREEZE_AFTER_DAYS,
            "method": "within-restaurant slope over weeks since the first scored week, restaurant-cluster 90% interval"}
