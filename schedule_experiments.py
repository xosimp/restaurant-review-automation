"""
schedule_experiments.py — live A/B variants of schedule generation, judged
by what owners did with the draft and what the week then did.

Every generated week belongs to one arm of each active experiment, chosen by
a hash of (experiment, restaurant, week start): deterministic, so a
regeneration of the same week (or the quality gate's partial redo) keeps its
arm, and no table has to be read to know it. An arm only switches
deterministic parts of the pipeline on or off (`flags`); it never adds a
model call — one generation per week, as without the experiment.

The first experiment asks whether the constraint solver's assignment
(schedule_solver, audit #47) beats the model's own: arm "model" skips the
solver, arm "solver" runs it. A future experiment is one more entry in
EXPERIMENTS whose arms set a new flag, and one `flag(arms, "name")` read at
the point in schedule_engine the flag controls.

Measured per arm, internal only (admin_ops / templates/admin.html):
  acceptance   the share of the generated draft's rows that went out
               unedited — schedule_versions.acceptance, the same figure the
               owner's draft-acceptance chart shows
  outcomes     coverage and no-show issues and labor % from
               schedule_outcomes, once the week is over
  quality      the Shift Quality score the draft was generated with

Owners never see an arm. The readout refuses to call a winner below
MIN_WEEKS_PER_ARM published weeks from MIN_RESTAURANTS_PER_ARM restaurants
in every arm (weeks inside one restaurant are not independent), and calls
one on the PRIMARIES — the Draft Rework Rate (schedule_scorecard) and the
week's outcomes (issues, labor %, labor against the hourly target, minutes
with no manager, uncovered no-shows): better on at least one at 90% and
worse on none (it used to call on acceptance, which reads a manager who
stopped looking as improvement). Each arm carries `power_read`: the
smallest difference its sample can detect and the weeks it would take to
see one worth seeing — honest about an experiment that cannot conclude. The
model arm of assign_v1 is clean: the optimizer's reassignments are off with
the solver. learning_v1 is a within-restaurant SWITCHBACK (switchback_arm):
the learned memory off one week in four, judged the same way — and the
offline learning ablation (schedule_scorecard) answers it with no customer
exposure every generation meanwhile.

The interval is CLUSTER-ROBUST by restaurant: weeks were treated as
independent, so one busy restaurant supplying 15 of an arm's 20 weeks could
narrow the interval enough to call a false winner that then shipped to
everyone. Now each restaurant counts at most MAX_WEEKS_PER_RESTAURANT of its
most recent published weeks per arm, the standard error is the
restaurant-cluster (CR1) sandwich — the difference's includes the covariance
of a restaurant's weeks in both arms — with a t quantile on (restaurants −
1) degrees of freedom; quality is the PUBLISHED version's score, not an
average over every regeneration; only restaurants that may teach are read
(models.learning_eligible, through intelligence.jobs.excluded_learning_ids —
never a demo's regenerations); and the verdict is stored weekly
(schedule_experiment_verdicts, record_verdicts), so what the readout said on
a given week can be read back.

Kill switches: the env var SCHEDULE_EXPERIMENT_PIN ("off", an arm key, or
"experiment:arm") pins every restaurant; a row in schedule_experiment_pins
pins one. A pinned week records the arm it was pinned to and is left out of
the comparison, since it was not randomised. "off" is the control arm.

An experiment that cannot conclude does not run: while fewer than
MIN_RESTAURANTS_PER_ARM restaurants that may teach
(models.learning_eligible) generated a schedule in the last
POWER_WINDOW_DAYS, every week takes the experiment's `until_powered` arm —
for assign_v1 the solver, the only pass that weighs fairness, experience
and minimum hours across the whole assignment — recorded as pinned with
pin_source "underpowered", so it never counts in the comparison, and the
readout says the experiment is paused and why. The 50/50 hash gave Simple
EJ's (the only live client) the model's assignment on three of its first
four weeks for a readout that could never arrive.

A customer-exposed SWITCHBACK (design "switchback": learning_v1, learning_v2)
never starts by itself (blind re-audit 10/20/26 R5-02): reaching
MIN_RESTAURANTS_PER_ARM used to switch the learned memory off on one week in
four, and every learned input off on one in eight, for every restaurant with
no admin action — the corrections a manager taught came back as edits the
next week. It runs its hash only once an admin lists it in
SCHEDULE_SWITCHBACKS (comma-separated experiment keys, or "all"); until then
every week takes its `until_powered` arm, pinned with pin_source
"not_started" and never counted, and the readout says so. The offline
learning ablation answers the memory question meanwhile with no exposure.

Promotion (ROI audit #46): once the readout calls a winner, an admin
promotes it — a reviewed step, recorded in schedule_experiment_promotions
with who promoted it, when, the verdict it rested on and a note. From then
every restaurant gets the promoted arm's flags from that stored row (no
code edit), as a pin with pin_source "promoted" (so those weeks stay out of
the comparison), and it holds even after the experiment is retired in code.
revert() ends it and the experiment randomises again. Precedence: the env
kill switch, then a restaurant's own pin, then a promotion, then the
experiment's own pin while it cannot conclude, then the hash.
"""
import hashlib
import json
import math
import os

from models import DB_PATH
import logging

log = logging.getLogger(__name__)

from schedule_context import degraded as _degraded  # noqa: E402  (the one degradation event)


def get_conn(*args, **kwargs):
    """Resolved through models at call time, so a patched models.get_conn
    (tests, the restore drill) reaches this module too."""
    import models
    return models.get_conn(*args, **kwargs)


EXPERIMENTS = (
    {"key": "assign_v1",
     "active": True,
     "started": "2026-09-23",
     # The model arm is the model's assignment: the optimizer's replace /
     # swap / trade moves are off with the solver ("reassign"), so the
     # readout compares the model's own assignment with the solver's, not two
     # partly optimizer-assigned weeks.
     "question": "Does the constraint solver's assignment (with the optimizer's reassignments) beat the model's own "
                 "assignment (nobody reassigned by code)?",
     "control": "model",
     # Every week runs the solver until the experiment can conclude.
     "until_powered": "solver",
     # The model arm is off the hash (weight 0) since the solver became the
     # draft (10/14/26): there is no model assignment by default to compare,
     # and turning the solver and reassignments off on half of all weeks once
     # the fleet powers the experiment would undo the default. A pin still
     # reaches it.
     "arms": ({"key": "model", "label": "Model assignment", "weight": 0,
               "flags": {"solver": False, "reassign": False}},
              {"key": "solver", "label": "Solver assignment", "weight": 1,
               "flags": {"solver": True, "reassign": True}})},
    # Whether binding the learned memory makes weeks better (the framework
    # never tested learning). A within-restaurant SWITCHBACK — the arm
    # follows the week, so each restaurant is its own control — with the
    # memory off on one week in four, judged on the outcome primaries and
    # the rework rate. Until it can conclude every week keeps the memory on
    # (pinned, not counted); the offline learning ablation
    # (schedule_scorecard: the same loop over the same answer with the
    # memory off, every generation) answers the same question with no
    # customer exposure meanwhile.
    {"key": "learning_v1",
     "active": True,
     "started": "2026-10-10",
     "design": "switchback",
     "question": "Does binding the learned scheduling memory in the solver, optimizer and score make weeks better? "
                 "(control: the memory on, as every week runs today; the treatment switches it off)",
     # The control is what every week does today, so the kill switch "off"
     # (every experiment to its control) keeps the memory ON.
     # (Arm keys never "off": that word is the kill switch's, meaning the
     # control.)
     "control": "memory_on",
     "until_powered": "memory_on",
     "arms": ({"key": "memory_off", "label": "Learned memory off", "weight": 1, "flags": {"learned": False}},
              {"key": "memory_on", "label": "Learned memory on", "weight": 3, "flags": {"learned": True}})},
    # Whether LEARNING AS A WHOLE makes weeks better (learning_v1 and the
    # ablation switch off only the learned memory). A small
    # within-restaurant switchback: one week in eight with EVERY learned
    # input off (learning_stats.learning_off — the prompt's learned lines,
    # learned headcount, measured staffing moves, driver standards, promoted
    # weights, reliability, learned preferences, retention, the edit
    # predictor and the memory), judged on the outcome primaries. Until it
    # can conclude every week keeps learning on (pinned, not counted).
    {"key": "learning_v2",
     "active": True,
     "started": "2026-10-12",
     "design": "switchback",
     "question": "Do the learners, all of them together, make weeks better than drafting with nothing learned? "
                 "(control: learning on, as every week runs today; the treatment switches every learned input off)",
     "control": "learning_on",
     "until_powered": "learning_on",
     "arms": ({"key": "learning_off", "label": "Every learned input off", "weight": 1, "flags": {"learning": False}},
              {"key": "learning_on", "label": "Learning on", "weight": 7, "flags": {"learning": True}})},
    # Where the draft comes from (cross-workstream contract #7 —
    # schedule_fallback): the model's week, the last published week carried
    # forward and repaired, or the week shaped in code and solved. Judged
    # like any arm, on acceptance and outcomes. Since the CONTROL is the
    # solver-first draft ("code_shaped"): it holds every invariant of the
    # schedule benchmark the carried week holds, in seconds
    # (scripts/run_schedule_benchmark.py; MODULE_OVERVIEW.md has the table), and
    # the model's draft paid 15-40 minutes and 48-56k thinking tokens for
    # work the solver re-did. Every week takes the solver's draft until the
    # experiment can conclude (until_powered) — the model's draft is an arm,
    # reachable by a restaurant's pin or SCHEDULE_DRAFT_SOURCE=model. The arm
    # keys are this experiment's own: SCHEDULE_EXPERIMENT_PIN="solver"
    # (assign_v1's arm) is not one of them.
    {"key": "draft_v1",
     "active": True,
     "started": "2026-10-10",
     "question": "Does the model's draft, or the last published week carried forward, do better than the week "
                 "shaped in code and solved?",
     "control": "code_shaped",
     "until_powered": "code_shaped",
     # The model's draft is off the hash too (weight 0, scale audit 10/19/26
     # A5-F1): at weight 1 it took half of every restaurant's weeks the
     # moment five restaurants had generated in the window - $0.29-0.57 and
     # 2-5 minutes a call, against "a pin or the env var reaches it" above.
     "arms": ({"key": "model_draft", "label": "The model's draft", "weight": 0, "flags": {"draft_source": "model"}},
              # Off the hash (weight 0, 10/14/26): the schedule benchmark
              # shows the carried week leaves people with no hours where the
              # solver starves nobody — not a week to give a restaurant on a
              # coin flip. A pin still reaches it.
              {"key": "carried", "label": "Last published week, carried forward", "weight": 0,
               "flags": {"draft_source": "carry_forward"}},
              {"key": "code_shaped", "label": "Shaped in code and solved", "weight": 1,
               "flags": {"draft_source": "solver"}},
              # The solver-first draft with the model as a critic of it
              # (schedule_critic): a few judged edits, each validated by
              # code. Off the hash until the eval reads it: reachable by a
              # pin.
              {"key": "code_critic", "label": "Shaped in code, solved, then reviewed by the model", "weight": 0,
               "flags": {"draft_source": "solver", "critic": True}})},
)
# What a week gets from a flag no active experiment sets.
DEFAULT_FLAGS = {"solver": True, "reassign": True, "learned": True, "draft_source": "solver", "learning": True}
PIN_ENV = "SCHEDULE_EXPERIMENT_PIN"
OFF = "off"
# The explicit admin start a switchback needs before its hash runs (R5-02):
# comma-separated experiment keys, or "all". Unset: none run.
SWITCHBACKS_ENV = "SCHEDULE_SWITCHBACKS"


def switchback_started(exp) -> bool:
    """Whether a customer-exposed switchback was started by an admin
    (SWITCHBACKS_ENV). An experiment of any other design needs no start."""
    if exp.get("design") != "switchback":
        return True
    raw = (os.environ.get(SWITCHBACKS_ENV) or "").strip().lower()
    if not raw:
        return False
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    return "all" in keys or str(exp["key"]).lower() in keys

MIN_WEEKS_PER_ARM = 20
MIN_RESTAURANTS_PER_ARM = 5
# How far back "generating schedules" looks when deciding whether an
# experiment can conclude (powered): eight weeks.
POWER_WINDOW_DAYS = 56
# No one restaurant carries an arm: its most recent published weeks per
# arm, at most this many.
MAX_WEEKS_PER_RESTAURANT = 8
# v2: judged on the primaries — the Draft Rework Rate and the week's
# outcomes — not on acceptance.
VERDICT_METHOD = "cluster_robust_outcomes_v3"
# v3: "better on any of six primaries at 90%, worse on none", re-read every
# week with a promote button, called a winner in 39-42% of null readouts and
# in every null run peeked weekly. Now an improvement must survive Holm's
# step-down at FAMILY_ALPHA over every (arm, primary) tested, each p-value
# ALWAYS-VALID (the mixture SPRT, learning_stats.always_valid_p, tau = the
# primary's MEANINGFUL difference) so the weekly re-reading does not inflate
# it, and promotion needs the same call on CONFIRM_SNAPSHOTS consecutive
# stored weekly verdicts as well as today's readout. "Worse" stays the plain
# 90% interval: anything worse blocks a call.
FAMILY_ALPHA = 0.05
CONFIRM_SNAPSHOTS = 3
Z90 = 1.645
# Bounds on the readout, which runs on an admin request.
READOUT_MAX_RESTAURANTS = 500
READOUT_WEEKS = 52

RULE = (f"A winner is called only when every arm has at least {MIN_WEEKS_PER_ARM} published weeks from at least "
        f"{MIN_RESTAURANTS_PER_ARM} restaurants with a rework rate read, and on the primaries — the edits per 100 "
        "drafted rows, issues, labor %, labor against the hourly target, minutes with no manager and uncovered "
        "no-shows — the leader is better on at least one past a Holm step-down at "
        f"{int(FAMILY_ALPHA * 100)}% over every arm and primary, each read with an always-valid (sequential) "
        "p-value so a weekly re-read does not inflate it, and worse on none at 90%; it is promoted only after "
        f"{CONFIRM_SNAPSHOTS} consecutive weekly verdicts made the same call; acceptance is shown, never "
        "decisive. Each arm also says the smallest difference its sample can detect and the weeks it would take to "
        "see one worth seeing. Intervals are clustered by restaurant, "
        f"each restaurant counts at most {MAX_WEEKS_PER_RESTAURANT} weeks per arm, demo and test accounts are "
        "left out, and pinned weeks are not counted. Until "
        f"{MIN_RESTAURANTS_PER_ARM} restaurants that may teach generate schedules (the last {POWER_WINDOW_DAYS} "
        "days), an experiment that cannot conclude is paused: every week runs its stated arm, pinned. A "
        f"switchback never starts by itself: it runs only once an admin lists it in {SWITCHBACKS_ENV}.")


def experiment(key):
    return next((e for e in EXPERIMENTS if e["key"] == key), None)


def _arm_def(exp, arm_key):
    return next((a for a in exp["arms"] if a["key"] == arm_key), None)


def switchback_arm(exp, restaurant_id, week_start) -> str:
    """A switchback's arm: the weeks of each restaurant cycle through the
    arms by their weights in a fixed order — with weights 1 and 3, one week
    in four off — offset per restaurant by a stable hash, so the restaurants
    are not all off the same week. Deterministic: a regeneration of the same
    week keeps its arm."""
    import datetime as _dt
    try:
        d = _dt.date.fromisoformat(str(week_start)[:10])
    except (TypeError, ValueError):
        return exp["arms"][-1]["key"]
    cycle = [a["key"] for a in exp["arms"] for _ in range(max(0, int(a.get("weight", 1))))] or \
        [exp["arms"][-1]["key"]]
    offset = int(hashlib.sha256(f"{exp['key']}|{int(restaurant_id)}".encode()).hexdigest()[:8], 16)
    return cycle[(d.toordinal() // 7 + offset) % len(cycle)]


def hashed_arm(exp, restaurant_id, week_start) -> str:
    """The arm a week falls in: a stable hash of (experiment, restaurant,
    week start) onto the arms' weights — or, for a switchback design, the
    restaurant's own cycle (switchback_arm)."""
    if exp.get("design") == "switchback":
        return switchback_arm(exp, restaurant_id, week_start)
    digest = hashlib.sha256(f"{exp['key']}|{int(restaurant_id)}|{week_start or ''}".encode()).hexdigest()
    x = int(digest[:15], 16) / float(16 ** 15)
    total = float(sum(max(0, a.get("weight", 1)) for a in exp["arms"])) or 1.0
    acc = 0.0
    for a in exp["arms"]:
        acc += max(0, a.get("weight", 1)) / total
        if x < acc:
            return a["key"]
    return exp["arms"][-1]["key"]


def _env_pin(exp):
    raw = (os.environ.get(PIN_ENV) or "").strip()
    if not raw:
        return None
    if ":" in raw:
        key, arm = raw.split(":", 1)
        if key.strip() != exp["key"]:
            return None
        raw = arm.strip()
    if raw == OFF:
        return exp["control"]
    return raw if _arm_def(exp, raw) else None


def _restaurant_pins(restaurant_id, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT experiment, arm FROM schedule_experiment_pins WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    return {r["experiment"]: r["arm"] for r in rows}


def powered(db_path=DB_PATH) -> dict:
    """{"powered", "restaurants", "need", "window_days"}: whether enough
    restaurants generate schedules for an experiment to conclude — at least
    MIN_RESTAURANTS_PER_ARM that may teach (models.learning_eligible,
    through intelligence.jobs.excluded_learning_ids) with a schedule
    generated in the last POWER_WINDOW_DAYS. Raises when it cannot be read;
    arms_for then keeps the experiment's own arm, the safe reading."""
    excluded = _excluded(db_path)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT restaurant_id FROM schedule_history WHERE generated_at >= datetime('now', ?)",
                            (f"-{int(POWER_WINDOW_DAYS)} days",)).fetchall()
    finally:
        conn.close()
    n = len({int(r[0]) for r in rows} - set(excluded))
    return {"powered": n >= MIN_RESTAURANTS_PER_ARM, "restaurants": n, "need": MIN_RESTAURANTS_PER_ARM,
            "window_days": POWER_WINDOW_DAYS}


def _power_or_pin(db_path) -> dict:
    """powered(), or — when it cannot be read — not powered, said: the
    experiment's own arm is the safe reading, never a coin flip."""
    try:
        return powered(db_path)
    except Exception as e:
        _degraded(f"whether the experiment can conclude is unreadable: {e}", e)
        try:
            import ops
            ops.capture(e, job="schedule_experiment_power", context="arms_for")
        except Exception as _cx:
            _degraded(f"could not record that failure: {_cx}", _cx)
        return {"powered": False, "restaurants": None, "need": MIN_RESTAURANTS_PER_ARM,
                "window_days": POWER_WINDOW_DAYS, "unreadable": True}


def arms_for(restaurant_id, week_start, db_path=DB_PATH) -> list:
    """[{experiment, arm, pinned, pin_source, flags}] for every active
    experiment, for this restaurant's week — and, for an experiment retired
    in code whose winner was promoted, the promoted arm, so its flags keep
    holding. An experiment that cannot conclude yet gives every week its
    `until_powered` arm, pinned ("underpowered")."""
    out = []
    power = None
    try:
        pins = _restaurant_pins(restaurant_id, db_path)
    except Exception as e:           # a pin that cannot be read never breaks generation
        _degraded(f"pins unavailable for restaurant {restaurant_id}: {e}", e)
        pins = {}
    try:
        promoted = active_promotions(db_path)
    except Exception as e:           # nor does a promotion that cannot be read
        _degraded(f"promotions unavailable: {e}", e)
        promoted = {}
    for exp in EXPERIMENTS:
        if not exp.get("active"):
            continue
        arm, source = _env_pin(exp), "env"
        if arm is None and pins.get(exp["key"]):
            raw = pins[exp["key"]]
            arm = exp["control"] if raw == OFF else (raw if _arm_def(exp, raw) else None)
            source = "restaurant"
        if arm is None and exp["key"] in promoted and _arm_def(exp, promoted[exp["key"]]["arm"]):
            arm, source = promoted[exp["key"]]["arm"], "promoted"
        if arm is None and exp.get("until_powered") and _arm_def(exp, exp["until_powered"]) \
                and not switchback_started(exp):
            # A switchback no admin started (R5-02): every week its stated
            # arm, pinned and not counted — however many restaurants generate.
            arm, source = exp["until_powered"], "not_started"
        if arm is None and exp.get("until_powered") and _arm_def(exp, exp["until_powered"]):
            # Not enough restaurants for a verdict ever to arrive: no week is
            # spent on the coin flip, and none of them counts.
            if power is None:
                power = _power_or_pin(db_path)
            if not power.get("powered"):
                arm, source = exp["until_powered"], "underpowered"
        if arm is None:
            arm, source = hashed_arm(exp, restaurant_id, week_start), None
        out.append({"experiment": exp["key"], "arm": arm, "pinned": source is not None, "pin_source": source,
                    "flags": dict(_arm_def(exp, arm).get("flags") or {})})
    live = {e["key"] for e in EXPERIMENTS if e.get("active")}
    for key, p in promoted.items():
        if key in live:
            continue
        # A promotion outlives the experiment's code — and so do the kill
        # switches: the env pin, then this restaurant's own pin, still come
        # before it (the precedence above). A retired experiment's promoted
        # arm used to be applied over both (re-audit B19).
        exp = experiment(key)

        def usable(c):
            return c is not None and (c == OFF or c == p["arm"] or bool(exp and _arm_def(exp, c)))
        choice, source = _env_choice(key), "env"
        if not usable(choice):
            choice, source = pins.get(key), "restaurant"
        if not usable(choice) or choice == p["arm"]:
            out.append({"experiment": key, "arm": p["arm"], "pinned": True,
                        "pin_source": source if usable(choice) else "promoted", "flags": dict(p.get("flags") or {})})
            continue
        arm = (exp["control"] if exp else OFF) if choice == OFF else choice
        adef = _arm_def(exp, arm) if exp else None
        # An arm the code no longer defines (the control of an experiment
        # deleted from EXPERIMENTS) has no flags to give: the week takes the
        # defaults (DEFAULT_FLAGS) — never the promoted arm's.
        out.append({"experiment": key, "arm": arm, "pinned": True, "pin_source": source,
                    "flags": dict((adef or {}).get("flags") or {})})
    return out


def _env_choice(key):
    """The env kill switch's choice for experiment `key` ("off", an arm key)
    — for an experiment retired in code, where _env_pin (which validates
    against the live definition) cannot be used — or None."""
    raw = (os.environ.get(PIN_ENV) or "").strip()
    if not raw:
        return None
    if ":" in raw:
        k, arm = raw.split(":", 1)
        return (arm.strip() or None) if k.strip() == key else None
    return raw


def flag(arms, name, default=None) -> bool:
    """What the week's arms say about one flag; the default when none sets it."""
    for a in arms or []:
        if name in (a.get("flags") or {}):
            return bool(a["flags"][name])
    return bool(DEFAULT_FLAGS.get(name) if default is None else default)


def value(arms, name, default=None):
    """A flag's own value (draft_source names a source, not a yes/no); the
    default when no arm sets it."""
    for a in arms or []:
        if name in (a.get("flags") or {}):
            return a["flags"][name]
    return DEFAULT_FLAGS.get(name) if default is None else default


def record(restaurant_id, history_id, arms, quality_score=None, solver_applied=None, db_path=DB_PATH,
           conn=None) -> None:
    """The week's arm, stored with the draft's score. A regeneration is a
    new history row with the same arm. With `conn`, on the caller's
    connection inside its transaction (the generation's save) — the
    caller commits."""
    if not history_id or not arms:
        return
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        for a in arms:
            conn.execute(
                "INSERT INTO schedule_experiment_weeks (history_id, restaurant_id, experiment, arm, week_start, "
                "pinned, pin_source, quality_score, solver_applied) "
                "SELECT ?, ?, ?, ?, week_start, ?, ?, ?, ? FROM schedule_history WHERE id=? AND restaurant_id=? "
                "ON CONFLICT(history_id, experiment) DO UPDATE SET arm=excluded.arm, pinned=excluded.pinned, "
                "pin_source=excluded.pin_source, quality_score=excluded.quality_score, "
                "solver_applied=excluded.solver_applied",
                (int(history_id), int(restaurant_id), a["experiment"], a["arm"], 1 if a.get("pinned") else 0,
                 a.get("pin_source"), quality_score, None if solver_applied is None else (1 if solver_applied else 0),
                 int(history_id), int(restaurant_id)))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def set_pin(restaurant_id, experiment_key, arm, pinned_by=None, db_path=DB_PATH) -> dict:
    """Pin one restaurant to an arm ("off" = the control), or unpin with
    arm None. The kill switch for one restaurant."""
    exp = experiment(experiment_key)
    if exp is None:
        return {"ok": False, "error": "No such experiment."}
    if arm is not None and arm != OFF and not _arm_def(exp, arm):
        return {"ok": False, "error": "No such arm."}
    conn = get_conn(db_path)
    try:
        if arm is None:
            conn.execute("DELETE FROM schedule_experiment_pins WHERE restaurant_id=? AND experiment=?",
                         (int(restaurant_id), exp["key"]))
        else:
            conn.execute("INSERT INTO schedule_experiment_pins (restaurant_id, experiment, arm, pinned_by) VALUES (?,?,?,?) "
                         "ON CONFLICT(restaurant_id, experiment) DO UPDATE SET arm=excluded.arm, "
                         "pinned_by=excluded.pinned_by, created_at=datetime('now')",
                         (int(restaurant_id), exp["key"], arm, (pinned_by or "")[:80]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "restaurant_id": int(restaurant_id), "experiment": exp["key"], "arm": arm}


# ── promotion: the reviewed step from a winner to the default (ROI #46) ─────

def active_promotions(db_path=DB_PATH) -> dict:
    """{experiment: {id, arm, flags, promoted_by, promoted_at, verdict, note}}
    for every promotion in force (not reverted)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM schedule_experiment_promotions WHERE reverted_at IS NULL "
                            "ORDER BY id").fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            flags = json.loads(r["flags_json"] or "{}") or {}
        except (TypeError, ValueError):
            flags = {}
        out[r["experiment"]] = {"id": r["id"], "arm": r["arm"], "flags": flags, "promoted_by": r["promoted_by"],
                                "promoted_at": r["promoted_at"], "verdict": r["verdict_text"], "note": r["note"]}
    return out


def promotions(experiment_key=None, db_path=DB_PATH) -> list:
    """Every promotion, in force or reverted, newest first — the audit trail."""
    conn = get_conn(db_path)
    try:
        sql, args = "SELECT * FROM schedule_experiment_promotions", ()
        if experiment_key:
            sql, args = sql + " WHERE experiment=?", (experiment_key,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT 100", args).fetchall()]
    finally:
        conn.close()


def promote(experiment_key, arm, promoted_by=None, note=None, db_path=DB_PATH, _readout=None) -> dict:
    """Make `arm` every restaurant's arm for this experiment. Refused unless
    the readout's verdict right now calls exactly this arm (state "winner")
    and nothing is already promoted — the review is reading that verdict;
    this records who acted on it and when, with the verdict's words."""
    exp = experiment(experiment_key)
    if exp is None:
        return {"ok": False, "error": "No such experiment."}
    arm_def = _arm_def(exp, arm)
    if arm_def is None:
        return {"ok": False, "error": "No such arm."}
    if experiment_key in active_promotions(db_path):
        return {"ok": False, "error": "An arm is already promoted for this experiment — revert it first."}
    data = _readout or readout(db_path=db_path)
    row = next((e for e in data.get("experiments") or [] if e["key"] == experiment_key), None)
    verdict = (row or {}).get("verdict") or {}
    if verdict.get("state") != "winner" or verdict.get("call") != arm:
        return {"ok": False, "error": "Only the arm the readout calls the winner can be promoted "
                                      f"(now: {verdict.get('text') or 'no verdict'})."}
    # The same call on CONFIRM_SNAPSHOTS consecutive stored weekly verdicts:
    # one readout that happens to say "winner" is not enough.
    held = confirmed_weeks(experiment_key, arm, db_path=db_path)
    if held < CONFIRM_SNAPSHOTS:
        return {"ok": False, "error": (f"The readout has called {arm} on {held} consecutive weekly verdict"
                                       f"{'s' if held != 1 else ''}; promotion needs {CONFIRM_SNAPSHOTS}.")}
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO schedule_experiment_promotions (experiment, arm, flags_json, verdict_text, promoted_by, note) "
            "VALUES (?,?,?,?,?,?)",
            (exp["key"], arm, json.dumps(dict(arm_def.get("flags") or {})), (verdict.get("text") or "")[:400],
             (promoted_by or "")[:80] or None, (str(note or "")[:300] or None)))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return {"ok": True, "experiment": exp["key"], "arm": arm, "promotion_id": pid}


def confirmed_weeks(experiment_key, arm, db_path=DB_PATH) -> int:
    """How many consecutive ISO weeks, newest back, the stored weekly
    verdict (record_verdicts, VERDICT_METHOD) called `arm` the winner."""
    import datetime as _dt
    try:
        rows = verdicts(experiment_key, db_path=db_path, limit=12)
    except Exception:
        return 0
    n, prev = 0, None
    for v in rows:
        try:
            y, w = str(v["week"]).split("-W")
            monday = _dt.date.fromisocalendar(int(y), int(w), 1)
        except (TypeError, ValueError, KeyError):
            break
        if prev is not None and (prev - monday).days != 7:
            break
        if v.get("state") != "winner" or v.get("call") != arm or v.get("method") != VERDICT_METHOD:
            break
        n += 1
        prev = monday
    return n


def revert(experiment_key, reverted_by=None, db_path=DB_PATH) -> dict:
    """End the promotion in force: the experiment randomises again."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE schedule_experiment_promotions SET reverted_at=datetime('now'), reverted_by=? "
                         "WHERE experiment=? AND reverted_at IS NULL",
                         ((reverted_by or "")[:80] or None, experiment_key)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        return {"ok": False, "error": "Nothing is promoted for this experiment."}
    return {"ok": True, "experiment": experiment_key, "reverted": n}


# ── the readout (internal only) ────────────────────────────────────────────

def mean_ci(values, lo=None, hi=None) -> dict:
    """n, mean and a 90% normal interval (None under two values)."""
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0, "mean": None, "ci90": None, "sd": None}
    m = sum(vals) / n
    if n < 2:
        return {"n": n, "mean": round(m, 4), "ci90": None, "sd": None}
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    half = Z90 * sd / math.sqrt(n)
    a, b = m - half, m + half
    if lo is not None:
        a = max(lo, a)
    if hi is not None:
        b = min(hi, b)
    return {"n": n, "mean": round(m, 4), "ci90": (round(a, 4), round(b, 4)), "sd": round(sd, 4)}


def diff_ci(a: dict, b: dict):
    """Welch 90% interval for mean(a) - mean(b); None without spread on both."""
    if a.get("sd") is None or b.get("sd") is None or a["n"] < 2 or b["n"] < 2:
        return None
    d = a["mean"] - b["mean"]
    se = math.sqrt(a["sd"] ** 2 / a["n"] + b["sd"] ** 2 / b["n"])
    return {"diff": round(d, 4), "ci90": (round(d - Z90 * se, 4), round(d + Z90 * se, 4))}


def _t90(df):
    """The two-sided 90% t quantile on `df` degrees of freedom."""
    try:
        import metrics
        return float(metrics.t_ppf(0.95, max(1, int(df))))
    except Exception:
        return Z90


def cluster_mean_ci(rows, lo=None, hi=None) -> dict:
    """n, mean and a 90% interval over [(restaurant, value)] that treats a
    restaurant's weeks as one cluster: se² = G/(G−1) × Σ_r (Σ_i∈r (x − m)
    ÷ n)² (CR1), t on G − 1 degrees of freedom. `restaurants` is G; the
    interval is None under two restaurants. `sd` is the plain spread over
    weeks, kept for reading."""
    pairs = [(r, float(v)) for r, v in rows if v is not None]
    n = len(pairs)
    if not n:
        return {"n": 0, "mean": None, "ci90": None, "sd": None, "restaurants": 0}
    m = sum(v for _r, v in pairs) / n
    sd = math.sqrt(sum((v - m) ** 2 for _r, v in pairs) / (n - 1)) if n > 1 else None
    u = {}
    for r, v in pairs:
        u[r] = u.get(r, 0.0) + (v - m) / n
    g = len(u)
    out = {"n": n, "mean": round(m, 4), "ci90": None, "sd": round(sd, 4) if sd is not None else None,
           "restaurants": g}
    if g < 2:
        return out
    se = math.sqrt(g / (g - 1.0) * sum(x * x for x in u.values()))
    half = _t90(g - 1) * se
    a, b = m - half, m + half
    if lo is not None:
        a = max(lo, a)
    if hi is not None:
        b = min(hi, b)
    out["ci90"] = (round(a, 4), round(b, 4))
    out["se"] = round(se, 6)
    return out


def cluster_diff(a_rows, b_rows):
    """{diff, ci90, clusters} for mean(a) − mean(b) over [(restaurant,
    value)] with the restaurant-cluster sandwich: each restaurant's
    influence is its share of a's residuals minus its share of b's, so a
    restaurant with weeks in both arms carries its own covariance; t on
    (restaurants − 1) degrees of freedom. None under two values in either
    arm or two restaurants overall."""
    a = [(r, float(v)) for r, v in a_rows if v is not None]
    b = [(r, float(v)) for r, v in b_rows if v is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    ma = sum(v for _r, v in a) / len(a)
    mb = sum(v for _r, v in b) / len(b)
    u = {}
    for r, v in a:
        u[r] = u.get(r, 0.0) + (v - ma) / len(a)
    for r, v in b:
        u[r] = u.get(r, 0.0) - (v - mb) / len(b)
    g = len(u)
    if g < 2:
        return None
    se = math.sqrt(g / (g - 1.0) * sum(x * x for x in u.values()))
    d = ma - mb
    t = _t90(g - 1)
    return {"diff": round(d, 4), "ci90": (round(d - t * se, 4), round(d + t * se, 4)), "clusters": g,
            "se": round(se, 6)}


def _negate(d):
    if not d:
        return d
    return dict(d, diff=-d["diff"], ci90=(-d["ci90"][1], -d["ci90"][0]))


def _excludes_zero(ci) -> int:
    """+1 when the interval is above zero, -1 below, 0 when it spans it."""
    if not ci or not ci.get("ci90"):
        return 0
    lo, hi = ci["ci90"]
    return 1 if lo > 0 else -1 if hi < 0 else 0


def _outcomes(conn, history_ids) -> dict:
    """{history_id: {issues, labor_pct}} over the recorded dates of each week."""
    out = {}
    ids = list(history_ids)
    for k in range(0, len(ids), 400):
        chunk = ids[k:k + 400]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT history_id, date, SUM(issues) AS issues, MAX(labor_pct) AS labor_pct "
                              f"FROM schedule_outcomes WHERE history_id IN ({marks}) GROUP BY history_id, date",
                              tuple(chunk)).fetchall():
            e = out.setdefault(r["history_id"], {"issues": 0, "labor": []})
            e["issues"] += int(r["issues"] or 0)
            if r["labor_pct"] is not None:
                e["labor"].append(float(r["labor_pct"]))
    return {h: {"issues": e["issues"], "labor_pct": (sum(e["labor"]) / len(e["labor"])) if e["labor"] else None}
            for h, e in out.items()}


# The verdict's primaries: what the week DID and the rework the draft
# needed — every one lower is better — never the owner's acceptance alone
# (which reads a manager who stopped looking as improvement). Acceptance
# is reported beside them.
PRIMARIES = (("drr", "edits per 100 drafted rows"), ("issues", "issues per week"), ("labor_pct", "labor %"),
             ("labor_vs_target", "labor % against the hourly target"),
             ("unmanaged_minutes", "minutes with no manager"), ("uncovered_no_shows", "uncovered no-shows"))
# What counts as a difference worth detecting, per primary — the honest
# power read says how many weeks it would take to see one.
MEANINGFUL = {"drr": 5.0, "issues": 1.0, "labor_pct": 1.0, "labor_vs_target": 1.0, "unmanaged_minutes": 30.0,
              "uncovered_no_shows": 0.5}
Z80 = 0.8416


def power_read(a, n_weeks) -> dict:
    """{metric: {mde, meaningful, weeks_needed}} for one arm against the
    control: the smallest difference this sample could detect (90% two-
    sided, 80% power: (z.95 + z.80) × the clustered standard error) and,
    for the difference worth detecting, about how many weeks per arm that
    takes (the sample scales as (mde / meaningful)²)."""
    out = {}
    for key, _label in PRIMARIES:
        d = (a.get("vs_control") or {}).get(key)
        if not d or d.get("se") is None:
            out[key] = {"mde": None, "meaningful": MEANINGFUL.get(key), "weeks_needed": None}
            continue
        mde = (Z90 + Z80) * float(d["se"])
        need = None
        if MEANINGFUL.get(key) and n_weeks:
            need = int(math.ceil(n_weeks * (mde / MEANINGFUL[key]) ** 2))
        out[key] = {"mde": round(mde, 3), "meaningful": MEANINGFUL.get(key), "weeks_needed": need}
    return out


def _scored(db_path, history_ids) -> dict:
    """{history_id: {drr, labor_vs_target, unmanaged_minutes,
    uncovered_no_shows}} from the Schedule Learning Scorecard
    (schedule_learning_scores) — {} for a week not scored yet."""
    ids = sorted(int(h) for h in history_ids or [] if h)
    out = {}
    if not ids:
        return out
    conn = get_conn(db_path)
    try:
        for k in range(0, len(ids), 400):
            chunk = ids[k:k + 400]
            marks = ",".join("?" for _ in chunk)
            try:
                for r in conn.execute(f"SELECT history_id, drr, labor_vs_target, unmanaged_minutes, uncovered_no_shows "
                                      f"FROM schedule_learning_scores WHERE history_id IN ({marks})", tuple(chunk)):
                    out[r["history_id"]] = dict(r)
            except Exception:
                return {}
    finally:
        conn.close()
    return out


def verdict(exp, arms: list) -> dict:
    """Whether to call a winner, by RULE: on the PRIMARIES (the rework rate
    and the week's outcomes), never on acceptance alone. An arm beats the
    control when some primary is better past the Holm step-down over every
    (arm, primary) at FAMILY_ALPHA on always-valid p-values and none is
    worse at 90% (clustered); one better on some and worse on another is a
    conflict. The control is called only when every other arm is worse.
    `arms` are readout rows."""
    control = next((a for a in arms if a["arm"] == exp["control"]), None)

    def n_of(a):
        return (a.get("drr") or {}).get("n", 0)
    short = [a for a in arms if n_of(a) < MIN_WEEKS_PER_ARM or a["restaurants"] < MIN_RESTAURANTS_PER_ARM]
    if short or control is None:
        need = ", ".join(f"{a['label']} {n_of(a)}/{MIN_WEEKS_PER_ARM} weeks from "
                         f"{a['restaurants']}/{MIN_RESTAURANTS_PER_ARM} restaurants" for a in arms)
        return {"call": None, "state": "insufficient",
                "text": f"No call: not enough published weeks yet ({need})."}
    import learning_stats as _ls
    # Every difference tested, both ways, one family.
    family = {}
    for a in arms:
        if a is control:
            continue
        for key, _label in PRIMARIES:
            d = (a.get("vs_control") or {}).get(key)
            if d and d.get("diff") is not None and d.get("se"):
                family[(a["arm"], key)] = _ls.always_valid_p(d["diff"], d["se"], MEANINGFUL.get(key) or 1.0)
    survivors = _ls.holm(family, FAMILY_ALPHA) if family else set()
    beats, lost, conflicts = [], [], []
    for a in arms:
        if a is control:
            continue
        better, worse, better_any, worse_any = [], [], [], []
        for key, label in PRIMARIES:
            sign = _excludes_zero((a.get("vs_control") or {}).get(key))
            sure = (a["arm"], key) in survivors
            if sign < 0:
                better_any.append(label)
                if sure:
                    better.append(label)
            elif sign > 0:
                worse_any.append(label)
                if sure:
                    worse.append(label)
        # A call needs a sure difference one way and nothing at a plain 90%
        # the other way (anything worse blocks a winner).
        if (better and worse_any) or (worse and better_any):
            conflicts.append(f"{a['label']} has fewer {' and '.join(better_any)} but more {' and '.join(worse_any)}")
        elif better:
            beats.append((a, better))
        elif worse:
            lost.append((a, worse))
    if conflicts:
        return {"call": None, "state": "conflicting", "text": "No call: " + "; ".join(conflicts) + "."}
    if beats:
        best, why = min(beats, key=lambda t: (t[0].get("drr") or {}).get("mean") or 0)
        return {"call": best["arm"], "state": "winner",
                "text": f"{best['label']} leads: fewer {' and '.join(why)} (Holm-adjusted, always-valid), and no "
                        "outcome worse."}
    if lost and len(lost) == len(arms) - 1:
        why = sorted({w for _a, ws in lost for w in ws})
        return {"call": control["arm"], "state": "winner",
                "text": f"{control['label']} leads: the other arm{'s have' if len(lost) > 1 else ' has'} more "
                        f"{' and '.join(why)} (Holm-adjusted, always-valid)."}
    return {"call": None, "state": "no_difference",
            "text": "No sure difference in the rework or the outcomes (Holm-adjusted, always-valid)."}


def _excluded(db_path):
    """Restaurants that may not teach any learner (intelligence.jobs.
    excluded_learning_ids — models.learning_eligible and the demo
    quarantine). Raises when unreadable: an unreadable set excluded none, so
    a test account's weeks joined a verdict. The weekly verdict job fails
    and is captured."""
    from intelligence.jobs import excluded_learning_ids
    return set(excluded_learning_ids(db_path=db_path))


def _learning_since(db_path):
    """{restaurant_id: learning_since} — a converted demo's weeks before it
    are its demo era and read in no arm. Raises when unreadable, like
    _excluded."""
    from intelligence.jobs import learning_since_by_id
    return learning_since_by_id(db_path=db_path)


def _before_learning(restaurant_id, week_start, since):
    if not since:
        return False
    from intelligence.jobs import before_learning
    return before_learning(restaurant_id, week_start, since)


def _sample_order(restaurant_id):
    """A restaurant's place in the readout's sample: a stable hash of its id
    (the same restaurants every week, spread across sign-up dates)."""
    import hashlib
    return hashlib.sha256(f"experiment-readout:{int(restaurant_id)}".encode()).hexdigest()


def _capped(rows, per=MAX_WEEKS_PER_RESTAURANT):
    """Each restaurant's `per` most recent weeks (by week start, then id)."""
    by = {}
    for r in rows:
        by.setdefault(r["restaurant_id"], []).append(r)
    out = []
    for rs in by.values():
        rs.sort(key=lambda r: (str(r.get("week_start") or ""), r["history_id"]), reverse=True)
        out.extend(rs[:per])
    return out


def readout(db_path=DB_PATH) -> dict:
    """Per experiment and arm: weeks generated, acceptance with a 90%
    interval, outcomes, the published draft's Shift Quality, and the verdict
    — every interval clustered by restaurant, at most MAX_WEEKS_PER_RESTAURANT
    published weeks per restaurant per arm, eligible restaurants only."""
    import schedule_versions as sv
    excluded = _excluded(db_path)
    since = _learning_since(db_path)
    conn = get_conn(db_path)
    try:
        # Eligibility and the demo-era floor in SQL, BEFORE the LIMIT: demo
        # and test churn filled the 20,000-row window before the filter ran,
        # so eligible restaurants' older weeks fell out. The Python check
        # below stays as the same rule's second reading.
        where = []
        if excluded:
            where.append(f"restaurant_id NOT IN ({','.join(str(int(i)) for i in sorted(excluded))})")
        try:
            import models as _m_rows
            where.append(_m_rows.learning_rows_sql("schedule_experiment_weeks.restaurant_id",
                                                   "schedule_experiment_weeks.week_start"))
        except Exception as e:
            _degraded(f"demo-era floor not applied in SQL: {e}", e)
        rows = conn.execute("SELECT history_id, restaurant_id, experiment, arm, week_start, pinned, quality_score, "
                            "solver_applied FROM schedule_experiment_weeks "
                            + (f"WHERE {' AND '.join(where)} " if where else "")
                            + "ORDER BY history_id DESC LIMIT 20000").fetchall()
        rows = [dict(r) for r in rows if r["restaurant_id"] not in excluded
                and not _before_learning(r["restaurant_id"], r["week_start"], since)]
        # Past READOUT_MAX_RESTAURANTS, a stable hash chooses the sample —
        # never the lowest ids, which read only the earliest customers.
        rids = sorted({r["restaurant_id"] for r in rows if not r["pinned"]},
                      key=_sample_order)[:READOUT_MAX_RESTAURANTS]
        in_scope = set(rids)
        pins = [dict(r) for r in conn.execute(
            "SELECT p.restaurant_id, p.experiment, p.arm, p.pinned_by, p.created_at, r.name AS restaurant "
            "FROM schedule_experiment_pins p LEFT JOIN restaurants r ON r.id=p.restaurant_id "
            "ORDER BY p.created_at DESC LIMIT 200").fetchall()]
        outcomes = _outcomes(conn, {r["history_id"] for r in rows if not r["pinned"]})
    finally:
        conn.close()
    # A week's acceptance is a person's verdict on the draft: a week the
    # automatic publish sent with nobody looking (or only an admin's hand)
    # has no share and is left out of the acceptance series — its outcomes
    # and quality still count for its arm.
    accepted, published = {}, set()
    # The primaries: each looked-at week's Draft Rework Rate (the
    # scorecard's, or read from the week now), and what the scorecard
    # measured of the week as it ran.
    scored = _scored(db_path, {r["history_id"] for r in rows if not r["pinned"]})
    rework = {h: s["drr"] for h, s in scored.items() if s.get("drr") is not None}
    for rid in rids:
        try:
            weeks_ = sv.acceptance(rid, weeks=READOUT_WEEKS, db_path=db_path).get("weeks") or []
            for w in weeks_:
                published.add(w["history_id"])
                if w.get("unchanged_share") is not None:
                    accepted[w["history_id"]] = w["unchanged_share"]
            need = [w["history_id"] for w in weeks_ if w.get("looked_at") and w["history_id"] not in rework]
            if need:
                for rec in sv.learning_weeks(rid, history_ids=need, include_unlooked=True, db_path=db_path):
                    d = drr(rec)
                    if d.get("rate") is not None:
                        rework[rec["history_id"]] = d["rate"]
        except Exception as e:
            _degraded(f"acceptance unavailable for restaurant {rid}: {e}", e)
    exps = []
    keys = [e["key"] for e in EXPERIMENTS] + sorted({r["experiment"] for r in rows} - {e["key"] for e in EXPERIMENTS})
    for key in keys:
        exp = experiment(key) or {"key": key, "active": False, "control": None, "question": "", "started": None,
                                  "arms": tuple({"key": a, "label": a} for a in sorted({r["arm"] for r in rows
                                                                                       if r["experiment"] == key}))}
        mine = [r for r in rows if r["experiment"] == key]
        arms = []
        series = {}
        for a in exp["arms"]:
            live = [r for r in mine if r["arm"] == a["key"] and not r["pinned"] and r["restaurant_id"] in in_scope]
            # The published version of each week (schedule_versions.acceptance
            # picks the latest publish per calendar week), capped per
            # restaurant: its acceptance, its outcomes and the quality it
            # was generated with — never every regeneration's.
            pub = _capped([r for r in live if r["history_id"] in published])
            # The acceptance series is capped on its own weeks: a restaurant's
            # newest weeks sent unattended must not crowd out its looked-at ones.
            looked = _capped([r for r in live if r["history_id"] in accepted])
            outs = [(r["restaurant_id"], outcomes[r["history_id"]]) for r in pub if r["history_id"] in outcomes]
            reworked = _capped([r for r in live if r["history_id"] in rework])
            series[a["key"]] = {
                "acceptance": [(r["restaurant_id"], accepted[r["history_id"]]) for r in looked],
                "quality": [(r["restaurant_id"], r["quality_score"]) for r in pub],
                "issues": [(rid, o["issues"]) for rid, o in outs],
                "labor_pct": [(rid, o["labor_pct"]) for rid, o in outs],
                "drr": [(r["restaurant_id"], rework[r["history_id"]]) for r in reworked]}
            # `metric`, never `key`: the experiment's own key is read after
            # this loop (it named every experiment "uncovered_no_shows").
            for metric in ("labor_vs_target", "unmanaged_minutes", "uncovered_no_shows"):
                series[a["key"]][metric] = [(r["restaurant_id"], scored[r["history_id"]][metric]) for r in pub
                                            if (scored.get(r["history_id"]) or {}).get(metric) is not None]
            arms.append({
                "arm": a["key"], "label": a.get("label") or a["key"], "flags": dict(a.get("flags") or {}),
                "generated": len(live), "pinned": sum(1 for r in mine if r["arm"] == a["key"] and r["pinned"]),
                # The restaurants a verdict rests on: those with a week
                # somebody looked at and a rework rate read.
                "restaurants": len({r["restaurant_id"] for r in reworked}),
                "acceptance": cluster_mean_ci(series[a["key"]]["acceptance"], 0.0, 1.0),
                "quality": cluster_mean_ci(series[a["key"]]["quality"], 0.0, 100.0),
                "issues": cluster_mean_ci(series[a["key"]]["issues"], 0.0),
                "labor_pct": cluster_mean_ci(series[a["key"]]["labor_pct"], 0.0),
                "drr": cluster_mean_ci(series[a["key"]]["drr"], 0.0),
                "labor_vs_target": cluster_mean_ci(series[a["key"]]["labor_vs_target"]),
                "unmanaged_minutes": cluster_mean_ci(series[a["key"]]["unmanaged_minutes"], 0.0),
                "uncovered_no_shows": cluster_mean_ci(series[a["key"]]["uncovered_no_shows"], 0.0),
                "solver_applied": sum(1 for r in live if r.get("solver_applied")),
            })
        control = next((a for a in arms if a["arm"] == exp.get("control")), None)
        for a in arms:
            a["vs_control"] = ({k: cluster_diff(series[a["arm"]][k], series[control["arm"]][k])
                                for k in ("acceptance", "quality", "issues", "labor_pct", "drr", "labor_vs_target",
                                          "unmanaged_minutes", "uncovered_no_shows")}
                               if control is not None and a is not control else None)
            # Honest powering: the smallest difference this sample can
            # see on each primary, and the weeks it would take to see the
            # one worth seeing.
            a["power_read"] = power_read(a, (a.get("drr") or {}).get("n")) if a.get("vs_control") else None
        call = verdict(exp, arms) if exp.get("control") else \
            {"call": None, "state": "retired", "text": "Retired experiment."}
        power = None
        if exp.get("active") and exp.get("until_powered"):
            power = _power_or_pin(db_path)
            if not switchback_started(exp) and call.get("state") != "winner":
                label = (_arm_def(exp, exp["until_powered"]) or {}).get("label") or exp["until_powered"]
                call = {"call": None, "state": "paused",
                        "text": (f"Not started: every restaurant runs {label} until an admin lists "
                                 f"{key} in {SWITCHBACKS_ENV}; those weeks are pinned and not counted.")}
            elif not power.get("powered") and call.get("state") != "winner":
                label = (_arm_def(exp, exp["until_powered"]) or {}).get("label") or exp["until_powered"]
                have = power.get("restaurants")
                call = {"call": None, "state": "paused",
                        "text": (f"Paused: every restaurant runs {label} until {power['need']} restaurants generate "
                                 f"schedules (now {have if have is not None else 'unknown'}); those weeks are pinned "
                                 "and not counted.")}
        exps.append({"key": key, "active": bool(exp.get("active")), "question": exp.get("question") or "",
                     "started": exp.get("started"), "control": exp.get("control"), "arms": arms,
                     "method": VERDICT_METHOD, "power": power, "verdict": call})
    env = (os.environ.get(PIN_ENV) or "").strip() or None
    try:
        promoted = active_promotions(db_path)
        history = promotions(db_path=db_path)
    except Exception as e:           # the promotions table predates this database
        _degraded(f"promotions unavailable: {e}", e)
        promoted, history = {}, []
    for e in exps:
        e["promotion"] = promoted.get(e["key"])
        e["promotions"] = [h for h in history if h["experiment"] == e["key"]][:10]
        # What the readout said, week by week (the stored verdicts).
        try:
            e["verdict_history"] = [{k: v.get(k) for k in ("week", "state", "call", "text", "method")}
                                    for v in verdicts(e["key"], db_path=db_path, limit=12)]
        except Exception as ex:      # a database from before the verdicts table
            _degraded(f"verdict history unavailable: {ex}", ex)
            e["verdict_history"] = []
    return {"ok": True, "experiments": exps, "pins": pins, "env_pin": env, "rule": RULE,
            "min_weeks": MIN_WEEKS_PER_ARM, "min_restaurants": MIN_RESTAURANTS_PER_ARM,
            "max_weeks_per_restaurant": MAX_WEEKS_PER_RESTAURANT, "method": VERDICT_METHOD}


def record_verdicts(db_path=DB_PATH, today=None) -> dict:
    """The weekly verdict snapshot: each experiment's verdict and per-arm
    summary as the readout gives it this ISO week, stored in
    schedule_experiment_verdicts (one row per experiment and week — the
    first of the week stands, like a band). Returns the standard counts."""
    import datetime as _dt
    today = today or _dt.date.today()
    y, w, _ = today.isocalendar()
    week = f"{y}-W{w:02d}"
    data = readout(db_path=db_path)
    written = 0
    conn = get_conn(db_path)
    try:
        for e in data.get("experiments") or []:
            v = e.get("verdict") or {}
            arms = [{k: a.get(k) for k in ("arm", "generated", "restaurants", "acceptance", "quality", "issues",
                                           "labor_pct", "vs_control")} for a in e.get("arms") or []]
            cur = conn.execute(
                "INSERT OR IGNORE INTO schedule_experiment_verdicts (week, experiment, state, call, text, method, "
                "arms_json) VALUES (?,?,?,?,?,?,?)",
                (week, e["key"], v.get("state") or "unknown", v.get("call"), (v.get("text") or "")[:400],
                 e.get("method") or VERDICT_METHOD, json.dumps(arms, default=str)))
            written += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    n = len(data.get("experiments") or [])
    return {"attempted": n, "ok": n, "failed": 0, "skipped": n - written, "hit_bound": False,
            "written": written, "week": week}


def verdicts(experiment_key=None, db_path=DB_PATH, limit=60) -> list:
    """The stored weekly verdicts, newest first — what the readout said each
    week."""
    conn = get_conn(db_path)
    try:
        sql, args = "SELECT * FROM schedule_experiment_verdicts", ()
        if experiment_key:
            sql, args = sql + " WHERE experiment=?", (experiment_key,)
        rows = conn.execute(sql + " ORDER BY week DESC LIMIT ?", (*args, int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["arms"] = json.loads(d.pop("arms_json") or "[]")
        except ValueError:
            d["arms"] = []
        out.append(d)
    return out


def init_schedule_experiments(db_path: str = DB_PATH):
    """Tables for the week's arm and the per-restaurant pins — created at
    boot (models.init_db), never on a request."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_experiment_weeks (
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        experiment     TEXT    NOT NULL,
        arm            TEXT    NOT NULL,
        week_start     TEXT,
        pinned         INTEGER NOT NULL DEFAULT 0,
        pin_source     TEXT,
        quality_score  INTEGER,
        solver_applied INTEGER,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (history_id, experiment)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_exp_weeks ON schedule_experiment_weeks(experiment, restaurant_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_experiment_pins (
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        experiment     TEXT    NOT NULL,
        arm            TEXT    NOT NULL,
        pinned_by      TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, experiment)
    )""")
    # A winning arm made the default by a reviewed admin step (ROI #46):
    # one row per promotion, kept after a revert as the audit trail.
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_experiment_promotions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment     TEXT    NOT NULL,
        arm            TEXT    NOT NULL,
        flags_json     TEXT    NOT NULL,
        verdict_text   TEXT,
        note           TEXT,
        promoted_by    TEXT,
        promoted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        reverted_by    TEXT,
        reverted_at    TEXT
    )""")
    import models as _models_ix   # duplicates refuse a unique index: recorded, the boot goes on (A05-15)
    _models_ix.create_unique_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS uq_sched_exp_promotion_live ON "
                                         "schedule_experiment_promotions(experiment) WHERE reverted_at IS NULL")
    # What the readout said, week by week: one row per experiment and ISO
    # week, the first of the week standing. Kept forever (a few rows a year
    # per experiment).
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_experiment_verdicts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        week           TEXT    NOT NULL,
        experiment     TEXT    NOT NULL,
        state          TEXT    NOT NULL,
        call           TEXT,
        text           TEXT,
        method         TEXT,
        arms_json      TEXT    NOT NULL,
        computed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(week, experiment)
    )""")
    conn.commit()
    conn.close()


EDIT_WEIGHTS = {"added": 1.0, "removed": 1.0, "moved": 1.0, "role_changed": 0.5}


RETIME_WEIGHT_PER_30 = 0.25


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
