"""
dsr.pipeline — the nightly stage machine (docs/plans/DSR_ENGINE_PLAN.md §3–4).

  run_night(restaurant, business_date, trigger)   one restaurant-night
  run_sweep()                                     the scheduler's entry, every
                                                  tick, claimed per 10 minutes
  start_manual(restaurant, business_date)         the "Close day" button

A night moves

  scheduled ─► awaiting_close ─► collecting ─► writing ─► final
                                                       └─► provisional | failed

and every step is stamped when it really happened (dsr.store: the stage
times, and each block's own collection time under stages_json["blocks"]),
because those times stay on the report as its provenance and drive the
progressive checklist the app shows while it runs.

THE RULES, each one pinned by tests/test_dsr_pipeline.py:

* Never before close. The sweep acts on a night only once the restaurant is
  past its own close (time_utils.service_window, local time, late closes
  owned by the business date). Only the manual Close day button skips that.
* The POS says when the day is over. RPOWER writes a closeday record
  (pos.fetch_day_closed), polled every POLL_MINUTES; a POS without one is
  taken as closed CLOSE_GRACE_MINUTES after the close time.
* A block still AWAITING is collected again with backoff (BACKOFF_MINUTES,
  the last step repeating) until the restaurant's deadline
  (restaurants.dsr_deadline_hour, local, default 4am) — so the 3am labor
  sync usually makes the report. The closeout never holds a night open
  (NEVER_HOLDS) but is re-read on every pass while it is.
* At the deadline only SALES decides (REQUIRED_BLOCKS): still awaiting →
  PROVISIONAL; in → FINAL, with every other block still awaiting labelled by
  its reason in facts.missing. (Food costs tonight's items from the Sales
  block's own pull, so it no longer waits on the 5am item sync — D1-8.)
* Close day never overrules a POS with a close-day record (day_closed): the
  night waits for the POS's close. A POS without one is refused before its
  close (manual_close_refusal) unless the owner says they closed early.
* A re-run (`force`) is made only from sales that are in; otherwise the
  report is unchanged and the night notes why (D1-12). A FINAL night the POS
  archive later disagrees with is re-pulled (recheck_final) and gets a new
  version only when the POS's own figure moved.
* A provisional night is re-checked every LATE_DATA_MINUTES for
  LATE_DATA_HOURS. When sales has arrived the night gets a new VERSION — the
  old one is never edited; Labor is re-read over the new net. A day the POS
  closed with no tickets still has none when the window ends: a FINAL
  version saying "No sales recorded" (figures None, never $0). Nothing else
  makes a version on its own; the owner can re-run a night (Close day with
  rerun, or `force`).
* Failures are bounded. A collector that raises is captured (ops.capture,
  so it reaches the admin console) and its block retried as awaiting; after
  MAX_FAILURES it is marked unavailable rather than holding the night. The
  pipeline itself raising is retried on the same backoff and, after
  MAX_FAILURES, the night is FAILED.
* Telling people is last. Once a version is saved final or provisional,
  dsr.deliver.on_terminal emails every owner and manager their view and
  pushes them (held through quiet hours) — once per night, plus one
  "Updated" when a provisional night goes final. It runs after the save and
  can never fail or roll back the report; a failed night tells nobody.
* Nothing double-runs. Every run claims (restaurant, business date,
  version) with ops.claim_period; a version that finished keeps its claim
  for good, one that is waiting for a retry gives it back, and one a deploy
  killed mid-run is reclaimed after STALE_RUN_MINUTES without progress.
  Re-running a final night does nothing unless `force`.

Blocks and the narrative are reached by module name (dsr.block_<name>,
dsr.narrative) and imported lazily, so a block another engineer has not
shipped yet is "Not available yet" rather than an import error at boot.
"""
import importlib
import json
import logging
from datetime import date, datetime, time as _time, timedelta, timezone

import dsr
from dsr import store

log = logging.getLogger("dsr")

TRIGGER_SWEEP = "sweep"
TRIGGER_MANUAL = "manual"
TRIGGER_LATE = "late_data"

POLL_MINUTES = 10                  # closeday poll while the POS has not closed the day
BACKOFF_MINUTES = (10, 20, 40)     # re-collect awaiting blocks / retry a failed run; the last step repeats
MAX_FAILURES = 3                   # retries after an error before a block is unavailable / a night failed
CLOSE_GRACE_MINUTES = 30           # a POS with no closeday record: closed this long after close time
DEFAULT_CLOSE = (23, 0)            # a restaurant that has set no hours at all
DEFAULT_DEADLINE_HOUR = 4
MIN_DEADLINE_AFTER_CLOSE_HOURS = 2 # a 3am close still gets two hours before the provisional deadline
LATE_DATA_MINUTES = 60             # how often a provisional night is re-checked
LATE_DATA_HOURS = 48               # and for how long after its deadline
STALE_RUN_MINUTES = 45             # a claim held this long with no progress is a dead run
SWEEP_MAX_SECONDS = 20 * 60
SWEEP_CURSOR_KEY = "dsr_sweep_cursor"
SWEEP_LOOKBACK_DAYS = 4            # nights the sweep still finishes or re-checks

NOT_AVAILABLE_YET = "Not available yet"
CLAIM_JOB = "dsr"

# The blocks a night cannot go out FINAL without. Only sales: a Food, Labor
# or Reviews block still awaiting at the deadline goes out labelled.
# A night is provisional only while one of these is awaiting at the
# deadline, and only these arriving late make a new version — Food catching
# up the next morning never does.
REQUIRED_BLOCKS = ("sales",)

# Blocks that never hold a night open (their absence is "unavailable", not
# "coming") but are read again on every pass while the night is still open,
# so a closeout filed at 11:45 still makes the report.
NEVER_HOLDS = ("closeout",)

# Owner-facing words for the narrative's outcome.
NO_SUMMARY_SALES_PENDING = "Not enough data tonight for a summary — sales are still syncing"
NO_SUMMARY_NO_SALES = "No summary without tonight's sales"
NO_SUMMARY_FAILED = "The summary couldn't be written tonight"


def _db(db_path):
    import models
    return db_path or models.DB_PATH


def _as_date(value):
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19].replace(" ", "T"))
    except ValueError:
        return None


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


# ── the restaurant's own clock ──────────────────────────────────────────────

def local_time(restaurant, now_utc):
    """Naive UTC → naive local wall clock in the restaurant's zone."""
    from time_utils import restaurant_tz
    return now_utc.replace(tzinfo=timezone.utc).astimezone(restaurant_tz(restaurant)).replace(tzinfo=None)


def to_utc(restaurant, local):
    """Naive local wall clock → naive UTC."""
    from time_utils import restaurant_tz
    return local.replace(tzinfo=restaurant_tz(restaurant)).astimezone(timezone.utc).replace(tzinfo=None)


def _has_hours(restaurant):
    for attr in ("open_times_json", "close_times_json"):
        try:
            if any(v for v in (json.loads(getattr(restaurant, attr, None) or "{}") or {}).values()):
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def close_at(restaurant, day):
    """When the service on business date `day` closes (naive local), or None
    when the restaurant is closed that weekday — it has set hours for other
    days and none for this one. A restaurant that has set no hours at all
    closes at DEFAULT_CLOSE."""
    from time_utils import service_window
    window = service_window(restaurant, day)
    if window:
        return window[1]
    if _has_hours(restaurant):
        return None
    return datetime.combine(day, _time(*DEFAULT_CLOSE))


def deadline_at(restaurant, day):
    """When a night still missing data goes out provisional: the morning
    after, at restaurants.dsr_deadline_hour local — never sooner than
    MIN_DEADLINE_AFTER_CLOSE_HOURS after close."""
    raw = getattr(restaurant, "dsr_deadline_hour", None)
    try:
        hour = int(raw) if raw is not None else DEFAULT_DEADLINE_HOUR
    except (TypeError, ValueError):
        hour = DEFAULT_DEADLINE_HOUR
    hour = hour if 0 <= hour <= 23 else DEFAULT_DEADLINE_HOUR
    deadline = datetime.combine(day + timedelta(days=1), _time(hour, 0))
    close = close_at(restaurant, day) or datetime.combine(day, _time(*DEFAULT_CLOSE))
    return max(deadline, close + timedelta(hours=MIN_DEADLINE_AFTER_CLOSE_HOURS))


def just_closed(restaurant, local):
    """The most recent business date whose service has closed by `local`, or
    None. From one close to the next, that is the night the sweep owns."""
    for day in (local.date(), local.date() - timedelta(days=1)):
        close = close_at(restaurant, day)
        if close is not None and local >= close:
            return day
    return None


def day_closed(restaurant, day, now_utc, trigger=None):
    """How the POS day is known to be over — "pos", "close_time" or
    "manual" — or None when it is not (or the POS could not be asked,
    which is never read as closed).

    Close day never overrules a POS that keeps a close-day record (D1-1):
    pressed at 10:40pm with tables still open, the night waits for the
    POS's own close like any other, rather than going out FINAL on half a
    night that nothing ever revisits. Only a POS with no such record takes
    the button's word — and the route refuses that before close
    (manual_close_refusal) unless the owner says they closed early."""
    import pos
    try:
        closed, _provider = pos.fetch_day_closed(restaurant.id, day)
        return "pos" if closed else None
    except pos.POSCapabilityError:
        if trigger == TRIGGER_MANUAL:
            return "manual"
        close = close_at(restaurant, day) or datetime.combine(day, _time(*DEFAULT_CLOSE))
        return "close_time" if local_time(restaurant, now_utc) >= close + timedelta(minutes=CLOSE_GRACE_MINUTES) else None
    except Exception as e:
        log.warning("dsr: closeday check failed rid=%s day=%s: %s", restaurant.id, day, e)
        return None


EARLY_CLOSE_MINUTES = 30           # Close day on a POS with no close-day record: not before close − this


def manual_close_refusal(restaurant, day, now_utc=None):
    """Why Close day can't finalise `day` yet, or None (D1-1). A POS that
    keeps a close-day record is asked by the pipeline itself (the night
    waits for it), so only a POS without one is judged here: before its
    close time less EARLY_CLOSE_MINUTES, pressing the button would build the
    report from a day still trading."""
    import pos
    from time_utils import mdy
    now_utc = now_utc or datetime.utcnow()
    try:
        if pos.supports(restaurant.id, "fetch_day_closed"):
            return None
    except Exception:
        return None
    close = close_at(restaurant, day)
    if close is None:
        return None
    if local_time(restaurant, now_utc) >= close - timedelta(minutes=EARLY_CLOSE_MINUTES):
        return None
    return (f"It's before your close ({close.strftime('%-I:%M %p').lower()}), and your POS doesn't tell Cavnar "
            f"when the day is closed — a report built now would miss the rest of {mdy(day)}'s sales.")


# ── collectors and the narrative, reached lazily by name ────────────────────

def _import(name):
    """The module, None when it has not been built yet, or raises when it
    exists and is broken."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            return None
        raise


def _collect(name, ctx):
    """(block, crashed). A missing collector is "Not available yet"; one
    that raises or returns something outside the contract is captured and
    comes back AWAITING so it is retried."""
    import ops
    try:
        mod = _import(f"dsr.block_{name}")
        if mod is None:
            return dsr.block(dsr.UNAVAILABLE, reason=NOT_AVAILABLE_YET, block_name=name), False
        raw = mod.collect(ctx)
        if not isinstance(raw, dict) or raw.get("status") not in dsr.STATUSES:
            raise ValueError(f"dsr.block_{name}.collect returned {type(raw).__name__}, not a block")
        blk = dsr.block(raw["status"], source=raw.get("source"), reason=raw.get("reason"),
                        metrics=raw.get("metrics"), detail=raw.get("detail"), block_name=name)
        json.dumps(blk)          # the report stores it; refuse it here rather than there
        return blk, False
    except Exception as e:
        ops.capture(e, job=f"dsr_{name}", context=f"restaurant_id={ctx.restaurant_id} business_date={ctx.day}")
        return dsr.block(dsr.AWAITING, block_name=name, detail={"error": "collector_failed"}), True


def _write(ctx, facts):
    """narrative.write(ctx, facts), normalised; never raises."""
    import ops
    try:
        mod = _import("dsr.narrative")
        if mod is None:
            return {"ok": False, "narrative": None, "reason": NOT_AVAILABLE_YET}
        out = mod.write(ctx, facts)
        ok = isinstance(out, dict) and bool(out.get("ok")) and isinstance(out.get("narrative"), dict)
        if ok:
            json.dumps(out["narrative"])
            return {"ok": True, "narrative": out["narrative"], "reason": None}
        reason = out.get("reason") if isinstance(out, dict) else None
        return {"ok": False, "narrative": None, "reason": str(reason or NO_SUMMARY_FAILED)[:300]}
    except Exception as e:
        ops.capture(e, job="dsr_narrative", context=f"restaurant_id={ctx.restaurant_id} business_date={ctx.day}")
        return {"ok": False, "narrative": None, "reason": NO_SUMMARY_FAILED}


# ── claims ──────────────────────────────────────────────────────────────────

def _period(rid, day, version):
    return f"{rid}:{day.isoformat()}:v{int(version)}"


def _claim_age_minutes(period):
    conn = store.get_conn()
    try:
        row = conn.execute("SELECT claimed_at FROM job_period_claims WHERE job_key=?",
                           (f"{CLAIM_JOB}:{period}",)).fetchone()
    finally:
        conn.close()
    at = _parse_utc(row["claimed_at"]) if row else None
    return None if at is None else (datetime.utcnow() - at).total_seconds() / 60.0


def _last_progress(report):
    stamps = [v for k, v in (report.get("stages") or {}).items() if k in dsr.STAGES and isinstance(v, str)]
    stamps += [v for v in ((report.get("stages") or {}).get("blocks") or {}).values() if isinstance(v, str)]
    parsed = [p for p in (_parse_utc(s) for s in stamps) if p]
    return max(parsed) if parsed else None


def _claim(rid, day, version, db):
    """Claim (restaurant, night, version). A held claim is taken over only
    when its run is plainly dead: held STALE_RUN_MINUTES, the version not
    finished, and no stage or block stamped in that long."""
    import ops
    period = _period(rid, day, version)
    if ops.claim_period(CLAIM_JOB, period):
        return True
    age = _claim_age_minutes(period)
    if age is None or age < STALE_RUN_MINUTES:
        return False
    report = store.get_report(rid, day, version=version, db_path=db)
    if report is not None:
        if report["status"] in dsr.TERMINAL_STAGES:
            return False
        last = _last_progress(report)
        if last and (datetime.utcnow() - last).total_seconds() / 60.0 < STALE_RUN_MINUTES:
            return False
    log.error("dsr: claim %s held %.0f minutes with no progress — reclaimed", period, age)
    ops.release_period(CLAIM_JOB, period)
    return ops.claim_period(CLAIM_JOB, period)


def _result(action, report=None, **extra):
    out = {"ok": True, "action": action}
    if report:
        out.update({"report_id": report.get("id"), "version": report.get("version"), "status": report.get("status")})
    out.update(extra)
    return out


def _run_claimed(restaurant, day, version, now_utc, db, fn):
    """Run `fn` holding the (restaurant, night, version) claim. The claim is
    kept only when that version ends terminal; anything else — waiting,
    retrying, nothing found — gives it back for the next attempt."""
    import ops
    rid = restaurant.id
    if not _claim(rid, day, version, db):
        return _result("in_progress", store.get_report(rid, day, db_path=db))
    try:
        return fn()
    except Exception as e:
        ops.capture(e, job="dsr", context=f"restaurant_id={rid} business_date={day} v{version}")
        return _failed_attempt(rid, day, version, now_utc, db, e)
    finally:
        try:
            done = store.get_report(rid, day, version=version, db_path=db)
            if not (done and done["status"] in dsr.TERMINAL_STAGES):
                ops.release_period(CLAIM_JOB, _period(rid, day, version))
        except Exception as e:
            ops.capture(e, job="dsr_claim_release", context=f"restaurant_id={rid} business_date={day} v{version}")


def _failed_attempt(rid, day, version, now_utc, db, error):
    """The pipeline itself raised: retry on BACKOFF_MINUTES, then fail."""
    report = store.get_report(rid, day, version=version, db_path=db)
    if report is None or report["status"] in dsr.TERMINAL_STAGES:
        return {"ok": False, "action": "error", "error": str(error)[:300]}
    failures = int((report.get("stages") or {}).get("failures") or 0) + 1
    store.note(report["id"], "failures", failures, db_path=db)
    if failures > MAX_FAILURES:
        store.set_stage(report["id"], "failed", db_path=db, error=str(error))
        store.schedule_retry(report["id"], None, db_path=db, count=False)
        return _result("failed", store.get_report_by_id(report["id"], db_path=db), error=str(error)[:300])
    wait = BACKOFF_MINUTES[min(failures - 1, len(BACKOFF_MINUTES) - 1)]
    store.schedule_retry(report["id"], now_utc + timedelta(minutes=wait), db_path=db, count=False)
    return _result("error_retry", report, error=str(error)[:300], next_attempt_at=_stamp(now_utc + timedelta(minutes=wait)))


# ── one night ───────────────────────────────────────────────────────────────

def _due(report, now_utc):
    nxt = _parse_utc(report.get("next_attempt_at"))
    return nxt is None or nxt <= now_utc


def run_night(restaurant, business_date, trigger, now_utc=None, db_path=None, force=False):
    """Create or resume one restaurant-night and move it as far as it can go
    right now. Returns {"ok", "action", "report_id", "version", "status", ...}.

    action is one of: before_close, closed_day (no service that weekday),
    none (already final/failed), waiting (a retry not due yet), in_progress
    (another run holds the claim), awaiting_close, retry, final, provisional,
    failed, still_awaiting / expired (a provisional night's late-data check),
    error_retry, error."""
    db = _db(db_path)
    now_utc = now_utc or datetime.utcnow()
    day = _as_date(business_date)
    rid = restaurant.id
    manual = trigger == TRIGGER_MANUAL
    latest = store.get_report(rid, day, db_path=db)
    status = latest["status"] if latest else None

    def fresh(probed=None):
        report = store.create_report(rid, day, trigger=trigger, db_path=db)
        return _advance(restaurant, report, trigger, now_utc, db, probed=probed)

    if latest is None:
        if not (manual or force):
            close = close_at(restaurant, day)
            if close is None:
                return _result("closed_day")
            if local_time(restaurant, now_utc) < close:
                return _result("before_close")
        return _run_claimed(restaurant, day, 1, now_utc, db, fresh)

    if status in dsr.TERMINAL_STAGES:
        # A finished night is re-run only on purpose: `force`, or someone
        # pressing Close day on a night that failed. Either is a new version.
        if force and status in store.FINISHED:
            # A re-run replaces what everyone reads, so it is made only from
            # sales that are in (D1-12): a POS timing out mid re-run used to
            # turn a good final night into a "no sales" provisional one for
            # good. Sales are pulled first; not in → the report is unchanged.
            sales = _probe_sales(restaurant, latest, trigger, now_utc, db)
            if sales.get("status") != dsr.READY:
                store.note(latest["id"], "rerun", {"at": _stamp(now_utc), "refused": RERUN_REFUSED,
                                                   "why": sales.get("reason")}, db_path=db)
                return _result("unchanged", latest, reason=RERUN_REFUSED)
            return _run_claimed(restaurant, day, latest["version"] + 1, now_utc, db,
                                lambda: fresh(probed={"sales": sales}))
        if force or (manual and status == "failed"):
            return _run_claimed(restaurant, day, latest["version"] + 1, now_utc, db, fresh)
        if status != "provisional":
            return _result("none", latest)
        if not manual and not _due(latest, now_utc):
            return _result("waiting", latest, next_attempt_at=latest.get("next_attempt_at"))
        return _run_claimed(restaurant, day, latest["version"] + 1, now_utc, db,
                            lambda: _upgrade(restaurant, latest, trigger, now_utc, db))

    # Still in flight: resume it when its retry is due (or someone asked).
    if not (manual or force) and not _due(latest, now_utc):
        return _result("waiting", latest, next_attempt_at=latest.get("next_attempt_at"))
    return _run_claimed(restaurant, day, latest["version"], now_utc, db,
                        lambda: _advance(restaurant, store.get_report_by_id(latest["id"], db_path=db),
                                         trigger, now_utc, db))


RERUN_REFUSED = "The POS didn't return this night's sales, so your report is unchanged"


def _probe_sales(restaurant, report, trigger, now_utc, db):
    """The Sales block for a finished night, pulled again now — nothing
    saved. The day was closed when the night finished, by the record it
    kept (or the POS asked again when it went out at the deadline)."""
    day = _as_date(report["business_date"])
    ctx = dsr.Context(restaurant, day, db_path=db, now_utc=now_utc, trigger=trigger)
    was = (report.get("stages") or {}).get("closed_by")
    ctx.day_closed = was if was in ("pos", "close_time", "manual") else (
        day_closed(restaurant, day, now_utc, trigger) or False)
    blk, _crashed = _collect("sales", ctx)
    return blk


RECHECK_MIN_DOLLARS = 1.0          # a re-pulled net this far from the report's makes a new version


def recheck_final(restaurant, business_date, now_utc=None, db_path=None):
    """A FINAL night the POS archive no longer agrees with (a late void, a
    check closed after the report — data_freshness.sales_consistency): pull
    the night's sales again, and when the POS's own figure has moved, the
    night gets a new version from it, as a late-data version does (D1-1).
    When the re-pull matches what the report says, nothing changes — the
    archive and the report count differently, and a new version would say
    the same. Returns the pipeline's result; never raises."""
    import ops
    db = _db(db_path)
    now_utc = now_utc or datetime.utcnow()
    day = _as_date(business_date)
    try:
        latest = store.get_report(restaurant.id, day, db_path=db)
        if not latest or latest["status"] != "final":
            return _result("none", latest)
        sm = (((latest.get("facts") or {}).get("blocks") or {}).get("sales") or {})
        was = (sm.get("metrics") or {}).get("net") if sm.get("status") == dsr.READY else None
        sales = _probe_sales(restaurant, latest, TRIGGER_LATE, now_utc, db)
        now_net = (sales.get("metrics") or {}).get("net") if sales.get("status") == dsr.READY else None
        if now_net is None or (was is not None and abs(float(now_net) - float(was)) < RECHECK_MIN_DOLLARS):
            return _result("unchanged", latest)

        def fresh():
            report = store.create_report(restaurant.id, day, trigger=TRIGGER_LATE, db_path=db)
            store.note(report["id"], "supersedes", latest["version"], db_path=db)
            return _advance(restaurant, report, TRIGGER_LATE, now_utc, db, probed={"sales": sales})
        return _run_claimed(restaurant, day, latest["version"] + 1, now_utc, db, fresh)
    except Exception as e:
        ops.capture(e, job="dsr_recheck", context=f"restaurant_id={restaurant.id} business_date={day}")
        return {"ok": False, "action": "error", "error": str(e)[:300]}


def _save_fiscal(report_id, restaurant, day, db):
    from dsr import fiscal
    position = fiscal.position(restaurant, day)
    store.save_fiscal(report_id, {**position, "label": fiscal.label(restaurant, day)}, db_path=db)


def _advance(restaurant, report, trigger, now_utc, db, probed=None, carried=None):
    """Move one version through its stages from wherever it stands."""
    day = _as_date(report["business_date"])
    report_id = report["id"]
    stages = report.get("stages") or {}
    facts = report.get("facts") or {}
    local = local_time(restaurant, now_utc)
    past_deadline = local >= deadline_at(restaurant, day)
    status = report["status"]
    if "fiscal" not in facts:
        _save_fiscal(report_id, restaurant, day, db)

    ctx = dsr.Context(restaurant, day, db_path=db, now_utc=now_utc, trigger=trigger)

    # awaiting_close: nothing is collected until the POS has closed the day,
    # or the deadline says to go with what there is.
    if status in ("scheduled", "awaiting_close"):
        if probed and (probed.get("sales") or {}).get("status") == dsr.READY:
            # Sales already pulled for this version (a re-run, a recheck):
            # the day was closed for that pull.
            how = ((probed["sales"].get("detail") or {}).get("closed_by")) or "manual"
        else:
            how = day_closed(restaurant, day, now_utc, trigger)
        if how is None and not past_deadline:
            if status != "awaiting_close":
                store.set_stage(report_id, "awaiting_close", db_path=db)
            nxt = now_utc + timedelta(minutes=POLL_MINUTES)
            store.schedule_retry(report_id, nxt, db_path=db, count=False)
            return _result("awaiting_close", store.get_report_by_id(report_id, db_path=db), next_attempt_at=_stamp(nxt))
        store.note(report_id, "closed_by", how or "deadline", db_path=db)
        ctx.day_closed = how or False
        closed_by = how or "deadline"
    else:
        closed_by = stages.get("closed_by")

    # collecting: each block saved as it finishes, so its stamp is real and
    # a later block reads the earlier ones from ctx.blocks.
    if status != "collecting":
        store.set_stage(report_id, "collecting", db_path=db)
    blocks = dict((facts.get("blocks") or {}))
    todo = [n for n in dsr.BLOCKS if n not in blocks or blocks[n].get("status") == dsr.AWAITING
            or (n in NEVER_HOLDS and blocks[n].get("status") != dsr.READY)]
    if ctx.day_closed is None and "sales" in todo and not (probed and "sales" in probed) \
            and not (carried and "sales" in carried):
        # A resumed night: re-read the close only when sales still needs it.
        ctx.day_closed = closed_by if closed_by in ("pos", "close_time", "manual") else (
            day_closed(restaurant, day, now_utc, trigger) or False)
    ctx.blocks = dict(blocks)
    crashes = dict(stages.get("crashes") or {})
    crashed_any = False
    for name in dsr.BLOCKS:
        if name not in todo:
            continue
        stamped_at = None
        if carried and name in carried:
            blk, stamped_at = carried[name]
        elif probed and name in probed:
            blk = probed[name]
        else:
            blk, crashed = _collect(name, ctx)
            if crashed:
                crashed_any = True
                crashes[name] = crashes.get(name, 0) + 1
                if crashes[name] > MAX_FAILURES:
                    blk = dsr.block(dsr.UNAVAILABLE, block_name=name, detail={"error": "collector_failed"})
        store.save_block(report_id, name, blk, db_path=db, stamped_at=stamped_at)
        ctx.blocks[name] = blk
        if name == "sales" and not (carried and name in carried):
            _record_sales_collect(restaurant, day, blk, db)
    if crashed_any:
        store.note(report_id, "crashes", crashes, db_path=db)

    # Until the deadline, anything still coming is worth waiting for (the
    # 3am labor sync usually makes it); a block that never holds is not.
    awaiting = [n for n in dsr.BLOCKS if (ctx.blocks.get(n) or {}).get("status") == dsr.AWAITING]
    holding = [n for n in awaiting if n not in NEVER_HOLDS]
    if holding and not past_deadline:
        attempts = int(report.get("attempts") or 0)
        wait = BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES) - 1)]
        nxt = now_utc + timedelta(minutes=wait)
        store.schedule_retry(report_id, nxt, db_path=db, count=True)
        return _result("retry", store.get_report_by_id(report_id, db_path=db), awaiting=holding,
                       next_attempt_at=_stamp(nxt))

    # tomorrow: grade what the previous report predicted about this night,
    # then the next night's prep, forecast and confidence (dsr.tomorrow), and
    # its predictions — recorded only while that night is still ahead.
    _tomorrow(restaurant, report_id, day, trigger, now_utc, db)

    # writing: one narrative, only over a night whose sales are in.
    sales_status = (ctx.blocks.get("sales") or {}).get("status")
    if sales_status == dsr.READY:
        facts_now = store.get_report_by_id(report_id, db_path=db)["facts"]
        able, why = _can_write(facts_now)
        if able:
            store.set_stage(report_id, "writing", db_path=db)
            written = _write(ctx, facts_now)
        else:
            written = {"ok": False, "narrative": None, "reason": why}
        store.save_narrative(report_id, written["narrative"], db_path=db)
        store.note(report_id, "narrative", {"status": "written" if written["ok"] else "skipped",
                                            "reason": written["reason"]}, db_path=db)
    else:
        store.save_narrative(report_id, None, db_path=db)
        store.note(report_id, "narrative", {"status": "skipped", "reason": NO_SUMMARY_SALES_PENDING
                                            if sales_status == dsr.AWAITING else NO_SUMMARY_NO_SALES}, db_path=db)

    # Provisional only when a REQUIRED block is still missing; every other
    # block still awaiting goes out labelled with its reason (facts.missing).
    required_missing = [n for n in REQUIRED_BLOCKS if n in awaiting]
    terminal = "provisional" if required_missing else "final"
    if "sales" in required_missing:
        # Waiting for sales is not a failure until the deadline; going out
        # without them is (DH5-1).
        _record_attempt(restaurant, False, db, error=f"Sales for {day.isoformat()} missing at the report deadline")
    store.set_stage(report_id, terminal, db_path=db)
    nxt = now_utc + timedelta(minutes=LATE_DATA_MINUTES) if required_missing else None
    store.schedule_retry(report_id, nxt, db_path=db, count=False)
    _deliver(restaurant, report_id, now_utc, db)
    return _result(terminal, store.get_report_by_id(report_id, db_path=db), awaiting=awaiting,
                   required_missing=required_missing)


NO_HOURS_SERVICE_START_HOUR = 5    # a night with no opening hours "starts" at 5am, for predictions


def service_start(restaurant, day):
    """When the service of business date `day` opens (naive local): its
    opening time, else NO_HOURS_SERVICE_START_HOUR on that date. A
    prediction about the night is a prediction only before this (D1-7)."""
    from time_utils import service_window
    window = service_window(restaurant, day)
    return window[0] if window else datetime.combine(day, _time(NO_HOURS_SERVICE_START_HOUR, 0))


def prediction_cutoff_utc(restaurant, day):
    """service_start as naive UTC — the moment after which nothing written
    about `day` counts as a prediction."""
    return to_utc(restaurant, service_start(restaurant, day))


def _tomorrow(restaurant, report_id, day, trigger, now_utc, db):
    """Grade this night's predictions, store the next night's snapshot and
    record its predictions. Never holds or fails the report."""
    try:
        from dsr import predictions, tomorrow
        facts_now = store.get_report_by_id(report_id, db_path=db)["facts"]
        # Only what was written before this night opened is graded (D1-7).
        predictions.grade(restaurant.id, day, facts_now, db_path=db,
                          cutoff_utc=prediction_cutoff_utc(restaurant, day))
        snap = tomorrow.build(restaurant, day, facts_now, db_path=db)
        preds = snap.pop("_preds", [])
        store.save_section(report_id, "tomorrow", snap, db_path=db)
        # A prediction is made BEFORE its night — strictly before the next
        # night's service opens, not merely on or before its calendar date
        # (D1-7): a provisional night's v2 landing the next evening, a Close
        # day or a re-run pressed the next day records nothing.
        if now_utc < prediction_cutoff_utc(restaurant, day + timedelta(days=1)):
            predictions.record(restaurant.id, day, day + timedelta(days=1), preds, db_path=db, made_at=now_utc)
    except Exception as e:
        log.warning("dsr: tomorrow not built rid=%s day=%s: %s", getattr(restaurant, "id", None), day, e)


def _record_attempt(restaurant, ok, db, error=None, data_through=None):
    """The nightly report's sales collection in the Data Health ledger
    (source `dsr`). Never raises."""
    try:
        import data_health
        from models import DB_PATH
        name, _mod = (None, None)
        try:
            import pos
            name, _mod = pos.connected_provider(restaurant.id)
        except Exception:
            pass
        data_health.record_attempt(restaurant.id, "dsr", ok, provider=name, error=error,
                                   data_through=data_through, db_path=None if db in (None, DB_PATH) else db)
    except Exception as e:
        log.warning("dsr sales attempt not recorded rid=%s: %s", getattr(restaurant, "id", None), e)


def _record_sales_collect(restaurant, day, blk, db):
    """One sales collection: READY is a success through `day`; a collector
    that crashed, or a POS that refused the login, is a failure. AWAITING
    (the POS still settling) is not recorded until the deadline decides it,
    and a POS that cannot report a day this way is not a failed sync."""
    status = (blk or {}).get("status")
    if status == dsr.READY:
        _record_attempt(restaurant, True, db, data_through=day.isoformat())
    elif (blk.get("detail") or {}).get("error") == "collector_failed":
        _record_attempt(restaurant, False, db, error="Sales collection failed")
    elif status == dsr.UNAVAILABLE and "attention" in str(blk.get("reason") or ""):
        _record_attempt(restaurant, False, db, error=str(blk.get("reason")))


def _deliver(restaurant, report_id, now_utc, db):
    """Tell the owners and managers (dsr.deliver) — after the version is
    saved terminal, and never able to fail it: the report is the record,
    the notice is best effort on top of it (captured, never raised). A
    FAILED night never gets here; its errors were captured on the way."""
    import ops
    try:
        from dsr import deliver
        deliver.on_terminal(restaurant, report_id, now_utc=now_utc, db_path=db)
    except Exception as e:
        ops.capture(e, job="dsr_deliver", context=f"restaurant_id={restaurant.id} report_id={report_id}")


def _can_write(facts):
    """(able, reason) from narrative.can_write when it has one — so a night
    the narrative would refuse never shows "Writing the summary"."""
    import ops
    try:
        mod = _import("dsr.narrative")
        if mod is None:
            return False, NOT_AVAILABLE_YET
        check = getattr(mod, "can_write", None)
        if check is None:
            return True, None
        able, reason = check(facts)
        return bool(able), (None if able else str(reason or NO_SUMMARY_FAILED)[:300])
    except Exception as e:
        ops.capture(e, job="dsr_narrative", context="can_write")
        return True, None            # write() itself decides, and never raises


def _upgrade(restaurant, previous, trigger, now_utc, db):
    """A provisional night's late-data check. A night is provisional only
    because a REQUIRED block (sales) was missing at the deadline, so only
    that block arriving makes a new version: the blocks that were missing
    are collected (and the closeout re-read); when sales is still not in,
    look again later. When it is, a new version: the blocks that were
    already there carried over with their original collection times, the
    late ones as they are now, the summary written over the complete facts."""
    day = _as_date(previous["business_date"])
    expired = local_time(restaurant, now_utc) > deadline_at(restaurant, day) + timedelta(hours=LATE_DATA_HOURS)
    blocks = (previous.get("facts") or {}).get("blocks") or {}
    awaiting = [n for n in dsr.BLOCKS if (blocks.get(n) or {}).get("status") == dsr.AWAITING]
    required = [n for n in REQUIRED_BLOCKS if n in awaiting]
    if not required:
        store.schedule_retry(previous["id"], None, db_path=db, count=False)
        return _result("expired" if expired else "none", previous)
    ctx = dsr.Context(restaurant, day, db_path=db, now_utc=now_utc, trigger=TRIGGER_LATE)
    ctx.blocks = dict(blocks)
    closed_by = None
    if "sales" in awaiting:
        closed_by = day_closed(restaurant, day, now_utc, trigger)
        ctx.day_closed = closed_by or False
    recheck = awaiting + [n for n in NEVER_HOLDS if n not in awaiting
                          and (blocks.get(n) or {}).get("status") != dsr.READY]
    if "sales" in required and "labor" not in recheck and (blocks.get("labor") or {}).get("status") == dsr.READY:
        # Labor % is over the night's net: a v1 without sales took it from
        # the POS archive's own sales (or had none), so the version that
        # brings sales re-reads Labor over the same net (D1-9). It reads
        # only local tables.
        recheck.append("labor")
    probed = {}
    for name in dsr.BLOCKS:
        if name in recheck:
            probed[name], _crashed = _collect(name, ctx)
            ctx.blocks[name] = probed[name]
    if not all(probed[n]["status"] == dsr.READY for n in required):
        sales = probed.get("sales") or {}
        if expired and (sales.get("detail") or {}).get("waiting_for") == "tickets":
            # The POS closed the day and, 48 hours on, still holds no ticket
            # for it (D1-20): the night had no sales. It goes FINAL saying
            # so — the figures stay unmeasured (None), never $0.
            from time_utils import mdy
            probed["sales"] = dsr.block(dsr.UNAVAILABLE, source=sales.get("source"), block_name="sales",
                                        reason=f"No sales recorded for {mdy(day)}",
                                        detail={"no_sales": True, "closed_by": closed_by})
        elif expired:
            store.schedule_retry(previous["id"], None, db_path=db, count=False)
            return _result("expired", previous)
        else:
            nxt = now_utc + timedelta(minutes=LATE_DATA_MINUTES)
            store.schedule_retry(previous["id"], nxt, db_path=db, count=False)
            return _result("still_awaiting", previous, next_attempt_at=_stamp(nxt))
        if "labor" in probed and "labor" not in awaiting:
            del probed["labor"]         # carried as it was: no new net to read it over

    stamps = (previous.get("stages") or {}).get("blocks") or {}
    carried = {n: (blocks[n], stamps.get(n)) for n in dsr.BLOCKS if n in blocks and n not in probed}
    trigger = trigger if trigger == TRIGGER_MANUAL else TRIGGER_LATE
    report = store.create_report(restaurant.id, day, trigger=trigger, db_path=db)
    store.note(report["id"], "supersedes", previous["version"], db_path=db)
    store.note(report["id"], "closed_by", closed_by or (previous.get("stages") or {}).get("closed_by") or "deadline",
               db_path=db)
    store.set_stage(report["id"], "collecting", db_path=db)
    # The earlier version is finished with: no more checks on it.
    store.schedule_retry(previous["id"], None, db_path=db, count=False)
    return _advance(restaurant, store.get_report_by_id(report["id"], db_path=db), trigger, now_utc, db,
                    probed=probed, carried=carried)


# ── the manual Close day button ─────────────────────────────────────────────

def _spawn(fn):
    """Run the night off the request thread (the POS pull and the model call
    take seconds); tests replace this to run inline."""
    import threading
    threading.Thread(target=fn, name="dsr-close-day", daemon=True).start()


def start_manual(restaurant, business_date, db_path=None, rerun=False):
    """Close day, now — or, with `rerun`, the owner re-running a finished
    night as a new version. Returns {"started": bool, ...} at once; the night
    runs on a background thread and the app follows it on
    /dsr/<date>/status."""
    import ops
    db = _db(db_path)
    day = _as_date(business_date)
    latest = store.get_report(restaurant.id, day, db_path=db)
    if latest and latest["status"] == "final" and not rerun:
        return {"started": False, "status": "final", "version": latest["version"]}

    def go():
        try:
            run_night(restaurant, day, TRIGGER_MANUAL, db_path=db_path,
                      force=bool(rerun and latest and latest["status"] in dsr.TERMINAL_STAGES))
        except Exception as e:
            ops.capture(e, job="dsr_manual", context=f"restaurant_id={restaurant.id} business_date={day}")
    _spawn(go)
    return {"started": True, "status": latest["status"] if latest else "scheduled",
            "version": latest["version"] if latest else None}


# ── the sweep ───────────────────────────────────────────────────────────────

def _live(r):
    return (getattr(r, "billing_status", None) or "trial").lower() not in ("churned", "cancelled", "canceled", "paused")


def _latest_by_night(db, since):
    """{restaurant_id: {business_date: latest-version row}} for recent nights,
    in one query — so the sweep asks each restaurant nothing it can answer
    from here."""
    conn = store.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT restaurant_id, business_date, version, status, next_attempt_at FROM dsr_reports d "
            "WHERE business_date >= ? AND version = (SELECT MAX(version) FROM dsr_reports x "
            "WHERE x.restaurant_id=d.restaurant_id AND x.business_date=d.business_date)",
            (since.isoformat(),)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["restaurant_id"], {})[r["business_date"]] = dict(r)
    return out


def _needs_work(row, now_utc):
    if row["status"] in ("final", "failed"):
        return False
    nxt = _parse_utc(row.get("next_attempt_at"))
    if row["status"] == "provisional":
        return nxt is not None and nxt <= now_utc
    return nxt is None or nxt <= now_utc


def _nights_for(restaurant, rows, now_utc):
    """The nights this restaurant needs a run for right now, oldest first."""
    days = set()
    night = just_closed(restaurant, local_time(restaurant, now_utc))
    if night is not None:
        row = rows.get(night.isoformat())
        if row is None or _needs_work(row, now_utc):
            days.add(night)
    for day_iso, row in rows.items():
        if _needs_work(row, now_utc):
            days.add(date.fromisoformat(day_iso))
    # A night the sweep never saw (a deploy or an outage from one close to
    # the next) was never created, and nothing said so (D1-15). Every night
    # between this restaurant's earliest recent report and the one just
    # closed that had service and is past its deadline, with no row, is run
    # now. Only after a report exists in the window — a restaurant just
    # switched on is not back-filled (and its owner not emailed) for nights
    # before it started.
    if rows and night is not None:
        local = local_time(restaurant, now_utc)
        d = min(date.fromisoformat(x) for x in rows) + timedelta(days=1)
        while d < night:
            if d.isoformat() not in rows and close_at(restaurant, d) is not None \
                    and local >= deadline_at(restaurant, d):
                days.add(d)
            d += timedelta(days=1)
    return sorted(days)


def _read_cursor(db):
    conn = store.get_conn(db)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (SWEEP_CURSOR_KEY,)).fetchone()
    finally:
        conn.close()
    return int(row["value"]) if row and str(row["value"]).isdigit() else 0


def _write_cursor(db, value):
    conn = store.get_conn(db)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (SWEEP_CURSOR_KEY, str(value)))
        conn.commit()
    finally:
        conn.close()


def run_sweep(now_utc=None, db_path=None):
    """Every tick, claimed per 10-minute slot (scheduler.scheduler_loop).

    For every live restaurant with the DSR on and a POS connected: the night
    that just closed (local time, past its own close), any recent night
    whose retry or late-data check is due, and any night since its earliest
    recent report that was never created (_nights_for). Bounded by SWEEP_MAX_SECONDS and
    resumable: the restaurants run in id order starting after the cursor in
    job_cursors, which records the last one this pass finished — so a pass
    that runs out of time is picked up where it stopped, not at the top."""
    import ops
    import pos
    import scheduler as _sched          # bounded_map; reached lazily, as pos.sync_all does
    from models import get_all_restaurants
    db = _db(db_path)
    frozen = now_utc
    now_utc = now_utc or datetime.utcnow()
    latest = _latest_by_night(db, now_utc.date() - timedelta(days=SWEEP_LOOKBACK_DAYS))
    work = {}
    for r in get_all_restaurants(db):
        if not _live(r) or not getattr(r, "dsr_enabled", 1):
            continue
        nights = _nights_for(r, latest.get(r.id, {}), now_utc)
        if nights:
            work[r.id] = (r, nights)
    # The POS check costs a lookup per provider, so only for restaurants
    # that have a night to run.
    ids = [rid for rid in sorted(work) if pos.connected_provider(rid)[0]]
    cursor = _read_cursor(db)
    order = [i for i in ids if i > cursor] + [i for i in ids if i <= cursor]
    actions = {}

    def _one(rid):
        r, nights = work[rid]
        for night in nights:
            try:
                out = run_night(r, night, TRIGGER_SWEEP, now_utc=frozen, db_path=db_path)
            except Exception as e:
                ops.capture(e, job="dsr_sweep", context=f"restaurant_id={rid} business_date={night}")
                out = {"action": "error"}
            actions[out.get("action")] = actions.get(out.get("action"), 0) + 1

    done, hit_bound = _sched.bounded_map(order, _one, 1, SWEEP_MAX_SECONDS)
    if done:
        _write_cursor(db, order[min(done, len(order)) - 1])
    return {"restaurants": len(order), "done": done, "hit_bound": hit_bound, "actions": actions}
