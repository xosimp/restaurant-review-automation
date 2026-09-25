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
    counts = {"outcomes_closed": 0, "goals_achieved": 0}

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

    _bounded_each("outcome_evaluations", _one, db_path)
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

    _bounded_each("outcome_rechecks", _one, db_path)
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

    _bounded_each("outcome_wins", _one, db_path)
    return {"wins_told": told["n"]}


RESULTS_MAX_SECONDS = 10 * 60


def _bounded_each(job, fn, db_path, max_seconds=RESULTS_MAX_SECONDS):
    """Run `fn(r)` for every live restaurant, bounded by wall clock and
    resumable from a cursor in job_cursors — CLAUDE.md's rule for work that
    iterates restaurants (run_labor_reminders is the same shape)."""
    import ops
    import scheduler as _sched
    key = f"{job}_cursor"
    order = sorted(_restaurants(db_path), key=lambda r: r.id)
    cursor = _read_cursor(key, db_path)
    order = [r for r in order if r.id > cursor] + [r for r in order if r.id <= cursor]

    def _failed(r, e):
        ops.capture(e, job=job, context=f"restaurant_id={r.id}")

    done, _ran_out = _sched.bounded_map(order, fn, 1, max_seconds, on_error=_failed)
    if order:
        _write_cursor(key, order[min(done, len(order)) - 1].id if done else cursor, db_path)
    return done


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

    _bounded_each("milestones", _one, db_path)
    return counts


def run_loss_sync(db_path=DB_PATH):
    import loss_detection, ops
    synced = unsupported = 0
    for r in _restaurants(db_path):
        try:
            out = loss_detection.sync(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="loss_sync", context=f"restaurant_id={r.id}")
            continue
        if out.get("ok"):
            synced += 1
            _loss_flags_to_issues(r, db_path)
        else:
            unsupported += 1
    return {"synced": synced, "not_supported": unsupported}


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
    ("food_cost", "module_inventory", r"\b(?:food\s+cost|waste|inventory|orders?|prep|portions?|suppliers?|"
                                       r"invoices?|counts?|stock)\b"),
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
    for r in _restaurants(db_path):
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
        try:
            from ask_cavnar import ask_with_tools
            # The readiness gate, per module (DH5-2): a module whose data
            # can't be stood on this week is held — the plan is told so, and
            # an item about it is not filed — while the others still plan.
            holds = plan_holds(r, db_path=db_path)
            question = WEEKLY_PLAN_PROMPT
            if holds:
                question += ("\n\nHELD THIS WEEK — the data behind these isn't current, so propose no action "
                             "about them: " + "; ".join(f"{m.replace('_', ' ')} ({why})" for m, why in holds.items())
                             + ".")
            answer, _trunc, _props, _meta = ask_with_tools(r, question, history=[], user=None,
                                                           read_only=True, delivery="unattended")
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
                           if held else _plan_item_problem(item, unverified, ctx, unsupported_names=names))
                if why_not:
                    ops.capture(RuntimeError(f"weekly plan item not filed: {why_not}"),
                                job="weekly_plan", context=f"restaurant_id={r.id}")
                    continue
                issues.create_issue(
                    r.id, "plan", item["title"],
                    detail=f"{item['why']} Owner: {item['owner']}. Due in {item['due_days']} days.",
                    severity="normal", source_key=f"plan:{week}:{i}", notify=False, db_path=db_path)
                filed += 1
        except Exception as e:
            ops.release_period(f"weekly_plan:{r.id}", week)
            from ai_utils import AIBudgetExceeded
            if not isinstance(e, AIBudgetExceeded):
                ops.capture(e, job="weekly_plan", context=f"restaurant_id={r.id}")
    return {"filed": filed}


def run_recipe_drafts(db_path=DB_PATH):
    """Tuesday 5am local, after the nightly depletion has created any new
    menu items: draft recipes for dishes that have none (recipes.py), a
    bounded number per restaurant per week. Only where Food Cost is on."""
    import ops, recipes
    from time_utils import restaurant_now
    drafted = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_inventory", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 1 or not (5 <= local.hour < 9):
            continue
        if not ops.claim_period(f"recipe_drafts:{r.id}", local.strftime("%G-W%V")):
            continue
        try:
            drafted += recipes.draft_missing(r.id, db_path=db_path).get("drafted", 0)
        except Exception as e:
            ops.capture(e, job="recipe_drafts", context=f"restaurant_id={r.id}")
    return {"drafted": drafted}


def run_issue_scan(db_path=DB_PATH, local_hour=None):
    """Reviews hourly; the daily operational signals and the opening
    checklist once a day at `local_hour` in the restaurant's own timezone
    (None = no gate, for a direct call)."""
    import issues, ops
    from time_utils import restaurant_now
    opened = 0
    for r in _restaurants(db_path):
        if getattr(r, "module_reviews", 0):
            try:
                opened += len(issues.open_from_reviews(r.id, db_path=db_path) or [])
            except Exception as e:
                ops.capture(e, job="issue_scan", context=f"restaurant_id={r.id}")
        try:
            # A review issue whose reply has posted is done — close it
            # rather than leave the manager chased about a fixed thing (#18).
            issues.auto_close(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="issue_auto_close", context=f"restaurant_id={r.id}")
        try:
            local = restaurant_now(r, naive=True)
            # The checklist is time-of-day sensitive, so it runs every pass —
            # its own grace period decides when it is late (issues.
            # open_from_checklists), and source_key keeps it to one a day.
            opened += len(issues.open_from_checklists(r.id, db_path=db_path, now_local=local) or [])
        except Exception as e:
            ops.capture(e, job="issue_checklists", context=f"restaurant_id={r.id}")
        if local_hour is not None:
            import scheduler
            if not scheduler.local_due(r, local_hour, claim_key="issue_signals"):
                continue
        try:
            opened += len(issues.open_from_signals(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="issue_signals", context=f"restaurant_id={r.id}")
    return {"opened": opened}


# A schedule generated this recently counts as "next week is handled" — the
# owner (or last week's auto-draft) already did it, and a second draft would
# only be noise in Schedule History.
AUTO_DRAFT_RECENT_DAYS = 5


def _recent_schedule(conn, restaurant_id):
    return conn.execute(
        "SELECT 1 FROM schedule_history WHERE restaurant_id=? AND "
        "generated_at >= datetime('now', ?)",
        (restaurant_id, f"-{AUTO_DRAFT_RECENT_DAYS} days")).fetchone() is not None


# Read by nothing since the weekly job records schedule_learning.
# calibrate_weights' suggestion (re-audit A-26). Kept, not deleted:
# candidate for future cleanup after additional verification.
CALIBRATION_MIN_WEEKS = 8
CALIBRATION_STEP = 2
CALIBRATION_MAX_WEIGHT = 30


def run_quality_calibration(db_path=DB_PATH):
    """Weekly: record the Shift Quality weight suggestion for each Labor
    restaurant, once per new published week, as a capability change
    (quality_weights_suggested, shown in the change history) — nothing is
    written to the weights until the owner applies it.

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
        if not cal.get("ready") or not cal.get("suggested_weights"):
            return
        current = get_quality_weights(r.id) or {}
        merged = dict(_sq.DEFAULT_WEIGHTS)
        merged.update(current)
        after = dict(merged)
        after.update({k: float(v) for k, v in cal["suggested_weights"].items()})
        if all(abs(float(after[k]) - float(merged.get(k, 0) or 0)) < 0.05 for k in after):
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
        if newest and not _ops_cal.claim_period("quality_calibration", f"{r.id}:{newest}"):
            return
        # Suggested, never applied: "Apply" is one tap and writes these same
        # numbers (_do_calibration_apply over calibrate_weights).
        record_capability_change(r.id, "quality_weights_suggested", subject="weights",
                                 before=_j.dumps(current), after=_j.dumps(after),
                                 changed_by="Cavnar AI (calibration)")
        changed["n"] += 1

    _bounded_each("quality_calibration", _one, db_path)
    return {"restaurants_changed": changed["n"]}


def run_auto_draft_schedules(db_path=DB_PATH):
    """Draft next week's schedule for every opted-in restaurant that hasn't
    already made one. The draft lands in Schedule History exactly as a
    hand-generated one does; nothing reaches staff until the owner publishes.

    Skipped where an external scheduling tool is named (the owner schedules
    in 7shifts/HotSchedules/etc. and a Cavnar draft would be a second,
    conflicting source of truth), and where Labor isn't on the plan."""
    import ops
    import threading
    from datetime import date as _date
    import scheduler as _sched
    import schedule_engine as _se
    counts = {"drafted": 0, "skipped": 0}
    lock = threading.Lock()
    # Bounded and resumable, like run_daily_fetch: a worker pool, a
    # wall-clock bound and a cursor. It used to be one serial 40-minute pass
    # claimed once per Thursday, so the restaurants past the bound waited a
    # whole week (SCHED-11 / DATA-8); the scheduler now runs a pass every
    # hour on Thursday, each starting at the cursor, and a restaurant is
    # attempted at most once a day (claimed before the paid generation).
    rows = [r for r in _restaurants(db_path)
            if getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)]
    order = sorted(rows, key=lambda r: r.id)
    cursor = _read_cursor(AUTO_DRAFT_CURSOR_KEY, db_path)
    order = [r for r in order if r.id > cursor] + [r for r in order if r.id <= cursor]
    day = _date.today().isoformat()

    def _bump(key):
        with lock:
            counts[key] += 1

    def _one(r):
        if (getattr(r, "external_scheduling_tool", None) or "").strip():
            _bump("skipped")
            return
        conn = get_conn(db_path)
        try:
            if _recent_schedule(conn, r.id):
                _bump("skipped")
                return
        finally:
            conn.close()
        if not ops.claim_period(f"auto_draft:{r.id}", day):
            _bump("skipped")               # attempted earlier today — never a second paid try
            return
        _draft_one(r, db_path, _se, _bump)

    def _failed(r, e):
        ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={r.id}")

    done, ran_out = _sched.bounded_map(order, _one, AUTO_DRAFT_WORKERS, AUTO_DRAFT_MAX_SECONDS, on_error=_failed)
    if order:
        last = order[min(done, len(order)) - 1].id if done else cursor
        _write_cursor(AUTO_DRAFT_CURSOR_KEY, last, db_path)
    return {"drafted": counts["drafted"], "skipped": counts["skipped"], "complete": not ran_out}


def _draft_one(r, db_path, _se, _bump):
    """One restaurant's auto-draft and, when it saved, the owner's nudge."""
    import ops
    job_id = f"auto-{uuid.uuid4().hex[:12]}"
    ops.start_async_job(job_id, "schedule", r.id)
    _se._run_schedule_job(job_id, r.id)
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
        import morning_brief
        audience = {u["id"] for u in morning_brief.recipients(r.id, db_path)}
        if not notify.briefing_allowed(r.id, "schedule_drafted", db_path):
            return
        notify.record_notification(r.id, "schedule_drafted", db_path=db_path)
        push.fire_push(r.id, "schedule_drafted", "Next week's schedule is drafted",
                       "Review it and publish when it looks right — nothing has gone to "
                       "your staff yet.", data={}, db_path=db_path,
                       user_ids=audience or None)
    except Exception as e:
        ops.capture(e, job="auto_draft_schedule_push", context=f"restaurant_id={r.id}")


AUTO_DRAFT_CURSOR_KEY = "auto_draft_schedule_cursor"
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
            lines.append(f"The week of {mdy(draft['week_start'])} is drafted but staff don't have it yet — send it from Labor.")
    finally:
        conn.close()
    return {"lines": lines, "history_id": hid}


def run_labor_reminders(db_path=DB_PATH):
    """9am local: one notice per Labor restaurant naming what is waiting on
    the manager before the next shifts (labor_waiting). Nothing waiting, no
    notice. Runs every hour; each restaurant is claimed once a day at its
    own 9am. Bounded and resumable."""
    import ops
    import scheduler as _sched
    order = sorted((r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)), key=lambda r: r.id)
    cursor = _read_cursor(LABOR_REMINDERS_CURSOR_KEY, db_path)
    order = [r for r in order if r.id > cursor] + [r for r in order if r.id <= cursor]
    sent = {"n": 0}

    def _one(r):
        if not _sched.local_due(r, 9, claim_key="labor_reminders"):
            return
        w = labor_waiting(r.id, db_path=db_path, draft=not getattr(r, "auto_publish_schedule", 0))
        if not w["lines"]:
            return
        body = w["lines"][0] if len(w["lines"]) == 1 else f"{len(w['lines'])} things before the next shifts."
        data = {"tab": "labor"}
        if w["history_id"]:
            data["history_id"] = w["history_id"]
        if _reach(r.id, "labor_reminder", "Waiting on you in Labor", body, data, db_path, lines=w["lines"]):
            sent["n"] += 1

    def _failed(r, e):
        ops.capture(e, job="labor_reminders", context=f"restaurant_id={r.id}")

    done, _ran_out = _sched.bounded_map(order, _one, 1, LABOR_REMINDERS_MAX_SECONDS, on_error=_failed)
    if order:
        _write_cursor(LABOR_REMINDERS_CURSOR_KEY, order[min(done, len(order)) - 1].id if done else cursor, db_path)
    return {"reminded": sent["n"]}


def run_schedule_outcomes(db_path=DB_PATH):
    """Monday: record what each published week actually did, by daypart
    (schedule_intel.record_outcomes), for every Labor restaurant."""
    import ops
    import schedule_intel
    import scheduler as _sched
    # Bounded and resumable (SCHED-27): a wall-clock bound and a cursor, so a
    # pass that runs out of time is picked up where it stopped instead of
    # starving the same tail every Monday.
    order = sorted((r for r in _restaurants(db_path) if getattr(r, "module_labor", 0)), key=lambda r: r.id)
    cursor = _read_cursor(OUTCOMES_CURSOR_KEY, db_path)
    order = [r for r in order if r.id > cursor] + [r for r in order if r.id <= cursor]
    written = {"n": 0}

    def _one(r):
        written["n"] += schedule_intel.record_outcomes(r.id, db_path=db_path).get("written", 0)
        # Then read each accepted recommendation against the night it was
        # about (rec_ledger outcome), now that the night is recorded.
        schedule_intel.measure_accepted_recommendations(r.id, db_path=db_path)

    def _failed(r, e):
        ops.capture(e, job="schedule_outcomes", context=f"restaurant_id={r.id}")

    done, _ran_out = _sched.bounded_map(order, _one, 1, OUTCOMES_MAX_SECONDS, on_error=_failed)
    if order:
        _write_cursor(OUTCOMES_CURSOR_KEY, order[min(done, len(order)) - 1].id if done else cursor, db_path)
    return {"rows": written["n"]}


OUTCOMES_CURSOR_KEY = "schedule_outcomes_cursor"
OUTCOMES_MAX_SECONDS = 20 * 60


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


def run_intraday_capture(db_path=DB_PATH):
    """Snapshot net sales so far, once an hour, while the restaurant is open.
    Each snapshot is also the baseline for the same weekday in later weeks —
    no hour-level history existed to compare a running day against."""
    import intraday, ops
    from time_utils import restaurant_now
    captured = skipped = 0
    for r in _restaurants(db_path):
        local = restaurant_now(r, naive=True)
        if not _open_now(r, local):
            skipped += 1
            continue
        if not ops.claim_period(f"intraday:{r.id}", f"{local.date().isoformat()}-{local.hour}"):
            continue
        try:
            captured += 1 if intraday.capture(r.id, now_local=local, db_path=db_path,
                                              restaurant=r).get("ok") else 0
        except Exception as e:
            ops.capture(e, job="intraday_capture", context=f"restaurant_id={r.id}")
    return {"captured": captured, "closed": skipped}


# The one interruption of the working day this product allows itself: late
# enough that the lunch numbers are in, early enough to change tonight's
# staffing, prep or a text to the guest club.
PULSE_HOUR = 16


def run_pre_dinner_pulse(db_path=DB_PATH):
    """One push before dinner, and only when today is materially off a
    typical same weekday at this hour. A pulse that fires every day is a
    notification people turn off."""
    import intraday, ops, push, scheduler
    from time_utils import restaurant_now
    sent = 0
    for r in _restaurants(db_path):
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, PULSE_HOUR, until=PULSE_HOUR + 2,
                                   claim_key="pre_dinner_pulse", now_local=local):
            continue
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
                                            db_path)
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
            push.fire_push(
                r.id, "intraday_pulse",
                f"{abs(p['pct']):.0f}% {word} a typical {p['weekday']}", body,
                data=data, db_path=db_path, user_ids=audience,
                on_delivered=rec_delivery.when_pushed(r.id, "alert_push", recs, db_path=db_path))
            sent += 1
        except Exception as e:
            ops.capture(e, job="pre_dinner_pulse", context=f"restaurant_id={r.id}")
    return {"sent": sent}


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
    floors = _sr.role_floors(restaurant)
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
    text = (f"{len(people)} {role.lower()}{'' if len(people) == 1 else 's'} on at {_clock(PULSE_CUT_HOUR)} "
            f"against a floor of {floor}: letting {who['employee']} (on till {end_label}) go at "
            f"{_clock(PULSE_CUT_HOUR)} {saving}.")
    import staff_settings as _ss
    return {"text": text, "employee": who["employee"], "role": role, "cut_at": _clock(PULSE_CUT_HOUR),
            "hours": hours, "dollars": dollars, "on": len(people), "floor": floor, "pay_caveat": pay_caveat,
            "key": f"pulse_cut:{local.date().isoformat()}:{_ss.name_key(who['employee'])}"}


def run_coverage_check(db_path=DB_PATH):
    """A scheduled person who hasn't clocked in becomes the routed manager's
    issue — the one staffing problem that is still fixable while it matters.
    One issue per person per day (source_key)."""
    import intraday, issues, ops
    from time_utils import restaurant_now
    opened = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_labor", 0):
            continue
        local = restaurant_now(r, naive=True)
        if not _open_now(r, local):
            continue
        if "manager" not in issues.get_routing(r.id, db_path):
            continue
        try:
            gaps = intraday.coverage_gaps(r.id, now_local=local, db_path=db_path, restaurant=r)
            # Everyone on today's schedule is busy; the person missing is the
            # gap. Best fits come from the same engine as the replacements
            # screen, folded into the text the manager actually reads.
            # "scheduled" is a count; the rows are "scheduled_rows". Iterating
            # the count crashed this job on every restaurant with a schedule
            # (MOD-LAB-1).
            on_today = {str(x.get("employee") or "") for x in (gaps.get("scheduled_rows") or [])}
            # Someone who turned up late closes their own no-show issue on
            # this pass — the manager is not left chasing a person already
            # on the floor (#13 / #18).
            if gaps.get("available"):
                issues.resolve_coverage(r.id, local.date().isoformat(), set(gaps.get("arrived_keys") or []),
                                        db_path=db_path)
            import staff_settings as _ss
            for m in (gaps.get("missing") or []):
                fits_text, fits = "", []
                try:
                    import labor_replacements
                    fits = labor_replacements.for_gap(r.id, m.get("role"), local.strftime("%A"),
                                                      exclude=on_today | {m["employee"]}, db_path=db_path,
                                                      on_date=local.date().isoformat(),
                                                      shift={"employee": m["employee"],
                                                             "shift_start": m.get("shift_start")}) or []
                    fits_text = labor_replacements.sentence(fits)
                except Exception as fe:
                    ops.capture(fe, job="coverage_replacements", context=f"restaurant_id={r.id}")
                # The suggested covers travel with the issue, so its page can
                # offer "Ask Ana to cover" as one tap (intraday.ask_to_cover).
                meta = {"missing": m["employee"], "role": m.get("role"), "shift_start": m.get("shift_start"),
                        "covers": [{"name": f["name"], "score": f.get("score")} for f in fits]}
                issue, token = issues.create_issue(
                    r.id, "coverage",
                    f"{m['employee']} hasn't clocked in",
                    detail=f"Scheduled {m['shift_start']} as {m['role']} — "
                           f"{m['minutes_late']} minutes ago, with no clock-in on the POS." + fits_text,
                    severity="high",
                    source_key=f"coverage:{local.date().isoformat()}:{_ss.name_key(m['employee'])}",
                    meta=meta, db_path=db_path)
                if token:
                    opened += 1
                    # The suggested covers are NOT presented here. The text the
                    # manager gets names the issue, not the covers, and a text
                    # Twilio refused still recorded "cover" on issue_sms (re-audit
                    # C3). They are presented where they are rendered: Home's
                    # open-issues list (GET /issues, strategy_routes) and the
                    # issue page a person acted on (/i/<token>).
        except Exception as e:
            ops.capture(e, job="coverage_check", context=f"restaurant_id={r.id}")
    return {"opened": opened}


def _metric_permissions(metric):
    """The permission a login needs to be told about a result on `metric`:
    a food-cost win is Food Cost's, comps and voids are LOSS_VIEW. None
    means nothing beyond the brief audience itself (sales)."""
    import permissions as _p
    base = str(metric or "").split(":", 1)[0]
    need = {"labor_pct": _p.LABOR_VIEW, "overtime_hours": _p.LABOR_VIEW,
            "food_cost_pct": _p.FOOD_COST_VIEW, "weekly_waste": _p.FOOD_COST_VIEW,
            "avg_rating": _p.REVIEWS_VIEW, "complaints": _p.REVIEWS_VIEW, "response_hours": _p.REVIEWS_VIEW,
            "comp_rate": _p.LOSS_VIEW, "void_rate": _p.LOSS_VIEW}.get(base)
    return {need} if need else None


def _reach(restaurant_id, alert_type, title, body, data, db_path, subject=None,
           lines=None, email_type=None, rec=None, permissions=None):
    """Push to the people who have the app, email the ones who don't.

    morning_brief.deliver established this — push OR email, never both,
    because a brief that arrives twice is one people learn to ignore. The
    closing summary and the outcome win were written push-only, so an owner
    without the app simply never learned their change had paid off. Same
    audience either way (morning_brief.recipients).

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
    people = morning_brief.recipients(restaurant_id, db_path)
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
    alert_id = notify.record_notification(restaurant_id, alert_type, db_path=db_path)
    pushed = {u["id"] for u in people if devices.get(u["id"])}
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
        push.fire_push(restaurant_id, alert_type, title, body, data=data,
                       db_path=db_path, user_ids=pushed,
                       on_delivered=(rec_delivery.when_pushed(restaurant_id, "alert_push", [rec], db_path=db_path)
                                     if rec else None))
    reached = len(pushed)

    emailed = [u for u in people if u["id"] not in pushed and u.get("email")]
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
    if rec and reached > len(pushed):
        rec_delivery.present_now(restaurant_id, "alert_email", [rec], db_path=db_path)
    return reached


def deliverable_audience(restaurant_id, user_ids, db_path=DB_PATH) -> set:
    """The logins in `user_ids` with a phone a push can reach right now
    (push.get_device_tokens for delivery: active logins, unparked tokens).
    A push-only job sends to this set, and to nobody when it is empty."""
    import push
    ids = {int(u) for u in (user_ids or ()) if u}
    if not ids:
        return set()
    try:
        return {int(t.get("user_id") or 0) for t in (push.get_device_tokens(restaurant_id, db_path, for_delivery=True)
                                                    or [])} & ids
    except Exception as e:
        print(f"[strategy_jobs] device lookup failed rid={restaurant_id}: {e}")
        return set()


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


def run_closing_summary(db_path=DB_PATH):
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
    from dsr.deliver import replaces_closing_summary
    for r in _restaurants(db_path):
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
            ops.capture(e, job="closing_summary", context=f"restaurant_id={r.id}")
    return {"sent": sent}


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


def run_demand_opportunity(db_path=DB_PATH):
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
    for r in _restaurants(db_path):
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
                                            db_path)
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
            # The fill, drafted: a post and a guest text for that night,
            # saved as drafts behind the same approval as any other. The
            # push used to ask "what could fill it?" — now it says "here's
            # what I wrote; approve it". Drafting can fail (budget, model)
            # without costing the owner the heads-up.
            drafted = {} if ignored >= QUIET_NIGHT_IGNORED_LIMIT else _draft_quiet_night_fill(r, out, db_path)
            body = (f"About ${out['typical_sales']:,.0f}, {out['below_average_pct']:.0f}% under a "
                    f"typical day across {out['samples']} of them. ")
            body += ("A post and a guest text are drafted — approve them from Marketing."
                     if drafted else "Two days to do something about it.")
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
            push.fire_push(
                r.id, "demand_opportunity",
                f"{out['weekday']} is usually your quietest night", body,
                data={"ask_prompt": f"What could fill {out['weekday']} night?", **drafted,
                      "alert_id": alert_id, "surface": "alert_push", "answerable": ans,
                      **({"rec_key": rec["key"]} if ans else {})},
                db_path=db_path, user_ids=audience,
                on_delivered=rec_delivery.when_pushed(r.id, "alert_push", [rec], db_path=db_path))
            sent += 1
        except Exception as e:
            ops.capture(e, job="demand_opportunity", context=f"restaurant_id={r.id}")
    return {"sent": sent}


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
    """Draft the post and the text that would fill the quiet night. Returns
    {"post_draft_id", "sms_draft_id"} for whatever was saved, {} if neither."""
    import ops
    saved = {}
    weekday = out.get("weekday") or "the quiet night"
    topic = f"{weekday}{_QN_POST_SUFFIX}"
    try:
        import marketing, marketing_drafts
        body = marketing.generate_content("instagram_post", topic, restaurant_id=r.id)
        if body and body.strip():
            res = marketing_drafts.save_draft(r.id, body.strip(), content_type="instagram_post", topic=topic)
            if res.get("ok"):
                saved["post_draft_id"] = res["id"]
    except Exception as e:
        ops.capture(e, job="quiet_night_post", context=f"restaurant_id={r.id}")
    try:
        import guest_marketing, marketing_drafts
        msg = guest_marketing.draft_campaign_message(r, campaign_type="slow_day", topic=topic)
        if msg and msg.strip():
            res = marketing_drafts.save_draft(r.id, msg.strip(), content_type="guest_sms",
                                              topic=f"{weekday}{_QN_SMS_SUFFIX}")
            if res.get("ok"):
                saved["sms_draft_id"] = res["id"]
    except Exception as e:
        ops.capture(e, job="quiet_night_sms", context=f"restaurant_id={r.id}")
    return saved


def run_trusted_orders(db_path=DB_PATH):
    """Monday 8am local: queue the supplier orders that can go on their own
    (ordering.py — a supplier with a record, a total in the usual band, no
    order this week), with an hour to undo, and tell the owner. Off unless
    auto_order_trusted is on for the restaurant."""
    import ops, ordering
    from time_utils import restaurant_now
    import scheduler
    queued = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_inventory", 0) or not getattr(r, "auto_order_trusted", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 0 or not scheduler.local_due(r, 8, claim_key="trusted_orders"):
            continue
        try:
            held = []
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
            ops.capture(e, job="trusted_orders", context=f"restaurant_id={r.id}")
    return {"queued": queued}


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


def run_preshift_nudge(db_path=DB_PATH):
    """Text the routed manager that tonight's lineup notes are ready, at the
    hour the owner chose (restaurants.preshift_nudge_hour; 0 = off).

    It goes to the MANAGER, not to staff: the pre-shift briefing is on the
    staff portal, and staff phone numbers carry no SMS consent — the one
    consented, routed number is the manager's. Nothing is sent when the
    briefing has nothing to say.
    """
    import issues, ops, preshift
    from time_utils import restaurant_now
    import scheduler
    sent = 0
    for r in _restaurants(db_path):
        hour = int(getattr(r, "preshift_nudge_hour", 0) or 0)
        if not hour:
            continue
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, hour, until=hour + 2, claim_key="preshift_nudge",
                                   now_local=local):
            continue
        try:
            brief = preshift.build(r.id, day=local.date(), db_path=db_path)
            items = brief.get("items") or []
            if not items:
                continue
            routing = issues.get_routing(r.id, db_path)
            manager = routing.get("manager")
            if not manager or not manager.get("phone"):
                continue
            from models import is_in_quiet_hours
            if is_in_quiet_hours(r.id, db_path=db_path):
                continue
            from auth import get_or_create_staff_portal_token
            from notify import send_sms
            token = get_or_create_staff_portal_token(r.id, db_path=db_path)
            base = config.base_url()
            lead = items[0]["text"]
            msg = (f"Cavnar AI · tonight's lineup notes are ready ({len(items)} point"
                   f"{'' if len(items) == 1 else 's'}): {lead} Read them with the team: "
                   f"{base}/staff/r/{token}")
            if send_sms(manager["phone"], msg[:320], use_case="alert"):
                sent += 1
        except Exception as e:
            ops.capture(e, job="preshift_nudge", context=f"restaurant_id={r.id}")
    return {"sent": sent}



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

    def _one(rid):
        r = by_id[rid]
        local = now_local or restaurant_now(r, naive=True)
        if local.weekday() != REVIEW_NUDGE_WEEKDAY or not (REVIEW_NUDGE_HOUR <= local.hour < REVIEW_NUDGE_HOUR + 4):
            return
        week = local.strftime("%G-W%V")
        if ops.period_claimed(f"review_request_nudge:{rid}", week):
            return
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

    scheduler.resumable_sweep(REVIEW_NUDGE_CURSOR_KEY, sorted(by_id), _one, REVIEW_NUDGE_MAX_SECONDS,
                              job="review_request_nudge")
    return {"sent": sent["n"]}
