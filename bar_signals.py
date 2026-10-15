"""
bar_signals.py — the Bar's figures where the owner already looks (barfix W9,
10/15/26: F4 / R17 / F13 / CMP-4 / CMP-6): Ask Cavnar AI's `read_bar`, the
daily report's weekly bar line, the weekly digest's bar line, the Home card
and the push for a price increase that held, and the game-week order hint.

Everything here is read from the Bar page's own payload (`bar_cost.build`)
— no new figure, no model — and every rule the page keeps holds here too:

- The purchase ratio is "bought vs sold" (basis "purchases"), never pour
  cost; "pour cost (counted)" only when two bar counts bracket the window.
  Ask types the ratio as an ESTIMATE (keys `est_*`) and a counted pour cost
  as MEASURED, so response_validation holds "your pour cost is 18%" to the
  basis it was read on.
- A reading that was not judged carries its reason and no verdict.
- A price change is news only when it HELD (W5's rows: kind "increase",
  persistent, listed, high confidence) and is worth at least
  `HEADLINE_MONTHLY_USD` a month at the bar's usual buying (the row's
  `headline`); a deal ending, an order-size price or one purchase at a new
  price is never one. The digest and Home carry it from $10 a month; the
  push only from `PUSH_MONTHLY_USD` (Decision 7).
- Forward money (`forward_monthly_usd`) is an estimate at usual buying and
  `already_paid_usd` what was measured; neither is ever value delivered
  (value_delivered never imports this module or the Bar).
- A figure whose source is missing is absent (None, or no line), never 0.

Recommendations (a held increase, "bar_price:<product>@<since>") go through
rec_ledger like every other module's: shown on Home / the digest / the push,
answered with Done or Not for us, with the card's measured confidence.
"""
from datetime import date, timedelta
from statistics import median

# barfix Decision 7: the digest line and Home from $10 a month (the row's
# own `headline` flag, bar.prices.HEADLINE_MONTHLY_USD), a push from $25.
PUSH_MONTHLY_USD = 25.0
# The rec_ledger kind of a held bar price increase.
PRICE_KIND = "bar_price"
# Game-week order hint (F13): games like the next one, each with its week of
# invoices, against the usual week.
EVENT_MIN_GAMES = 3
EVENT_MIN_BASELINE_WEEKS = 4
EVENT_BASELINE_WEEKS = 12
EVENT_MIN_LIFT_PCT = 15.0
EVENT_AHEAD_DAYS = 7
# The weekly price push: Mondays from this local hour.
PUSH_HOUR = 9
PUSH_UNTIL = 14
PUSH_CURSOR_KEY = "bar_price_push_cursor"
PUSH_MAX_SECONDS = 240
_CATS = ("beer", "liquor", "wine")


def payload(restaurant_id, today=None, db_path=None):
    """The Bar page's payload (bar_cost.build, served from bar.cache: kept
    until the data under it changes — read it, never change it), or None
    when it cannot be read. Never raises."""
    try:
        import bar.cache as _bcache
        return _bcache.full(int(restaurant_id), db_path=db_path, today=today)
    except Exception as e:
        print(f"[bar_signals] bar payload unreadable for {restaurant_id}: {e!r}")
        return None


def _money(v) -> str:
    v = float(v or 0)
    return f"${abs(v):,.2f}" if abs(v) < 100 and round(v) != v else f"${abs(v):,.0f}"


# ── price increases that held ───────────────────────────────────────────────

def headline_increases(p, floor=None) -> list:
    """The held increases worth saying, biggest first: W5's rows with kind
    "increase", persistent, listed, `headline` (high confidence, at least
    $10 a month forward), still bought, and at least `floor` a month when
    given."""
    rows = []
    for r in ((p or {}).get("price_watch") or {}).get("increases") or []:
        if not (r.get("kind") == "increase" and r.get("persistent") is True and r.get("listed") is not False
                and r.get("headline") is True and not r.get("not_bought_lately")):
            continue
        fwd = float(r.get("forward_monthly_usd") or 0)
        if fwd <= 0 or (floor is not None and fwd < floor):
            continue
        rows.append(r)
    rows.sort(key=lambda r: -float(r.get("forward_monthly_usd") or 0))
    return rows


def price_rec_key(row) -> str:
    """"bar_price:<product>@<since>" — the same product's next increase is a
    new recommendation, the same one shown anywhere is one episode."""
    import rec_ledger
    subject = f"{row.get('canonical_key') or row.get('product_key')}@{row.get('since')}"
    return rec_ledger.rec_key(PRICE_KIND, subject)


def price_title(row) -> str:
    return (f"Raise {row.get('name') or row.get('product')}'s price with {row.get('distributor')} — "
            f"up {abs(float(row.get('pct') or 0)):g}% and it has held")


def price_sentence(row) -> str:
    """One plain sentence: what it costs now, since when, from what, and the
    money a month at the bar's usual buying (an estimate, said so)."""
    unit = row.get("unit") or "unit"
    return (f"{row.get('name') or row.get('product')} from {row.get('distributor')}: "
            f"{_money(row.get('to'))} a {unit} since {row.get('since_mdy')}, after {_money(row.get('from'))} "
            f"— about +{_money(row.get('forward_monthly_usd'))} a month at your usual buying")


# ── Ask Cavnar AI: read_bar ─────────────────────────────────────────────────

def _range_facts(c) -> dict:
    rg = c.get("range") or None
    src = c.get("range_source") or {}
    if not rg:
        return {}
    return {"industry_range_low_pct": rg[0], "industry_range_high_pct": rg[1],
            "range_is": ((src.get("label") or "a range") + " — it describes counted pour cost, so against a "
                         "purchase ratio it is context, never a verdict")}


def ask_read(restaurant_id, today=None, db_path=None, p=None) -> dict:
    """read_bar's answer: the Bar page, compact, every figure named by kind
    (keys: `est_*` an estimate, `*_target_pct` the owner's plan,
    `industry_*` a benchmark, the rest measured)."""
    p = p if p is not None else payload(restaurant_id, today=today, db_path=db_path)
    if p is None:
        return {"available": False, "why_not": "The Bar page couldn't be read just now."}
    if not p.get("available"):
        return {"available": False, "why_not": p.get("why_not") or "No distributor invoices yet."}
    pc = p.get("pour_cost") or {}
    feed = p.get("feed") or {}
    out = {"available": True,
           "how_to_say_it": ("Bought vs sold is what the bar bought from distributors as a % of what the POS sold "
                             "— purchases, not what was poured: deliveries and stock-ups move it, and only two bar "
                             "counts make it pour cost. Never call it pour cost. Say a reading that was not judged "
                             "is not judged, and why. A price change is one that held; deals, order-size prices and "
                             "a single purchase at a new price are not price changes. Monthly price money is an "
                             "estimate at usual buying, never money saved or lost."),
           "invoices_through": feed.get("invoices_through_mdy"), "sales_through": feed.get("sales_through_mdy"),
           "invoice_feed_stale_days": feed.get("stale_days")}
    if pc.get("available"):
        counted = pc.get("basis") == "counted"
        w = pc.get("window") or {}
        tot = pc.get("total") or {}
        key = "pour_cost_counted" if counted else "purchase_ratio"
        reading = {"label": pc.get("label"), "what_it_is": pc.get("sub"),
                   "window": f"{w.get('from_mdy')}–{w.get('to_mdy')}", "window_days": w.get("days"),
                   "judged": bool(pc.get("judged")),
                   "not_judged_because": (tot.get("not_judged_text") if not pc.get("judged") else None),
                   "purchases_usd": tot.get("purchases"), "sales_usd": tot.get("sales"),
                   "categories_left_out": [x.get("why") for x in tot.get("excluded") or []] or None}
        reading["pct" if counted else "est_pct"] = tot.get("pct")
        if tot.get("target") is not None:
            reading["owner_target_pct"] = tot.get("target")
        cats = []
        for c in pc.get("categories") or []:
            row = {"category": c.get("label"), "judged": bool(c.get("judged")),
                   "not_judged_because": c.get("not_judged_text") if not c.get("judged") else None,
                   "purchases_usd": c.get("purchases"), "sales_usd": c.get("sales")}
            row["pct" if (c.get("basis") or pc.get("basis")) == "counted" else "est_pct"] = c.get("pct")
            if c.get("target") is not None:
                row["owner_target_pct"] = c.get("target")
            row.update(_range_facts(c))
            if c.get("judged") and c.get("over_target_usd"):
                row["est_over_target_usd"] = c.get("over_target_usd")
            cats.append(row)
        reading["categories"] = cats
        out[key] = reading
        gaps = [{"distributor": g.get("distributor"), "from": g.get("from_mdy"), "to": g.get("to_mdy"),
                 "categories": g.get("categories")} for g in pc.get("gaps") or []]
        if gaps:
            out["invoices_not_on_file"] = gaps
    else:
        out["purchase_ratio"] = {"available": False, "why_not": pc.get("why_not")}
    counted_block = p.get("counted") or {}
    out["bar_counts"] = {"last_count": counted_block.get("last_count_mdy"),
                         "next_count_due": counted_block.get("next_due_mdy"),
                         "counted_pour_cost_available": bool(counted_block.get("available"))}
    pw = p.get("price_watch") or {}
    held = [r for r in (pw.get("increases") or []) + (pw.get("decreases") or [])
            if r.get("persistent") is True and r.get("kind") in ("increase", "decrease")]
    held.sort(key=lambda r: -abs(float(r.get("forward_monthly_usd") or 0)))
    out["changes_that_held"] = [
        {"product": r.get("name") or r.get("product"), "distributor": r.get("distributor"), "kind": r.get("kind"),
         "unit": r.get("unit"), "price_was": r.get("from"), "price_now": r.get("to"), "change_pct": r.get("pct"),
         "since": r.get("since_mdy"), "est_forward_monthly_usd": r.get("forward_monthly_usd"),
         "already_paid_usd": r.get("already_paid_usd"), "headline": bool(r.get("headline")),
         "explain": r.get("explain")} for r in held[:10]]
    s = pw.get("summary") or {}
    out["not_price_changes"] = {"deal_prices_n": s.get("deals_n"), "order_size_prices_n": s.get("tiers_n"),
                                "one_purchase_so_far_n": s.get("unconfirmed_n")}
    ins = (p.get("insights") or [])[:3]
    out["what_deserves_a_look"] = [{"headline": i.get("headline"), "detail": i.get("metric"),
                                    "action": i.get("action"), "basis": i.get("basis")} for i in ins]
    hint = event_order_hint(restaurant_id, today=today, db_path=db_path)
    if hint:
        out["game_week_order_hint"] = hint
    reorder = [{"product": r.get("name"), "distributor": r.get("distributor"),
                "usually_every_days": r.get("usual_every_days"), "last_bought": r.get("last_bought_mdy")}
               for r in (p.get("reorder") or [])[:3]]
    if reorder:
        out["usually_reordered_by_now"] = reorder
    return out


# ── the daily report's weekly view ──────────────────────────────────────────

def dsr_week(restaurant_id, start, end, db_path=None, p=None):
    """The bar line for the DSR's week [start, end]: the four-week reading
    as of the week's last night (bar_cost.build(today=end)) with its basis,
    whether it was judged and why not, and the top held increase. None when
    the restaurant has no Bar (no distributor invoices): absent, never 0."""
    end = end if isinstance(end, date) else date.fromisoformat(str(end)[:10])
    p = p if p is not None else payload(restaurant_id, today=end, db_path=db_path)
    if not p or not p.get("available"):
        return None
    pc = p.get("pour_cost") or {}
    out = {"available": False, "basis": pc.get("basis"), "label": pc.get("label"), "sub": pc.get("sub"),
           "pct": None, "judged": False, "not_judged_text": None, "window": None, "categories": [],
           "top_price_increase": None, "text": None}
    bits = []
    if pc.get("available"):
        w = pc.get("window") or {}
        tot = pc.get("total") or {}
        out.update(available=True, pct=tot.get("pct"), judged=bool(pc.get("judged")),
                   not_judged_text=None if pc.get("judged") else tot.get("not_judged_text"),
                   window={"from_mdy": w.get("from_mdy"), "to_mdy": w.get("to_mdy"), "days": w.get("days")},
                   categories=[{"key": c.get("key"), "label": c.get("label"), "pct": c.get("pct"),
                                "judged": bool(c.get("judged"))} for c in pc.get("categories") or []])
        if tot.get("pct") is not None:
            name = pc.get("label") or "Bought vs sold"
            bits.append(f"Bar, four weeks to {w.get('to_mdy')}: {name.lower() if name else name} "
                        f"{tot['pct']:g}% ({pc.get('sub')})")
            if not pc.get("judged") and tot.get("not_judged_text"):
                bits[-1] += f" — not judged: {tot['not_judged_text'].rstrip('.')}"
    top = headline_increases(p)
    if top:
        r = top[0]
        out["top_price_increase"] = {"name": r.get("name"), "distributor": r.get("distributor"),
                                     "from": r.get("from"), "to": r.get("to"), "pct": r.get("pct"),
                                     "unit": r.get("unit"), "since_mdy": r.get("since_mdy"),
                                     "forward_monthly_usd": r.get("forward_monthly_usd"),
                                     "rec_key": price_rec_key(r)}
        bits.append("A price that held: " + price_sentence(r))
    if not bits:
        return out if out["available"] else None
    out["text"] = ". ".join(b.rstrip(".") for b in bits) + "."
    return out


# ── the weekly digest and Home ──────────────────────────────────────────────

def digest_line(restaurant_id, today=None, db_path=None, p=None):
    """{"text", "rec": {key, title, dollars}} for the weekly digest — the
    biggest held increase at $10 a month or more — or None."""
    p = p if p is not None else payload(restaurant_id, today=today, db_path=db_path)
    top = headline_increases(p)
    if not top:
        return None
    r = top[0]
    more = len(top) - 1
    text = price_sentence(r) + "." + (f" {more} more price increase{'' if more == 1 else 's'} held — "
                                       "see Food Cost → Bar." if more else "")
    return {"text": text, "rec": {"key": price_rec_key(r), "title": price_title(r),
                                  "dollars": float(r.get("forward_monthly_usd") or 0)}, "row": r}


def home_rec(restaurant_id, today=None, db_path=None, p=None):
    """The Home card's inputs for the biggest held increase, or None:
    {key, title, why, evidence, impact, dollars, ev, row}."""
    p = p if p is not None else payload(restaurant_id, today=today, db_path=db_path)
    top = headline_increases(p)
    if not top:
        return None
    r = top[0]
    hist = r.get("history") or []
    return {"key": price_rec_key(r), "title": price_title(r),
            "why": (f"{_money(r.get('to'))} a {r.get('unit') or 'unit'} since {r.get('since_mdy')}, after "
                    f"{_money(r.get('from'))} — it has held, so it isn't a deal ending or an order-size price."),
            "evidence": f"{len(hist)} purchase{'' if len(hist) == 1 else 's'} on the distributor's invoices",
            "impact": f"about +{_money(r.get('forward_monthly_usd'))}/month at your usual buying (an estimate)",
            "dollars": float(r.get("forward_monthly_usd") or 0),
            "ev": {"n": len(hist) or r.get("purchases") or None, "kind": "invoices",
                   "basis": "purchases on the distributor's invoices"},
            "row": r}


# ── the game-week order hint (F13) ──────────────────────────────────────────

def _week_spend(lines, end_day, days=7) -> dict:
    lo = (end_day - timedelta(days=days - 1)).isoformat()
    hi = end_day.isoformat()
    out = {c: 0.0 for c in _CATS}
    for l in lines:
        d = str(l.get("invoice_date"))[:10]
        if lo <= d <= hi and l.get("category") in _CATS:
            out[l["category"]] += float(l.get("net") or 0)
    return out


def event_order_hint(restaurant_id, today=None, db_path=None, lines=None):
    """"Bears Sunday this week: in the last three weeks with a game like it
    the bar bought 30% more beer & seltzer than its usual week" — from
    measured history only: the next followed game within EVENT_AHEAD_DAYS,
    at least EVENT_MIN_GAMES past games like it (event_intel.engine
    .same_kind) whose week of invoices is on file, and at least
    EVENT_MIN_BASELINE_WEEKS game-free weeks to call usual. None — never
    a guessed lift — when any of that is missing, or no category rose by
    EVENT_MIN_LIFT_PCT in every one of those weeks' median."""
    try:
        from event_intel import engine, store
    except Exception:
        return None
    try:
        if not store.follows(restaurant_id, db_path=db_path):
            return None
        ups = engine.upcoming(restaurant_id, days=EVENT_AHEAD_DAYS, today=today, db_path=db_path)
    except Exception as e:
        print(f"[bar_signals] events unreadable for {restaurant_id}: {e!r}")
        return None
    if not ups:
        return None
    if lines is None:
        try:
            from bar.purchases import _lines
            lines = _lines(restaurant_id, db_path)
        except Exception:
            return None
    if not lines:
        return None
    first_inv = min(str(l.get("invoice_date"))[:10] for l in lines)
    last_inv = max(str(l.get("invoice_date"))[:10] for l in lines)
    labels = {"beer": "beer & seltzer", "liquor": "liquor", "wine": "wine"}
    for u in ups:
        e = u["event"]
        try:
            games = [g["event"] for g in engine.past_games(restaurant_id, e, db_path=db_path)
                     if engine.same_kind(e, g["event"])]
        except Exception:
            continue
        days = []
        for g in games:
            d = date.fromisoformat(str(g["event_date"])[:10])
            if (d - timedelta(days=6)).isoformat() >= first_inv and d.isoformat() <= last_inv:
                days.append(d)
        days = sorted(set(days), reverse=True)[:EVENT_MIN_GAMES]
        if len(days) < EVENT_MIN_GAMES:
            continue
        # Every dated game of the series, so a "usual" week holds none.
        try:
            lo = (min(days) - timedelta(days=7 * EVENT_BASELINE_WEEKS + 7)).isoformat()
            all_games = {str(x["event_date"])[:10] for x in store.events_for([e["series_id"]], lo,
                                                                            max(days).isoformat(), db_path=db_path)}
        except Exception:
            continue
        lifts = {c: [] for c in _CATS}
        game_spend = {c: [] for c in _CATS}
        usual_spend = {c: [] for c in _CATS}
        ok = True
        for d in days:
            base = []
            for k in range(1, EVENT_BASELINE_WEEKS + 1):
                b_end = d - timedelta(days=7 * k)
                b_lo = b_end - timedelta(days=6)
                if b_lo.isoformat() < first_inv:
                    break
                if any(b_lo.isoformat() <= g <= b_end.isoformat() for g in all_games):
                    continue
                base.append(_week_spend(lines, b_end))
            if len(base) < EVENT_MIN_BASELINE_WEEKS:
                ok = False
                break
            gw = _week_spend(lines, d)
            for c in _CATS:
                usual = median(b[c] for b in base)
                if usual > 0:
                    lifts[c].append((gw[c] - usual) / usual * 100)
                    game_spend[c].append(gw[c])
                    usual_spend[c].append(usual)
        if not ok:
            continue
        best = None
        for c in _CATS:
            if len(lifts[c]) < EVENT_MIN_GAMES or min(lifts[c]) < EVENT_MIN_LIFT_PCT:
                continue
            m = median(lifts[c])
            if best is None or m > best[1]:
                best = (c, m)
        if best is None:
            continue
        c, lift = best
        lk = engine.local_kickoff(e, engine.restaurant_clock(restaurant_id, db_path=db_path)) or (None, None)
        from time_utils import mdy
        when = mdy(lk[0] or e["event_date"])
        return {"event": u.get("describe") or u.get("label"), "date": when, "category": c,
                "category_label": labels[c], "games": len(days),
                "game_weeks": [mdy(d) for d in days],
                "lift_pct": round(lift), "game_week_usd": round(median(game_spend[c]), 2),
                "usual_week_usd": round(median(usual_spend[c]), 2),
                "text": (f"{u.get('label') or u.get('describe')} {when}: in the last {len(days)} weeks with a game "
                         f"like it the bar bought {round(lift)}% more {labels[c]} than its usual week "
                         f"(about {_money(median(game_spend[c]))} against {_money(median(usual_spend[c]))}) — "
                         "worth it in this week's order"),
                "basis": ("invoice dollars in the 7 days up to each past game like this one, against the median "
                          "of the game-free weeks before it")}
    return None


# ── the weekly push for a price increase worth $25 a month ──────────────────

def _counts(attempted=0, ok=0, failed=0, skipped=0, hit_bound=False, **extra):
    out = {"attempted": int(attempted), "ok": int(ok), "failed": int(failed), "skipped": int(skipped),
           "hit_bound": bool(hit_bound)}
    out.update(extra)
    return out


def _restaurants_with_invoices(db_path=None) -> list:
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT restaurant_id FROM fintech_invoices WHERE restaurant_id IS NOT NULL"
                            ).fetchall()
    finally:
        conn.close()
    return sorted(int(r["restaurant_id"]) for r in rows)


def _already_said(restaurant_id, key, db_path=None) -> bool:
    """True when this increase was already pushed (a rec_events showing on
    alert_push), or the owner answered it anywhere."""
    import rec_ledger
    try:
        if rec_ledger.silenced(restaurant_id, key, db_path=db_path or rec_ledger.DB_PATH):
            return True
    except Exception:
        pass
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        row = conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND surface='alert_push' "
                           "LIMIT 1", (int(restaurant_id), key)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    return row is not None


def push_increase(restaurant_id, today=None, db_path=None, p=None) -> int:
    """Push the biggest held increase worth PUSH_MONTHLY_USD a month or more
    — once per increase (an episode pushed before, or answered, is never
    pushed again) — through strategy_jobs._reach: push only (its card is on
    Home and the digest already), the briefing budget, quiet hours and the
    Food Cost audience. Returns the people reached."""
    p = p if p is not None else payload(restaurant_id, today=today, db_path=db_path)
    top = headline_increases(p, floor=PUSH_MONTHLY_USD)
    if not top:
        return 0
    r = top[0]
    key = price_rec_key(r)
    if _already_said(restaurant_id, key, db_path=db_path):
        return 0
    import models
    from strategy_jobs import _reach
    return _reach(restaurant_id, PRICE_KIND, f"{r.get('name') or r.get('product')} is up and it has held",
                  price_sentence(r) + ".", {"tab": "inventory", "nav": "bar"}, db_path or models.DB_PATH,
                  rec={"key": key, "title": price_title(r), "module": "food"}, email=False)


def run_price_push(db_path=None, now=None) -> dict:
    """Hourly on Mondays: from PUSH_HOUR local, once a day per restaurant
    with distributor invoices (scheduler.local_due), push its biggest held
    bar price increase worth $25 a month or more (push_increase). Refused
    off the production scheduler (scheduling_allowed); paused and lapsed
    accounts skipped; bounded and resumable (job_cursors). Returns the
    standard counts."""
    import models as _m
    import scheduler as _sched
    ids = _restaurants_with_invoices(db_path)
    if not _sched.scheduling_allowed():
        return _counts(skipped=len(ids), reason="not the production scheduler")
    tally = {"attempted": 0, "failed": 0, "pushed": 0, "skipped": 0}

    def _one(rid):
        r = _m.get_restaurant(rid, db_path=db_path) if db_path else _m.get_restaurant(rid)
        if r is None or (getattr(r, "billing_status", None) or "trial").lower() in _m.BLOCKED_BILLING_STATES:
            return
        if not getattr(r, "module_inventory", 0):
            return
        from time_utils import restaurant_now
        local = now or restaurant_now(r, naive=True)
        if local.weekday() != 0:
            return
        if not _sched.local_due(r, PUSH_HOUR, until=PUSH_UNTIL, claim_key="bar_price_push", now_local=local):
            return
        tally["attempted"] += 1
        reached = push_increase(rid, today=local.date(), db_path=db_path)
        if reached:
            tally["pushed"] += 1
        else:
            tally["skipped"] += 1

    def _run(rid):
        try:
            _one(rid)
        except Exception:
            tally["failed"] += 1
            raise

    _done, ran_out = _sched.resumable_sweep(PUSH_CURSOR_KEY, ids, _run, PUSH_MAX_SECONDS, workers=1,
                                            job="bar_price_push")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"] - tally["skipped"], tally["failed"],
                   tally["skipped"], hit_bound=ran_out, pushed=tally["pushed"])
