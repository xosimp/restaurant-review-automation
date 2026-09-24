"""
owner_report.py — "What worked for you": the owner's own record, said in
sentences (rec-ROI audit #28).

The audit's target sentence was "Over the past 6 months, you accepted 83% of
our labor recommendations. Those recommendations were associated with a
14.2% reduction in overtime and approximately $8,460 in measured labor
savings. Recommendations related to weekend staffing have been the most
consistently effective for your restaurant." Every clause of it now has a
stored source; this module joins them, and says a clause only when its own
minimum is met:

  acceptance      rec_learning.summary — episodes shown in the window, by
                  module, taken ÷ (answered + ignored). An ignored
                  recommendation stays in the denominator. Quoted only for a
                  module with `enough` (MIN_SETTLED_FOR_RATE settled).
  changes         recommendation_outcomes' stored before/after values and
                  delta_pct — the results of recommendations the owner took,
                  one per metric per non-overlapping window (two readings of
                  the same weeks are one change), results that still count
                  plus the ones with no clear change (so the average is not
                  only the movers). Quoted only for a metric with
                  MIN_RESULTS_PER_METRIC results.
  measured        outcomes.cumulative over the window — dollars summed over
                  days actually measured, net of changes that got worse.
                  Quoted only after MIN_MEASURED_DAYS measured days.
  most effective  rec_learning.most_effective — the subject tag with enough
                  measured results and the best lower bound of success.

DETERMINISTIC. No model writes a word of it: the same rows give the same
sentences, and every figure in a sentence is in `facts` beside it. The
words are the product's honest ones — "associated with", "measured before
and after, not proven cause" — never "caused", "saved you" or "thanks to".
Nothing is estimated to fill a gap: a clause whose minimum is not met is
left out, and with none met the report says `enough: false` and no
sentence at all.

Redacted per viewer like /recs/summary: the acceptance figures come through
rec_learning.viewer_sees; the outcome rows and the dollars drop every module
the login may not open (value_delivered.viewer_denied), comps and voids
without LOSS_VIEW, and any result whose recommendation the login may not see.
viewer=None is an internal caller (the monthly email to the owner).

Level 1 only: every read is this restaurant's. No network.
"""
from datetime import date, datetime, timedelta

from models import DB_PATH

WINDOWS = (90, 180)
DEFAULT_DAYS = 180
WINDOW_LABELS = {90: "the past 3 months", 180: "the past 6 months"}

# Minimums. Below each, its clause is left out (never estimated).
# Non-overlapping measured results on one number before an average change is
# quoted — and the sentence states their range (CA4 F17): an "average
# reduction" over two results was one result and a coin.
MIN_RESULTS_PER_METRIC = 3
MIN_MEASURED_DAYS = 14           # measured days before a dollar total is quoted
MAX_MODULES_IN_SENTENCE = 3
MAX_CHANGE_SENTENCES = 2

# Sources that start a tracker from one of Cavnar's own recommendations. A
# manual tracker or Ask's track_outcome is the owner's own idea, and an
# observed action (a schedule published, an order sent) or an alert read is
# not a recommendation taken — they are not "what worked" of Cavnar's.
REC_SOURCES = ("recommendation", "reprice", "slow_day_campaign", "schedule")
LOSS_METRICS = ("comp_rate", "void_rate")

# rec_ledger.MODULES as an owner reads them in "…of Cavnar's X recommendations".
MODULE_LABELS = {"reviews": "reviews", "labor": "labor", "schedule": "scheduling", "food": "food cost",
                 "marketing": "marketing", "intel": "competitor", "guests": "guest marketing",
                 "ops": "operations", "home": "cross-module", "ask": "Ask"}
# outcomes' value vocabulary (who a measured dollar is credited to).
VALUE_MODULE_LABELS = {"labor": "labor", "inventory": "food cost", "reviews": "reviews",
                       "marketing": "marketing", "intel": "competitor", "other": "other changes"}

CAVEAT = "Measured before and after, not proven cause."


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if db_path is None or db_path == DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


# ── wording helpers ─────────────────────────────────────────────────────────

def _join(parts):
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _lower_first(label):
    s = str(label or "")
    return s[:1].lower() + s[1:] if s else s


def _money(v):
    return f"${abs(float(v)):,.0f}"


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


# ── acceptance ──────────────────────────────────────────────────────────────

def _acceptance(summary):
    rows = []
    for module, m in (summary.get("by_module") or {}).items():
        taken = int(m.get("accepted", 0)) + int(m.get("completed", 0)) + int(m.get("implemented", 0))
        rows.append({"module": module, "label": MODULE_LABELS.get(module, module), "n": int(m.get("n") or 0),
                     "taken": taken, "dismissed": int(m.get("dismissed") or 0),
                     "ignored": int(m.get("ignored") or 0), "accept_rate": m.get("accept_rate"),
                     "accept_rate_low": m.get("accept_rate_low"), "accept_rate_high": m.get("accept_rate_high"),
                     "enough": bool(m.get("enough"))})
    rows.sort(key=lambda r: (-r["n"], r["module"]))
    return rows


def _acceptance_sentence(rows, window):
    quoted = [r for r in rows if r["enough"] and r["accept_rate"] is not None][:MAX_MODULES_IN_SENTENCE]
    if not quoted:
        return None
    parts = [f"{round(r['accept_rate'] * 100)}% of Cavnar's {r['label']} recommendations "
             f"({r['taken']} of {r['n']})" for r in quoted]
    s = f"Over {window}, you accepted {_join(parts)}."
    ignored = sum(r["ignored"] for r in quoted)
    if ignored:
        s += (f" That counts the {ignored} you left unanswered as not accepted." if ignored > 1
              else " That counts the one you left unanswered as not accepted.")
    return s


# ── measured changes (stored before/after, one per window) ─────────────────

def _linked_episodes(conn, rid):
    """{tracker_id: episode row} — the ledger episodes that name a tracker."""
    try:
        rows = conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND tracker_id IS NOT NULL",
                            (rid,)).fetchall()
    except Exception as e:
        print(f"[owner_report] linked episodes unreadable rid={rid}: {e}")
        return {}
    out = {}
    for r in rows:
        r = dict(r)
        out.setdefault(r["tracker_id"], r)
    return out


def _from_a_recommendation(r, linked):
    if r["id"] in linked:
        return True
    src = r.get("source") or ""
    if src in REC_SOURCES:
        return True
    # Home's Done on a recommendation starts an "observed" tracker keyed by
    # the recommendation itself; an observed ACTION's key is "observed:…".
    return src == "observed" and not str(r.get("source_key") or "").startswith("observed:")


def _eligible_results(rid, since, until, viewer, db_path, denied, sees_loss):
    """Evaluated results of recommendations the owner took, in the window,
    that this viewer may see: still counting (improved or worse, not faded,
    not disowned) or no clear change. Unknown verdicts are never here."""
    import outcomes
    import rec_learning
    conn = get_conn(db_path)
    try:
        linked = _linked_episodes(conn, rid)
    finally:
        conn.close()
    out = []
    for r in outcomes.list_outcomes(rid, status="evaluated", limit=500, db_path=db_path):
        day = str(r.get("evaluate_on") or "")[:10]
        if not day or day < since or day > until:
            continue
        # The one admission rule value and learning share (outcomes.
        # result_counts, CA2 #7): not informational, not disowned, no
        # "something else changed", not measured against its trigger window.
        if not outcomes.result_counts(r):
            continue
        v = r.get("verdict")
        if not ((v in ("improved", "worsened") and r.get("counts")) or v == "no_clear_change"):
            continue
        if not _from_a_recommendation(r, linked):
            continue
        base = str(r.get("metric") or "").split(":", 1)[0]
        if base in LOSS_METRICS and not sees_loss:
            continue
        if outcomes._denied_row(r, denied):
            continue
        ep = linked.get(r["id"]) or {"key": r.get("source_key"), "module": None}
        if not rec_learning.viewer_sees(viewer, ep):
            continue
        out.append(r)
    return out


def _non_overlapping(rows):
    """One result per window: of results on the same metric whose
    after-windows overlap, the earliest is kept (two readings of the same
    weeks are one change, whichever way it went)."""
    import outcomes
    kept, last_end = [], ""
    for r in sorted(rows, key=lambda x: (outcomes._after_window(x)[0], x["id"])):
        start, end = outcomes._after_window(r)
        if kept and start <= last_end:
            continue
        kept.append(r)
        last_end = max(last_end, end)
    return kept


def _changes(rows):
    import metrics
    by_metric = {}
    for r in rows:
        if r.get("delta_pct") is None or r.get("baseline_value") in (None, 0):
            continue
        by_metric.setdefault(r["metric"], []).append(r)
    out = []
    for metric, group in by_metric.items():
        if not metrics.known(metric):
            continue
        kept = _non_overlapping(group)
        if len(kept) < MIN_RESULTS_PER_METRIC:
            continue
        info = metrics.describe(metric)
        n = len(kept)
        deltas = [float(r["delta"]) if r.get("delta") is not None
                  else float(r["after_value"]) - float(r["baseline_value"]) for r in kept]
        # The per-result statistic the sentence quotes (points for a
        # percentage, % change otherwise) — its range is said beside the
        # average (CA4 F17).
        stats = deltas if info["unit"] == "%" else [float(r["delta_pct"]) for r in kept]
        mean_delta = sum(deltas) / n
        mean_base = sum(float(r["baseline_value"]) for r in kept) / n
        # A direction is said only when the results agree on it AND the
        # average move is past the metric's own noise band (re-audit A32):
        # one result up and one down averaged to "an average reduction".
        mixed = any(d > 0 for d in deltas) and any(d < 0 for d in deltas)
        within = metrics.compare(metric, mean_base, mean_base + mean_delta)["verdict"] not in ("improved",
                                                                                               "worsened")
        out.append({
            "metric": metric, "label": info["label"], "unit": info["unit"],
            "lower_is_better": info["lower_is_better"], "family": info["family"],
            "results": n,
            "improved": sum(1 for r in kept if r.get("verdict") == "improved"),
            "worsened": sum(1 for r in kept if r.get("verdict") == "worsened"),
            "no_clear_change": sum(1 for r in kept if r.get("verdict") == "no_clear_change"),
            "mean_delta": round(mean_delta, 2),
            "mean_delta_pct": round(sum(float(r["delta_pct"]) for r in kept) / n, 1),
            "range": [round(min(stats), 1), round(max(stats), 1)],
            "consistent_direction": not (mixed or within),
            "outcome_ids": [r["id"] for r in kept],
        })
    out.sort(key=lambda c: (-c["results"], -abs(c["mean_delta_pct"]), c["metric"]))
    return out


def _change_sentence(c):
    bits = []
    if c["improved"]:
        bits.append(f"{c['improved']} improved")
    if c["worsened"]:
        bits.append(f"{c['worsened']} got worse")
    if c["no_clear_change"]:
        bits.append(f"{c['no_clear_change']} no clear change")
    lead = f"Across {c['results']} measured results that didn't overlap ({', '.join(bits)}), "
    if not c.get("consistent_direction", True):
        # Results that disagree, or an average inside the noise band, have
        # no direction to quote (re-audit A32).
        return (lead + f"the recommendations you took showed no consistent direction in "
                f"{_lower_first(c['label'])}. {CAVEAT}")
    # The size and the direction come from ONE statistic: points for a
    # percentage, the average % change otherwise.
    stat = c["mean_delta"] if c["unit"] == "%" else c["mean_delta_pct"]
    if round(abs(stat), 1) == 0:
        return None
    mag = f"{abs(stat):.1f}-point" if c["unit"] == "%" else f"{abs(stat):.1f}%"
    rng = ""
    lo_hi = c.get("range")
    if lo_hi and lo_hi[0] != lo_hi[1]:
        # Every result moved the same way (consistent_direction), so the
        # range is two sizes of the same move.
        a, b = sorted((abs(lo_hi[0]), abs(lo_hi[1])))
        unit = " points" if c["unit"] == "%" else "%"
        rng = f" (individual results ranged from {a:.1f}{unit} to {b:.1f}{unit})"
    return (lead + f"the recommendations you took were associated with an average {mag} "
            f"{'reduction' if stat < 0 else 'increase'} in {_lower_first(c['label'])}{rng}. {CAVEAT}")


# ── measured dollars over the window ────────────────────────────────────────

def _measured(rid, since, db_path, denied, sees_loss, viewer=None):
    import outcomes
    # The results whose recommendation this login may not see are dropped
    # before the sum too, as the change sentences drop them (re-audit A26).
    cum = outcomes.cumulative(rid, db_path=db_path, denied_modules=denied, since=since,
                              exclude_metrics=() if sees_loss else LOSS_METRICS,
                              exclude_ids=outcomes.hidden_tracker_ids(rid, viewer, db_path=db_path))
    return cum


def _sales_lift_sentence(cum):
    """A sales lift measured over the window, apart from the savings — it is
    gross revenue, not profit (re-audit A6)."""
    lift = (cum or {}).get("sales_lift") or {}
    if lift.get("total") is None or (lift.get("measured_days") or 0) < MIN_MEASURED_DAYS:
        return None
    total = float(lift["total"])
    if round(total) == 0:
        return None
    word = "more" if total > 0 else "less"
    return (f"Sales on the days measured came to about {_money(total)} {word} than before the changes over "
            f"{int(lift['measured_days'])} measured days — gross revenue, not profit, so it is kept apart from "
            f"the savings.")


def _measured_sentence(cum):
    if not cum or cum.get("total") is None or (cum.get("measured_days") or 0) < MIN_MEASURED_DAYS:
        return None
    mods = sorted(((m, float(v)) for m, v in (cum.get("by_module") or {}).items() if round(float(v)) != 0),
                  key=lambda x: (-abs(x[1]), x[0]))
    total = float(cum["total"])
    if round(total) == 0 and not mods:
        return None
    days = int(cum["measured_days"])
    if total < 0:
        # It is the DOLLARS that went the wrong way — not a count of changes
        # (re-audit A33).
        return (f"Measured before and after, more dollars were lost than gained: your changes came to "
                f"{_money(total)} less over {days} measured days, net. {CAVEAT}")
    s = (f"Measured before and after, your changes came to about {_money(total)} over {days} measured days, "
         f"net of any that got worse")
    if len(mods) == 1:
        s += f", all of it in {VALUE_MODULE_LABELS.get(mods[0][0], mods[0][0])}."
    elif mods:
        s += ": " + _join([f"{_money(v)}{' less' if v < 0 else ''} in {VALUE_MODULE_LABELS.get(m, m)}"
                           for m, v in mods[:MAX_MODULES_IN_SENTENCE]]) + "."
    else:
        s += "."
    # The part tied to other changes in the same weeks is said apart from the
    # part that was clear or held (CA2 #7) — two figures side by side, never
    # one summed into the other.
    g = cum.get("by_grade") or {}
    clear, assoc = float(g.get("consistent_or_held") or 0), float(g.get("associated") or 0)
    if round(assoc) != 0 and round(clear) != 0:
        s += (f" Of that, {_money(clear)} was a clear move or held at its re-check; {_money(assoc)}"
              f"{' less' if assoc < 0 else ''} came alongside other changes or crossed normal variation only once.")
    elif round(assoc) != 0:
        s += " All of it came alongside other changes or crossed normal variation only once."
    return s + f" {CAVEAT}"


# ── the most effective subject ─────────────────────────────────────────────

def _most_effective_sentence(best, by_tag):
    if not best:
        return None
    row = next((t for t in by_tag or [] if t.get("tag") == best["tag"] and t.get("module") == best["module"]),
               None)
    if not row:
        return None
    label = best["label"] if str(best["tag"]).startswith("day:") else _lower_first(best["label"])
    # "Most often followed by an improvement" — what the count says. "Most
    # consistently effective" claimed an effect a before-and-after cannot
    # show (re-audit B13; the ranking itself is rec_learning's).
    return (f"Of what Cavnar has measured, recommendations about {label} have most often been followed by an "
            f"improvement at your restaurant: {row['improved']} of {row['measured']} measured results improved. "
            f"{CAVEAT}")


# ── the report ─────────────────────────────────────────────────────────────

def what_worked(restaurant_id, days=DEFAULT_DAYS, viewer=None, db_path=DB_PATH, today=None) -> dict:
    """{ok, days, enough, sentences, facts} — see the module docstring and
    API_REFERENCE.md → GET /recs/what-worked. `days` is 90 or 180."""
    import issues
    import rec_learning
    import value_delivered
    days = int(days)
    if days not in WINDOWS:
        raise ValueError("days must be 90 or 180")
    today = today or datetime.utcnow().date()
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()
    window = WINDOW_LABELS[days]
    denied = value_delivered.viewer_denied(viewer)
    sees_loss = issues.viewer_sees_loss(viewer)

    summary = rec_learning.summary(restaurant_id, days=days, viewer=viewer, db_path=db_path)
    acceptance = _acceptance(summary)
    try:
        changes = _changes(_eligible_results(restaurant_id, since, until, viewer, db_path, denied, sees_loss))
    except Exception as e:
        print(f"[owner_report] changes unreadable rid={restaurant_id}: {e}")
        changes = []
    try:
        measured = _measured(restaurant_id, since, db_path, denied, sees_loss, viewer=viewer)
    except Exception as e:
        print(f"[owner_report] measured dollars unreadable rid={restaurant_id}: {e}")
        measured = None
    best = summary.get("most_effective")

    sentences = []
    s = _acceptance_sentence(acceptance, window)
    if s:
        sentences.append(s)
    for c in changes[:MAX_CHANGE_SENTENCES]:
        s = _change_sentence(c)
        if s:
            sentences.append(s)
    s = _measured_sentence(measured)
    if s:
        sentences.append(s)
    s = _sales_lift_sentence(measured)
    if s:
        sentences.append(s)
    s = _most_effective_sentence(best, summary.get("by_tag"))
    if s:
        sentences.append(s)

    return {
        "ok": True, "days": days, "enough": bool(sentences), "sentences": sentences,
        "facts": {
            "since": since, "window": window,
            "acceptance": acceptance,
            "changes": changes,
            "measured": measured,
            "most_effective": best,
            "minimums": {"settled_for_rate": rec_learning.MIN_SETTLED_FOR_RATE,
                         "measured_for_tag": rec_learning.MIN_MEASURED_FOR_RATE,
                         "results_per_metric": MIN_RESULTS_PER_METRIC,
                         "measured_days": MIN_MEASURED_DAYS},
            "caveat": CAVEAT,
        },
    }


def email_lines(restaurant_id, days=DEFAULT_DAYS, db_path=DB_PATH, today=None) -> list:
    """The sentences for the owner's monthly email (viewer None — the owner's
    own report); [] when nothing meets its minimum. Never raises."""
    try:
        return what_worked(restaurant_id, days=days, viewer=None, db_path=db_path, today=today)["sentences"]
    except Exception as e:
        print(f"[owner_report] email lines unavailable rid={restaurant_id}: {e}")
        return []
