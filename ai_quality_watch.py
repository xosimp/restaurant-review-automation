"""
ai_quality_watch.py — the AI's output quality, the schedule generations'
fallbacks and the weekly smoke eval as platform warnings, and the console's
per-template comparison (Audit #25 A07-04, A07-05, A07-06, A07-11).

The validator's refusals and the callers' fallbacks were recorded
(ai_quality_events) and shown only in the console; the one automatic action
was the learner's weekly revert of a console override. A prompt edit that
made the DSR narrative refuse 40% of its openings told nobody. Here, read by
ops.check_platform_sla where the supervisor pages (send=True), each kept an
hour (an hourly rule on a five-minute supervisor):

  surface_regressions   per surface, the last 24 hours' refusals and
                        fallbacks (ai_utils.FALLBACK_EVENT_KINDS) per call
                        of the surface's own ledger actions, against the
                        trailing 14 days': over AI_QUALITY_RATIO (2x) with at
                        least AI_QUALITY_MIN_CALLS calls in each window and
                        AI_QUALITY_MIN_EVENTS events today, a warning naming
                        the build and prompt template most of today's events
                        came from (ai_quality_events.build/template_hash).
  generation_fallbacks  the share of the last GEN_FALLBACK_SAMPLE schedule
                        generations whose days were drafted by a fallback
                        (a model-source week with days built in code, or a
                        code source that fell to the other one — the
                        review's draft_source), against the 14 days before
                        them: over twice the baseline, or over
                        GEN_FALLBACK_ABS_SHARE with no baseline.
  eval_deviations       the newest weekly smoke eval of each surface off its
                        recorded baseline (ai_eval.smoke_deviations).

template_outcomes is the console's table: outcome, refusal and fallback
rates, cost and first sighting per (caller, template_hash).

Read-only; never pages by itself; never raises into the platform check.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


AI_QUALITY_RATIO = float(os.getenv("AI_QUALITY_RATIO", "2.0"))
AI_QUALITY_MIN_CALLS = int(os.getenv("AI_QUALITY_MIN_CALLS", "20"))
AI_QUALITY_MIN_EVENTS = 3
AI_QUALITY_BASELINE_DAYS = 14
GEN_FALLBACK_SAMPLE = 20
GEN_FALLBACK_MIN = 5
GEN_FALLBACK_RATIO = 2.0
GEN_FALLBACK_ABS_SHARE = 0.5
GEN_FALLBACK_DAYS = 14
_CACHE_SECONDS = 3600
_cache = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("ai_quality_watch._cache", _cache)


def _stamp(dt) -> str:
    """A UTC datetime as the ledgers' TEXT stamps (datetime('now'))."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _utcnow(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)


def _cached(name, key, fn, use_cache):
    hit = _cache.get((name, key))
    if use_cache and hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    val = fn()
    _cache[(name, key)] = (time.monotonic(), val)
    return val


def _surface_actions(surface, event_actions=()):
    import ai_utils
    acts = {surface} | set(ai_utils._SURFACE_ACTIONS.get(surface, ())) | {a for a in event_actions if a}
    if surface == "safety_disagreement":
        acts = {"review_analysis"}
    return acts


def surface_regressions(db_path=None, now=None, use_cache=True) -> list:
    """[{surface, calls_24h, events_24h, rate_24h, calls_base, events_base,
    rate_base, ratio, build, template_hash}] for every surface whose refusal
    and fallback rate over the last 24 hours is more than AI_QUALITY_RATIO
    times its trailing AI_QUALITY_BASELINE_DAYS rate (module docstring)."""
    return _cached("surface", (db_path or "", str(now)), lambda: _surface_regressions(db_path, now), use_cache)


def _surface_regressions(db_path, now):
    import ai_utils
    t = _utcnow(now)
    day, base = _stamp(t - timedelta(days=1)), _stamp(t - timedelta(days=1 + AI_QUALITY_BASELINE_DAYS))
    kinds = tuple(ai_utils.FALLBACK_EVENT_KINDS)
    marks = ",".join("?" * len(kinds))
    conn = get_conn(db_path)
    try:
        ev = conn.execute(
            "SELECT surface, action, SUM(CASE WHEN created_at >= ? THEN n ELSE 0 END) AS e24, "
            "SUM(CASE WHEN created_at < ? THEN n ELSE 0 END) AS eb FROM ai_quality_events "
            f"WHERE created_at >= ? AND kind IN ({marks}) GROUP BY surface, action",
            (day, day, base) + kinds).fetchall()
        calls = {r["action"]: (int(r["c24"] or 0), int(r["cb"] or 0)) for r in conn.execute(
            "SELECT action, SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS c24, "
            "SUM(CASE WHEN created_at < ? THEN 1 ELSE 0 END) AS cb FROM ai_usage "
            "WHERE created_at >= ? AND COALESCE(outcome, 'ok') <> 'blocked' "
            "AND COALESCE(vendor, 'anthropic') = 'anthropic' GROUP BY action", (day, day, base)).fetchall()}
        by = {}
        for r in ev:
            s = by.setdefault(r["surface"], {"e24": 0, "eb": 0, "actions": set()})
            s["e24"] += int(r["e24"] or 0)
            s["eb"] += int(r["eb"] or 0)
            s["actions"].add(r["action"])
        out = []
        for surface, s in sorted(by.items()):
            acts = _surface_actions(surface, s["actions"])
            c24 = sum(calls.get(a, (0, 0))[0] for a in acts)
            cb = sum(calls.get(a, (0, 0))[1] for a in acts)
            if c24 < AI_QUALITY_MIN_CALLS or cb < AI_QUALITY_MIN_CALLS or s["e24"] < AI_QUALITY_MIN_EVENTS:
                continue
            rate, rate_b = s["e24"] / float(c24), s["eb"] / float(cb)
            if rate <= AI_QUALITY_RATIO * rate_b:
                continue
            top = conn.execute(
                "SELECT build, template_hash, SUM(n) AS n FROM ai_quality_events WHERE surface=? AND created_at >= ? "
                f"AND kind IN ({marks}) GROUP BY build, template_hash ORDER BY SUM(n) DESC LIMIT 1",
                (surface, day) + kinds).fetchone()
            out.append({"surface": surface, "calls_24h": c24, "events_24h": s["e24"], "rate_24h": round(rate, 4),
                        "calls_base": cb, "events_base": s["eb"], "rate_base": round(rate_b, 4),
                        "ratio": (round(rate / rate_b, 1) if rate_b else None),
                        "build": top["build"] if top else None, "template_hash": top["template_hash"] if top else None})
        return out
    finally:
        conn.close()


def _fell_back(ds) -> bool:
    """Whether a saved week's draft_source (schedule_job's review) shows a
    fallback: the model source with days drafted in code, or a code source
    whose days came from the other code source (the solver failing to
    carry_forward)."""
    if not isinstance(ds, dict):
        return False
    chosen, code_source = ds.get("chosen"), ds.get("code_source")
    if chosen == "model":
        return bool(ds.get("code_dates"))
    return bool(code_source) and chosen in ("solver", "carry_forward") and code_source != chosen


def generation_fallbacks(db_path=None, now=None, use_cache=True) -> dict:
    """{n, fell_back, share, base_n, base_share, over, whys}: the last
    GEN_FALLBACK_SAMPLE schedule generations' fallback share against the
    GEN_FALLBACK_DAYS before them (module docstring)."""
    return _cached("gen", (db_path or "", str(now)), lambda: _generation_fallbacks(db_path, now), use_cache)


def _generation_fallbacks(db_path, now):
    t = _utcnow(now)
    since = _stamp(t - timedelta(days=GEN_FALLBACK_DAYS + 7))
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT review_json, generated_at FROM schedule_history WHERE total_seconds IS NOT NULL "
                            "AND generated_at >= ? ORDER BY id DESC LIMIT 400", (since,)).fetchall()
    finally:
        conn.close()
    flags, whys = [], {}
    for r in rows:
        try:
            ds = (json.loads(r["review_json"] or "null") or {}).get("draft_source")
        except (TypeError, ValueError, AttributeError):
            ds = None
        fb = _fell_back(ds)
        flags.append(fb)
        if fb and len(flags) <= GEN_FALLBACK_SAMPLE:
            for w in set(str(x) for x in ((ds or {}).get("why") or {}).values() if x):
                whys[w] = whys.get(w, 0) + 1
    recent, base = flags[:GEN_FALLBACK_SAMPLE], flags[GEN_FALLBACK_SAMPLE:]
    n, nb = len(recent), len(base)
    fell = sum(1 for f in recent if f)
    share = fell / float(n) if n else 0.0
    base_share = (sum(1 for f in base if f) / float(nb)) if nb >= GEN_FALLBACK_MIN else None
    if n < GEN_FALLBACK_MIN or fell < 3:
        over = False
    elif base_share is None:
        over = share >= GEN_FALLBACK_ABS_SHARE
    else:
        over = share > GEN_FALLBACK_RATIO * base_share and share - base_share >= 0.15
    return {"n": n, "fell_back": fell, "share": round(share, 3), "base_n": nb,
            "base_share": None if base_share is None else round(base_share, 3), "over": bool(over),
            "whys": dict(sorted(whys.items(), key=lambda kv: -kv[1]))}


def eval_deviations(db_path=None) -> list:
    """[{surface, model, deviations}] — the newest weekly smoke eval of each
    surface that deviated from its recorded baseline."""
    import ai_eval
    out = []
    for surface, r in sorted(ai_eval.latest_runs("smoke", db_path=db_path).items()):
        dev = list((r.get("scores") or {}).get("deviations") or [])
        if dev:
            out.append({"surface": surface, "model": r.get("model"), "deviations": dev,
                        "created_at": r.get("created_at")})
    return out


def platform_problems(db_path=None, now=None) -> list:
    """[{key, line}] — the warnings check_platform_sla adds (each its own
    warn kind, so a new one joining the set pages sooner)."""
    out = []
    try:
        for r in surface_regressions(db_path=db_path, now=now):
            names = []
            if r.get("build"):
                names.append(f"build {r['build']}")
            if r.get("template_hash"):
                names.append(f"template {r['template_hash']}")
            out.append({"key": f"ai_quality:{r['surface']}",
                        "line": f"AI quality: {r['surface']} refused or fell back on {r['events_24h']} of "
                                f"{r['calls_24h']} calls in 24 hours ({r['rate_24h'] * 100:.0f}%), against "
                                f"{r['rate_base'] * 100:.1f}% over the 14 days before"
                                + (f" — most from {', '.join(names)}" if names else "")
                                + ". The events are on the console's AI Quality page."})
    except Exception as e:
        log.warning("AI quality regressions unreadable: %s", e)
    try:
        g = generation_fallbacks(db_path=db_path, now=now)
        if g["over"]:
            why = ", ".join(f"{k} {v}" for k, v in list(g["whys"].items())[:3])
            base = (f"{g['base_share'] * 100:.0f}% over the 14 days before" if g["base_share"] is not None
                    else "no baseline yet")
            out.append({"key": "schedule_fallbacks",
                        "line": f"Schedule generations are falling back: {g['fell_back']} of the last {g['n']} "
                                f"({g['share'] * 100:.0f}%) had days drafted by a fallback, against {base}"
                                + (f" ({why})" if why else "") + " — each week's review names its days."})
    except Exception as e:
        log.warning("schedule generation fallbacks unreadable: %s", e)
    try:
        for d in eval_deviations(db_path=db_path):
            out.append({"key": f"ai_eval:{d['surface']}",
                        "line": f"The weekly AI smoke eval of {d['surface']} on {d['model']} is off its baseline: "
                                + "; ".join(d["deviations"][:2]) + " (scripts/run_ai_eval.py)."})
    except Exception as e:
        log.warning("AI smoke eval deviations unreadable: %s", e)
    return out


def template_outcomes(days=30, restaurant_id=None, db_path=None) -> list:
    """[{caller, template_hash, first_seen, last_seen, calls, ok, errors,
    refused, truncated, unparseable, fallbacks, cost_usd, refusal_rate,
    current}] per (caller, template_hash) over the last `days` — a prompt
    edit's before and after side by side (A07-05). `refusal_rate` is the
    provider's refusals, truncations and unparseable answers plus the
    callers' fallbacks, per call; `first_seen` is the template's first call
    on record (ai_calls keeps 120 days); `current` marks each caller's
    newest template. Ranked by caller, then refusal rate."""
    import ai_utils
    since = _stamp(_utcnow() - timedelta(days=int(days)))
    rid_sql, rid_args = ("", []) if restaurant_id is None else (" AND c.restaurant_id=?", [int(restaurant_id)])
    kinds = tuple(ai_utils.FALLBACK_EVENT_KINDS)
    marks = ",".join("?" * len(kinds))
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT c.caller, c.template_hash, MIN(c.created_at) AS first_in, MAX(c.created_at) AS last_seen, "
            "COUNT(*) AS calls, SUM(CASE WHEN c.outcome='ok' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN c.outcome='error' THEN 1 ELSE 0 END) AS errors, "
            "SUM(CASE WHEN c.outcome='refused' THEN 1 ELSE 0 END) AS refused, "
            "SUM(CASE WHEN c.outcome='truncated' THEN 1 ELSE 0 END) AS truncated, "
            "SUM(CASE WHEN c.outcome='unparseable' THEN 1 ELSE 0 END) AS unparseable "
            f"FROM ai_calls c WHERE c.created_at >= ? AND c.caller IS NOT NULL{rid_sql} "
            "GROUP BY c.caller, c.template_hash", [since] + rid_args).fetchall()
        fb = {(r["caller"], r["template_hash"]): int(r["n"] or 0) for r in conn.execute(
            "SELECT c.caller, c.template_hash, SUM(e.n) AS n FROM ai_quality_events e "
            f"JOIN ai_calls c ON c.call_id = e.call_id WHERE e.created_at >= ? AND e.kind IN ({marks}){rid_sql} "
            "GROUP BY c.caller, c.template_hash", [since] + list(kinds) + rid_args).fetchall()}
        cost = {(r["caller"], r["template_hash"]): float(r["usd"] or 0.0) for r in conn.execute(
            "SELECT c.caller, c.template_hash, SUM(u.cost_usd) AS usd FROM ai_usage u "
            f"JOIN ai_calls c ON c.call_id = u.call_id WHERE u.created_at >= ?{rid_sql} "
            "GROUP BY c.caller, c.template_hash", [since] + rid_args).fetchall()}
        hashes = sorted({r["template_hash"] for r in rows if r["template_hash"]})
        first = {}
        for i in range(0, len(hashes), 200):
            part = hashes[i:i + 200]
            for r in conn.execute("SELECT template_hash, MIN(created_at) AS f FROM ai_calls WHERE template_hash IN ("
                                  + ",".join("?" * len(part)) + ") GROUP BY template_hash", part).fetchall():
                first[r["template_hash"]] = r["f"]
    finally:
        conn.close()
    out = []
    for r in rows:
        k = (r["caller"], r["template_hash"])
        calls = int(r["calls"] or 0)
        bad = int(r["refused"] or 0) + int(r["truncated"] or 0) + int(r["unparseable"] or 0) + fb.get(k, 0)
        out.append({"caller": r["caller"], "template_hash": r["template_hash"],
                    "first_seen": first.get(r["template_hash"]) or r["first_in"], "last_seen": r["last_seen"],
                    "calls": calls, "ok": int(r["ok"] or 0), "errors": int(r["errors"] or 0),
                    "refused": int(r["refused"] or 0), "truncated": int(r["truncated"] or 0),
                    "unparseable": int(r["unparseable"] or 0), "fallbacks": fb.get(k, 0),
                    "cost_usd": round(cost.get(k, 0.0), 4),
                    "refusal_rate": round(bad / float(calls), 4) if calls else None, "current": False})
    newest = {}
    for o in out:
        if o["caller"] not in newest or str(o["last_seen"]) > str(newest[o["caller"]]["last_seen"]):
            newest[o["caller"]] = o
    for o in newest.values():
        o["current"] = True
    out.sort(key=lambda o: (o["caller"] or "", -(o["refusal_rate"] or 0), str(o["first_seen"] or "")))
    return out
