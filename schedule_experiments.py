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
one only when the 90% interval for the difference in acceptance excludes
zero and no outcome is significantly worse for the leader.

Kill switches: the env var SCHEDULE_EXPERIMENT_PIN ("off", an arm key, or
"experiment:arm") pins every restaurant; a row in schedule_experiment_pins
pins one. A pinned week records the arm it was pinned to and is left out of
the comparison, since it was not randomised. "off" is the control arm.
"""
import hashlib
import math
import os

from models import DB_PATH


def get_conn(*args, **kwargs):
    """Resolved through models at call time, so a patched models.get_conn
    (tests, the restore drill) reaches this module too."""
    import models
    return models.get_conn(*args, **kwargs)


EXPERIMENTS = (
    {"key": "assign_v1",
     "active": True,
     "started": "2026-09-23",
     "question": "Does the constraint solver's assignment beat the model's own?",
     "control": "model",
     "arms": ({"key": "model", "label": "Model assignment", "weight": 1, "flags": {"solver": False}},
              {"key": "solver", "label": "Solver assignment", "weight": 1, "flags": {"solver": True}})},
)
# What a week gets from a flag no active experiment sets.
DEFAULT_FLAGS = {"solver": True}
PIN_ENV = "SCHEDULE_EXPERIMENT_PIN"
OFF = "off"

MIN_WEEKS_PER_ARM = 20
MIN_RESTAURANTS_PER_ARM = 5
Z90 = 1.645
# Bounds on the readout, which runs on an admin request.
READOUT_MAX_RESTAURANTS = 500
READOUT_WEEKS = 52

RULE = (f"A winner is called only when every arm has at least {MIN_WEEKS_PER_ARM} published weeks from at least "
        f"{MIN_RESTAURANTS_PER_ARM} restaurants, the 90% interval for the difference in acceptance excludes zero, "
        "and the leader's issues and labor % are not significantly worse. Pinned weeks are not counted.")


def experiment(key):
    return next((e for e in EXPERIMENTS if e["key"] == key), None)


def _arm_def(exp, arm_key):
    return next((a for a in exp["arms"] if a["key"] == arm_key), None)


def hashed_arm(exp, restaurant_id, week_start) -> str:
    """The arm a week falls in: a stable hash of (experiment, restaurant,
    week start) onto the arms' weights."""
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


def arms_for(restaurant_id, week_start, db_path=DB_PATH) -> list:
    """[{experiment, arm, pinned, pin_source, flags}] for every active
    experiment, for this restaurant's week."""
    out = []
    try:
        pins = _restaurant_pins(restaurant_id, db_path)
    except Exception as e:           # a pin that cannot be read never breaks generation
        print(f"[experiments] pins unavailable for restaurant {restaurant_id}: {e}")
        pins = {}
    for exp in EXPERIMENTS:
        if not exp.get("active"):
            continue
        arm, source = _env_pin(exp), "env"
        if arm is None and pins.get(exp["key"]):
            raw = pins[exp["key"]]
            arm = exp["control"] if raw == OFF else (raw if _arm_def(exp, raw) else None)
            source = "restaurant"
        if arm is None:
            arm, source = hashed_arm(exp, restaurant_id, week_start), None
        out.append({"experiment": exp["key"], "arm": arm, "pinned": source is not None, "pin_source": source,
                    "flags": dict(_arm_def(exp, arm).get("flags") or {})})
    return out


def flag(arms, name, default=None) -> bool:
    """What the week's arms say about one flag; the default when none sets it."""
    for a in arms or []:
        if name in (a.get("flags") or {}):
            return bool(a["flags"][name])
    return bool(DEFAULT_FLAGS.get(name) if default is None else default)


def record(restaurant_id, history_id, arms, quality_score=None, solver_applied=None, db_path=DB_PATH) -> None:
    """The week's arm, stored with the draft's score. A regeneration is a
    new history row with the same arm."""
    if not history_id or not arms:
        return
    conn = get_conn(db_path)
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
        conn.commit()
    finally:
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


def verdict(exp, arms: list) -> dict:
    """Whether to call a winner, by RULE. `arms` are readout rows."""
    control = next((a for a in arms if a["arm"] == exp["control"]), None)
    short = [a for a in arms if a["acceptance"]["n"] < MIN_WEEKS_PER_ARM or a["restaurants"] < MIN_RESTAURANTS_PER_ARM]
    if short or control is None:
        need = ", ".join(f"{a['label']} {a['acceptance']['n']}/{MIN_WEEKS_PER_ARM} weeks from "
                         f"{a['restaurants']}/{MIN_RESTAURANTS_PER_ARM} restaurants" for a in arms)
        return {"call": None, "state": "insufficient",
                "text": f"No call: not enough published weeks yet ({need})."}
    best, conflicts = None, []
    for a in arms:
        if a is control:
            continue
        sign = _excludes_zero(a["vs_control"]["acceptance"])
        if sign == 0:
            continue
        leader = a if sign > 0 else control
        other = control if sign > 0 else a
        worse = []
        for key, label in (("issues", "issues per week"), ("labor_pct", "labor %")):
            d = diff_ci(leader[key], other[key])
            if _excludes_zero(d) > 0:
                worse.append(label)
        if worse:
            conflicts.append(f"{leader['label']} is accepted more but its {' and '.join(worse)} "
                             f"{'is' if len(worse) == 1 else 'are'} worse")
            continue
        if best is None or leader["acceptance"]["mean"] > best["acceptance"]["mean"]:
            best = leader
    if conflicts:
        return {"call": None, "state": "conflicting", "text": "No call: " + "; ".join(conflicts) + "."}
    if best is None:
        return {"call": None, "state": "no_difference",
                "text": "No detectable difference in acceptance at 90%."}
    return {"call": best["arm"], "state": "winner",
            "text": f"{best['label']} leads on acceptance with no outcome worse at 90%."}


def readout(db_path=DB_PATH) -> dict:
    """Per experiment and arm: weeks generated, acceptance with a 90%
    interval, outcomes, the draft's Shift Quality, and the verdict."""
    import schedule_versions as sv
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT history_id, restaurant_id, experiment, arm, week_start, pinned, quality_score, "
                            "solver_applied FROM schedule_experiment_weeks ORDER BY history_id DESC LIMIT 20000").fetchall()
        rows = [dict(r) for r in rows]
        rids = sorted({r["restaurant_id"] for r in rows if not r["pinned"]})[:READOUT_MAX_RESTAURANTS]
        in_scope = set(rids)
        pins = [dict(r) for r in conn.execute(
            "SELECT p.restaurant_id, p.experiment, p.arm, p.pinned_by, p.created_at, r.name AS restaurant "
            "FROM schedule_experiment_pins p LEFT JOIN restaurants r ON r.id=p.restaurant_id "
            "ORDER BY p.created_at DESC LIMIT 200").fetchall()]
        outcomes = _outcomes(conn, {r["history_id"] for r in rows if not r["pinned"]})
    finally:
        conn.close()
    accepted = {}
    for rid in rids:
        try:
            for w in (sv.acceptance(rid, weeks=READOUT_WEEKS, db_path=db_path).get("weeks") or []):
                if w.get("unchanged_share") is not None:
                    accepted[w["history_id"]] = w["unchanged_share"]
        except Exception as e:
            print(f"[experiments] acceptance unavailable for restaurant {rid}: {e}")
    exps = []
    keys = [e["key"] for e in EXPERIMENTS] + sorted({r["experiment"] for r in rows} - {e["key"] for e in EXPERIMENTS})
    for key in keys:
        exp = experiment(key) or {"key": key, "active": False, "control": None, "question": "", "started": None,
                                  "arms": tuple({"key": a, "label": a} for a in sorted({r["arm"] for r in rows
                                                                                       if r["experiment"] == key}))}
        mine = [r for r in rows if r["experiment"] == key]
        arms = []
        for a in exp["arms"]:
            live = [r for r in mine if r["arm"] == a["key"] and not r["pinned"] and r["restaurant_id"] in in_scope]
            pub = [r for r in live if r["history_id"] in accepted]
            outs = [outcomes[r["history_id"]] for r in live if r["history_id"] in outcomes]
            arms.append({
                "arm": a["key"], "label": a.get("label") or a["key"], "flags": dict(a.get("flags") or {}),
                "generated": len(live), "pinned": sum(1 for r in mine if r["arm"] == a["key"] and r["pinned"]),
                "restaurants": len({r["restaurant_id"] for r in pub}),
                "acceptance": mean_ci([accepted[r["history_id"]] for r in pub], 0.0, 1.0),
                "quality": mean_ci([r["quality_score"] for r in live], 0.0, 100.0),
                "issues": mean_ci([o["issues"] for o in outs], 0.0),
                "labor_pct": mean_ci([o["labor_pct"] for o in outs], 0.0),
                "solver_applied": sum(1 for r in live if r.get("solver_applied")),
            })
        control = next((a for a in arms if a["arm"] == exp.get("control")), None)
        for a in arms:
            a["vs_control"] = ({k: diff_ci(a[k], control[k]) for k in ("acceptance", "quality", "issues", "labor_pct")}
                               if control is not None and a is not control else None)
        exps.append({"key": key, "active": bool(exp.get("active")), "question": exp.get("question") or "",
                     "started": exp.get("started"), "control": exp.get("control"), "arms": arms,
                     "verdict": verdict(exp, arms) if exp.get("control") else
                     {"call": None, "state": "retired", "text": "Retired experiment."}})
    env = (os.environ.get(PIN_ENV) or "").strip() or None
    return {"ok": True, "experiments": exps, "pins": pins, "env_pin": env, "rule": RULE,
            "min_weeks": MIN_WEEKS_PER_ARM, "min_restaurants": MIN_RESTAURANTS_PER_ARM}


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
    conn.commit()
    conn.close()
