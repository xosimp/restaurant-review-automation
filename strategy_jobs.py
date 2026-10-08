"""
strategy_jobs.py — the scheduled half of the strategic-foundation modules.

Each function here is one scheduler job, called from scheduler.scheduler_loop
through ops.run_job and gated there by ops.claim_period. They are kept out of
scheduler.py only to keep that file's loop readable; the gating, the order
and the cadence all live in the loop.

  run_outcome_evaluations   daily   — close outcome trackers whose window ended,
                                      and mark goals whose target is met
  run_milestones            daily   — fire the once-ever moments (savings tiers,
                                      anniversaries, goals met, every review
                                      answered) and notify on the big ones
  run_loss_sync             daily   — pull comps/voids/refunds from POSes that
                                      report them (RPOWER today)
  run_issue_scan            hourly  — open an issue for a fresh bad review,
                                      where the owner has routed a manager
  run_auto_draft_schedules  weekly  — draft next week's schedule for owners who
                                      opted in; a DRAFT in Schedule History,
                                      never published to staff
  run_intraday_capture      hourly  — net sales so far today, while a POS that
                                      can be read during service is open
  run_pre_dinner_pulse      daily   — one push before dinner when the day is
                                      materially off a typical same weekday
  run_coverage_check        service — scheduled staff who have not clocked in
"""
import logging
import threading
import config
import uuid

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)


def _restaurants(db_path=DB_PATH):
    from models import get_all_restaurants
    for r in get_all_restaurants(db_path):
        if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
            continue
        yield r


# A result worth interrupting for. Below this, "it moved a little" is a line
# in the monthly review, not a notification.
OUTCOME_WORTH_TELLING = 100.0   # dollars a month


def run_outcome_evaluations(db_path=DB_PATH):
    """6am operator time: close the trackers whose window ended. The owner
    is told about a win at their own WIN_HOUR (run_outcome_wins) — telling
    them from here pushed a Pacific owner at 4am and Hawaii at 1am (A-10).

    One restaurant at a time on its own calendar date (re-audit A8), bounded
    by wall clock and resumable from a cursor (_bounded_each, re-audit A37):
    evaluate_due over every restaurant at once read UTC's date for all of
    them and had no bound at all."""
    import outcomes, goals, ops
    counts = {"outcomes_closed": 0, "goals_achieved": 0, "goals_missed": 0}

    def _one(r):
        try:
            got = outcomes.evaluate_due(r.id, db_path=db_path, today=outcomes.local_today(r.id, db_path)) or []
            counts["outcomes_closed"] += len(got) if isinstance(got, (list, tuple)) else int(got or 0)
        except Exception as e:
            ops.capture(e, job="evaluate_outcomes", context=f"restaurant_id={r.id}")
        try:
            counts["goals_achieved"] += len(goals.mark_achieved(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="goals_mark_achieved", context=f"restaurant_id={r.id}")
        # A goal missed past its grace leaves the prompts and waits in Goals
        # to be renewed or closed (memory re-audit 9/29/26, R3).
        try:
            counts["goals_missed"] += len(goals.retire_missed(
                r.id, db_path=db_path, today=outcomes.local_today(r.id, db_path)) or [])
        except Exception as e:
            ops.capture(e, job="goals_retire_missed", context=f"restaurant_id={r.id}")

    attempted, failed, hit = _bounded_each("outcome_evaluations", _one, db_path)
    counts.update(_counts(attempted, attempted - failed, failed, hit_bound=hit))
    return counts


def run_outcome_rechecks(db_path=DB_PATH):
    """6am operator time, after run_outcome_evaluations: re-check each
    measured move at outcomes.RECHECK_DAYS (a win that no longer holds stops
    counting and stops accruing), then accrue measured dollars day by day
    into outcome_value_days — only days actually measured, only while each
    move holds (rec-ROI #14, #33). Bounded and resumable (_bounded_each);
    safe to run twice: a re-check is written once, and each tracker's
    accrued_through is a cursor no day is read past twice. Sends nothing."""
    import outcomes
    counts = {"rechecked": 0, "days_accrued": 0}

    def _one(r):
        # The restaurant's own date, not the server's UTC one (re-audit A8).
        today = outcomes.local_today(r.id, db_path)
        counts["rechecked"] += len(outcomes.recheck_due(r.id, db_path=db_path, today=today) or [])
        counts["days_accrued"] += outcomes.accrue_due(r.id, db_path=db_path, today=today)

    attempted, failed, hit = _bounded_each("outcome_rechecks", _one, db_path)
    counts.update(_counts(attempted, attempted - failed, failed, hit_bound=hit))
    return counts


# Owner-facing results and milestones go out at this hour in the
# restaurant's own timezone, with the catch-up window closing at
# RESULTS_UNTIL_HOUR so an outage never delivers one late at night.
WIN_HOUR = 9
RESULTS_UNTIL_HOUR = 20
# A tracker closed this recently is still news.
WIN_NEWS_DAYS = 7


def run_outcome_wins(db_path=DB_PATH):
    """Hourly: at each restaurant's own WIN_HOUR, tell the owner about the
    biggest result closed since they were last told. Each result is claimed
    once (ops.claim_period), so none is told twice and a day missed to an
    outage is told the next day."""
    import ops, scheduler as _sched
    from datetime import date as _date, timedelta as _td
    told = {"n": 0}

    def _one(r):
        if not _sched.local_due(r, WIN_HOUR, until=RESULTS_UNTIL_HOUR, claim_key="outcome_wins"):
            return
        import outcomes as _outcomes
        since = (_date.today() - _td(days=WIN_NEWS_DAYS)).isoformat()
        fresh = []
        for row in _outcomes.list_outcomes(r.id, status="evaluated", limit=20, db_path=db_path):
            if row.get("verdict") != "improved" or str(row.get("evaluate_on") or "") < since:
                continue
            if ops.claim_period(f"outcome_win_told:{r.id}", str(row["id"])):
                fresh.append(dict(row, restaurant_id=r.id))
        told["n"] += _tell_owners_what_worked(fresh, db_path)

    attempted, failed, hit = _bounded_each("outcome_wins", _one, db_path)
    return _counts(attempted, attempted - failed, failed, hit_bound=hit, wins_told=told["n"])


RESULTS_MAX_SECONDS = 10 * 60


def _counts(attempted=0, ok=0, failed=0, skipped=0, hit_bound=False, **extra):
    """The standard job result (#39): what ops.run_job judges a run by."""
    out = {"attempted": int(attempted), "ok": int(ok), "failed": int(failed), "skipped": int(skipped),
           "hit_bound": bool(hit_bound)}
    out.update(extra)
    return out


def _bounded_each(job, fn, db_path, max_seconds=RESULTS_MAX_SECONDS):
    """Run `fn(r)` for every live restaurant, bounded by wall clock and
    resumable from a cursor in job_cursors — CLAUDE.md's rule for work that
    iterates restaurants. Returns (attempted, failed, hit_bound).

    Through scheduler.resumable_sweep (#84): the cursor was written once,
    from the SUCCESS count, so a pass with failures landed short and re-ran
    them, and the bound was dropped (`_ran_out`) — no capture, no hit_bound,
    so a pass that stopped every night read clean."""
    import scheduler as _sched
    by_id = {r.id: r for r in _restaurants(db_path)}
    tally = {"attempted": 0, "failed": 0}
    lock = threading.Lock()

    def _run(rid):
        with lock:
            tally["attempted"] += 1
        try:
            fn(by_id[rid])
        except Exception:
            with lock:
                tally["failed"] += 1
            raise

    _done, ran_out = _sched.resumable_sweep(f"{job}_cursor", sorted(by_id), _run, max_seconds,
                                            workers=1, job=job)
    if ran_out:
        import ops
        ops.capture(RuntimeError(f"{job} stopped at its {max_seconds}s bound; the rest lead the next pass"),
                    job=job, context="time_bound")
    return tally["attempted"], tally["failed"], bool(ran_out)


class _BoundedWalk:
    """The restaurants for one serial job, from after its cursor, until
    `max_seconds` have passed — the rest lead the next pass — with the
    cursor saved as each restaurant's turn completes (#84). For loops that
    walked every restaurant with no bound and no cursor; the loop body is
    unchanged (`for r in walk:`). `hit_bound` says whether the bound cut
    the walk short."""

    def __init__(self, job, restaurants, db_path, max_seconds):
        self.job, self.db_path, self.max_seconds = job, db_path, max_seconds
        rows = sorted(restaurants, key=lambda r: r.id)
        cursor = _read_cursor(f"{job}_cursor", db_path)
        self.order = [r for r in rows if r.id > cursor] + [r for r in rows if r.id <= cursor]
        self.hit_bound = False

    def __iter__(self):
        import time as _time
        started, last = _time.monotonic(), None
        for r in self.order:
            if last is not None and _time.monotonic() - started > self.max_seconds:
                self.hit_bound = True
                import ops
                ops.capture(RuntimeError(f"{self.job} stopped at its {self.max_seconds}s bound; "
                                         "the rest lead the next pass"), job=self.job, context="time_bound")
                return
            yield r
            last = r.id
            _write_cursor(f"{self.job}_cursor", last, self.db_path)


# The walks above, bounded (#84). Each is per-restaurant model or POS work.
LOSS_SYNC_MAX_SECONDS = 30 * 60
# The POS archive's walk (run_pos_archive): a week of backfill is ~25 reads.
POS_ARCHIVE_MAX_SECONDS = 20 * 60
WEEKLY_PLAN_MAX_SECONDS = 45 * 60
RECIPE_DRAFTS_MAX_SECONDS = 30 * 60
ISSUE_SCAN_MAX_SECONDS = 15 * 60
TRUSTED_ORDERS_MAX_SECONDS = 10 * 60


def _tell_owners_what_worked(results, db_path):
    """Tell an owner when a number they changed something about improved.

    This is the only notification in the product that is about money the
    owner ALREADY made rather than money they are losing, and it is the one
    that makes every other recommendation worth reading. evaluate_due has
    computed it daily since outcomes shipped and nobody was ever told.

    One per restaurant per pass, the biggest — five separate "this worked"
    pushes on the same morning is how a win becomes noise.
    """
    import ops, push
    import outcomes as _oc
    best, kept = {}, {}
    for row in results:
        if not isinstance(row, dict) or row.get("verdict") != "improved":
            continue
        if row.get("counts") is False:
            continue          # disowned at check-in, or no longer holding
        dollars = row.get("dollars_monthly")
        if not dollars or abs(float(dollars)) < OUTCOME_WORTH_TELLING:
            continue
        rid = row.get("restaurant_id")
        if rid is None:
            continue
        if row.get("id") is not None:
            # Only a result the value figures count: not a narrower reading
            # of a family the broader one already measured, and not a labor
            # share that fell only because sales rose (re-audit A5, A7).
            if rid not in kept:
                kept[rid] = _oc.counted_ids(rid, db_path=db_path)
            if row["id"] not in kept[rid]:
                continue
        if abs(float(dollars)) > abs(float(best.get(rid, {}).get("dollars_monthly") or 0)):
            best[rid] = row
    told = 0
    for rid, row in best.items():
        try:
            import outcomes as _outcomes
            dollars = abs(float(row["dollars_monthly"]))
            # The title says what moved while the change was in place, never
            # that the change caused it — "A change you made paid off" did
            # (re-audit A14); the body is the result's own graded sentence
            # (rec-ROI #23), which names any other change in the same weeks.
            label = row.get("metric_label") or "Your number"
            body = row.get("attribution_label") or _outcomes.win_message(row)
            money = (f"about ${dollars:,.0f}/month more in sales (revenue, not profit)"
                     if _outcomes.metrics.family(row.get("metric")) == "sales"
                     # The MOVE is measured; its dollars are the move priced
                     # at this restaurant's own sales and costs
                     # (metrics.monthly_dollars: "always an estimate"), so the
                     # title never calls them measured (NS1 M7, NS3 L1).
                     else f"worth about ${dollars:,.0f}/month (an estimate from the measured move)")
            if _reach(rid, "outcome_achieved",
                      f"{label} improved while your change was in place — {money}", body,
                      {"ask_prompt": f"What did {row.get('title') or 'that change'} actually do?"},
                      db_path, subject=f"Measured: {_lower_first(label)} improved",
                      # Only the people who may see this result: its metric's
                      # module (A-15), and the recommendation behind it — an
                      # owner-only or loss recommendation's title never
                      # reaches a manager (re-audit A14).
                      permissions=_win_permissions(rid, row, db_path)):
                told += 1
        except Exception as e:
            ops.capture(e, job="outcome_win_push", context=f"restaurant_id={rid}")
    return told


def _lower_first(label):
    s = str(label or "")
    return s[:1].lower() + s[1:] if s else s


def _win_permissions(restaurant_id, row, db_path=DB_PATH):
    """Every permission a login needs to be told about this result: its
    metric's (_metric_permissions) and what rec_learning.viewer_sees asks of
    the recommendation behind it — LOSS_VIEW for a loss, principal
    (TEAM_INVITE) for an owner-only one, each module's view permission. The
    same line /outcomes draws for the same login (re-audit A14, A26)."""
    import outcomes as _outcomes
    import permissions as _p
    need = set(_metric_permissions(row.get("metric")) or ())
    ep = _outcomes.linked_episodes(restaurant_id, db_path=db_path).get(row.get("id"))
    if ep is None and row.get("source_key"):
        try:
            import rec_learning
            ep = rec_learning.episode_for(restaurant_id, row["source_key"], db_path=db_path)
        except Exception as e:
            print(f"[strategy_jobs] win episode unreadable rid={restaurant_id}: {e}")
            ep = None
    ep = ep or {"key": row.get("source_key"), "module": None}
    try:
        import rec_learning
        if rec_learning._is_loss(ep):
            need.add(_p.LOSS_VIEW)
        if ep.get("owner_only"):
            need.add(_p.TEAM_INVITE)
        by_module = {"reviews": _p.REVIEWS_VIEW, "labor": _p.LABOR_VIEW, "schedule": _p.LABOR_VIEW,
                     "food": _p.FOOD_COST_VIEW, "marketing": _p.MARKETING_VIEW, "guests": _p.MARKETING_VIEW,
                     "intel": _p.INTEL_VIEW}
        need.update(by_module[m] for m in rec_learning.modules_of(ep) if m in by_module)
    except Exception as e:
        # Fails closed: only the account holder hears about it.
        print(f"[strategy_jobs] win audience check failed rid={restaurant_id}: {e}")
        need.add(_p.TEAM_INVITE)
    return need or None


def run_milestones(db_path=DB_PATH):
    """Mark the moments worth marking, and tell the owner about the ones
    worth interrupting for.

    Every detector is idempotent by construction — milestones.fire() only
    ever returns a row the first time — so this is safe to run daily and
    produces nothing on almost every day, which is the point.

    NOT everything that fires gets a push. An anniversary and a goal met
    are worth a notification; a savings tier is worth one because it is the
    product proving its own case. A record is deliberately NOT pushed from
    here: good_news already puts one in the morning brief, and a record
    plus a brief line plus a milestone is the same news three times.
    """
    import milestones
    import scheduler as _sched
    counts = {"fired": 0, "notified": 0}

    def _one(r):
        # At the restaurant's own WIN_HOUR, once a local day. This ran for
        # everyone at 7am Chicago: 5am in Los Angeles, 2am in Honolulu (A-10).
        if not _sched.local_due(r, WIN_HOUR, until=RESULTS_UNTIL_HOUR, claim_key="milestones"):
            return
        for m in (milestones.check_all(r.id, restaurant=r, db_path=db_path) or []):
            counts["fired"] += 1
            if m.get("kind") not in ("savings", "anniversary", "goal"):
                continue
            if _reach(r.id, "milestone", m["title"], m.get("body") or "",
                      {"ask_prompt": f"Tell me more about this: {m['title']}"},
                      db_path, subject=m["title"], email_type="milestone"):
                milestones.mark_notified(r.id, m["key"], db_path=db_path)
                counts["notified"] += 1

    attempted, failed, hit = _bounded_each("milestones", _one, db_path)
    counts.update(_counts(attempted, attempted - failed, failed, hit_bound=hit))
    return counts


def run_loss_sync(db_path=DB_PATH):
    """Nightly: comps, voids and refunds from POSes that report them.
    Bounded and resumable (_BoundedWalk, #84)."""
    import loss_detection, ops
    synced = unsupported = failed = 0
    walk = _BoundedWalk("loss_sync", _restaurants(db_path), db_path, LOSS_SYNC_MAX_SECONDS)
    for r in walk:
        try:
            out = loss_detection.sync(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="loss_sync", context=f"restaurant_id={r.id}")
            failed += 1
            _record_loss(r.id, False, str(e), None, db_path)
            continue
        if out.get("ok"):
            synced += 1
            _record_loss(r.id, True, None, out.get("provider"), db_path)
            _loss_flags_to_issues(r, db_path)
        else:
            # A POS that cannot report comps and voids is a normal state,
            # not a failed sync: nothing is recorded for it.
            unsupported += 1
    # The counts job_runs judges a partial or failed night by (DH2-1, #39).
    return _counts(synced + failed, synced, failed, unsupported, walk.hit_bound,
                   synced=synced, not_supported=unsupported)


def run_pos_archive(db_path=DB_PATH):
    """Nightly: the ticket-level POS archive (pos_archive.run_for) — each
    store that can archive stores yesterday, any day its POS restated, and a
    week of backfill. Bounded and resumable (_BoundedWalk, #84)."""
    import ops
    import pos_archive
    ok = failed = unsupported = days = restated = 0
    walk = _BoundedWalk("pos_archive", _restaurants(db_path), db_path, POS_ARCHIVE_MAX_SECONDS)
    for r in walk:
        if pos_archive.provider_for(r.id)[1] is None:
            unsupported += 1
            continue
        try:
            out = pos_archive.run_for(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="pos_archive", context=f"restaurant_id={r.id}")
            failed += 1
            continue
        days += len(out.get("archived") or [])
        restated += out.get("restated") or 0
        if out.get("ok"):
            ok += 1
        else:
            failed += 1
            ops.capture(RuntimeError(f"pos_archive: days not archived {out.get('failed')}"),
                        job="pos_archive", context=f"restaurant_id={r.id}")
    return _counts(ok + failed, ok, failed, unsupported, walk.hit_bound, days=days, restated=restated)


def _record_loss(restaurant_id, ok, error, provider, db_path):
    """The loss sync's attempt in the Data Health ledger (source `loss`)."""
    try:
        import data_health
        data_health.record_attempt(restaurant_id, "loss", ok, provider=provider, error=error,
                                   db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        pass


def _loss_flags_to_issues(r, db_path):
    """A comp/void concentration on one approver becomes an owner-facing
    issue — filed, not texted (notify=False): it names a manager, so it
    goes to the person who can review the tickets, never to the routed
    manager who may be its subject. One per approver per week (source_key)."""
    try:
        import loss_detection, issues
        sig = loss_detection.signals(r.id, db_path=db_path)
        if not sig.get("available"):
            return
        week = (sig.get("week") or ["?"])[0]
        for entry in sig.get("kinds") or []:
            for f in entry.get("flags") or []:
                if f.get("type") != "concentration":
                    continue
                who = _approver_name(r.id, f.get("approver"), db_path)
                headline = f["headline"]
                if who:
                    headline = headline.replace(f"One manager (POS id {f.get('approver')})", f"{who} (POS id {f.get('approver')})")
                issues.create_issue(
                    r.id, "loss", headline[:120],
                    detail=((f"POS approver id {f.get('approver')} is {who} on your staff list. " if who else
                             f"POS approver id {f.get('approver')} is not on your staff list — add the POS id under "
                             f"Labor → Staff contacts to name them next time. ")
                            + f"{f.get('alternative', '')} {sig.get('note', '')}").strip(),
                    severity="normal", source_key=f"loss:{week}:{entry['kind']}:{f.get('approver')}",
                    notify=False, db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="loss_issues", context=f"restaurant_id={r.id}")


def _approver_name(restaurant_id, pos_id, db_path):
    """The staff contact whose pos_id matches, or None. A name only from
    a mapping the owner entered — never guessed from a similar string."""
    if not pos_id or str(pos_id) == "unrecorded":
        return None
    try:
        from models import get_conn
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT employee_name FROM staff_contacts WHERE restaurant_id=? AND pos_id=?",
                               (restaurant_id, str(pos_id))).fetchone()
        finally:
            conn.close()
        return row["employee_name"] if row else None
    except Exception:
        return None


WEEKLY_PLAN_PROMPT = (
    "It is Monday morning. Using your tools, read this week's business snapshot, the review brief, "
    "open issues, goals, recent outcomes and any cross-module findings. Then return ONLY a JSON array "
    "of at most 3 objects, no prose, each: {\"title\": an imperative of at most 80 characters, "
    "\"why\": at most 200 characters citing a figure you actually read, "
    "\"owner\": one of \"owner\", \"manager\", \"kitchen\", \"due_days\": an integer 1-7}. "
    "Only actions the data supports; fewer is fine; an empty array if nothing is worth a week."
)


PLAN_OWNERS = ("owner", "manager", "kitchen")


def _parse_plan(answer):
    # The first JSON array of objects in the answer. A greedy [.*] match ran
    # from the first "[" in any preamble to the last "]" in any sign-off, so
    # prose with brackets of its own lost the whole week (AI-17 / AI-26).
    from ai_utils import parse_json_reply
    try:
        items = parse_json_reply(answer, expect=list,
                                 accept=lambda v: all(isinstance(x, dict) for x in v))
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("title") or "").strip():
            continue
        try:
            due = max(1, min(7, int(it.get("due_days") or 7)))
        except (TypeError, ValueError):
            due = 7
        # The owner field is a closed list (R13, B5 #17): a name the model
        # wrote ("Marco Ruiz") would land on Home as the person responsible.
        owner = str(it.get("owner") or "owner").strip().lower()
        out.append({"title": str(it["title"]).strip()[:80], "why": str(it.get("why") or "").strip()[:200],
                    "owner": owner if owner in PLAN_OWNERS else "owner", "due_days": due})
    return out[:3]


# A plan that failed is retried on a later tick the same morning, up to this
# many attempts a week — a transient failure (a budget stop, an outage) used
# to lose the week, because the week was claimed before the call (AI-17).
WEEKLY_PLAN_MAX_ATTEMPTS = 3


def _plan_item_unverified(item, unverified):
    """Whether a plan item states a figure the answer's verifier could not
    trace to anything the model read. Filed issues are unattended output:
    nobody reads the answer before the item lands on Home."""
    text = f"{item.get('title') or ''} {item.get('why') or ''}"
    return any(fig and fig in text for fig in (unverified or []))


# How far back the guest text the echo check reads goes (H7): the snapshot
# and the review tools hand the model reviews from about this window.
PLAN_ECHO_REVIEW_DAYS = 120
PLAN_ECHO_REVIEW_LIMIT = 400


def _guest_texts(restaurant_id, db_path=DB_PATH) -> list:
    """This restaurant's recent review text — what the weekly plan must
    never repeat six words of (the Response Validation Layer's untrusted
    text, its I1 echo check)."""
    try:
        from models import get_conn
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT text FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND text IS NOT NULL "
                "AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', ?) "
                "ORDER BY id DESC LIMIT ?",
                (restaurant_id, f"-{PLAN_ECHO_REVIEW_DAYS} days", PLAN_ECHO_REVIEW_LIMIT)).fetchall()
        finally:
            conn.close()
        return [r["text"] for r in rows if r["text"]]
    except Exception as e:
        print(f"[weekly_plan] guest text unreadable rid={restaurant_id}: {e}")
        return []


def _guest_shingles(restaurant_id, db_path=DB_PATH) -> set:
    """Six-word runs of this restaurant's recent review text. The plan's
    echo check is the engine's now (it reads _guest_texts); kept, and
    reading the same rows, as a candidate for future cleanup after
    additional verification."""
    from ai_guard import shingles
    out = set()
    for t in _guest_texts(restaurant_id, db_path=db_path):
        out |= shingles(t)
    return out


def _plan_anchors(restaurant_id, db_path=DB_PATH) -> list:
    """The causes this system holds for a restaurant, with their strength —
    the stored review and food cost diagnoses and labor.diagnose's lead
    driver: each cause "likely", each alternative "association" (the
    digest's rules). A recommended action is never an anchor. The only
    causes a plan item may state (H2)."""
    import response_validation as rv
    import rec_trust
    out = []

    def add(d, strength="likely"):
        if not strength:
            return
        out.extend(rv.anchor(d.get("cause"), strength) + rv.anchor(d.get("alternative_cause"), "association"))
    # A stored diagnosis's own age counts (DH3-9): past its refresh it is an
    # association only, and past rec_trust.STALE_ANCHOR_MAX_DAYS no anchor —
    # a six-week-old cause no longer licenses causal wording the plan files
    # unattended.
    try:
        import review_intelligence as _ri
        for d in _ri.get_diagnoses(restaurant_id, db_path=db_path, include_stale=True)[:3]:
            add(d, rec_trust.diagnosis_anchor_strength(d))
    except Exception:
        pass
    try:
        import food_cost_intelligence as _fci
        _fd = _fci.get_diagnosis(restaurant_id, db_path=db_path, include_stale=True) or {}
        add(_fd, rec_trust.diagnosis_anchor_strength(_fd))
    except Exception:
        pass
    try:
        import labor
        add(labor.diagnose(labor.analyse_shifts_for_restaurant(restaurant_id)) or {})
    except Exception:
        pass
    return out


def _plan_cause_anchors(restaurant_id, db_path=DB_PATH) -> list:
    """The anchor texts alone (_plan_anchors without their strengths)."""
    return [a["text"] for a in _plan_anchors(restaurant_id, db_path=db_path)]


# The weekly plan's modules, each with the words that put a plan item on it
# (a held module's items are not filed — DH5-2).
PLAN_MODULES = (
    ("reviews", "module_reviews", r"\b(?:reviews?|rating|stars?|guests?\s+(?:said|wrote|complain\w*))\b"),
    ("labor", "module_labor", r"\b(?:labou?r|staff\w*|schedul\w*|shifts?|overtime|servers?|cooks?|payroll)\b"),
    # Prices, margins and recipes are food-cost figures too: an item about
    # repricing a dish is filed as food cost (plan_item_modules) and held
    # with it (memory re-audit PEOPLE-14).
    ("food_cost", "module_inventory", r"\b(?:food\s+cost|waste|inventory|orders?|prep|portions?|suppliers?|"
                                       r"invoices?|counts?|stock|(?:re)?pric\w*|margins?|recipes?|"
                                       r"plate\s+costs?|cogs)\b"),
    ("marketing", "module_marketing", r"\b(?:posts?|instagram|facebook|marketing|social|campaigns?|reach)\b"),
)


def plan_holds(restaurant, db_path=DB_PATH) -> dict:
    """{module: why} for the weekly plan's modules whose data can't be stood
    on this week (data_health.unattended_hold): the plan is told not to
    propose an action about them, and an item about one is not filed."""
    import data_health
    out = {}
    rid = getattr(restaurant, "id", None)
    for module, flag, _pat in PLAN_MODULES:
        if not getattr(restaurant, flag, 1):
            continue
        why = data_health.unattended_hold(rid, module, db_path=db_path if db_path != DB_PATH else None)
        if why:
            out[module] = why
    return out


def plan_item_held(item, holds) -> str | None:
    """The held module a plan item is about, or None."""
    import re
    text = f"{(item or {}).get('title') or ''}. {(item or {}).get('why') or ''}"
    for module, _flag, pat in PLAN_MODULES:
        if module in (holds or {}) and re.search(pat, text, re.I):
            return module
    return None


# PLAN_MODULES' names as permissions.MODULE_VIEW_PERMISSIONS keys.
_PLAN_VIEW_MODULE = {"reviews": "reviews", "labor": "labor", "food_cost": "inventory", "marketing": "marketing"}


def plan_item_modules(item) -> list:
    """Every module a plan item is about (PLAN_MODULES, the holds' own
    reading), as view-module keys. Filed on the issue (meta["modules"]) so
    a login without that module's view never reads it: the plan is written
    with the owner's food-cost view, but its items are issues every console
    login lists (memory re-audit PEOPLE-14 / PROMPTS-8)."""
    import re
    text = f"{(item or {}).get('title') or ''}. {(item or {}).get('why') or ''}"
    return sorted({_PLAN_VIEW_MODULE[m] for m, _flag, pat in PLAN_MODULES if re.search(pat, text, re.I)})


def _plan_context(restaurant_id=None, anchors=(), guest_texts=(), meta=None, data_state=None):
    """The weekly plan's ValidationContext (surface "weekly_plan",
    unattended): the anchors with their strengths (a bare string is a
    "likely" cause), the guest text as untrusted, every other tenant's name
    denied, and what Ask's own validation read — its K1 confidence, and its
    typed facts / prompt text when meta carries them (meta["facts"],
    meta["context_text"]). Without those the engine checks no figure here:
    the item's figures are held to Ask's verdict on the answer
    (meta.unverified_all), which is the engine's own F-rule findings."""
    import response_validation as rv
    meta = meta or {}
    denied = set()
    if restaurant_id:
        try:
            import models as _m
            denied = _m.other_tenant_names(restaurant_id)
        except Exception:
            denied = set()
    conf = meta.get("confidence_detail")
    # A plan item that cuts staff is held to the restaurant's floors and its
    # "never cut below" default (A2; schedule_rules.cut_floor).
    import schedule_rules as _sr
    cut = _sr.cut_policy(restaurant_id) if restaurant_id else {}
    return rv.ValidationContext(
        restaurant_id=restaurant_id, surface="weekly_plan", facts=list(meta.get("facts") or []),
        context_text=str(meta.get("context_text") or ""),
        cause_anchors=[a if isinstance(a, dict) else {"text": a, "strength": "likely"} for a in anchors or ()],
        untrusted=[t for t in guest_texts or () if t], tenant_names_denied=denied,
        confidence=conf if isinstance(conf, dict) else None,
        # The registry's state of what the plan read (DH1-2, DH3-7).
        data_state=dict(data_state or meta.get("data_state") or {}),
        policy={"action": "weekly_plan", **cut})


def _plan_item_problem(item, unverified, ctx=None, unsupported_names=()):
    """Why a plan item is not filed, or None (H7). The plan is filed with
    nobody reading it first, from a snapshot that carries public review
    text, so an item needs what an owner would have checked.

    Its own rules: at least one money, percentage or rating figure the
    answer's verifier traced to what the model read (the prompt's "cite a
    figure you actually read", now enforced), no figure it could not trace
    (meta.unverified_all), and no name Ask's check found nowhere in what it
    read (meta.unsupported_names — NS6 A2: the plan used to ignore it).

    Then the Response Validation Layer (surface "weekly_plan", unattended)
    on the title and the why, each as a line: injection residue and the
    six-word echo of a guest's words (I1 — the plan's own copies of those
    are gone), a cause no stored diagnosis holds (K1), another tenant's name
    (T1), peer and industry claims with no benchmark (B1), certainty (C1),
    unsafe actions (A2). A line the engine drops keeps the item from being
    filed; its rewrites (a lowered modal, a softened cause) are written back
    onto the item that is filed. The verdicts are logged."""
    import re
    import response_validation as rv
    from ai_guard import figure_claims
    text = f"{item.get('title') or ''}. {item.get('why') or ''}"
    for name in (n for n in unsupported_names or () if n):
        if re.search(r"(?<![\w])" + re.escape(str(name)) + r"(?![\w])", text):
            return f"it names {name}, who is not in anything it read"
    if _plan_item_unverified(item, unverified):
        return "it states a figure nothing it read supports"
    bad = [u for u in (unverified or []) if u]
    # Every figure the item states must be one the verifier traced (R6):
    # each checkable claim (money, %, rating, count) is matched to the
    # unverified list claim by claim, not only by substring of the text.
    from ai_guard import checkable_claims
    if any(any(c == u or c in u or u in c for u in bad) for c in checkable_claims(text)):
        return "it states a figure nothing it read supports"
    figures = [c for c in figure_claims(text) if c["kind"] in ("money", "pct", "star") and not c["year"]
               and not any(c["raw"] in u or u in c["raw"] for u in bad)]
    if not figures:
        return "it cites no verified figure"
    if ctx is None:
        ctx = _plan_context()
    fields = [f for f in ("title", "why") if str(item.get(f) or "").strip()]
    res = rv.validate_lines([item[f] for f in fields], ctx)
    for f, v in zip(fields, res.verdicts):
        rv.log(v, ctx, original=item[f])
    if res.dropped:
        v = next(v for v in res.verdicts if v.verdict == "refuse" or not v.text.strip())
        f = next((x for x in v.findings if x["severity"] in ("drop", "refuse")), None)
        if f and f["rule"] == "I1" and "untrusted" in (f.get("detail") or ""):
            return "it repeats a guest's own words"
        if f and f["rule"] == "K1":
            return "it states a cause no stored diagnosis supports"
        return f"{f['detail']} ({f['rule']})" if f else "the validation layer refused it"
    # Filed with the engine's rewrites — they only ever lower a claim.
    for f, v in zip(fields, res.verdicts):
        item[f] = v.text
    return None


def plan_memory(restaurant_id, db_path=DB_PATH) -> str:
    """What Cavnar AI remembers, for the Monday plan (memory audit 9/29/26,
    memory_context surface "weekly_plan"): the owner's constraints and
    goals, the latest claims Cavnar AI made and what happened since,
    decisions, what has worked and how events moved sales here — fenced and
    M/D/YY-dated by the assembler. "" when there is nothing. Never raises."""
    try:
        import memory_context
        block = memory_context.memory_context(restaurant_id, "weekly_plan",
                                              db_path=None if db_path == DB_PATH else db_path)
        if not block.text:
            return ""
        from ai_guard import MEMORY_FENCE_NOTE
        return ("\n\nWHAT CAVNAR AI REMEMBERS ABOUT THIS RESTAURANT (do not re-propose what the owner declined, "
                "and weigh what did and didn't hold. " + MEMORY_FENCE_NOTE + "):\n" + block.text)
    except Exception as e:
        print(f"[weekly_plan] memory unavailable rid={restaurant_id}: {e}")
        return ""


# ── the plan's memory (memory audit 9/29/26, "weekly_plan") ──────────────────
#
# The Monday plan had no memory of last week's plan and no check against
# what the owner declined: "Trim Tuesday dinner by one server" was filed
# three Mondays running as three open issues (plan:<week>:<i> is a new key
# each week, and issues dedupe on source_key only), and advice declined on
# Home came back as a plan item. Now the plan is handed its earlier items
# with their state and any measured verdict — an unresolved one is
# escalated or retired, never re-filed word for word — and an item is not
# filed when its advice was declined or answered on any surface, or when
# it matches an open plan item.

PLAN_MEMORY_OPEN_ITEMS = 6


def _plan_norm(text):
    import re
    return re.sub(r"[^a-z0-9 ]", "", " ".join(str(text or "").lower().split()))


def _prev_week(week):
    """"2026-W40" -> "2026-W39" (ISO weeks)."""
    from datetime import date, timedelta
    try:
        y, w = str(week).split("-W")
        monday = date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError):
        return None
    return (monday - timedelta(days=7)).strftime("%G-W%V")


def plan_history(restaurant_id, week, db_path=DB_PATH) -> list:
    """Last week's plan items, and any older plan item still open (the
    newest PLAN_MEMORY_OPEN_ITEMS), each {id, title, status, filed_on,
    resolved_on, note, verdict, weeks_open, signature}. Dates M/D/YY."""
    from time_utils import mdy
    prev = _prev_week(week)
    out = []
    try:
        import models as _m_ph
        conn = _m_ph.get_conn() if db_path == DB_PATH else _m_ph.get_conn(db_path)
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT id, title, status, source_key, created_at, resolved_at, acknowledged_at, resolution_note "
            "FROM ops_issues WHERE restaurant_id=? AND source_key LIKE 'plan:%' "
            "AND (source_key LIKE ? OR status != 'resolved') ORDER BY id DESC LIMIT 40",
            (restaurant_id, f"plan:{prev}:%" if prev else "plan:none:%")).fetchall()
        for r in rows:
            v = None
            try:
                o = conn.execute("SELECT verdict, status FROM recommendation_outcomes WHERE restaurant_id=? "
                                 "AND source_key=? ORDER BY id DESC LIMIT 1", (restaurant_id, r["source_key"])).fetchone()
                if o and o["status"] == "evaluated" and o["verdict"] in ("improved", "worsened", "no_clear_change"):
                    v = o["verdict"]
            except Exception:
                v = None
            filed_week = str(r["source_key"] or "").split(":")[1] if str(r["source_key"]).count(":") >= 2 else None
            weeks_open = None
            if filed_week and r["status"] != "resolved":
                try:
                    from datetime import date as _d
                    fy, fw = filed_week.split("-W")
                    ty, tw = str(week).split("-W")
                    weeks_open = max(1, (_d.fromisocalendar(int(ty), int(tw), 1)
                                         - _d.fromisocalendar(int(fy), int(fw), 1)).days // 7)
                except (ValueError, TypeError):
                    weeks_open = None
            try:
                import insight_store
                sig = insight_store.advice_signature("plan:x", r["title"])
            except Exception:
                sig = None
            out.append({"id": r["id"], "title": r["title"], "status": r["status"],
                        "filed_on": mdy(str(r["created_at"] or "")[:10]) if r["created_at"] else None,
                        "resolved_on": mdy(str(r["resolved_at"])[:10]) if r["resolved_at"] else None,
                        "note": r["resolution_note"], "verdict": v, "weeks_open": weeks_open,
                        "last_week": bool(prev and str(r["source_key"]).startswith(f"plan:{prev}:")),
                        "signature": sig})
    except Exception as e:
        print(f"[weekly_plan] plan history unreadable for {restaurant_id}: {e}")
    finally:
        conn.close()
    lw = [x for x in out if x["last_week"]]
    older_open = [x for x in out if not x["last_week"] and x["status"] != "resolved"][:PLAN_MEMORY_OPEN_ITEMS]
    return lw + older_open


def plan_memory_block(items) -> str:
    """The question's LAST WEEK'S PLAN section: each earlier item's state,
    its measured verdict, and what to do with one still open. The titles
    and notes were written by a model or a person: fenced."""
    if not items:
        return ""
    from ai_guard import wrap_untrusted
    lines = []
    for it in items:
        state = {"resolved": "done", "acknowledged": "acknowledged, not finished"}.get(it["status"], "still open")
        line = f"- {wrap_untrusted(it['title'])} — {state}"
        if it.get("filed_on"):
            line += f", filed {it['filed_on']}"
        if it.get("weeks_open") and it["status"] != "resolved":
            line += f", open {it['weeks_open']} week{'s' if it['weeks_open'] != 1 else ''}"
        if it.get("resolved_on"):
            line += f", resolved {it['resolved_on']}"
        if it.get("note"):
            line += " — note: " + wrap_untrusted(str(it["note"])[:160])
        if it.get("verdict"):
            line += f" — measured afterwards: {it['verdict'].replace('_', ' ')} (before and after, not proof)"
        lines.append(line)
    return ("\n\nLAST WEEK'S PLAN — what you filed before and what became of it. An item still open is "
            "either escalated (say it is still open and for how long) or retired (say why it no longer "
            "applies); never file the same item again in the same or other words. Build on what was done "
            "and measured.\n" + "\n".join(lines))


def plan_item_repeat(restaurant_id, item, history=None, db_path=DB_PATH):
    """Why a plan item is not filed because it repeats memory, or None: its
    advice was declined or answered on any surface (insight_store's
    declined and answered signatures — the rule pick_one_thing follows), or
    it matches a plan item still open (same words, or the same advice)."""
    text = f"{item.get('title') or ''}. {item.get('why') or ''}"
    try:
        import insight_store
        sig = insight_store.advice_signature("plan:x", text,
                                             subjects=insight_store.known_subjects(restaurant_id, db_path=db_path))
        if sig:
            if sig in insight_store.declined_signatures(restaurant_id, db_path=db_path):
                return "the owner passed on this advice"
            if sig in insight_store.answered_signatures(restaurant_id, db_path=db_path):
                return "the owner already answered this advice"
    except Exception as e:
        print(f"[weekly_plan] signature check unavailable: {e}")
        sig = None
    title = _plan_norm(item.get("title"))
    for h in history if history is not None else plan_history(restaurant_id, "", db_path=db_path):
        if h["status"] == "resolved":
            continue
        if _plan_norm(h["title"]) == title or (sig and h.get("signature") == sig):
            return f"it repeats plan item #{h['id']}, still open"
    return None


def run_weekly_plan(db_path=DB_PATH):
    """Monday 7am local: the agent — not a script — reads the week and files
    up to three owned actions as issues (notify=False: they appear on Home,
    nobody is texted). The morning brief is deterministic by design; this is
    the one place the model is asked to hold the why across modules. Off
    unless weekly_plan_enabled; claimed per ISO week.

    Unattended, so it is offered read tools only (no direct actions, no
    proposals, no memory writes), an item whose figures failed verification
    is not filed, and a run that fails gives its claim back so the next tick
    retries it (AI-17)."""
    import ops, issues
    from time_utils import restaurant_now
    filed = 0
    tally = {"attempted": 0, "failed": 0}
    # Bounded and resumable (#84): an Ask-with-tools run per restaurant,
    # serially, with no bound, inside one Monday tick.
    walk = _BoundedWalk("weekly_plan", _restaurants(db_path), db_path, WEEKLY_PLAN_MAX_SECONDS)
    for r in walk:
        if not getattr(r, "weekly_plan_enabled", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 0 or not (7 <= local.hour < 11):
            continue
        week = local.strftime("%G-W%V")
        # Claimed before the call so two ticks cannot both run it; given
        # back below if the run fails, within a bounded number of attempts.
        if not ops.claim_period(f"weekly_plan:{r.id}", week):
            continue
        if not any(ops.claim_period(f"weekly_plan_attempt:{r.id}", f"{week}#{n}")
                   for n in range(WEEKLY_PLAN_MAX_ATTEMPTS)):
            continue    # attempts for this week are spent; the claim stays
        tally["attempted"] += 1
        try:
            from ask_cavnar import ask_with_tools
            # The readiness gate, per module (DH5-2): a module whose data
            # can't be stood on this week is held — the plan is told so, and
            # an item about it is not filed — while the others still plan.
            holds = plan_holds(r, db_path=db_path)
            question = WEEKLY_PLAN_PROMPT
            # Last week's plan and what became of it (memory audit, weekly_plan),
            # and what Cavnar AI remembers about the restaurant (memory_context
            # surface "weekly_plan": the owner's rules, constraints, goals, its
            # last claims and what followed, decisions, what worked, events).
            # A system block and part of the verified corpus, not the question:
            # a measured figure found only in the question could never back an
            # item (memory re-audit 9/29/26, PROMPTS-3).
            history = plan_history(r.id, week, db_path=db_path)
            plan_mem = (plan_memory_block(history) + plan_memory(r.id, db_path=db_path)).strip()
            if holds:
                question += ("\n\nHELD THIS WEEK — the data behind these isn't current, so propose no action "
                             "about them: " + "; ".join(f"{m.replace('_', ' ')} ({why})" for m, why in holds.items())
                             + ".")
            # Its own ledger action and one correlation id per run (#148):
            # the Monday plan was filed as "ask_cavnar", so its spend could
            # not be told apart from the owner's questions.
            import ai_utils as _ai_wp
            with _ai_wp.ai_context(trigger="scheduler", correlation_id=f"weekly_plan:{r.id}:{week}"):
                answer, _trunc, _props, _meta = ask_with_tools(r, question, history=[], user=None,
                                                               read_only=True, delivery="unattended",
                                                               action="weekly_plan", memory_block=plan_mem)
            # The FULL list (R6, B5 #6): unverified_figures is cut to five for
            # the screen, and the sixth invented figure was filed unattended.
            unverified = (_meta or {}).get("unverified_all")
            if unverified is None:
                unverified = (_meta or {}).get("unverified_figures") or []
            # The Response Validation Layer's context for every item (surface
            # "weekly_plan", unattended): the stored causes with their
            # strengths, the review text as untrusted, other tenants' names,
            # and what Ask's own validation carried in meta.
            ctx = _plan_context(r.id, _plan_anchors(r.id, db_path=db_path),
                                _guest_texts(r.id, db_path=db_path), _meta)
            names = (_meta or {}).get("unsupported_names") or []
            for i, item in enumerate(_parse_plan(answer)):
                held = plan_item_held(item, holds)
                why_not = (f"it is about {held.replace('_', ' ')}, whose data isn't current ({holds[held]})"
                           if held else (plan_item_repeat(r.id, item, history=history, db_path=db_path)
                                         or _plan_item_problem(item, unverified, ctx, unsupported_names=names)))
                if why_not:
                    # An AI-quality finding (fix round G #58), not a failing job.
                    _ai_wp.record_quality_event("weekly_plan", "item_dropped", restaurant_id=r.id,
                                                action="weekly_plan", detail=f"weekly plan item not filed: {why_not}")
                    continue
                issues.create_issue(
                    r.id, "plan", item["title"],
                    detail=f"{item['why']} Owner: {item['owner']}. Due in {item['due_days']} days.",
                    severity="normal", source_key=f"plan:{week}:{i}", notify=False, db_path=db_path,
                    meta={"modules": plan_item_modules(item)})
                filed += 1
        except Exception as e:
            tally["failed"] += 1
            ops.release_period(f"weekly_plan:{r.id}", week)
            from ai_utils import AIBudgetExceeded
            if not isinstance(e, AIBudgetExceeded):
                ops.capture(e, job="weekly_plan", context=f"restaurant_id={r.id}")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"],
                   hit_bound=walk.hit_bound, filed=filed)


def run_recipe_drafts(db_path=DB_PATH):
    """Tuesday 5am local, after the nightly depletion has created any new
    menu items: draft recipes for dishes that have none (recipes.py), a
    bounded number per restaurant per week. Only where Food Cost is on.

    The drafts go through Message Batches where they may (recipes.
    draft_missing(batch=True), AI cost audit 10/7/26 #60): nobody waits on
    them, so they are written at half price within the hour and stored when
    the collector hands them back; `batched` counts the dishes sent."""
    import ops, recipes
    from time_utils import restaurant_now
    drafted, attempted, failed, batched = 0, 0, 0, 0
    walk = _BoundedWalk("recipe_drafts", _restaurants(db_path), db_path, RECIPE_DRAFTS_MAX_SECONDS)
    for r in walk:
        if not getattr(r, "module_inventory", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 1 or not (5 <= local.hour < 9):
            continue
        if not ops.claim_period(f"recipe_drafts:{r.id}", local.strftime("%G-W%V")):
            continue
        attempted += 1
        try:
            out = recipes.draft_missing(r.id, db_path=db_path, batch=True)
            drafted += out.get("drafted", 0)
            batched += out.get("batched", 0)
        except Exception as e:
            failed += 1
            ops.capture(e, job="recipe_drafts", context=f"restaurant_id={r.id}")
    return _counts(attempted, attempted - failed, failed, hit_bound=walk.hit_bound, drafted=drafted,
                   batched=batched)


def run_issue_scan(db_path=DB_PATH, local_hour=None):
    """Reviews hourly; the daily operational signals and the opening
    checklist once a day at `local_hour` in the restaurant's own timezone
    (None = no gate, for a direct call)."""
    import issues, ops
    from time_utils import restaurant_now
    opened, attempted, failed = 0, 0, 0
    # Bounded and resumable (#84): hourly, over every restaurant, with no bound.
    walk = _BoundedWalk("issue_scan", _restaurants(db_path), db_path, ISSUE_SCAN_MAX_SECONDS)
    for r in walk:
        attempted += 1
        broke = False
        if getattr(r, "module_reviews", 0):
            try:
                opened += len(issues.open_from_reviews(r.id, db_path=db_path) or [])
            except Exception as e:
                broke = True
                ops.capture(e, job="issue_scan", context=f"restaurant_id={r.id}")
        try:
            # A review issue whose reply has posted is done — close it
            # rather than leave the manager chased about a fixed thing (#18).
            issues.auto_close(r.id, db_path=db_path)
        except Exception as e:
            broke = True
            ops.capture(e, job="issue_auto_close", context=f"restaurant_id={r.id}")
        try:
            local = restaurant_now(r, naive=True)
            # The checklist is time-of-day sensitive, so it runs every pass —
            # its own grace period decides when it is late (issues.
            # open_from_checklists), and source_key keeps it to one a day.
            opened += len(issues.open_from_checklists(r.id, db_path=db_path, now_local=local) or [])
        except Exception as e:
            broke = True
            ops.capture(e, job="issue_checklists", context=f"restaurant_id={r.id}")
        failed += 1 if broke else 0
        if local_hour is not None:
            import scheduler
            if not scheduler.local_due(r, local_hour, claim_key="issue_signals"):
                continue
        try:
            opened += len(issues.open_from_signals(r.id, db_path=db_path) or [])
        except Exception as e:
            if not broke:
                failed += 1
            ops.capture(e, job="issue_signals", context=f"restaurant_id={r.id}")
    return _counts(attempted, attempted - failed, failed, hit_bound=walk.hit_bound, opened=opened)


def _week_has_schedule(conn, restaurant_id, week_start) -> bool:
    """Whether the week the auto-draft would write already has a draft in
    force or went out (re-audit 10/4/26 PIPE-3): then that week is handled,
    and a new draft would supersede it. The guard used to be any week's row
    generated in the last five days, which let the auto-draft supersede a
    draft the owner had made a week earlier and edited since — and
    auto-publish could then send the unedited AI draft."""
    return conn.execute(
        "SELECT 1 FROM schedule_history WHERE restaurant_id=? AND week_start=? "
        "AND (superseded_by IS NULL OR published_at IS NOT NULL) LIMIT 1",
        (restaurant_id, week_start)).fetchone() is not None


# A suggestion this strong waits for nobody to open the intel screen
# (schedule audit 10/3/26 L-14): the largest fitted effect behind a weight it
# moves, or behind a floor or bar it moves, is surfaced in the action queue
# (action_queue — "Shift Quality calibration has a strong suggestion"). The
# owner still applies it; nothing is applied on its own.
CALIBRATION_STRONG_EVIDENCE = 0.2


def run_quality_calibration(db_path=DB_PATH):
    """Weekly: record the Shift Quality suggestion for each Labor restaurant,
    once per new published week, as capability changes — the weights
    (quality_weights_suggested) and, since SQ-22, each profile's floors and
    bar (quality_profiles_suggested), shown in the change history — and
    into the scheduling memory's log (`calibration_suggested`, with its
    evidence), which the action queue reads to put a STRONG suggestion
    (CALIBRATION_STRONG_EVIDENCE) in front of the owner (L-14: the loop
    closed only if somebody opened the right screen). Nothing is written to
    the weights, floors or bars until the owner applies it.

    ONE algorithm: the suggestion is schedule_learning.calibrate_weights,
    exactly what "Apply" (strategy_routes._do_calibration_apply) writes.
    This job used to run its own clean-versus-troubled-weeks rule, so the
    history said one set of weights was suggested and Apply wrote a
    different one (re-audit A-26)."""
    import json as _j
    import ops as _ops_cal
    import schedule_learning as _sl
    import shift_quality as _sq
    from models import get_quality_weights, record_capability_change
    changed = {"n": 0}

    def _one(r):
        if not getattr(r, "module_labor", 0):
            return
        cal = _sl.calibrate_weights(r.id, db_path=db_path)
        if not cal.get("ready") or not (cal.get("suggested_weights") or cal.get("suggested_profiles")):
            return
        current = get_quality_weights(r.id) or {}
        merged = dict(_sq.DEFAULT_WEIGHTS)
        merged.update(current)
        after = dict(merged)
        after.update({k: float(v) for k, v in (cal.get("suggested_weights") or {}).items()})
        weights_move = any(abs(float(after[k]) - float(merged.get(k, 0) or 0)) >= 0.05 for k in after)
        if not weights_move and not cal.get("suggested_profiles"):
            return                                 # nothing to suggest
        # Once per new published week: the same evidence read again next
        # week is not a new suggestion.
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT MAX(week_start) AS w FROM schedule_history WHERE restaurant_id=? "
                               "AND published_at IS NOT NULL", (r.id,)).fetchone()
        finally:
            conn.close()
        newest = row["w"] if row else None
        # A once-ever marker per (restaurant, published week), in a table
        # that is never pruned (#157): in the 45-day claims table a
        # restaurant that stopped publishing got the same suggestion again
        # every 45 days. Existing claims were carried over at boot.
        if newest and not _ops_cal.claim_marker("quality_calibration", f"{r.id}:{newest}"):
            return
        # Suggested, never applied: "Apply" is one tap and writes these same
        # numbers (_do_calibration_apply over calibrate_weights).
        if weights_move:
            record_capability_change(r.id, "quality_weights_suggested", subject="weights",
                                     before=_j.dumps(current), after=_j.dumps(after),
                                     changed_by="Cavnar AI (calibration)")
        if cal.get("suggested_profiles"):
            prof = cal.get("profiles") or {}
            before_p = {k: {"min_quality": ((prof.get(k) or {}).get("bar") or {}).get("current"),
                            "floors": {d: e.get("current") for d, e in ((prof.get(k) or {}).get("floors") or {}).items()
                                       if d in ((cal["suggested_profiles"].get(k) or {}).get("floors") or {})}}
                        for k in cal["suggested_profiles"]}
            record_capability_change(r.id, "quality_profiles_suggested", subject="profiles",
                                     before=_j.dumps(before_p), after=_j.dumps(cal["suggested_profiles"]),
                                     changed_by="Cavnar AI (calibration)")
        # How strong it is, kept where the action queue reads it: the largest
        # fitted effect behind a weight that moves, and whether a floor or a
        # bar moves (its own evidence floor is 0.5 SD, past which it is strong).
        dims = cal.get("dimensions") or {}
        evidence = max((abs(float(dims[k].get("evidence") or 0)) for k in (cal.get("moving") or []) if k in dims),
                       default=0.0)
        import schedule_memory as _smem_cal
        _smem_cal.observe(r.id, "calibration_suggested", week_start=newest, value={
            "evidence": round(evidence, 3), "moving": list(cal.get("moving") or []),
            "profiles": sorted(cal.get("suggested_profiles") or {}),
            "strong": evidence >= CALIBRATION_STRONG_EVIDENCE or bool(cal.get("suggested_profiles"))},
            origin="system", phase="as_run", authority="system", source="calibration",
            db_path=None if db_path == DB_PATH else db_path, fact_key=f"calibration_suggested|{newest}")
        changed["n"] += 1

    attempted, failed, hit = _bounded_each("quality_calibration", _one, db_path)
    return _counts(attempted, attempted - failed, failed, hit_bound=hit, restaurants_changed=changed["n"])


def run_auto_draft_schedules(db_path=DB_PATH, now=None):
    """Draft next week's schedule for every opted-in restaurant that hasn't
    already made one. The draft lands in Schedule History exactly as a
    hand-generated one does; nothing reaches staff until the owner publishes.

    Skipped where an external scheduling tool is named (the owner schedules
    in 7shifts/HotSchedules/etc. and a Cavnar draft would be a second,
    conflicting source of truth), and where Labor isn't on the plan.

    `now` is the scheduler tick's instant (naive Chicago, scheduler._chi_now):
    whose draft day it is is read from it, so the pass and the loop agree on
    the clock. Without it, each restaurant's own wall clock now."""
    import ops
    import threading
    import scheduler as _sched
    import schedule_engine as _se
    counts = {"drafted": 0, "skipped": 0}
    lock = threading.Lock()
    # Bounded and resumable, like run_daily_fetch: a worker pool, a
    # wall-clock bound and a cursor. It used to be one serial 40-minute pass
    # claimed once per Thursday, so the restaurants past the bound waited a
    # whole week (SCHED-11 / DATA-8); the scheduler now runs a pass every
    # hour, each starting at the cursor, and a restaurant is attempted at
    # most once a day (claimed before the paid generation).
    #
    # Each restaurant on ITS day (auto_draft_weekday: the owner's pick, else
    # Monday-Thursday spread by restaurant — AI cost audit 10/7/26 #5, a
    # Thursday for everyone queued every draft on two generation slots)
    # from AUTO_DRAFT_HOUR in its own zone; the loop used to gate every
    # restaurant on Chicago's Thursday 6am.
    from models import auto_draft_weekday
    from time_utils import restaurant_now, restaurant_tz, OPERATOR_TZ
    from zoneinfo import ZoneInfo
    at = None if now is None else (now if now.tzinfo else now.replace(tzinfo=ZoneInfo(OPERATOR_TZ)))
    local = {}
    for r in _restaurants(db_path):
        if not (getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)):
            continue
        now_l = (restaurant_now(r, naive=True) if at is None
                 else at.astimezone(restaurant_tz(r)).replace(tzinfo=None))
        if now_l.weekday() == auto_draft_weekday(r) and now_l.hour >= AUTO_DRAFT_HOUR:
            local[r.id] = (r, now_l.date().isoformat())
    by_id = {v[0].id: v[0] for v in local.values()}
    counts["failed"] = 0

    def _bump(key):
        with lock:
            counts[key] += 1

    def _one(rid):
        r = by_id[rid]
        if (getattr(r, "external_scheduling_tool", None) or "").strip():
            _bump("skipped")
            return
        # The week it would write, as the job reads it (schedule_engine.
        # _week_monday of the restaurant's own today): next week.
        from datetime import datetime as _dt_ad
        target = _se._week_monday(_dt_ad.strptime(local[r.id][1], "%Y-%m-%d")).strftime("%Y-%m-%d")
        conn = get_conn(db_path)
        try:
            if _week_has_schedule(conn, r.id, target):
                _bump("skipped")
                return
        finally:
            conn.close()
        # The restaurant's own date: the server's is UTC, which turned over
        # at 7pm in Chicago and allowed a second paid try the same evening.
        if not ops.claim_period(f"auto_draft:{r.id}", local[r.id][1]):
            _bump("skipped")               # attempted earlier today — never a second paid try
            return
        try:
            _draft_one(r, db_path, _se, _bump, period=local[r.id][1])
        except Exception:
            _bump("failed")
            raise

    # The longest finished prefix is the cursor (scheduler.resumable_sweep,
    # #84): it was written from the success count, so with failures it
    # landed short, and the bound reached job_runs only as `complete`.
    _done, ran_out = _sched.resumable_sweep(AUTO_DRAFT_CURSOR_KEY, sorted(by_id), _one, AUTO_DRAFT_MAX_SECONDS,
                                            workers=AUTO_DRAFT_WORKERS, job="auto_draft_schedule")
    attempted = counts["drafted"] + counts["failed"]
    return _counts(attempted, counts["drafted"], counts["failed"], counts["skipped"], ran_out,
                   drafted=counts["drafted"], complete=not ran_out)


def _draft_one(r, db_path, _se, _bump, period=None):
    """One restaurant's auto-draft and, when it saved, the owner's nudge.

    Claimed like an owner's press (ops.claim_async_job): it used to start a
    job of its own beside one the owner had running for the same restaurant
    — two paid generations of the same week, the later one superseding the
    other (schedule audit 10/3/26 P-39). When the owner's is running, theirs
    is the draft; today's claim is handed back so a later pass can try again
    if theirs fails. And it takes a slot of the bounded generation pool
    (schedule_engine.generation_scope) like any generation, with the same
    wall clock."""
    import ops
    from schedule_engine import generation_scope, generation_request, GEN_PRIORITY_BACKGROUND
    # What it asks, as an owner's press of Generate for next week would: an
    # owner's identical press joins it, and it never joins (or is joined by)
    # a generation of anything else (re-audit 10/4/26 UI-8).
    from datetime import datetime as _dt_req
    _today = _dt_req.strptime(period, "%Y-%m-%d") if period else None
    try:
        job_id, joined = ops.claim_async_job(f"auto-{uuid.uuid4().hex[:12]}", "schedule", r.id,
                                             request=generation_request(r.id, today=_today))
    except ops.JobBusy:
        job_id, joined = None, True
    if joined:
        if period:
            ops.release_period(f"auto_draft:{r.id}", period)
        _bump("skipped")
        return
    # Behind any owner's press (AI cost audit 10/7/26 #5): an owner who
    # presses Generate takes the next free slot ahead of queued drafts.
    try:
        with generation_scope(job_id, priority=GEN_PRIORITY_BACKGROUND):
            _se._run_schedule_job(job_id, r.id)
    except _se.GenerationSlotTimeout:
        # Every slot stayed busy (owners' presses first): the job is closed,
        # the day handed back, and the next hourly pass tries again.
        ops.finish_async_job(job_id, "error", {"ok": False, "error": "No generation slot came free — "
                                                                     "it will try again within the hour."})
        if period:
            ops.release_period(f"auto_draft:{r.id}", period)
        _bump("skipped")
        return
    # _run_schedule_job reports its own failures into the job row rather
    # than raising, so the push below must wait on that verdict — telling
    # an owner a draft is waiting when none was saved is worse than silence.
    state = ops.read_async_job(job_id, restaurant_id=r.id) or {}
    if state.get("status") != "done":
        return
    _bump("drafted")
    try:
        import notify, push
        # The owner's task, not the line cook's: a teammate with the app
        # was told to review and publish a schedule they cannot publish.
        # morning_brief.recipients is the same audience the brief uses.
        # Only the ones who can publish it (SCHEDULE_PUBLISH).
        import morning_brief
        from permissions import has_permission, SCHEDULE_PUBLISH
        audience = {u["id"] for u in morning_brief.recipients(r.id, db_path)
                    if has_permission(u, SCHEDULE_PUBLISH)}
        if not notify.briefing_allowed(r.id, "schedule_drafted", db_path):
            return
        notify.record_notification(r.id, "schedule_drafted", db_path=db_path)
        # An empty audience is nobody, not everyone: `user_ids=None` is
        # every phone at the restaurant, members who cannot publish
        # included — the case this audience exists to prevent (F2-11).
        if not audience:
            return
        # A week with days the generation could not write is saved with the
        # rest kept (schedule audit 10/3/26 P-34) — said here too, never
        # announced as a finished draft.
        gaps = ((state.get("result") or {}).get("unwritten_dates") or []) if isinstance(state.get("result"), dict) else []
        body = ("Review it and publish when it looks right — nothing has gone to your staff yet." if not gaps else
                f"{len(gaps)} day{'s' if len(gaps) != 1 else ''} couldn't be written — redo "
                f"{'it' if len(gaps) == 1 else 'them'} before you publish. Nothing has gone to your staff yet.")
        push.fire_push(r.id, "schedule_drafted",
                       "Next week's schedule is drafted" if not gaps else "Next week's schedule is partly drafted",
                       body, data={}, db_path=db_path, user_ids=audience)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule_push", context=f"restaurant_id={r.id}")


AUTO_DRAFT_CURSOR_KEY = "auto_draft_schedule_cursor"
AUTO_DRAFT_HOUR = 6                 # local; the day is the owner's (models.auto_draft_weekday)
AUTO_DRAFT_MAX_SECONDS = 40 * 60
AUTO_DRAFT_WORKERS = 3              # each draft is minutes of model wait; three share the pass


def _read_cursor(key, db_path) -> int:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row and str(row["value"]).isdigit() else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _write_cursor(key, value, db_path) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, str(value)))
        conn.commit()
    except Exception as e:
        import ops
        ops.capture(e, job="auto_draft_cursor", context=key)
    finally:
        conn.close()


LABOR_REMINDERS_CURSOR_KEY = "labor_reminders_cursor"
LABOR_REMINDERS_MAX_SECONDS = 10 * 60
REQUEST_REMIND_DAYS = 1          # a drop or swap for today or tomorrow still unanswered
TIME_OFF_REMIND_DAYS = 2         # time off starting within two days still unanswered
UNPUBLISHED_REMIND_DAYS = 3      # next week starts within three days and staff don't have it


def labor_waiting(restaurant_id, db_path=DB_PATH, today=None, draft=True) -> dict:
    """What a manager owes before the next shifts: requests close to their
    date nobody has answered, and (with `draft`; auto-publish restaurants
    hear about their draft from that job) a drafted week that has not gone
    out. {"lines": [...], "history_id": id or None}. Read-only."""
    from datetime import timedelta
    import shift_requests as _sreq
    from models import get_conn as _gc
    from time_utils import mdy
    today = _sreq._today(restaurant_id, today)
    lines, hid = [], None
    conn = _gc(db_path)
    try:
        for r in conn.execute("SELECT employee_name, date, shift_start, kind FROM shift_change_requests "
                              "WHERE restaurant_id=? AND status='pending' AND date BETWEEN ? AND ? ORDER BY date, shift_start",
                              (restaurant_id, today.isoformat(),
                               (today + timedelta(days=REQUEST_REMIND_DAYS)).isoformat())).fetchall():
            what = "swap" if (r["kind"] or "") == "swap" else "drop"
            lines.append(f"{r['employee_name']} asked to {what} {mdy(r['date'])} {r['shift_start'] or ''}".rstrip()
                         + " — still unanswered.")
        for r in conn.execute("SELECT employee_name, start_date FROM staff_time_off WHERE restaurant_id=? AND status='pending' "
                              "AND start_date BETWEEN ? AND ? ORDER BY start_date",
                              (restaurant_id, today.isoformat(),
                               (today + timedelta(days=TIME_OFF_REMIND_DAYS)).isoformat())).fetchall():
            lines.append(f"{r['employee_name']}'s time off from {mdy(r['start_date'])} — still unanswered.")
        soon = (today + timedelta(days=UNPUBLISHED_REMIND_DAYS)).isoformat()
        draft = draft and conn.execute(
            # >= today: a week that STARTS today and still hasn't gone to
            # staff is the most urgent one to remind about (A-30).
            "SELECT h.id, h.week_start FROM schedule_history h WHERE h.restaurant_id=? AND h.week_start >= ? "
            "AND h.week_start <= ? AND h.published_at IS NULL AND h.superseded_by IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM schedule_history p WHERE p.restaurant_id=h.restaurant_id "
            "AND p.week_start=h.week_start AND p.published_at IS NOT NULL) ORDER BY h.id DESC LIMIT 1",
            (restaurant_id, today.isoformat(), soon)).fetchone()
        if draft:
            hid = draft["id"]
            lines.append(f"The week of {mdy(draft['week_start'])} is built but staff don't have it yet — send it from Labor.")
    finally:
        conn.close()
    return {"lines": lines, "history_id": hid}


def run_labor_reminders(db_path=DB_PATH):
    """9am local: one notice per Labor restaurant naming what is waiting on
    the manager before the next shifts (labor_waiting). Nothing waiting, no
    notice. Runs every hour; each restaurant is claimed once a day at its
    own 9am. Bounded and resumable."""
    import scheduler as _sched
    by_id = {r.id: r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)}
    sent = {"n": 0}
    tally = {"attempted": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        r = by_id[rid]
        if not _sched.local_due(r, 9, claim_key="labor_reminders"):
            return
        with lock:
            tally["attempted"] += 1
        w = labor_waiting(r.id, db_path=db_path, draft=not getattr(r, "auto_publish_schedule", 0))
        if not w["lines"]:
            return
        body = w["lines"][0] if len(w["lines"]) == 1 else f"{len(w['lines'])} things before the next shifts."
        data = {"tab": "labor"}
        if w["history_id"]:
            data["history_id"] = w["history_id"]
        if _reach(r.id, "labor_reminder", "Waiting on you in Labor", body, data, db_path, lines=w["lines"]):
            sent["n"] += 1

    def _run(rid):
        try:
            _one(rid)
        except Exception:
            with lock:
                tally["failed"] += 1
            raise

    # resumable_sweep (#84): the prefix cursor, and the bound reported.
    _done, ran_out = _sched.resumable_sweep(LABOR_REMINDERS_CURSOR_KEY, sorted(by_id), _run,
                                            LABOR_REMINDERS_MAX_SECONDS, workers=1, job="labor_reminders")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"],
                   hit_bound=ran_out, reminded=sent["n"])


def run_schedule_outcomes(db_path=DB_PATH):
    """Monday: record what each published week actually did, by daypart
    (schedule_intel.record_outcomes — the punches beside the plan, L-12),
    score each ended week as it ran beside its planned score
    (record_as_run_quality, L-29), and read every recommendation the manager
    carried out against the night or the week as it ran
    (measure_recommendations_as_run, L-34) as well as the watched nights'
    issue share (measure_accepted_recommendations), for every Labor
    restaurant. A failing step is captured and counts the restaurant as
    failed; the steps after it still run."""
    import schedule_intel
    import scheduler as _sched
    # Bounded and resumable (SCHED-27): a wall-clock bound and a cursor, so a
    # pass that runs out of time is picked up where it stopped instead of
    # starving the same tail every Monday. The prefix cursor and the bound
    # reported (resumable_sweep, #84).
    by_id = {r.id: r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)}
    written = {"n": 0}
    tally = {"attempted": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        try:
            written["n"] += schedule_intel.record_outcomes(rid, db_path=db_path).get("written", 0)
        except Exception:
            with lock:
                tally["failed"] += 1
            raise
        broke = False
        # The ended weeks as they ran, then each carried-out recommendation
        # read against them (rec_ledger outcome), now that the nights are
        # recorded — each on its own, a failure captured and counted.
        for step, fn in (("as_run_quality", schedule_intel.record_as_run_quality),
                         ("as_run_recommendations", schedule_intel.measure_recommendations_as_run),
                         ("accepted_recommendations", schedule_intel.measure_accepted_recommendations)):
            try:
                fn(rid, db_path=db_path)
            except Exception as e:
                import ops
                ops.capture(e, job="schedule_outcomes", context=f"restaurant_id={rid} {step}")
                broke = True
        if broke:
            with lock:
                tally["failed"] += 1

    _done, ran_out = _sched.resumable_sweep(OUTCOMES_CURSOR_KEY, sorted(by_id), _one, OUTCOMES_MAX_SECONDS,
                                            workers=1, job="schedule_outcomes")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"],
                   hit_bound=ran_out, rows=written["n"])


OUTCOMES_CURSOR_KEY = "schedule_outcomes_cursor"
OUTCOMES_MAX_SECONDS = 20 * 60


def run_schedule_memory(db_path=DB_PATH):
    """6am, after people_nightly has kept the standing patterns and last
    night's attendance current: every Labor restaurant's scheduling memory
    rebuilt from its sources (schedule_memory.consolidate — the manager's
    habits, openers and sections, overtime and late closes, teams, the
    owner's redos, and the other learners' facts, each with its evidence,
    confidence, decay and status; schedule audit 10/3/26 L-29). Bounded and
    resumable (resumable_sweep, a cursor in job_cursors). A learner that
    fails is captured and leaves its facts as they were; a restaurant whose
    learner failed counts as failed. Sends nothing."""
    import schedule_memory
    import scheduler as _sched
    by_id = {r.id: r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)}
    tally = {"attempted": 0, "failed": 0, "skipped": 0, "memories": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        try:
            s = schedule_memory.consolidate(rid, db_path=None if db_path == DB_PATH else db_path)
        except Exception:
            with lock:
                tally["failed"] += 1
            raise
        with lock:
            if s.get("skipped"):
                tally["skipped"] += 1
            if s.get("failed"):
                tally["failed"] += 1
            tally["memories"] += int(s.get("created") or 0) + int(s.get("updated") or 0)

    _done, ran_out = _sched.resumable_sweep(MEMORY_CURSOR_KEY, sorted(by_id), _one, MEMORY_MAX_SECONDS,
                                            workers=1, job="schedule_memory")
    if ran_out:
        import ops
        ops.capture(RuntimeError(f"schedule_memory stopped at its {MEMORY_MAX_SECONDS}s bound; the rest lead "
                                 "the next pass"), job="schedule_memory", context="time_bound")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"], tally["skipped"],
                   hit_bound=ran_out, memories=tally["memories"])


MEMORY_CURSOR_KEY = "schedule_memory_cursor"
MEMORY_MAX_SECONDS = 20 * 60


def run_people_nightly(db_path=DB_PATH, today=None):
    """5am, after the POS sync: what last night taught about the people
    (memory audit 9/29/26 — identity, attendance, shift_facts, uncaptured,
    standing_patterns), for every Labor restaurant:

      * people and per-shift facts for a restaurant that has none yet, and a
        person_id on every name-keyed store (people.stamp_person_ids);
      * attendance: each published night of the last week against the POS
        punches, once its POS day is final (attendance.join_published — the
        only attendance RPOWER can have), and what the live clock-in check
        saw (attendance.from_coverage_issues);
      * who took a cover when asked (people.record_cover_signals) and guests
        naming staff, proposed for the owner (people.match_review_mentions);
      * the schedule's standing patterns kept current (schedule_versions.
        refresh_standing_patterns);
      * the per-person quarterly summaries kept forever (shift_facts.
        rollup_quarters).

    Bounded and resumable (resumable_sweep, a cursor in job_cursors). Sends
    nothing."""
    import attendance
    import people
    import shift_facts
    import scheduler as _sched
    from datetime import date as _date, timedelta as _td
    try:
        people.backfill_people(db_path=None if db_path == DB_PATH else db_path, max_seconds=60)
        shift_facts.backfill_from_csv(db_path=None if db_path == DB_PATH else db_path, max_seconds=60)
    except Exception as e:
        import ops
        ops.capture(e, job="people_nightly", context="backfill")
    by_id = {r.id: r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)}
    tally = {"attempted": 0, "failed": 0, "attendance": 0, "signals": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        try:
            from time_utils import restaurant_now_by_id
            day = today or restaurant_now_by_id(rid, naive=True).date()
            people.stamp_person_ids(rid)
            n = 0
            for back in range(1, attendance.JOIN_DAYS + 1):
                n += attendance.join_published(rid, (day - _td(days=back)).isoformat())["recorded"]
            n += attendance.from_coverage_issues(rid, today=day)
            sig = people.record_cover_signals(rid, today=day) + people.match_review_mentions(rid)
            try:
                import schedule_versions
                schedule_versions.refresh_standing_patterns(rid)
            except Exception as e:
                import ops
                ops.capture(e, job="people_nightly", context=f"restaurant_id={rid} standing patterns")
            shift_facts.rollup_quarters(rid, today=day)
            with lock:
                tally["attendance"] += n
                tally["signals"] += sig
        except Exception:
            with lock:
                tally["failed"] += 1
            raise

    _done, ran_out = _sched.resumable_sweep(PEOPLE_CURSOR_KEY, sorted(by_id), _one, PEOPLE_MAX_SECONDS,
                                            workers=1, job="people_nightly")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"],
                   hit_bound=ran_out, attendance=tally["attendance"], signals=tally["signals"])


PEOPLE_CURSOR_KEY = "people_nightly_cursor"
PEOPLE_MAX_SECONDS = 20 * 60


# Used only when a restaurant hasn't set its hours: without a fallback the
# intraday features would silently never run for them, which reads exactly
# like the POS not being supported.
DEFAULT_SERVICE_HOURS = (10, 23)


def _open_now(r, local):
    """True when the restaurant is inside its opening hours — including a
    close at or after midnight, which is last night's service still running
    (time_utils.is_open_at, A-1) — or inside a plain daytime window when it
    hasn't set hours for today."""
    from time_utils import is_open_at
    return is_open_at(r, local, default_hours=DEFAULT_SERVICE_HOURS)


# ── the 20-minute service slot (DH5-9) ──────────────────────────────────────
#
# Intraday capture and its five siblings each walked every restaurant
# serially, with no bound and no cursor, re-reading all 200-odd columns of
# every restaurant six times a slot. The slot now reads the list once
# (slot_restaurants) and hands it to each job; each walks it from its own
# cursor under its own wall-clock bound, so one slow POS region can no
# longer overrun the slot, and the restaurants a bound cut off lead the next
# slot (CLAUDE.md: work over every restaurant is bounded and resumable).
INTRADAY_WORKERS = 4
INTRADAY_CAPTURE_MAX_SECONDS = 10 * 60
SLOT_JOB_MAX_SECONDS = 4 * 60


def slot_restaurants(db_path=DB_PATH) -> list:
    """Every live restaurant, read once per slot for all six jobs."""
    return list(_restaurants(db_path))


def _slot_order(job, restaurants, db_path):
    rows = list(restaurants) if restaurants is not None else list(_restaurants(db_path))
    rows.sort(key=lambda r: r.id)
    cursor = _read_cursor(f"{job}_cursor", db_path)
    return [r for r in rows if r.id > cursor] + [r for r in rows if r.id <= cursor]


def _slot_iter(job, restaurants=None, db_path=DB_PATH, max_seconds=SLOT_JOB_MAX_SECONDS, state=None):
    """The slot's restaurants for one serial job, from after its cursor,
    stopping once `max_seconds` have passed (the rest lead the next slot).
    The cursor is saved when the walk ends — finished, cut off by the
    bound, or abandoned — at the last restaurant whose turn completed.
    `state`, a dict, is told `hit_bound` so the job can report it (#39)."""
    import time as _time
    order = _slot_order(job, restaurants, db_path)
    started, last = _time.monotonic(), None
    try:
        for r in order:
            if last is not None and _time.monotonic() - started > max_seconds:
                import ops
                ops.capture(RuntimeError(f"{job} stopped at its {max_seconds}s bound; the rest lead the next "
                                         "slot"), job=job, context="time_bound")
                if state is not None:
                    state["hit_bound"] = True
                break
            yield r
            last = r.id
    finally:
        if last is not None:
            _write_cursor(f"{job}_cursor", last, db_path)


def _slot_counts(st, **extra):
    """The standard counts for a slot job from its walk state: `attempted`
    and `failed` the job counted, the bound _slot_iter reported."""
    attempted, failed = int(st.get("attempted") or 0), int(st.get("failed") or 0)
    return _counts(attempted, attempted - failed, failed, hit_bound=st.get("hit_bound", False), **extra)


def _slot_sweep(job, restaurants, fn, db_path, workers=1, max_seconds=SLOT_JOB_MAX_SECONDS):
    """`fn(r)` over the slot's restaurants on a small pool
    (scheduler.bounded_map), from after the job's cursor, under
    `max_seconds`. The cursor is the end of the longest finished prefix, so
    a restaurant still in flight is never skipped. Returns hit_bound."""
    import ops
    import scheduler as _sched
    order = _slot_order(job, restaurants, db_path)
    if not order:
        return False
    lock = threading.Lock()
    finished, state = set(), {"prefix": 0}

    def _covered(r):
        with lock:
            finished.add(r.id)
            p = state["prefix"]
            while p < len(order) and order[p].id in finished:
                p += 1
            state["prefix"] = p

    def _run(r):
        fn(r)
        _covered(r)

    def _failed(r, e):
        ops.capture(e, job=job, context=f"restaurant_id={r.id}")
        _covered(r)

    _done, hit = _sched.bounded_map(order, _run, workers, max_seconds, on_error=_failed)
    if state["prefix"]:
        _write_cursor(f"{job}_cursor", order[state["prefix"] - 1].id, db_path)
    if hit:
        ops.capture(RuntimeError(f"{job} stopped at its {max_seconds}s bound; the rest lead the next slot"),
                    job=job, context="time_bound")
    return bool(hit)


def run_intraday_capture(db_path=DB_PATH, restaurants=None):
    """Snapshot net sales so far, once an hour, while the restaurant is open.
    Each snapshot is also the baseline for the same weekday in later weeks —
    no hour-level history existed to compare a running day against.

    Bounded and resumable (DH5-9): four POS calls in flight, a wall-clock
    bound, a cursor; `restaurants` is the slot's one read of the list
    (slot_restaurants). Each capture that reached the POS is recorded as
    `pos_intraday` in the Data Health ledger."""
    import intraday, ops
    from time_utils import restaurant_now
    c = {"captured": 0, "closed": 0, "attempted": 0, "failed": 0, "not_yet": 0}
    lock = threading.Lock()

    def _one(r):
        local = restaurant_now(r, naive=True)
        if not _open_now(r, local):
            with lock:
                c["closed"] += 1
            return
        if not ops.claim_period(f"intraday:{r.id}", f"{local.date().isoformat()}-{local.hour}"):
            return
        try:
            out = intraday.capture(r.id, now_local=local, db_path=db_path, restaurant=r)
        except Exception as e:
            ops.capture(e, job="intraday_capture", context=f"restaurant_id={r.id}")
            out = {"ok": False, "reason": "the POS didn't answer"}
        if out.get("not_yet"):
            # Nothing posted yet is the normal state at opening, and the
            # admin card read red every morning at 11 (10/1/26). It is a
            # failure only past NOT_YET_GRACE_HOURS after today's opening.
            if not _not_yet_overdue(r, local):
                with lock:
                    c["not_yet"] += 1
                return
            out = dict(out, reason=f"no sales posted {NOT_YET_GRACE_HOURS} hours after opening")
        answered = out.get("ok") or out.get("reason") == "the POS didn't answer" or out.get("not_yet")
        with lock:
            c["captured"] += 1 if out.get("ok") else 0
            if answered:
                c["attempted"] += 1
                c["failed"] += 0 if out.get("ok") else 1
        if answered:
            _record_intraday(r.id, bool(out.get("ok")), out.get("provider"), db_path,
                             error=None if out.get("ok") else out.get("reason"))

    hit = _slot_sweep("intraday_capture", restaurants, _one, db_path, workers=INTRADAY_WORKERS,
                      max_seconds=INTRADAY_CAPTURE_MAX_SECONDS)
    return _counts(c["attempted"], c["attempted"] - c["failed"], c["failed"], c["closed"] + c["not_yet"], hit,
                   captured=c["captured"], closed=c["closed"], not_yet=c["not_yet"])


# How long after today's opening "nothing posted yet" is still normal:
# the first checks close, and the POS's cloud copy catches up.
NOT_YET_GRACE_HOURS = 2


def _not_yet_overdue(r, local) -> bool:
    """True once it is NOT_YET_GRACE_HOURS past today's opening (the
    restaurant's own open_times; 11am when unset)."""
    try:
        import json as _j
        from schedule_rules import parse_minutes
        opens = (_j.loads(getattr(r, "open_times_json", None) or "{}") or {}).get(local.strftime("%A"))
        m = parse_minutes(opens or "") if opens else None
    except Exception:
        m = None
    open_m = m if m is not None else 11 * 60
    return local.hour * 60 + local.minute >= open_m + NOT_YET_GRACE_HOURS * 60


def _record_intraday(restaurant_id, ok, provider, db_path, error=None):
    try:
        import data_health
        data_health.record_attempt(restaurant_id, "pos_intraday", ok, provider=provider,
                                   error=None if ok else (error or "the POS didn't answer"),
                                   db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        pass


# The one interruption of the working day this product allows itself: late
# enough that the lunch numbers are in, early enough to change tonight's
# staffing, prep or a text to the guest club.
PULSE_HOUR = 16


def run_pre_dinner_pulse(db_path=DB_PATH, restaurants=None):
    """One push before dinner, and only when today is materially off a
    typical same weekday at this hour. A pulse that fires every day is a
    notification people turn off."""
    import intraday, ops, push, scheduler
    from time_utils import restaurant_now
    sent = 0
    st = {"attempted": 0, "failed": 0}
    for r in _slot_iter("pre_dinner_pulse", restaurants, db_path, state=st):
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, PULSE_HOUR, until=PULSE_HOUR + 2,
                                   claim_key="pre_dinner_pulse", now_local=local):
            continue
        st["attempted"] += 1
        try:
            p = intraday.pulse(r.id, now_local=local, db_path=db_path, restaurant=r)
            if not p.get("available") or not p.get("off"):
                continue
            # The people who already get the morning brief — owners, and
            # managers the owner put on it. Same audience, same day's numbers.
            import morning_brief
            # Push only, so only the people whose phone can take it: a pulse
            # "sent" to an audience with no deliverable device reached
            # nobody, and was recorded as shown all the same (re-audit C2).
            audience = deliverable_audience(r.id, {u["id"] for u in morning_brief.recipients(r.id, db_path)},
                                            db_path, alert_type="intraday_pulse")
            if not audience:
                continue
            word = "behind" if p["direction"] == "behind" else "ahead of"
            import notify
            if not notify.briefing_allowed(r.id, "intraday_pulse", db_path):
                continue
            # Behind by enough, with more people on for dinner than the
            # floor needs: the pulse names ONE specific move and its numbers.
            # A suggestion only — nothing is sent to anyone (#50).
            move = staffing_move(r, local, p, db_path=db_path)
            body = (f"${p['net_sales']:,.0f} by {_clock(p['hour'])} against about ${p['typical']:,.0f} "
                    f"on the last {p['samples']} {p['weekday']}s.")
            if move:
                body += " " + move["text"]
            alert_id = notify.record_notification(r.id, "intraday_pulse", db_path=db_path,
                                                  value=move["dollars"] if move else None)
            data = {"ask_prompt": f"Why is today running {word} a normal {p['weekday']}?", "alert_id": alert_id,
                    "surface": "alert_push"}
            # "X% behind a typical Friday" is news, not a recommendation.
            # The staffing move is advice, but the push is the only place it
            # is ever shown and the app offers nothing on a push but the tap —
            # so it is not presented (rec_delivery.NOT_PRESENTED_PREFIXES,
            # re-audit C8) and says so: `answerable` false, no key to open.
            # Should a client ever answer it, the hook below presents it on
            # delivery without another change here.
            import rec_delivery
            recs = []
            if move:
                ans = rec_delivery.answerable(move["key"])
                data["staffing_move"] = dict(move, answerable=ans, **({"rec_key": move["key"]} if ans else {}))
                recs.append({"key": move["key"], "module": "labor", "title": move["text"],
                             "dollar_value": move["dollars"]})
                if ans:
                    data["rec_key"] = move["key"]
                data["answerable"] = ans
            queued = push.fire_push(
                r.id, "intraday_pulse",
                f"{abs(p['pct']):.0f}% {word} a typical {p['weekday']}", body,
                data=data, db_path=db_path, user_ids=audience,
                on_delivered=rec_delivery.when_pushed(r.id, "alert_push", recs, db_path=db_path))
            if queued == 0:
                # No device took it: the row comes back out of the history
                # and the briefing budget (notify.withdraw_notification).
                notify.withdraw_notification(r.id, alert_id, db_path=db_path)
                continue
            sent += 1
        except Exception as e:
            st["failed"] += 1
            ops.capture(e, job="pre_dinner_pulse", context=f"restaurant_id={r.id}")
    return _slot_counts(st, sent=sent)


def _clock(hour, minute=0) -> str:
    """16 -> "4pm", 20:30 -> "8:30pm" — the way an owner says a time."""
    h = int(hour) % 24
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{':%02d' % minute if minute else ''}{suffix}"


# When a slow night suggests letting someone go, the time it suggests.
PULSE_CUT_HOUR = 20
PULSE_MOVE_MIN_BEHIND_PCT = 15
PULSE_MOVE_MIN_HOURS = 1.0


def _pulse_suppressed(restaurant, day, db_path=DB_PATH):
    """Why no cut is suggested today, or None: a holiday, or a day the
    owner has an event or booked covers on — the night is not "running
    behind" a typical one, and the pulse's own history cannot say what it
    needs (NS5 H6)."""
    try:
        import schedule_economics as _se
        name = _se._holiday_dates(day.year).get(day.isoformat())
        if name:
            return f"today is {name}"
    except Exception:
        pass
    try:
        import demand_signals as _ds
        sig = _ds.by_date(restaurant.id, [day.isoformat()], db_path=db_path).get(day.isoformat())
        if sig and (sig.get("labels") or sig.get("covers")):
            return "there are reservations or an event on the book today"
    except Exception:
        pass
    return None


def _cut_is_legal(restaurant, day, rows, who, cut_label, db_path=DB_PATH):
    """Whether sending `who` home at the cut leaves the day inside every
    rule it was inside before: the day is re-checked with the cut applied
    (schedule_rules.violations), and any violation the cut adds — keyholder
    cover until close, the last-of-role-after-close rule, a floor, a manager
    on duty — refuses it. The pulse named the only keyholder and the closer
    the owner's own rule keeps (NS5 H6 probes A and B)."""
    import schedule_rules as _sr
    iso = day.isoformat()
    weekday = day.strftime("%A")
    base = [dict(r, date=iso, day=weekday) for r in rows]
    try:
        c = _sr.build_constraints(restaurant.id, [iso], [weekday], restaurant=restaurant, db_path=db_path)
    except Exception:
        return False
    # Only rules about who is on the floor: a single-day sweep would read
    # "days off" and hours-per-week on a one-day week.
    def _key(v):
        return (v["kind"], (v.get("employee") or "").strip().lower(), v.get("shift_start"))

    def _sweep(rs):
        return {_key(v) for v in _sr.violations(rs, c) if v["kind"] not in ("days_off", "under_min_hours")}
    before = _sweep(base)
    after_rows = []
    for r in base:
        r = dict(r)
        if (r.get("employee") or "") == who["employee"] and r.get("shift_start") == who["shift_start"]:
            r["shift_end"] = cut_label
            s_m = _sr.parse_minutes(r.get("shift_start", "")) or 0
            r["scheduled_hours"] = f"{max(0.0, (PULSE_CUT_HOUR * 60 - s_m) / 60.0):.1f}"
        after_rows.append(r)
    new = _sweep(after_rows) - before
    # Judged by kind: a day-level rule (keyholder until close, stays after
    # close) is pinned to the day's last row, which the cut itself moves, so
    # the same breach already there before reads as a new key. A kind the
    # day did not break before is the cut's doing.
    kinds_before = {k[0] for k in before}
    return all(k[0] in kinds_before for k in new)


def staffing_move(restaurant, local, pulse, db_path=DB_PATH):
    """ONE specific staffing move for a night running behind, from the
    published week and the restaurant's own labor rates — or None.

    Only when today is at least PULSE_MOVE_MIN_BEHIND_PCT behind, not a
    holiday or a day with reservations or an event on the book, and only
    in a role with more people on at PULSE_CUT_HOUR than its dinner cut
    floor (schedule_rules.cut_floor: the owner's night floor for the role,
    else Will's admin role minimum, else the restaurant's "never cut below"
    cut_floor_default, 2 unless the owner changed it — never one person
    because nobody set a floor).
    The person suggested is that role's latest starter whose cut leaves
    the day inside every rule (_cut_is_legal) — never the only keyholder,
    never the closer a "stays after close" rule keeps. The saving is the
    hours from the cut to their scheduled end at the role's rate, and says
    it is before any predictability pay when a notice rule is set.
    {"text", "employee", "role", "cut_at", "hours", "dollars", "on",
    "floor", "key", "pay_caveat"}. Servers are considered first — the usual
    first cut on a slow floor — then whichever role has the most spare."""
    try:
        pct = float(pulse.get("pct") or 0)
    except (TypeError, ValueError):
        return None
    if pulse.get("direction") != "behind" or abs(pct) < PULSE_MOVE_MIN_BEHIND_PCT:
        return None
    import intraday
    # A cut rests on the hour's own history: under MIN_SAMPLES_FOR_CUT past
    # same-weekday readings the pulse says how the night is running and
    # stops there (CA1 L28).
    if int(pulse.get("samples") or 0) < intraday.MIN_SAMPLES_FOR_CUT:
        return None
    if _pulse_suppressed(restaurant, local.date(), db_path):
        return None
    # Tonight's other evidence first (memory audit 9/29/26): a campaign aimed
    # at filling tonight means no cut; a service complaint cluster on this
    # weekday's dinner is said beside the cut.
    try:
        import staffing_signals as _stsig
        _guard = _stsig.trim_guard(restaurant.id, local.strftime("%A"), daypart="night", on_date=local.date(),
                                   db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        _guard = {}
    if _guard.get("suppress"):
        return None
    import schedule_rules as _sr
    from models import get_role_rates
    rows = intraday.published_rows(restaurant.id, local.date(), db_path=db_path)
    if not rows:
        return None
    cut = PULSE_CUT_HOUR * 60
    weekday = local.strftime("%A")
    on_by_role = {}
    for x in rows:
        start, end = _sr.parse_minutes(x["shift_start"]), _sr.parse_minutes(x["shift_end"])
        if start is None or end is None:
            continue
        if end <= start:
            end += 24 * 60                       # closes past midnight
        if start <= cut < end:
            on_by_role.setdefault((x["role"] or "Staff").strip(), []).append({**x, "_start": start, "_end": end})
    # Tonight's floors as the draft holds them: the owner's staffing rules
    # and confirmed Studio note rules included (blind audit, 10/1/26).
    floors = _sr.effective_role_floors(restaurant, local.date())
    minimums = _sr.role_minimums(restaurant)
    choices = []
    for role, people in on_by_role.items():
        floor = _sr.cut_floor(restaurant, role, weekday, "night", floors=floors, minimums=minimums)
        spare = len(people) - floor
        if spare <= 0:
            continue
        choices.append(("server" not in role.lower(), -spare, role, people, floor))
    if not choices:
        return None
    choices.sort(key=lambda c: (c[0], c[1], c[2]))
    pick = None
    for _srv, _spare, role, people, floor in choices:
        for who in sorted(people, key=lambda x: (x["_start"], x["_end"]), reverse=True):
            if round((who["_end"] - cut) / 60.0, 1) < PULSE_MOVE_MIN_HOURS:
                continue
            if _cut_is_legal(restaurant, local.date(), rows, who, _clock(PULSE_CUT_HOUR), db_path):
                pick = (role, people, floor, who)
                break
        if pick:
            break
    if not pick:
        return None
    role, people, floor, who = pick
    hours = round((who["_end"] - cut) / 60.0, 1)
    rates = get_role_rates(restaurant.id, db_path=db_path) or {}
    rate = None
    for k, v in rates.items():
        if k != "_default" and k.strip().lower() == role.lower():
            rate = float(v)
    if rate is None:
        rate = float(rates.get("_default") or getattr(restaurant, "hourly_rate", None) or 0) or None
    dollars = round(hours * rate) if rate else None
    end_label = who["shift_end"] or "close"
    # A notice rule in force means a same-day cut may carry predictability
    # pay the saving does not net out — said, not computed (NS5 H6).
    try:
        notice = _sr.compliance(restaurant).get("notice_days")
    except Exception:
        notice = None
    pay_caveat = bool(notice)
    saving = (f"saves about {hours:g}h" + (f" (~${dollars:,.0f})" if dollars else "")
              + (" in wages, before any predictability pay a same-day change may owe under your notice rule"
                 " — check with counsel" if pay_caveat else ""))
    from shift_quality import role_words as _rw
    text = (f"{len(people)} {_rw(role, len(people))} on at {_clock(PULSE_CUT_HOUR)} "
            f"against a floor of {floor}: letting {who['employee']} (on till {end_label}) go at "
            f"{_clock(PULSE_CUT_HOUR)} {saving}.")
    import staff_settings as _ss
    if _guard.get("caution"):
        text = f"{text} {_guard['caution']}"
    return {"text": text, "employee": who["employee"], "role": role, "cut_at": _clock(PULSE_CUT_HOUR),
            "hours": hours, "dollars": dollars, "on": len(people), "floor": floor, "pay_caveat": pay_caveat,
            "key": f"pulse_cut:{local.date().isoformat()}:{_ss.name_key(who['employee'])}"}


def run_coverage_check(db_path=DB_PATH, restaurants=None):
    """A scheduled person who hasn't clocked in becomes the routed manager's
    issue — the one staffing problem that is still fixable while it matters.

    Everything here runs on the restaurant's BUSINESS date — the issue key,
    the arrivals it closes, the night's coverage marker, the covers it
    suggests and the week their legality is judged on (schedule audit
    10/3/26 E-4). It used the calendar date: at 12:10am on a 2am close a
    10pm starter missing since 10:15pm got a second issue keyed to
    Saturday, and their arrival never closed Friday's, which the nightly
    attendance read then recorded as a no-show.

    One issue per business date and role family (_raise_coverage, E-31): a
    mass call-off is one text — "3 of 6 servers haven't clocked in" — not a
    burst, each gap gets its own covers instead of the same two names for
    every gap, and someone already on whose shift ends as the gap begins
    (the lunch server who could stay) is suggested before anyone off."""
    import intraday, issues, ops
    from time_utils import restaurant_now, business_date
    opened = 0
    st = {"attempted": 0, "failed": 0}
    for r in _slot_iter("coverage_check", restaurants, db_path, state=st):
        if not getattr(r, "module_labor", 0):
            continue
        local = restaurant_now(r, naive=True)
        # Every pass, open or not: a gap whose shift has ended stops asking
        # for a cover, and an earlier day's issue closes as a miss (it stayed
        # on Home as "hasn't clocked in" until someone resolved it by hand).
        try:
            issues.close_ended_coverage(r.id, business_date(r, local).isoformat(), local, db_path=db_path)
        except Exception as ce:
            ops.capture(ce, job="coverage_check", context=f"restaurant_id={r.id} ended shifts")
        if not _open_now(r, local):
            continue
        if "manager" not in issues.get_routing(r.id, db_path):
            continue
        st["attempted"] += 1
        try:
            gaps = intraday.coverage_gaps(r.id, now_local=local, db_path=db_path, restaurant=r)
            # The service the clock is in — the date coverage_gaps read the
            # published week and the clock-ins for (E-4).
            bday = str(gaps.get("business_date") or business_date(r, local).isoformat())[:10]
            # Someone who turned up late closes their own no-show issue on
            # this pass — the manager is not left chasing a person already
            # on the floor (#13 / #18).
            if gaps.get("available"):
                issues.resolve_coverage(r.id, bday, set(gaps.get("arrived_keys") or []), db_path=db_path)
                # The nightly report states "0 no-shows" only for a night
                # this check really read the clock-ins (dsr D1-17).
                try:
                    from dsr import store as _dsr_store
                    _dsr_store.mark_coverage_ran(r.id, bday, db_path=db_path)
                except Exception as me:
                    ops.capture(me, job="coverage_check", context=f"restaurant_id={r.id} dsr marker")
            import staff_comms
            due = []
            for m in (gaps.get("missing") or []):
                # They said they're running late (staff app, COM-05): no
                # "hasn't clocked in" until their start + ETA + the grace;
                # after that the issue opens and says what they told us.
                hold = None
                try:
                    hold = staff_comms.late_hold(r.id, m["employee"], m.get("shift_start"), local,
                                                 restaurant=r, db_path=db_path)
                except Exception as he:
                    ops.capture(he, job="coverage_check", context=f"restaurant_id={r.id} running-late hold")
                if hold and hold["holding"]:
                    continue
                due.append(dict(m, hold=staff_comms.hold_sentence(hold)))
            if due:
                opened += _raise_coverage(r, bday, due, gaps, local, db_path)
        except Exception as e:
            st["failed"] += 1
            ops.capture(e, job="coverage_check", context=f"restaurant_id={r.id}")
    return _slot_counts(st, opened=opened)


# Covers suggested for each gap on a coverage issue (the issue page offers
# "Ask Ana to cover" for each).
COVERS_PER_GAP = 2


def _raise_coverage(r, bday, due, gaps, local, db_path=DB_PATH) -> int:
    """The coverage check's missing people onto the business date's issues,
    one per role family (schedule audit 10/3/26 E-31): somebody not on any
    issue yet joins their role's open issue — the manager is texted its new
    title once — or opens one ("#2" when a manager already closed the
    first). Somebody already on an issue for that date, open or closed, is
    never raised twice. Returns how many issues it opened.

    The suggested covers travel with the issue (meta.covers), so its page
    can offer "Ask Ana to cover" as one tap (intraday.ask_to_cover). They are
    NOT presented here: the text names the issue, not the covers, and a
    text Twilio refused still recorded "cover" on issue_sms (re-audit C3) —
    they are presented where they are rendered: Home's open-issues list
    (GET /issues, strategy_routes) and the issue page (/i/<token>)."""
    import issues
    import schedule_rules as _sr
    import staff_settings as _ss
    from shift_quality import role_family
    from datetime import date as _date
    day = _date.fromisoformat(bday)
    c = _coverage_constraints(r, day, due, db_path)
    families = getattr(c, "role_families", None) or None
    on_today = gaps.get("scheduled_rows") or []

    def _gap_id(p):
        return _ss.name_key(p.get("employee")), _sr.parse_minutes(p.get("shift_start") or "")

    raised, newest = set(), {}
    known = issues.coverage_issues_for(r.id, bday, db_path=db_path)
    if local.date().isoformat() != bday:
        # A person-keyed issue from before this check keyed by business date
        # carries the calendar date it was opened on.
        known += [i for i in issues.coverage_issues_for(r.id, local.date().isoformat(), db_path=db_path)
                  if not issues.is_group_coverage(i.get("source_key"))]
    for iss in known:
        raised |= {_gap_id(p) for p in issues.coverage_people(iss)}
        if issues.is_group_coverage(iss.get("source_key")):
            newest[(iss.get("meta") or {}).get("family") or ""] = iss         # oldest first: the last one wins
    groups = {}
    for m in sorted(due, key=lambda x: (_sr.parse_minutes(x.get("shift_start") or "") or 0, x["employee"])):
        if _gap_id(m) not in raised:
            groups.setdefault(role_family(m.get("role") or "", families) or "staff", []).append(m)
    # Nobody missing and nobody let off today is asked to cover; everyone
    # else on today only to stay on as their own shift ends (stay_on).
    away = [m["employee"] for m in due] + [x.get("employee") for x in (gaps.get("released") or [])]
    stamp = local.strftime("%Y-%m-%d %H:%M")
    opened = 0
    for fam, people in groups.items():
        iss = newest.get(fam)
        live = iss is not None and iss.get("status") != "resolved"
        # A name the open issue already suggests is not suggested again for
        # a second gap: each gap gets its own (E-31).
        taken = [x.get("name") for x in ((iss or {}).get("meta") or {}).get("covers") or []] if live else []
        covers = _gap_covers(r, day, people, on_today, away, taken, c, db_path)
        entries = [{"employee": m["employee"], "role": m.get("role"), "shift_start": m.get("shift_start"),
                    "shift_end": m.get("shift_end"), "minutes_late": m.get("minutes_late"), "status": "missing",
                    "hold": m.get("hold") or "", "asked_off": bool(m.get("asked_off")),
                    "notice_minutes": m.get("notice_minutes"), "seen_at": stamp} for m in people]
        in_role = len({_ss.name_key(x.get("employee")) for x in on_today
                       if (role_family(x.get("role") or "", families) or "staff") == fam})
        if live:
            def _merge(meta, entries=entries, covers=covers, in_role=in_role):
                have = {_gap_id(p) for p in meta.get("people") or [] if isinstance(p, dict)}
                add = [e for e in entries if _gap_id(e) not in have]
                if not add:
                    return False
                meta.setdefault("people", []).extend(add)
                meta.setdefault("covers", []).extend(covers)
                meta["scheduled_in_role"] = in_role
                return True
            issues.update_coverage(r.id, iss["id"], _merge, renotify=True, db_path=db_path)
            continue
        seq = 1
        if iss is not None:
            tail = str(iss.get("source_key") or "").rsplit("#", 1)
            seq = (int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else 1) + 1
        meta = {"business_date": bday, "role": fam, "family": fam, "people": entries, "covers": covers,
                "scheduled_in_role": in_role,
                # The first person, as a one-person issue always carried them.
                "missing": entries[0]["employee"], "shift_start": entries[0]["shift_start"]}
        title, detail = issues.coverage_texts(meta)
        _issue, token = issues.create_issue(r.id, "coverage", title, detail=detail, severity="high",
                                            source_key=issues.coverage_key(bday, fam, seq), meta=meta,
                                            db_path=db_path)
        if token:
            opened += 1
    return opened


def _coverage_constraints(r, day, due, db_path=DB_PATH):
    """The rules of the published week holding `day`, built once for every
    cover's legality check on this pass (each check built its own), or None
    when no published row can be found to judge by."""
    import labor_replacements
    import schedule_rules as _sr
    from datetime import datetime as _dt
    for m in due:
        rows, _idx = labor_replacements.gap_week(r.id, day, {"employee": m.get("listed_as") or m["employee"],
                                                            "shift_start": m.get("shift_start")}, db_path)
        if rows:
            dates = sorted({x.get("date") for x in rows if x.get("date")})
            try:
                return _sr.build_constraints(r.id, dates, [_dt.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates],
                                             restaurant=r)
            except Exception as e:
                import ops
                ops.capture(e, job="coverage_replacements", context=f"restaurant_id={r.id} rules")
                return None
    return None


def _gap_covers(r, day, people, on_today, away, taken, c, db_path=DB_PATH) -> list:
    """Covers for each gap, earliest gap first: someone on today whose shift
    ends as it begins (labor_replacements.stay_on), else someone off today
    (for_gap) — never a name already suggested for another gap (`taken`, the
    open issue's own) or anyone in `away` (missing, or let off today). Dealt
    a round at a time — every gap its first cover before any gap its second
    — so the first gap cannot take every good name. Each names the gap it is
    for: [{"name", "score", "kind", "how", "for", "shift_start"}]."""
    import labor_replacements
    import ops
    used = [n for n in taken if n]
    on_names = [x.get("employee") for x in on_today if x.get("employee")]
    gaps = []
    for m in people:
        listed = m.get("listed_as") or m["employee"]
        try:
            rows, idx = labor_replacements.gap_week(r.id, day, {"employee": listed,
                                                               "shift_start": m.get("shift_start")}, db_path)
        except Exception as fe:
            ops.capture(fe, job="coverage_replacements", context=f"restaurant_id={r.id} week")
            rows, idx = None, None
        gaps.append((m, listed, rows, idx))
    out = []
    for _round in range(COVERS_PER_GAP):
        for m, listed, rows, idx in gaps:
            pick = []
            try:
                pick = labor_replacements.stay_on(
                    r.id, {"employee": listed, "role": m.get("role"), "shift_start": m.get("shift_start"),
                           "date": day.isoformat()},
                    on_today, exclude=used + list(away), db_path=db_path, limit=1, constraints=c, rows=rows,
                    index=idx)
            except Exception as fe:
                ops.capture(fe, job="coverage_replacements", context=f"restaurant_id={r.id} stay on")
            if not pick:
                try:
                    pick = [dict(f, kind="off") for f in labor_replacements.for_gap(
                        r.id, m.get("role"), day.strftime("%A"), exclude=set(on_names) | set(away) | set(used),
                        db_path=db_path, limit=1, on_date=day.isoformat(),
                        shift={"employee": listed, "shift_start": m.get("shift_start")}, constraints=c) or []]
                except Exception as fe:
                    ops.capture(fe, job="coverage_replacements", context=f"restaurant_id={r.id}")
                    pick = []
            for f in pick[:1]:
                if f["name"] in used:
                    continue                # never one name for two gaps, whatever a picker returns
                used.append(f["name"])
                out.append({"name": f["name"], "score": f.get("score"), "kind": f.get("kind") or "off",
                            "how": f.get("how"), "for": m["employee"], "shift_start": m.get("shift_start")})
    return out


def _metric_permissions(metric):
    """The permission a login needs to be told about a result on `metric`:
    a food-cost win is Food Cost's, comps and voids are LOSS_VIEW. None
    means nothing beyond the brief audience itself (sales)."""
    import metrics as _metrics
    import permissions as _p
    base = str(metric or "").split(":", 1)[0]
    # The one metric rule first (metrics.metric_permission): an item-waste
    # result is Food Cost's too (memory re-audit PEOPLE-2).
    fam = _metrics.metric_permission(base)
    if fam == "food":
        return {_p.FOOD_COST_VIEW}
    if fam == "loss":
        return {_p.LOSS_VIEW}
    need = {"labor_pct": _p.LABOR_VIEW, "overtime_hours": _p.LABOR_VIEW,
            "food_cost_pct": _p.FOOD_COST_VIEW, "weekly_waste": _p.FOOD_COST_VIEW,
            "avg_rating": _p.REVIEWS_VIEW, "complaints": _p.REVIEWS_VIEW, "response_hours": _p.REVIEWS_VIEW,
            "comp_rate": _p.LOSS_VIEW, "void_rate": _p.LOSS_VIEW}.get(base)
    return {need} if need else None


def _notification_ref(data) -> dict:
    """What the history row is about, from the push payload: the queued send
    an Undo stops (delayed_action_id) or the staff request Approve / Deny
    answers (request_id + request_kind). The payload always carried these to
    the phone; the alert_log row now keeps them too, so the web bell can act
    on the row (web desk finding 4). Empty when the payload names neither."""
    data = data or {}
    try:
        if data.get("delayed_action_id"):
            return {"ref_kind": "delayed_action", "ref_id": int(data["delayed_action_id"])}
        if data.get("request_id"):
            kind = "time_off" if "time" in str(data.get("request_kind") or "").lower() else "shift"
            return {"ref_kind": kind, "ref_id": int(data["request_id"])}
    except (TypeError, ValueError):
        pass
    return {}


def _reach(restaurant_id, alert_type, title, body, data, db_path, subject=None,
           lines=None, email_type=None, rec=None, permissions=None, deciders=False):
    """Push to the people who have the app, email the ones who don't.

    morning_brief.deliver established this — push OR email, never both,
    because a brief that arrives twice is one people learn to ignore. The
    closing summary and the outcome win were written push-only, so an owner
    without the app simply never learned their change had paid off. Same
    audience either way (morning_brief.recipients).

    `deciders`: the notice waits on someone's decision (a staff request),
    so it goes to every console login holding `permissions`, whether or not
    their brief is on — a GM who turned the brief off never heard "Dana
    asked to drop Friday" (F2-6).

    Returns the number of people reached.
    """
    import morning_brief, notify, push
    if not notify.briefing_allowed(restaurant_id, alert_type, db_path):
        return 0
    # A paused or lapsed account asked for quiet; the jobs' own iteration
    # filters most of these out, but a nudge fired directly (while-away,
    # connection lost) has to check for itself.
    from models import get_restaurant as _gr
    _r = _gr(restaurant_id, db_path=db_path)
    if _r and (_r.billing_status or "trial").lower() in ("paused", "churned", "cancelled", "canceled"):
        return 0
    people = morning_brief.recipients(restaurant_id, db_path, include_opted_out=bool(deciders))
    # Only the people allowed to read what this is about — the rule
    # notify.alert_audience applies to alerts. The brief's audience includes
    # managers who cannot open Food Cost, and they were pushed food-cost
    # wins and supplier-order notices (re-audit A-15). `permissions` names
    # it when the type alone doesn't (a win is about its metric).
    need = permissions if permissions is not None else notify.alert_permissions([alert_type])
    if need:
        from permissions import has_permission
        people = [u for u in people if all(has_permission(u, p) for p in need)]
    if not people:
        return 0
    devices = {}
    for token in (push.get_device_tokens(restaurant_id, db_path, for_delivery=True) or []):
        devices.setdefault(int(token.get("user_id") or 0), []).append(token)

    import rec_delivery
    if rec and not rec_delivery.presentable(rec.get("key")):
        rec = None
    if rec and rec.get("key") and not notify.never_silenced(alert_type):
        # Not to a login whose own "not for us" already answers it
        # (rec_ledger.own_silences — memory re-audit 9/29/26, PEOPLE-4).
        import rec_ledger
        people = [u for u in people
                  if rec["key"] not in rec_ledger.own_silences(restaurant_id, u, db_path=db_path)]
        if not people:
            return 0
    # Who it can actually reach, decided BEFORE the row is written (the row is
    # a budgeted briefing): a phone whose login lets this type through
    # (deliverable_audience with the type — a login who muted it is not
    # reached, and not emailed instead: they have the app and said no), and
    # by email the people with no phone. Nobody: no row, no slot spent.
    pushed = deliverable_audience(restaurant_id, {u["id"] for u in people if devices.get(u["id"])},
                                  db_path, alert_type=alert_type)
    emailable = [u for u in people if not devices.get(u["id"]) and u.get("email")]
    if not pushed and not emailable:
        return 0
    alert_id = notify.record_notification(restaurant_id, alert_type, db_path=db_path,
                                          **_notification_ref(data))
    queued = 0
    if pushed:
        # The history row's id rides the payload so the open can name it
        # (#39); the recommendation key too, when this is one, with the
        # ledger surface a tap is an open on and whether the app may offer
        # Done / Not for us on it.
        data = dict(data or {}, alert_id=alert_id, surface="alert_push",
                    **({"rec_key": rec["key"], "answerable": rec_delivery.answerable(rec["key"])} if rec else {}))
        # Inside the owner's quiet hours it still arrives, but silently —
        # no sound, no banner break (push.py "quiet"). A catch-up pass after
        # an outage used to fire these with sound late at night (A-10).
        try:
            from models import is_in_quiet_hours
            if is_in_quiet_hours(restaurant_id, db_path=db_path):
                data["quiet"] = True
        except Exception as qe:
            print(f"[strategy_jobs] quiet-hours check failed rid={restaurant_id}: {qe}")
        # Presented on alert_push once a phone took it, never on the
        # queueing (re-audit C2/C4).
        queued = push.fire_push(restaurant_id, alert_type, title, body, data=data,
                                db_path=db_path, user_ids=pushed,
                                on_delivered=(rec_delivery.when_pushed(restaurant_id, "alert_push", [rec],
                                                                       db_path=db_path)
                                              if rec else None))
    # fire_push returns how many devices it queued for: 0 is nobody.
    reached = 0 if pushed and queued == 0 else len(pushed)

    emailed = emailable
    if emailed:
        import html as _h
        import emails as _emails
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id)
        name = (restaurant.location_name or restaurant.name) if restaurant else "your restaurant"
        # With a recommendation behind it the email links to the dashboard
        # naming it (rec=, src=alert_email), so an open is recorded (#32).
        # The name and the title are text, not markup: "Rosa & Sons
        # <Trattoria>" opened a tag the email never closed (re-audit C12).
        body_html = _emails.report_shell(
            kicker=_h.escape(name),
            title=_h.escape(title),
            subtitle="",
            sections=[_emails.report_paragraph(_h.escape(line))
                      for line in (lines or [body])],
            **({"cta_label": "Open your dashboard →",
                "cta_url": _h.escape(rec_delivery.dashboard_url(rec["key"], "alert_email",
                                                                rid=restaurant_id))} if rec else {}),
        )
        for user in emailed:
            result = _emails.deliver(
                email_type=email_type or alert_type, restaurant_id=restaurant_id, payload={
                    "from": _emails.sender("client"),
                    "to": [user["email"]],
                    "subject": f"{subject or title} — {name}",
                    "preheader": body[:120],
                    "html": body_html,
                })
            if getattr(result, "ok", False):
                reached += 1
    if rec and reached > (0 if queued == 0 else len(pushed)):
        rec_delivery.present_now(restaurant_id, "alert_email", [rec], db_path=db_path)
    if not reached:
        # Written for a push no device took and no email that went: it comes
        # back out of the history and the budget (notify.withdraw_notification).
        notify.withdraw_notification(restaurant_id, alert_id, db_path=db_path)
    return reached


def deliverable_audience(restaurant_id, user_ids, db_path=DB_PATH, alert_type=None) -> set:
    """The logins in `user_ids` with a phone a push can reach right now
    (push.get_device_tokens for delivery: active logins, unparked tokens) —
    and, given `alert_type`, whose own choices let it through
    (preferences.push_allowed: push off, this type muted, their own quiet
    hours — the test fire_push applies per device). A push-only job sends to
    this set, and writes no budgeted briefing row when it is empty: a push
    every recipient muted reached nobody and must not spend a slot."""
    import push
    ids = {int(u) for u in (user_ids or ()) if u}
    if not ids:
        return set()
    try:
        out = {int(t.get("user_id") or 0) for t in (push.get_device_tokens(restaurant_id, db_path, for_delivery=True)
                                                   or [])} & ids
    except Exception as e:
        print(f"[strategy_jobs] device lookup failed rid={restaurant_id}: {e}")
        return set()
    if alert_type and out:
        import preferences
        cache = {}
        out = {u for u in out if preferences.push_allowed(u, restaurant_id, alert_type, db_path=db_path, _cache=cache)}
    return out


def _close_hour(r, local):
    """The hour this restaurant is done for the night, rounded up past any
    half hour, or 22 when it hasn't set hours — for the business date
    `local` belongs to. A close past midnight is 24+ (1:00am -> 25): it is
    the same night's close, not the next calendar day's (A-1 / A-11)."""
    from datetime import datetime as _dt
    from time_utils import business_date
    day = business_date(r, local)
    since = _closing_send_at(r, day) - _dt.combine(day, _dt.min.time())
    return int(since.total_seconds() // 3600)


def _closing_send_at(r, day):
    """When the closing summary for the service on business date `day` is
    due: its close, rounded up to the next hour past any half hour, or
    22:00 when that weekday has no close set. A close at or after midnight
    is the next calendar morning — still `day`'s service."""
    from datetime import datetime as _dt, time as _time, timedelta as _td
    from time_utils import opening_hours, service_window
    hours = opening_hours(r, day.strftime("%A"))
    if not hours or not hours[1]:
        return _dt.combine(day, _time(DEFAULT_SERVICE_HOURS[1] - 1, 0))
    closes = service_window(r, day)[1]
    if closes.minute:
        closes = closes.replace(minute=0) + _td(hours=1)
    return closes


def _as_of_label(summary, r, day):
    """"9pm" when the night's last reading was taken before close (a POS
    that couldn't be read at close), else None — so a partial figure is
    never presented as the night's total (A-20)."""
    hour = summary.get("hour")
    if hour is None:
        return None
    from datetime import datetime as _dt, timedelta as _td
    close_at = _closing_send_at(r, day)
    taken = _dt.combine(day, _dt.min.time()) + _td(hours=int(hour))
    return _clock(int(hour) % 24) if taken + _td(hours=1) <= close_at else None


# The closing summary goes out in this many hours after close, or not at all
# (a summary at breakfast is the morning brief's job).
CLOSING_SUMMARY_WINDOW_HOURS = 2


def _closing_due_day(r, local):
    """The business date whose closing summary is due at `local`, or None.
    The candidate services are yesterday's (a late close lands after
    midnight) and today's. Owned by the business date, not the calendar
    date: a Friday that closes at 1:00am is summarised at 1am Saturday as
    FRIDAY, and Thursday's 10pm close is not re-sent then (A-11)."""
    from datetime import timedelta as _td
    for day in (local.date() - _td(days=1), local.date()):
        send_at = _closing_send_at(r, day)
        if send_at <= local < send_at + _td(hours=CLOSING_SUMMARY_WINDOW_HOURS):
            return day
    return None


def run_closing_summary(db_path=DB_PATH, restaurants=None):
    """How tonight went, sent once the doors are shut.

    The morning brief tells an owner how YESTERDAY went. Nothing told them
    how TODAY went, while they can still picture the room — which is the one
    moment the number means something specific instead of being a figure in
    a table. Pairs it with the close-out handoff, so whatever the closing
    manager wrote reaches the owner the same night rather than at 7am.

    P4: passive, no sound, no Focus break. It is a summary, not an alert.
    """
    import closeout, intraday, ops
    from models import is_in_quiet_hours
    from time_utils import restaurant_now
    sent = 0
    st = {"attempted": 0, "failed": 0}
    from dsr.deliver import replaces_closing_summary
    for r in _slot_iter("closing_summary", restaurants, db_path, state=st):
        if not getattr(r, "morning_brief_enabled", 1):
            continue
        local = restaurant_now(r, naive=True)
        day = _closing_due_day(r, local)
        if day is None:
            continue
        # The nightly DSR says how tonight went for every restaurant it runs
        # for (DSR on, POS connected) — one notification, not two
        # (DSR_ENGINE_PLAN.md §1). Every other restaurant keeps this one.
        if replaces_closing_summary(r):
            continue
        if not ops.claim_period(f"closing_summary:{r.id}", day.isoformat()):
            continue
        # An owner who asked not to be disturbed at night meant this too.
        # It is in tomorrow's brief either way.
        if is_in_quiet_hours(r.id, db_path=db_path):
            continue
        st["attempted"] += 1
        try:
            # The hourly captures stop at close, so the last one was taken
            # up to an hour before it: one more reading now is the night's
            # real total (A-20). A POS that can't be read during service
            # just keeps what it has.
            try:
                intraday.capture(r.id, now_local=local, db_path=db_path, restaurant=r, business_day=day)
            except Exception as ce:
                ops.capture(ce, job="closing_capture", context=f"restaurant_id={r.id}")
            summary = intraday.closing_summary(r.id, day=day, db_path=db_path, restaurant=r)
            summary["as_of"] = _as_of_label(summary, r, day)
            note = closeout.get(r.id, day, db_path=db_path)
            title, body = _closing_text(summary, note)
            if not title:
                continue
            if _reach(r.id, "closing_summary", title, body,
                      {"ask_prompt": f"How did {day.strftime('%A')} actually go?"},
                      db_path, subject="How tonight went"):
                sent += 1
        except Exception as e:
            st["failed"] += 1
            ops.capture(e, job="closing_summary", context=f"restaurant_id={r.id}")
    return _slot_counts(st, sent=sent)


def _closing_text(summary, note):
    """(title, body), or (None, None) when there is nothing worth sending.

    Nothing worth sending is a real outcome: a night with no POS reading and
    no close-out is a night Cavnar has nothing to say about, and saying it
    anyway is how a summary becomes something people turn off.
    """
    lines = []
    as_of = summary.get("as_of")
    if as_of and summary.get("net_sales") is not None:
        # The last reading predates close: a part of the night, said as
        # such, and never compared with other nights' full totals (A-20).
        title = f"${summary['net_sales']:,.0f} by {as_of}"
        lines.append(f"The POS wasn't read at close, so this is the figure as of {as_of}.")
    elif summary.get("available"):
        pct = summary.get("pct") or 0
        word = "behind" if summary["direction"] == "behind" else "ahead of"
        if abs(pct) < 5:
            title = f"${summary['net_sales']:,.0f} — about a normal {summary['weekday']}"
        else:
            title = (f"${summary['net_sales']:,.0f} — {abs(pct):.0f}% {word} "
                     f"a typical {summary['weekday']}")
        lines.append(f"Against about ${summary['typical']:,.0f} on the last "
                     f"{summary['samples']} {summary['weekday']}s.")
    elif summary.get("net_sales") is not None:
        title = f"${summary['net_sales']:,.0f} tonight"
        lines.append(summary.get("reason") or "")
    else:
        title = None
    if note:
        for field, label in (("went_wrong", "Went wrong"), ("eighty_sixed", "86'd"),
                             ("callouts", "Call-outs")):
            value = (note.get(field) or "").strip()
            if value:
                lines.append(f"{label}: {value}")
        if title is None and any((note.get(f) or "").strip() for f in
                                 ("went_well", "went_wrong", "eighty_sixed", "callouts")):
            title = "Tonight's handover is in"
    if title is None:
        return None, None
    return title, " ".join(l for l in lines if l)[:300] or "Open Cavnar AI for the detail."


# Once a week at most. A "quiet night coming up" that arrives every day is
# a calendar, not an opportunity.
DEMAND_OPPORTUNITY_HOUR = 10


def run_demand_opportunity(db_path=DB_PATH, restaurants=None):
    """A quiet night two days out, while there is still time to fill it.

    Marketing and demand were the one area of the product that produced no
    notification at all — an owner had to go and look, and the whole point
    of a slow Tuesday is that it is knowable in advance. P5: informational,
    never buzzes, and folded into nothing because it is a genuine one-a-week
    thing rather than part of the morning battery.
    """
    import demand, ops, push
    from time_utils import restaurant_now
    sent = 0
    st = {"attempted": 0, "failed": 0}
    for r in _slot_iter("demand_opportunity", restaurants, db_path, state=st):
        if not getattr(r, "module_marketing", 0):
            continue
        try:
            expire_quiet_night_drafts(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="quiet_night_expire", context=f"restaurant_id={r.id}")
        local = restaurant_now(r, naive=True)
        if not (DEMAND_OPPORTUNITY_HOUR <= local.hour < DEMAND_OPPORTUNITY_HOUR + 4):
            continue
        week = local.strftime("%G-W%V")
        if ops.period_claimed(f"demand_opportunity:{r.id}", week):
            continue
        st["attempted"] += 1
        try:
            # The date is checked BEFORE the week is claimed. The job looks
            # two days out, so Monday's run asks about Wednesday; claiming
            # first meant a Monday with no quiet Wednesday spent the week,
            # and the quiet Thursday the Tuesday run would have found was
            # never told (#32). Now only a run that has something to say
            # claims the week.
            out = demand.quiet_night_ahead(r.id, today=local.date(), db_path=db_path)
            if not out.get("available"):
                continue
            # "Not for us" to the same advice on any surface (T2, B4 H6):
            # Home's slow_day:Wednesday declined is this push's Wednesday.
            # Checked before the week is claimed — a declined night spends
            # nothing.
            qn_key = f"quiet_night:{out.get('date') or out['weekday']}"
            qn_title = f"{out['weekday']} is usually your quietest night"
            if quiet_night_declined(r.id, qn_key, qn_title, db_path=db_path):
                continue
            import morning_brief, notify
            # Push only: only the people whose phone can take it, checked
            # before the week is claimed — a week "sent" to nobody's phone
            # was spent and recorded as shown (re-audit C2).
            audience = deliverable_audience(r.id, {u["id"] for u in morning_brief.recipients(r.id, db_path)},
                                            db_path, alert_type="demand_opportunity")
            if not audience:
                continue
            # Claimed on the ISO WEEK, not the date — once a week at most.
            if not ops.claim_period(f"demand_opportunity:{r.id}", week):
                continue
            if not notify.briefing_allowed(r.id, "demand_opportunity", db_path):
                continue
            # Two weeks running of drafts nobody approved is an answer:
            # keep the heads-up, stop spending model calls on copy that goes
            # unused. Read BEFORE this week's notification is recorded —
            # this week has no drafts yet and would read as "not an answer".
            ignored = quiet_night_ignored_weeks(r.id, db_path=db_path)
            alert_id = notify.record_notification(r.id, "demand_opportunity", db_path=db_path,
                                                  value=float(out["typical_sales"]))
            # The fill: a post for that night, saved as a draft behind the
            # same approval as any other, and the guest text drafted where it
            # can be sent — Marketing's card for the night opens the Campaign
            # Studio (text channel, audience, target day). The push used to
            # say "a post and a guest text are drafted — approve them", and
            # approving the text sent nothing (AUX-5 / #38). Drafting can
            # fail (budget, model) without costing the owner the heads-up.
            drafted = {} if ignored >= QUIET_NIGHT_IGNORED_LIMIT else _draft_quiet_night_fill(r, out, db_path)
            body = (f"About ${out['typical_sales']:,.0f}, {out['below_average_pct']:.0f}% under a "
                    f"typical day across {out['samples']} of them. ")
            # A post sent to Message Batches (#62) is in the drafts within
            # the hour, not yet: said so.
            body += (f"A post is drafted, and Marketing's Fill {out['weekday']} card writes the guest text."
                     if drafted.get("post_draft_id") else
                     f"A post is being drafted for your approval, and Marketing's Fill {out['weekday']} card "
                     f"writes the guest text."
                     if drafted.get("post_draft_queued") else
                     f"Two days to do something about it: Marketing's Fill {out['weekday']} card writes the text.")
            # Its measured confidence and the date the sales run through,
            # on the push itself (T1).
            conf = quiet_night_confidence(r.id, qn_key, out, db_path=db_path)
            import rec_trust
            _label = rec_trust.outbound_label(conf)
            if _label:
                body += f" {_label}."
            rec = {"key": qn_key, "module": "marketing", "title": qn_title,
                   "model_written": bool(drafted), "confidence": conf}
            # The heads-up is shown on the push and nowhere else, and nothing
            # answers it there — so it is not presented (rec_delivery.
            # NOT_PRESENTED_PREFIXES, re-audit C8) and says so. The hook
            # presents it on delivery should a client ever answer it.
            import rec_delivery
            ans = rec_delivery.answerable(rec["key"])
            queued = push.fire_push(
                r.id, "demand_opportunity",
                f"{out['weekday']} is usually your quietest night", body,
                # It opens Marketing, where the post and the night's card are
                # (it used to open Ask on "What could fill …?").
                data={"nav": "marketing", **{k: v for k, v in drafted.items() if k == "post_draft_id"},
                      "alert_id": alert_id, "surface": "alert_push", "answerable": ans,
                      **({"rec_key": rec["key"]} if ans else {})},
                db_path=db_path, user_ids=audience,
                on_delivered=rec_delivery.when_pushed(r.id, "alert_push", [rec], db_path=db_path))
            if queued == 0:
                notify.withdraw_notification(r.id, alert_id, db_path=db_path)
                continue
            sent += 1
        except Exception as e:
            st["failed"] += 1
            ops.capture(e, job="demand_opportunity", context=f"restaurant_id={r.id}")
    return _slot_counts(st, sent=sent)


def quiet_night_declined(restaurant_id, key, title, db_path=DB_PATH) -> bool:
    """True when the owner said "not for us" to the same advice (its
    insight_store.advice_signature) on any surface. Never raises (False)."""
    try:
        import insight_store
        sig = insight_store.advice_signature(key, title)
        return bool(sig) and sig in insight_store.declined_signatures(restaurant_id, db_path=db_path)
    except Exception as e:
        print(f"[quiet_night] declined signatures unavailable rid={restaurant_id}: {e}")
        return False


def quiet_night_confidence(restaurant_id, key, out, db_path=DB_PATH) -> dict:
    """The quiet-night heads-up's K1 confidence: the weekday's average is
    over `samples` of that weekday (N_FULL "weekdays"), the sales' freshness,
    this restaurant's record of the kind. Never raises."""
    try:
        import rec_trust
        n = out.get("samples")
        import data_freshness
        # The demand sources (sales, the POS, the weather — #28, DH3-5): a
        # failing POS lowers the heads-up; "sales" alone never carried its error.
        return rec_trust.assess(restaurant_id, key, sources=data_freshness.sources_for(["demand"]),
                                db_path=db_path, evidence={
            "n": n, "kind": "weekdays",
            "basis": f"{n} past {out.get('weekday')}s against a typical day" if n is not None else "the weekday's history"})
    except Exception as e:
        print(f"[quiet_night] confidence unavailable rid={restaurant_id}: {e}")
        import confidence_engine
        return confidence_engine.unknown()


# Drafted-and-ignored weeks in a row after which the fill is no longer drafted.
QUIET_NIGHT_IGNORED_LIMIT = 2
# The topic suffixes _draft_quiet_night_fill writes — how its drafts are known.
# It no longer drafts the guest text (AUX-5); _QN_SMS_SUFFIX stays so the
# guest-text drafts written before still expire and still count as a week's
# drafts (expire_quiet_night_drafts, quiet_night_ignored_weeks).
_QN_POST_SUFFIX = " night — a reason to come in this week"
_QN_SMS_SUFFIX = " night guest text"
# A quiet-night draft is about a night two days after it was written; a day
# past that it can only be wrong.
QUIET_NIGHT_DRAFT_DAYS = 3


def _quiet_night_drafts(conn, restaurant_id):
    return conn.execute(
        "SELECT id, status, created_at, approved_at FROM marketing_drafts WHERE restaurant_id=? "
        "AND (topic LIKE ? OR topic LIKE ?) ORDER BY created_at DESC, id DESC LIMIT 60",
        (restaurant_id, "%" + _QN_POST_SUFFIX, "%" + _QN_SMS_SUFFIX)).fetchall()


def quiet_night_ignored_weeks(restaurant_id, db_path=DB_PATH) -> int:
    """How many of the most recent quiet-night weeks in a row had drafts and
    none of them approved. A week whose drafts were deleted unapproved
    counts as ignored too (its demand_opportunity notification says a fill
    was drafted that week)."""
    conn = get_conn(db_path)
    try:
        drafts = _quiet_night_drafts(conn, restaurant_id)
        pushes = conn.execute(
            "SELECT fired_at FROM alert_log WHERE restaurant_id=? AND alert_type='demand_opportunity' "
            "ORDER BY id DESC LIMIT 6", (restaurant_id,)).fetchall()
    except Exception:
        return 0
    finally:
        conn.close()
    from datetime import datetime as _dt

    def _wk(stamp):
        try:
            return _dt.strptime(str(stamp)[:10], "%Y-%m-%d").strftime("%G-W%V")
        except ValueError:
            return None
    by_week = {}
    for d in drafts:
        wk = _wk(d["created_at"])
        if wk:
            # An approved draft that later expired unsent was still an
            # approval: expire_quiet_night_drafts retires approved ones too.
            by_week.setdefault(wk, []).append("approved" if d["approved_at"] else d["status"])
    weeks = [w for w in dict.fromkeys(_wk(p["fired_at"]) for p in pushes) if w]
    ignored = 0
    for wk in weeks:
        statuses = by_week.get(wk)
        if statuses is None:
            break                       # nothing drafted that week — not an answer
        if any(st == "approved" for st in statuses):
            break
        ignored += 1
    return ignored


def expire_quiet_night_drafts(restaurant_id, db_path=DB_PATH) -> int:
    """A quiet-night post or guest text not sent by the time its night has
    passed is retired (status 'expired'), so nobody can approve — or send —
    a "come in Tuesday" on Thursday. approve_draft only approves
    status='draft'.

    Approved drafts expire too (M-21): approval is not sending, and an
    approved but unsent quiet-night post stayed approved and sendable on
    Thursday and forever after."""
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "UPDATE marketing_drafts SET status='expired', updated_at=datetime('now') "
            "WHERE restaurant_id=? AND status IN ('draft', 'approved') AND (topic LIKE ? OR topic LIKE ?) "
            "AND created_at < datetime('now', ?)",
            (restaurant_id, "%" + _QN_POST_SUFFIX, "%" + _QN_SMS_SUFFIX,
             f"-{QUIET_NIGHT_DRAFT_DAYS} days")).rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def _draft_quiet_night_fill(r, out, db_path):
    """Draft the post that would fill the quiet night. Returns
    {"post_draft_id"} when it was saved, {} if not.

    It used to draft a guest text too (content_type 'guest_sms', campaign
    type "slow_day"), and that text dead-ended (Marketing audit AUX-5 /
    AI-5 / #38): no client could send a guest_sms draft, Approve only flipped
    its status, Open loaded it into the social composer, and "slow_day" was
    not a campaign prompt at all (it fell back to "general"). An owner
    approved it and believed guests had been texted. The text is now drafted
    where it can be sent: the push opens Marketing, whose Opportunity Feed
    card for the same night ("Fill Tuesday dinner") drafts it in the
    Campaign Studio with the text channel, the audience and the target day
    set - the fill-a-night plan (guest_marketing.plan_campaign), measured by
    the slow-day tracker. The post stays a draft here: it has a real publish
    path from the Content tab.

    The night is two days out and the post waits for the owner's approval
    anyway, so it goes through Message Batches where it may (AI cost audit
    10/7/26 #62): {"post_draft_queued": True} — the draft is saved when the
    collector hands the answer back (on_quiet_night_post), or written
    synchronously if the batch cannot answer by QUIET_NIGHT_BATCH_CUTOFF_HOURS."""
    weekday = out.get("weekday") or "the quiet night"
    topic = f"{weekday}{_QN_POST_SUFFIX}"
    sent = _submit_quiet_night_post(r, topic)
    if sent == "submitted":
        return {"post_draft_queued": True}
    if sent == "blocked":
        # A gate refused the item (budget, breaker, readiness) and its
        # callback was told so, which drafts nothing — the synchronous call
        # would be refused the same. Not drafted: the push keeps the old
        # wording and never promises a post that is not coming (AI cost
        # audit 10/7/26 re-audit #5).
        return {}
    return _quiet_night_post_now(r, topic)


def _quiet_night_post_now(r, topic):
    """The quiet night's post written and saved as a draft now:
    {"post_draft_id"} when it was saved, {} if not."""
    import ops
    saved = {}
    try:
        import marketing, marketing_drafts
        # The topic is ours, not the owner's: nothing in it is an offer
        # source (AI-2).
        body = marketing.generate_content("instagram_post", topic, restaurant_id=r.id, topic_is_owner=False)
        if body and body.strip():
            # The model draft it is (draft_ref), so approving it files the
            # outcome on the run that wrote it (re-audit #8).
            res = marketing_drafts.save_draft(r.id, body.strip(), content_type="instagram_post", topic=topic,
                                              draft_ref=getattr(body, "draft_ref", None),
                                              content_log_id=getattr(body, "content_log_id", None))
            if res.get("ok"):
                saved["post_draft_id"] = res["id"]
    except Exception as e:
        ops.capture(e, job="quiet_night_post", context=f"restaurant_id={r.id}")
    return saved


# The quiet-night post through Message Batches (AI cost audit 10/7/26 #62).
# An owner's own post (the Content tab, the Campaign Studio) is written now.
QUIET_NIGHT_BATCH_WORKFLOW = "quiet_night_post"
QUIET_NIGHT_BATCH_CALLBACK = "strategy_jobs:on_quiet_night_post"
QUIET_NIGHT_BATCH_CUTOFF_HOURS = 6


def _submit_quiet_night_post(r, topic):
    """The post's request sent as one batch item (the prompt built now, as
    the synchronous draft would build it). "submitted" when it went;
    "blocked" when a gate refused it — the synchronous call would have been
    refused too, and nothing will be drafted (re-audit #5: this used to read
    as sent, and the push promised a post that never came); None when it
    must be written now (batches off here, the submit failed). Never raises."""
    try:
        import ai_batches
        if not ai_batches.enabled(QUIET_NIGHT_BATCH_WORKFLOW):
            return None
        from datetime import datetime, timedelta
        import ai_orchestrator
        import ai_workflows as wf
        import data_health
        import marketing
        state = marketing.content_prompt("instagram_post", topic, restaurant_id=r.id, topic_is_owner=False)
        run_id = ai_orchestrator.new_run_id("marketing_content")
        cid = f"qn-{int(r.id)}-{uuid.uuid4().hex[:12]}"
        res = ai_batches.submit(QUIET_NIGHT_BATCH_WORKFLOW, [{
            "custom_id": cid, "restaurant_id": r.id, "action": "marketing_content",
            "request": marketing.social_post_request(state["prompt"],
                                                     wf.route_for(wf.policy("marketing_content"), 0)),
            "readiness": data_health.NOT_APPLICABLE, "callback": QUIET_NIGHT_BATCH_CALLBACK,
            "correlation_id": run_id,
            "cutoff_at": datetime.utcnow() + timedelta(hours=QUIET_NIGHT_BATCH_CUTOFF_HOURS),
            "context": {"topic": topic, "state": state, "run_id": run_id}}])
        status = res.get(cid)
        if status == ai_batches.SUBMITTED:
            return "submitted"
        if status == ai_batches.BLOCKED:
            return "blocked"
        return None
    except Exception as e:
        import ops
        ops.capture(e, job="quiet_night_post", context=f"restaurant_id={r.id} batch submit")
        return None


def on_quiet_night_post(item, message=None, error=None):
    """ai_batches' callback for the quiet night's post. The answer is the
    marketing_content run's first rung (marketing.generate_content with the
    prompt it was sent with); a refusal by the public-copy check escalates
    now, as it would have synchronously; the draft is saved as the
    synchronous path saves it. No answer (errored, expired, past the
    cutoff) writes the post now; a gate's refusal saves nothing."""
    import ai_batches
    import ops
    rid = (item or {}).get("restaurant_id")
    ctx = (item or {}).get("context") or {}
    topic = ctx.get("topic")
    if not rid or not topic:
        return
    if error is not None and not isinstance(error, ai_batches.BatchItemFailed):
        return
    from models import get_restaurant
    r = get_restaurant(rid)
    if r is None or not getattr(r, "module_marketing", 0):
        return
    if error is not None:
        _quiet_night_post_now(r, topic)
        return
    try:
        import marketing, marketing_drafts
        body = marketing.generate_content("instagram_post", topic, restaurant_id=rid, topic_is_owner=False,
                                          first=message, run_id=ctx.get("run_id"), state=ctx.get("state"))
        if body and body.strip():
            marketing_drafts.save_draft(rid, body.strip(), content_type="instagram_post", topic=topic,
                                        draft_ref=getattr(body, "draft_ref", None),
                                        content_log_id=getattr(body, "content_log_id", None))
    except Exception as e:
        # A second refusal (MarketingCopyRejected) is already a quality
        # event; nothing is saved, as the synchronous draft would save none.
        ops.capture(e, job="quiet_night_post", context=f"restaurant_id={rid} (batch)")


def run_trusted_orders(db_path=DB_PATH):
    """8am local on the owner's order day (auto_order_weekday, Monday unless
    they picked another): queue the supplier orders that can go on their own
    (ordering.py — a supplier with a record, a total in the usual band, no
    order this week), with an hour to undo, and tell the owner. Off unless
    auto_order_trusted is on for the restaurant."""
    import ops, ordering
    from models import auto_order_weekday
    from time_utils import restaurant_now
    import scheduler
    queued, attempted, failed = 0, 0, 0
    # Bounded and resumable (#84).
    walk = _BoundedWalk("trusted_orders", _restaurants(db_path), db_path, TRUSTED_ORDERS_MAX_SECONDS)
    for r in walk:
        if not getattr(r, "module_inventory", 0) or not getattr(r, "auto_order_trusted", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != auto_order_weekday(r) or not scheduler.local_due(r, 8, claim_key="trusted_orders"):
            continue
        attempted += 1
        try:
            held = []
            # Stock is the last count minus depletion since: with depletion
            # more than a day behind it reads high and the order comes out
            # short (DH2-4). Nothing is queued; the owner hears why.
            import data_freshness
            behind = data_freshness.depletion_behind(r, max_days_behind=1, db_path=db_path)
            if behind:
                held.append({"supplier_name": "Every trusted order",
                             "reason": f"{behind} — stock can't be trusted until it catches up"})
                rows = []
            else:
                rows = ordering.queue_trusted_orders(r.id, restaurant=r, db_path=db_path, held=held)
            # A held order is news: the owner expected it to go out and it
            # did not, so they hear why and what to do.
            if held:
                names = ", ".join(h.get("supplier_name") or h.get("supplier_email") or "a supplier" for h in held[:3])
                # Its own type (re-audit A-22): as "order_send_pending" it
                # shared the day's collapse id with the queued orders, so the
                # next banner replaced it on the lock screen, and the bell
                # called it "Supplier order going out".
                _reach(r.id, "order_send_held", "A supplier order was held",
                       f"{names}: {held[0].get('reason') or 'the last count is too old'}. "
                       "Count the stock and send it from Food Cost.",
                       {"tab": "food"}, db_path, subject=f"A supplier order didn't go out — {r.name}")
            for row in rows:
                # One banner per order: keyed on the action, not the day.
                _reach(r.id, "order_send_pending",
                       "A supplier order goes out in an hour",
                       f"{row.get('label')}. Undo from Home if you'd rather look first.",
                       {"delayed_action_id": row["id"], "collapse_key": f"order-{row['id']}"}, db_path,
                       subject=f"Supplier order going out at {_local_clock(r, row['execute_at'])} — {r.name}")
                queued += 1
        except Exception as e:
            failed += 1
            ops.capture(e, job="trusted_orders", context=f"restaurant_id={r.id}")
    return _counts(attempted, attempted - failed, failed, hit_bound=walk.hit_bound, queued=queued)


def _local_clock(restaurant, utc_stamp) -> str:
    """A stored UTC "YYYY-MM-DD HH:MM:SS" as the restaurant's own wall clock,
    "9:00am". The trusted-order subject said "going out at 13:00 UTC" to an
    owner in Chicago (#16)."""
    from datetime import datetime as _dt, timezone as _tz
    from time_utils import restaurant_tz
    try:
        at = _dt.strptime(str(utc_stamp)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
        local = at.astimezone(restaurant_tz(restaurant))
    except (TypeError, ValueError):
        return "shortly"
    return _clock(local.hour, local.minute)


def run_preshift_nudge(db_path=DB_PATH, restaurants=None):
    """Text the routed manager that tonight's lineup notes are ready, at the
    hour the owner chose (restaurants.preshift_nudge_hour; 0 = off).

    It goes to the MANAGER, not to staff: the pre-shift briefing is on the
    staff portal, and staff phone numbers carry no SMS consent — the one
    consented, routed number is the manager's. Nothing is sent when the
    briefing has nothing to say.

    This is also when the day's one model rewrite of the lineup notes is
    written (staff_brief.draft — at most one per restaurant per day, a
    DRAFT nobody on staff sees until a manager approves it), and the text
    asks the manager to approve the brief and pick tonight's focus item.
    A failed draft never stops the text.
    """
    import issues, ops, preshift
    from time_utils import restaurant_now
    import scheduler
    sent = 0
    st = {"attempted": 0, "failed": 0}
    for r in _slot_iter("preshift_nudge", restaurants, db_path, state=st):
        hour = int(getattr(r, "preshift_nudge_hour", 0) or 0)
        if not hour:
            continue
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, hour, until=hour + 2, claim_key="preshift_nudge",
                                   now_local=local):
            continue
        st["attempted"] += 1
        try:
            brief = preshift.build_cached(r.id, day=local.date(), db_path=db_path)
            items = brief.get("items") or []
            if not items:
                continue
            drafted = False
            try:
                import staff_brief
                row = staff_brief.draft(r.id, day=local.date(), db_path=db_path, items=items)
                drafted = (row or {}).get("draft_status") == "drafted"
            except Exception as de:
                ops.capture(de, job="preshift_nudge", context=f"restaurant_id={r.id} staff brief draft")
            routing = issues.get_routing(r.id, db_path)
            manager = routing.get("manager")
            if not manager or not manager.get("phone"):
                continue
            from models import is_in_quiet_hours
            if is_in_quiet_hours(r.id, db_path=db_path):
                continue
            from auth import get_or_create_staff_portal_token
            from notify import send_sms, sms_context
            token = get_or_create_staff_portal_token(r.id, db_path=db_path)
            base = config.base_url()
            lead = items[0]["text"]
            # The team reads them in the staff app (the web portal is gone,
            # 9/30/26); the link names the restaurant and its code for anyone
            # who hasn't got the app yet.
            # The manager's say: approve the rewrite (or write their own) and
            # pick tonight's focus item, before the team reads it.
            # The links are never the part cut to fit one 320-character text:
            # the lead line is.
            ask = ("Approve the drafted brief, or pick a focus item: " if drafted
                   else "Add a brief or a focus item: ")
            head = (f"Cavnar AI · tonight's lineup notes ({len(items)} point"
                    f"{'' if len(items) == 1 else 's'}): ")
            tail = (f" {ask}{base}/?nav=account%2Fnotifications The team reads them in the Cavnar AI app: "
                    f"{base}/staff/r/{token}")
            room = max(0, 320 - len(head) - len(tail))
            if len(lead) > room:
                lead = lead[:max(0, room - 1)].rstrip() + "…"
            msg = head + lead + tail
            # The text is this restaurant's in sms_log (fix round E, #14).
            with sms_context(r.id):
                texted = send_sms(manager["phone"], msg[:320], use_case="alert")
            if texted:
                sent += 1
        except Exception as e:
            st["failed"] += 1
            ops.capture(e, job="preshift_nudge", context=f"restaurant_id={r.id}")
    return _slot_counts(st, sent=sent)



# ── Review requests: measured, and nudged (#49) ───────────────────────────────
# review_requests rows were written on every send and read by nothing: nobody
# could say whether asking guests for a review ever produced one. Measured
# here, honestly: Google does not give a reviewer's phone or email, so the
# only join available is the NAME the guest gave us against the name on the
# review. That over-counts common names and misses nicknames and initials,
# and it says so wherever the figure is shown.
REVIEW_REQUEST_MATCH_DAYS = 14
REVIEW_REQUEST_MEASURE_DAYS = 90
REVIEW_REQUEST_OFF_DAYS = 30
REVIEW_NUDGE_WEEKDAY = 0                 # Monday
REVIEW_NUDGE_HOUR = 10
REVIEW_NUDGE_CURSOR_KEY = "review_request_nudge_cursor"
REVIEW_NUDGE_MAX_SECONDS = 5 * 60
REVIEW_MATCH_CAVEAT = ("Matched by name only — Google doesn't share a reviewer's phone or email, so a "
                       "common name can be over-counted and a nickname missed.")


def _parse_stamp(value):
    """A stored timestamp or date as a naive UTC-ish datetime, else None.
    The offset, where there is one, is dropped: a 14-day window does not
    turn on a few hours."""
    from datetime import datetime as _dt
    text = str(value or "").strip().replace("Z", "")[:19]
    try:
        return _dt.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def review_request_conversion(restaurant_id, days=REVIEW_REQUEST_MEASURE_DAYS, db_path=DB_PATH) -> dict:
    """Of the review requests sent in the last `days` whose 14-day window has
    closed, how many were followed by a Google review from someone of the
    same name within REVIEW_REQUEST_MATCH_DAYS.

    {"measured", "matched", "rate", "window_days", "sent_recently",
     "basis": "name", "caveat"} — rate is None below 5 measured requests,
    because 1 of 3 is not a conversion rate."""
    import staff_settings as _ss
    from datetime import datetime as _dt, timedelta as _td
    conn = get_conn(db_path)
    try:
        reqs = conn.execute(
            "SELECT customer_name, sent_at FROM review_requests WHERE restaurant_id=? "
            "AND COALESCE(status,'sent') NOT IN ('failed') AND sent_at >= datetime('now', ?) "
            "ORDER BY sent_at", (restaurant_id, f"-{int(days)} days")).fetchall()
        reviews = conn.execute(
            "SELECT id, author, COALESCE(NULLIF(review_date,''), fetched_at) AS at FROM reviews "
            "WHERE restaurant_id=? AND platform='google' AND deleted_at IS NULL "
            "AND datetime(COALESCE(NULLIF(review_date,''), fetched_at)) >= datetime('now', ?)",
            (restaurant_id, f"-{int(days) + REVIEW_REQUEST_MATCH_DAYS} days")).fetchall()
    finally:
        conn.close()
    now = _dt.utcnow()
    window = _td(days=REVIEW_REQUEST_MATCH_DAYS)
    recent = sum(1 for q in reqs if (_parse_stamp(q["sent_at"]) or now) >= now - _td(days=REVIEW_REQUEST_OFF_DAYS))
    by_name = {}
    for rv in reviews:
        k = _ss.name_key(rv["author"])
        at = _parse_stamp(rv["at"])
        if k and at:
            by_name.setdefault(k, []).append((at, rv["id"]))
    used, measured, matched = set(), 0, 0
    for q in reqs:
        sent = _parse_stamp(q["sent_at"])
        k = _ss.name_key(q["customer_name"])
        if not sent or sent > now - window:
            continue                        # its window hasn't closed yet
        measured += 1
        if not k:
            continue
        for at, rid_ in by_name.get(k, []):
            if rid_ not in used and sent <= at <= sent + window:
                used.add(rid_)
                matched += 1
                break
    return {"measured": measured, "matched": matched,
            "rate": round(matched / measured, 3) if measured >= 5 else None,
            "window_days": REVIEW_REQUEST_MATCH_DAYS, "sent_recently": recent,
            "basis": "name", "caveat": REVIEW_MATCH_CAVEAT}


def _google_review_counts(restaurant_id, db_path=DB_PATH):
    """(last 28 days, the 28 before) — reviews guests wrote on Google."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(d >= datetime('now','-28 days')) AS now_n, "
            "SUM(d < datetime('now','-28 days') AND d >= datetime('now','-56 days')) AS prev_n FROM "
            "(SELECT datetime(COALESCE(NULLIF(review_date,''), fetched_at)) AS d FROM reviews "
            " WHERE restaurant_id=? AND platform='google' AND deleted_at IS NULL)", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return int(row["now_n"] or 0), int(row["prev_n"] or 0)


def review_request_nudge(restaurant, db_path=DB_PATH):
    """(title, body, rec, lines) when this restaurant should hear about review
    requests this week, else None: requests have gone quiet (none sent in
    REVIEW_REQUEST_OFF_DAYS), or new Google reviews are flat or falling
    against the four weeks before. Always with the measured conversion when
    there is one, and its caveat."""
    rid = restaurant.id
    conv = review_request_conversion(rid, db_path=db_path)
    now_n, prev_n = _google_review_counts(rid, db_path)
    off = conv["sent_recently"] == 0
    flat = prev_n > 0 and now_n <= prev_n
    if not off and not flat:
        return None
    lines = []
    if off:
        title = "No review requests went out this month"
        lines.append(f"Nobody was asked for a review in the last {REVIEW_REQUEST_OFF_DAYS} days.")
    else:
        title = "New Google reviews have gone flat"
        lines.append(f"{now_n} new Google review{'' if now_n == 1 else 's'} in the last 4 weeks, "
                     f"against {prev_n} the 4 weeks before.")
    if conv["rate"] is not None:
        lines.append(f"When you asked: {conv['matched']} of {conv['measured']} guests "
                     f"({conv['rate'] * 100:.0f}%) left a Google review within {conv['window_days']} days. "
                     + conv["caveat"])
    elif conv["measured"]:
        lines.append(f"{conv['measured']} request{'' if conv['measured'] == 1 else 's'} measured so far — "
                     "too few to call a rate yet.")
    lines.append("Review requests go out after a visit from Marketing → Guests.")
    rec = {"key": "review_requests", "module": "reviews", "title": title}
    return title, " ".join(lines), rec, lines


def run_review_request_nudge(db_path=DB_PATH, now_local=None):
    """Monday morning, each restaurant in its own timezone: at most one
    review-request nudge a week, through the briefing budget (_reach).
    Bounded and resumable (scheduler.resumable_sweep); the ISO-week claim is
    taken only on the day and hour it is meant for, and only when there is
    something to say."""
    import ops, scheduler
    from time_utils import restaurant_now
    by_id = {r.id: r for r in _restaurants(db_path) if getattr(r, "module_reviews", 0)}
    sent = {"n": 0}
    tally = {"attempted": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        try:
            _nudge(rid)
        except Exception:
            with lock:
                tally["failed"] += 1
            raise

    def _nudge(rid):
        r = by_id[rid]
        local = now_local or restaurant_now(r, naive=True)
        if local.weekday() != REVIEW_NUDGE_WEEKDAY or not (REVIEW_NUDGE_HOUR <= local.hour < REVIEW_NUDGE_HOUR + 4):
            return
        week = local.strftime("%G-W%V")
        if ops.period_claimed(f"review_request_nudge:{rid}", week):
            return
        with lock:
            tally["attempted"] += 1
        out = review_request_nudge(r, db_path=db_path)
        if not out:
            return
        title, body, rec, lines = out
        import notify
        if rec["key"] in notify.silenced_keys(rid, db_path):
            return
        if not ops.claim_period(f"review_request_nudge:{rid}", week):
            return
        if _reach(rid, "review_request_nudge", title, body,
                  {"ask_prompt": "How are review requests working for us?"}, db_path,
                  subject=title, lines=lines, rec=rec):
            sent["n"] += 1

    _done, hit = scheduler.resumable_sweep(REVIEW_NUDGE_CURSOR_KEY, sorted(by_id), _one, REVIEW_NUDGE_MAX_SECONDS,
                                           job="review_request_nudge")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"], tally["failed"],
                   hit_bound=hit, sent=sent["n"])
