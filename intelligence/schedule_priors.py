"""Cross-restaurant priors for the scheduler (scheduler audit 10/10/26
LEARNING-27).

Every scheduling estimate was per restaurant: a new restaurant relearned
from zero for months — its first Cubs game, first snow, first Christmas,
its labor standards and its no-show base rate all started uninformed. Only
a starting headcount was ever borrowed (intelligence.staffing).

Level 1, nightly (features.compute → compute_features): each restaurant's
own MEASURED figures as ratios — no names, no dollars:

  sched_prior.std.<family>.<part>   work one person carries at the rush
                                    (staffing_model's own median, guests or
                                    tickets per person-hour)
  sched_prior.wx.<part>.<cond>      a weather bucket's lift as a multiplier
                                    (demand_drivers.weather_effects: rain,
                                    snow, cold, hot per daypart; 1.0 = none)
  sched_prior.noshow                the restaurant's own weighted no-show
                                    base rate (staff_settings.attendance_bases)
  sched_prior.event.<kind>          the median measured lift of its events
                                    of a kind (event_memory's summaries:
                                    holiday, event), a multiplier

Level 3, nightly (benchmarks.compute): banded per peer partition (the
staff family's ladder), behind privacy.MIN_COHORT and MIN_ORGS, the
viewer's own organisation always out (benchmarks.published).

On request: the published p50 for this restaurant — the prior its own
estimate is shrunk toward (staffing_model's standard, demand_drivers'
weather, attendance_risk's no-show base, demand_drivers' role lifts), each
in proportion to how little of its own record there is. Below the floor:
None, and each estimate stands on its own record alone. Every payload
passes privacy.assert_anonymous.
"""
from datetime import date

from models import DB_PATH
from . import privacy, categories

PREFIX = "sched_prior."
PARTS = ("morning", "night")
WEATHER_CONDS = ("rain", "snow", "cold", "hot")


def get_conn(db_path=None):
    import models as _m
    if db_path is None or db_path == DB_PATH:
        return _m.get_conn()
    return _m.get_conn(db_path)


# ── Level 1: this restaurant's own figures ────────────────────────────────

def compute_features(restaurant_id: int, today: date = None, db_path: str = DB_PATH) -> dict:
    """{sched_prior.*: ratio} for this restaurant (see the module note); a
    figure it has not measured is absent, never 0. Never raises."""
    today = today or date.today()
    out = {}
    try:
        import staffing_model as _sm
        model = _sm.measure(restaurant_id, db_path=db_path, today=today, prior={}, overrides={})
        for fam, e in ((model or {}).get("families") or {}).items():
            for part, pe in (e.get("parts") or {}).items():
                if pe.get("source") == "measured" and pe.get("own") and int(pe.get("n") or 0) >= _sm.STD_MIN_DAYS:
                    out[f"{PREFIX}std.{fam}.{part}"] = round(float(pe["own"]), 2)
    except Exception as e:
        print(f"[schedule_priors] standards unavailable for {restaurant_id}: {e}")
    try:
        import demand_drivers as _dd
        eff = _dd.weather_effects(restaurant_id, db_path=db_path, today=today)
        for part, conds in (eff or {}).items():
            for c, s in (conds or {}).items():
                if int(s.get("n") or 0) >= _dd.EFFECT_MIN_N:
                    out[f"{PREFIX}wx.{part}.{c}"] = round(1.0 + float(s["lift_pct"]) / 100.0, 3)
    except Exception as e:
        print(f"[schedule_priors] weather unavailable for {restaurant_id}: {e}")
    try:
        import staff_settings as _ss
        rec = _ss.attendance_record(restaurant_id, today=today, db_path=db_path)
        shifts = sum(int(t.get("shifts") or 0) for t in (rec.get("tally") or {}).values())
        if shifts >= 50:
            out[f"{PREFIX}noshow"] = round(float((rec.get("base") or {}).get("miss") or 0.0), 3)
    except Exception as e:
        print(f"[schedule_priors] no-show base unavailable for {restaurant_id}: {e}")
    try:
        import event_memory as _em
        from datetime import timedelta as _td
        # The summaries are current state (event_effects): a past week's
        # backfill never reads them — nothing after `today` (PLATFORM-5).
        current = today >= date.today() - _td(days=7)
        by_kind = {}
        for s in (_em.summaries(restaurant_id, limit=200, db_path=None if db_path == DB_PATH else db_path)
                  if current else []):
            if s.get("applies") and s.get("kind") in ("holiday", "event"):
                by_kind.setdefault(s["kind"], []).append(1.0 + float(s.get("median_lift_pct") or 0) / 100.0)
        for kind, vals in by_kind.items():
            vals.sort()
            out[f"{PREFIX}event.{kind}"] = round(vals[len(vals) // 2], 3)
        # How each role family moves on event nights against the night's
        # sales lift (demand_drivers.role_event_lifts' raw ratio over 1 +
        # the lift): the bar ahead of it, the dish pit behind it — the
        # cohort prior a role's own lift is shrunk toward (LEARNING-20).
        if current:
            import demand_drivers as _dd
            from .staffing import family_of
            rel = {}
            labels = [s for s in _em.summaries(restaurant_id, limit=200,
                                               db_path=None if db_path == DB_PATH else db_path)
                      if s.get("applies") and s.get("kind") in ("holiday", "event")][:ROLE_LIFT_LABELS]
            for s in labels:
                lift = 1.0 + float(s.get("median_lift_pct") or 0) / 100.0
                if lift <= 0:
                    continue
                for role, e in (_dd.role_event_lifts(restaurant_id, s["label"], s.get("median_lift_pct"),
                                                     db_path=db_path, family_prior={}) or {}).items():
                    fam = family_of(role)
                    if fam and e.get("n", 0) >= _dd.EFFECT_MIN_N:
                        rel.setdefault(fam, []).append(float(e["raw"]) / lift)
            for fam, vals in rel.items():
                vals.sort()
                out[f"{PREFIX}rolelift.{fam}"] = round(vals[len(vals) // 2], 3)
    except Exception as e:
        print(f"[schedule_priors] event lifts unavailable for {restaurant_id}: {e}")
    return out


ROLE_LIFT_LABELS = 10


def role_lift_prior(restaurant_id, db_path=DB_PATH) -> dict:
    """{family: factor} — how far each role family moves on event nights
    against the night's own sales lift, at similar restaurants (1.0 = with
    the sales)."""
    from .staffing import ROLE_FAMILIES
    out = {}
    scope = _scope(restaurant_id, db_path=db_path)
    if not scope[0]:
        return out
    for fam, _rx in ROLE_FAMILIES:
        p = prior(restaurant_id, f"{PREFIX}rolelift.{fam}", db_path=db_path, scope=scope)
        if p:
            out[fam] = p["value"]
    return out


# ── reading the cohort's prior ────────────────────────────────────────────

def _ladder(restaurant_id, db_path=DB_PATH):
    import models as _m
    r = _m.get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else _m.get_restaurant(restaurant_id, db_path)
    prof = categories.profile_for(r) if r is not None else None
    if not prof or not prof.get("confirmed"):
        return []
    vb = None
    try:
        from . import features as _features
        own = _features.latest(restaurant_id, db_path=db_path)
        vb = ((own or {}).get("features") or {}).get("volume_band")
    except Exception:
        vb = None
    return categories.partition_ladder(prof, "staff", {"volume_band": vb}) or \
        [categories.partition_key(prof, "staff", volume_band=vb)]


def _scope(restaurant_id, db_path=DB_PATH):
    """(ladder, viewer org) — read once per call of the readers below."""
    from .benchmarks import viewer_org
    ladder = _ladder(restaurant_id, db_path=db_path)
    return ladder, (viewer_org(restaurant_id, db_path=db_path) if ladder else None)


def prior(restaurant_id, metric, db_path=DB_PATH, scope=None) -> dict | None:
    """{"value", "n", "cohort_label"} — the published median of `metric`
    (a sched_prior.* key) over this restaurant's peers, the finest rung of
    its partition ladder that clears the floors; None below them, without a
    confirmed profile, or on any failure. `scope` — _scope(), when the
    caller reads several."""
    try:
        from .benchmarks import published, cohort_label
        ladder, org = scope if scope is not None else _scope(restaurant_id, db_path=db_path)
        for rung in ladder or []:
            b = published(rung, metric, exclude_org=org, db_path=db_path)
            if b and not b.get("withheld") and b.get("p50") is not None:
                return privacy.assert_anonymous({"value": float(b["p50"]), "n": int(b["n"]),
                                                 "cohort_label": cohort_label(rung)})
    except Exception as e:
        print(f"[schedule_priors] prior unavailable for {restaurant_id} {metric}: {e}")
    return None


def standard_prior(restaurant_id, db_path=DB_PATH) -> dict:
    """{family: {part: per person-hour}} — the cohort's labor standards at
    the rush (staffing_model shrinks the restaurant's own toward them)."""
    import labor_standards as _ls
    out = {}
    scope = _scope(restaurant_id, db_path=db_path)
    if not scope[0]:
        return out
    for fam in _ls.WORK:
        for part in PARTS:
            p = prior(restaurant_id, f"{PREFIX}std.{fam}.{part}", db_path=db_path, scope=scope)
            if p:
                out.setdefault(fam, {})[part] = p["value"]
    return out


def weather_prior(restaurant_id, db_path=DB_PATH) -> dict:
    """{part: {condition: lift %}} — the cohort's weather effects
    (demand_drivers.weather_effects shrinks toward them)."""
    out = {}
    scope = _scope(restaurant_id, db_path=db_path)
    if not scope[0]:
        return out
    for part in PARTS:
        for c in WEATHER_CONDS:
            p = prior(restaurant_id, f"{PREFIX}wx.{part}.{c}", db_path=db_path, scope=scope)
            if p:
                out.setdefault(part, {})[c] = round((p["value"] - 1.0) * 100.0, 1)
    return out


def noshow_prior(restaurant_id, db_path=DB_PATH):
    """The cohort's no-show base rate, or None."""
    p = prior(restaurant_id, f"{PREFIX}noshow", db_path=db_path)
    return p["value"] if p else None


def event_prior(restaurant_id, kind, db_path=DB_PATH):
    """The cohort's median lift (%) of an event kind ("holiday", "event"),
    or None — a first holiday here starts from it."""
    p = prior(restaurant_id, f"{PREFIX}event.{kind}", db_path=db_path)
    return round((p["value"] - 1.0) * 100.0, 1) if p else None
