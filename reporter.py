import os, smtplib, logging, html as _html
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from models import get_reviews_since, WeeklyReport

log = logging.getLogger(__name__)


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

SENTIMENT_COLOR = {"positive": "#16a34a", "neutral": "#6b7280", "negative": "#dc2626"}
STAR_FILLED = "★"
STAR_EMPTY  = "☆"


def build_report(restaurant_id: int, restaurant_name: str,
                 days: int = 7) -> WeeklyReport:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    reviews = get_reviews_since(restaurant_id, since)

    report = WeeklyReport(
        restaurant_id=restaurant_id,
        period_start=(datetime.now() - timedelta(days=days)).strftime("%b %d"),
        period_end=datetime.now().strftime("%b %d, %Y"),
    )

    if not reviews:
        return report

    report.total_reviews = len(reviews)
    report.avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1)

    cat_counts: dict = {}
    for r in reviews:
        report.sentiment[r.sentiment or "neutral"] += 1
        for cat in (r.categories or []):
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    report.top_issues = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    report._reviews = reviews  # attach for email rendering
    return report


def _stars(rating: int) -> str:
    return STAR_FILLED * rating + STAR_EMPTY * (5 - rating)


def _review_card(r) -> str:
    color = SENTIMENT_COLOR.get(r.sentiment or "neutral", "#6b7280")
    urgency_banner = (
        '<div style="background:#fef2f2;border-left:3px solid #dc2626;'
        'padding:6px 10px;font-size:12px;color:#dc2626;margin-bottom:8px">'
        'Needs immediate attention</div>'
    ) if r.urgency == "high" else ""
    # Escaped like every other field on this card. It was the one that wasn't
    # — author, text, sentiment and platform all went through _html.escape and
    # the AI-authored draft went in raw, so a reply containing an angle
    # bracket broke the card's markup in the owner's inbox.
    draft_section = (
        f'<div style="background:#f8fafc;border-radius:6px;padding:10px 12px;'
        f'font-size:13px;margin-top:8px"><strong>Suggested reply:</strong><br>'
        f'<span style="color:#374151">{_html.escape(r.draft_response)}</span></div>'
    ) if r.draft_response else ""

    return f"""
<div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin:8px 0">
  {urgency_banner}
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <strong style="font-size:14px">{_html.escape(r.author or "")}</strong>
    <span style="color:#f59e0b;font-size:16px">{_stars(r.rating)}</span>
  </div>
  <p style="margin:0 0 6px;font-size:14px;color:#1f2937;line-height:1.5">{_html.escape(r.text or "")}</p>
  <div style="font-size:11px;color:{color};text-transform:uppercase;letter-spacing:.04em">
    {_html.escape(r.sentiment or "")} &nbsp;·&nbsp; {_html.escape(r.platform or "")}</div>
  {draft_section}
</div>"""


def generate_ai_digest_summary(report, restaurant_name, owner_name=None, restaurant_id=None):
    """Generate a short AI summary paragraph for the weekly digest."""
    try:
        import anthropic, os
        from ai_utils import create_with_retry, extract_text, get_client, model_for
        client = get_client()
        reviews = getattr(report, "_reviews", [])
        pos = report.sentiment.get("positive", 0)
        neg = report.sentiment.get("negative", 0)
        urgent_count = sum(1 for r in reviews if r.urgency == "high")
        top_themes = ", ".join(cat.replace("_"," ") for cat, n in (report.top_issues or [])[:3])
        # Pull labor and inventory context if available.
        #
        # Every module below now produces BOTH a prose line (for the prompt)
        # and a structured `_facts` dict (for the correlation block). The
        # correlations used to be substring tests against the prose — a
        # case-sensitive `"UP" in inventory_context` that a waste item named
        # SOUP BASE would satisfy with no waste increase at all, and a
        # `"trending UP" in labor_context` that broke silently the moment the
        # wording changed. Comparing the floats they were rendered from cannot
        # drift and cannot be fooled by an ingredient's name.
        labor_context = ""
        inventory_context = ""
        marketing_context = ""
        _facts = {"labor": None, "inventory": None, "marketing": None}
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(report.restaurant_id)
            if labor and labor.get("overall_labor_pct"):
                lp = labor.get("overall_labor_pct", 0)
                ot_risk = labor.get("overtime_risk", [])
                labor_context = f"Labor: {lp:.1f}% of revenue this week"
                if ot_risk:
                    labor_context += f", {len(ot_risk)} overtime risk"
                _facts["labor"] = {"pct": float(lp), "direction": None,
                                   "from_pct": None, "weeks": 0,
                                   "overtime_risk": len(ot_risk)}
                # Pull labor trend from history
                try:
                    from models import get_conn as _gc_lr
                    _conn_lr = _gc_lr()
                    _lh = _conn_lr.execute(
                        """SELECT labor_pct, period_start FROM labor_history
                           WHERE restaurant_id=? ORDER BY period_start DESC LIMIT 3""",
                        (report.restaurant_id,)
                    ).fetchall()
                    _conn_lr.close()
                    if len(_lh) >= 2:
                        _vals = [r["labor_pct"] for r in reversed(_lh)]
                        _facts["labor"].update({"from_pct": float(_vals[0]), "weeks": len(_vals)})
                        if _vals[-1] > _vals[0] + 1.5:
                            _facts["labor"]["direction"] = "up"
                            labor_context += f" — trending UP from {_vals[0]:.1f}% ({len(_vals)} weeks)"
                        elif _vals[-1] < _vals[0] - 1.5:
                            _facts["labor"]["direction"] = "down"
                            labor_context += f" — trending DOWN from {_vals[0]:.1f}% ({len(_vals)} weeks, improving)"
                except Exception:
                    pass
                labor_context += "."
        except Exception:
            pass
        try:
            # is_live was discarded here, and load_inventory_for_restaurant
            # falls back to a bundled 20-item sample pantry that is always
            # truthy — so a client with no inventory connected received a
            # weekly email naming sample ingredients as their own top waste
            # item and their own critically-low stock.
            from inventory import analysis_for
            inv, inv_live, analysis = analysis_for(report.restaurant_id)
            if inv and inv_live:
                waste = analysis.get("waste_items", [])
                low = analysis.get("critical_low", [])
                top_waste = waste[0]["item"] if waste else None
                inventory_context = f"Inventory: top waste item is {top_waste}" if top_waste else ""
                _facts["inventory"] = {"top_waste_item": top_waste,
                                       "critical_low": len(low or []),
                                       "waste_direction": None, "waste_change_pct": None}
                # Pull inventory trend from history
                try:
                    from models import get_conn as _gc_iv
                    _conn_iv = _gc_iv()
                    import json as _json_rpt
                    _prev = _conn_iv.execute(
                        """SELECT waste_json FROM inventory_history
                           WHERE restaurant_id=? AND week_end < date('now','-1 day')
                           ORDER BY week_end DESC LIMIT 1""",
                        (report.restaurant_id,)
                    ).fetchone()
                    _conn_iv.close()
                    if _prev and _prev["waste_json"]:
                        _prev_data = _json_rpt.loads(_prev["waste_json"])
                        _prev_total = _prev_data.get("total_waste_cost", 0)
                        _curr_total = analysis.get("total_waste_cost_week", 0)
                        if _prev_total > 0 and _curr_total > 0:
                            _diff = _curr_total - _prev_total
                            _pct = round(abs(_diff) / _prev_total * 100, 0)
                            if abs(_diff) > 20:
                                _facts["inventory"]["waste_direction"] = "up" if _diff > 0 else "down"
                                _facts["inventory"]["waste_change_pct"] = float(_pct)
                                inventory_context += f" (waste {'UP' if _diff > 0 else 'DOWN'} {int(_pct)}% vs last week)"
                except Exception:
                    pass
                if low:
                    inventory_context += f", {low[0]} critically low"
                if inventory_context:
                    inventory_context += "."
        except Exception:
            pass
        # Pull recent marketing post performance for email context
        try:
            from models import get_conn as _gc_mkt
            _conn_mkt = _gc_mkt()
            _mkt_rows = _conn_mkt.execute(
                """SELECT topic, reach, impressions, likes
                   FROM marketing_content_log
                   WHERE restaurant_id=? AND post_id IS NOT NULL
                     AND (reach > 0 OR impressions > 0 OR likes > 0)
                   ORDER BY created_at DESC LIMIT 5""",
                (report.restaurant_id,)
            ).fetchall()
            _conn_mkt.close()
            if _mkt_rows:
                _best = max(_mkt_rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0))
                _br = (_best["reach"] or 0) + (_best["impressions"] or 0)
                if _br > 0:
                    marketing_context = f"Marketing: best recent post was '{_best['topic']}' ({_br} reach+impr)."
                    _facts["marketing"] = {"best_topic": _best["topic"], "best_reach": int(_br),
                                           "measured_posts": len(_mkt_rows)}
        except Exception:
            pass

        # Cross-module correlation — patterns that span more than one module.
        #
        # Three things were wrong with this and all three are the same
        # mistake: it inspected the RENDERED PROSE instead of the numbers the
        # prose was rendered from. `"UP" in inventory_context` is true of a
        # waste item called SOUP BASE. `"trending UP" in labor_context` breaks
        # the moment that sentence is reworded. And the review floor was
        # `(pos + neg) > 3` — four classified reviews was the entire
        # evidentiary bar for telling an owner their kitchen is understaffed.
        #
        # Now: floats compared to floats, the same review floor the rest of
        # the module uses, and language that says two things moved together
        # rather than that one caused the other. A 90-day co-movement in a
        # restaurant is a prompt to go look, not a finding.
        correlation_context = ""
        # Bound before the try: the import below can fail, and `signals` is
        # read again after the except when the digest is assembled.
        signals = []
        try:
            from notify import MIN_TREND_REVIEWS_PER_WEEK as _MIN_WK_RPT
            _classified = pos + neg
            _enough_reviews = _classified >= _MIN_WK_RPT * 2
            _lab, _inv, _mkt = _facts["labor"], _facts["inventory"], _facts["marketing"]

            _ops_issues = {"food_quality", "wait_time", "service"}
            _food_complaints = any(
                (i[0] if isinstance(i, tuple) else i.get("category", i.get("label", ""))).lower()
                in _ops_issues
                for i in (report.top_issues or [])[:5]
            )

            if _lab and _lab.get("direction") == "up" and _food_complaints and _enough_reviews:
                signals.append(
                    f"Labor moved from {_lab['from_pct']:.1f}% to {_lab['pct']:.1f}% over "
                    f"{_lab['weeks']} weeks in the same period service and kitchen complaints "
                    f"appeared. These moved together; that is not proof one caused the other. "
                    f"Worth checking whether the busiest shifts are the ones the complaints name.")

            _neg_rising = _enough_reviews and neg >= _MIN_WK_RPT and neg > pos
            if _inv and _inv.get("waste_direction") == "up" and _neg_rising:
                signals.append(
                    f"Waste is up {int(_inv['waste_change_pct'])}% on last week and negative "
                    f"reviews ({neg} of {_classified} classified) outnumber positive ones in the "
                    f"same week. Higher-than-expected volume would produce both. Check covers "
                    f"before concluding anything.")

            if _lab and _lab.get("direction") == "down" and _enough_reviews and pos > neg * 2:
                signals.append(
                    f"Labor improved from {_lab['from_pct']:.1f}% to {_lab['pct']:.1f}% and guest "
                    f"sentiment held at {pos} positive against {neg} negative — the schedule came "
                    f"down without the guest experience following it.")

            if _mkt and report.total_reviews < _MIN_WK_RPT:
                signals.append(
                    f"'{_mkt['best_topic']}' reached {_mkt['best_reach']} and only "
                    f"{report.total_reviews} reviews came in this week — reach is not converting "
                    f"into reviews. A review ask in the caption is the cheapest thing to try.")

            if signals:
                correlation_context = (
                    "\n\nCross-module observations (these are CO-MOVEMENTS, not established "
                    "causes — if you mention one, say the two moved together, never that one "
                    "caused the other):\n" + "\n".join(f"- {s}" for s in signals))
        except Exception as _corr_err:
            print(f"[digest] correlation block failed: {_corr_err}")

        # The review module's own root-cause diagnosis, if one exists. This is
        # the difference between a digest that says "food quality was your top
        # theme" — which the owner already knew — and one that says what most
        # likely produced it and what would confirm that.
        diagnosis_context = ""
        try:
            import review_intelligence as _ri_rpt
            _dg = _ri_rpt.get_diagnoses(report.restaurant_id, include_stale=True)
            if _dg:
                _d0 = _dg[0]
                diagnosis_context = (
                    f"\n\nROOT-CAUSE DIAGNOSIS for the '{_d0['category'].replace('_',' ')}' cluster "
                    f"({_d0['mention_count']} negative reviews, {_d0['confidence']} confidence):\n"
                    f"- Most likely cause: {_d0['cause']}\n"
                    + (f"- Alternative: {_d0['alternative_cause']}\n" if _d0.get("alternative_cause") else "")
                    + (f"- What would confirm it: {_d0['what_would_confirm']}\n" if _d0.get("what_would_confirm") else "")
                    + (f"- Recommended: {_d0['recommended_action']}\n" if _d0.get("recommended_action") else "")
                    + "Use this for the ACTION line. Do not substitute a cause of your own.")
        except Exception:
            pass

        extra_context = ""
        if labor_context:
            extra_context += f"\n- {labor_context}"
        if inventory_context:
            extra_context += f"\n- {inventory_context}"
        if marketing_context:
            extra_context += f"\n- {marketing_context}"
        if correlation_context:
            extra_context += correlation_context
        if diagnosis_context:
            extra_context += diagnosis_context

        # Pull last week's stats for comparison.
        #
        # This read `fetched_at` — when Cavnar pulled the review — while every
        # other review surface had moved to the guest's own time axis, and it
        # counted soft-deleted rows that every other query excludes. On a
        # first connect an entire multi-year history arrives stamped with one
        # fetched_at, so "last week" was whatever happened to sync then.
        wow_context = ""
        try:
            from datetime import timedelta
            from models import get_reviews_since, get_conn as _gc_r, REVIEW_TIME_AXIS_BARE as _AX_RPT
            from time_utils import restaurant_now_by_id
            now_chi = restaurant_now_by_id(restaurant_id or report.restaurant_id)
            last_week_start = (now_chi - timedelta(days=14)).isoformat()
            last_week_end = (now_chi - timedelta(days=7)).isoformat()
            _conn_r = _gc_r()
            last_week = _conn_r.execute(
                f"""SELECT COUNT(*) as cnt, AVG(rating) as avg_r FROM reviews
                   WHERE restaurant_id=? AND deleted_at IS NULL
                     AND {_AX_RPT} >= ? AND {_AX_RPT} < ?""",
                (report.restaurant_id, last_week_start, last_week_end)
            ).fetchone()
            _conn_r.close()
            if last_week and last_week["cnt"] > 0:
                diff = report.total_reviews - last_week["cnt"]
                diff_str = f"+{diff}" if diff >= 0 else str(diff)
                avg_diff = round((report.avg_rating or 0) - (last_week["avg_r"] or 0), 1)
                avg_diff_str = f"+{avg_diff}" if avg_diff >= 0 else str(avg_diff)
                wow_context = f"\n- vs last week: {diff_str} reviews, rating {avg_diff_str}"
        except Exception:
            pass

        # Pull 1-2 specific notable reviews to call out by name
        specific_reviews = ""
        try:
            notable = [r for r in reviews if r.urgency == "high" or r.rating == 5][:2]
            if notable:
                lines = []
                for r in notable:
                    # `review_name` is the Google API's RESOURCE name for the
                    # review ("accounts/…/reviews/…"), used for auto-posting —
                    # not a person. Reading it here meant the model was handed
                    # either a URL path or the fallback "A guest", whose first
                    # word is "A" — which is why weekly digests kept naming
                    # "reviewer A". The guest's name is `author`.
                    reviewer = ((r.author or "").strip() or "A guest").split()[0]
                    snippet = (r.text or "")[:100].strip()
                    stars = f"{r.rating}★"
                    lines.append("- " + reviewer + " left a " + stars + " review: " + snippet[:80])
                specific_reviews = "\n" + "\n".join(lines)
            else:
                specific_reviews = " None particularly notable this week."
        except Exception:
            specific_reviews = " No specific reviews to highlight."

        # Response backlog — how many reviews still unresponded
        backlog_context = ""
        try:
            from models import get_conn as _gc_bl
            _conn_bl = _gc_bl()
            _backlog = _conn_bl.execute(
                """SELECT COUNT(*) as cnt FROM reviews
                   WHERE restaurant_id=? AND deleted_at IS NULL
                   AND response_status IN ('pending','drafted')
                   AND draft_response IS NOT NULL AND draft_response != ''""",
                (report.restaurant_id,)
            ).fetchone()
            _conn_bl.close()
            _bl_cnt = _backlog["cnt"] if _backlog else 0
            if _bl_cnt > 0:
                backlog_context = f"\n- {_bl_cnt} review{'s' if _bl_cnt != 1 else ''} still awaiting a response (drafted but not posted)"
        except Exception:
            pass

        greeting = f"Hi {owner_name}" if owner_name else "Hi"
        from time_utils import restaurant_now_by_id as _rnbi_rpt
        today_rpt = _rnbi_rpt(restaurant_id or report.restaurant_id).strftime('%B %d, %Y')

        # Build module context for full system clients
        module_lines = []
        if extra_context:
            module_lines.append(extra_context.strip())

        # Determine active modules for this client
        try:
            from models import get_restaurant as _gr_rpt
            _rest = _gr_rpt(restaurant_id or report.restaurant_id)
            has_labor = _rest and _rest.module_labor
            has_inventory = _rest and _rest.module_inventory
            has_marketing = _rest and _rest.module_marketing
            has_all_four = _rest and all([_rest.module_reviews, _rest.module_labor,
                                          _rest.module_inventory, _rest.module_marketing])
        except Exception:
            has_labor = has_inventory = has_marketing = has_all_four = False

        modules_active = []
        if has_labor: modules_active.append("Labor Optimizer")
        if has_inventory: modules_active.append("Food Cost Control")
        if has_marketing: modules_active.append("Marketing Autopilot")

        # Build per-module instructions.
        #
        # This block used to say, three separate times, that the model must
        # write a line for every active module "even if the data section above
        # is thin or missing for it" and must "always write something specific
        # and useful for it". That is an instruction to manufacture prose out
        # of a missing-data condition, in the one AI artefact that reaches the
        # owner unattended, weekly, signed "the Cavnar AI Consultant". The
        # figure check downstream could not catch it either: a line with no
        # number in it — "Labor is trending up; tighten next week's schedule"
        # — passes `unsupported_figures` clean and gets emailed.
        #
        # A module the client pays for that has no data this week is a real
        # thing worth saying, and saying it is a deterministic Python string,
        # not a generation. `_module_gap_lines` below carries those; the model
        # is only ever asked to write about modules that actually reported.
        _module_data = {"LABOR": bool(labor_context),
                        "INVENTORY": bool(inventory_context),
                        "MARKETING": bool(marketing_context)}
        _active = {"LABOR": has_labor, "INVENTORY": has_inventory, "MARKETING": has_marketing}
        required_lines = ["REVIEWS"]
        for key in ("LABOR", "INVENTORY", "MARKETING"):
            if _active[key] and _module_data[key]:
                required_lines.append(key)
        # Active, paid for, and silent this week. The owner is told plainly
        # rather than being handed a sentence invented about it.
        _MODULE_GAP_COPY = {
            "LABOR": "Labor: no shifts synced this week, so there is no labor read. Upload or reconnect your POS to get one.",
            "INVENTORY": "Food cost: no inventory counted this week, so there is no waste read. Submit a count to get one.",
            "MARKETING": "Marketing: no post performance recorded this week, so there is nothing to measure yet.",
        }
        module_gap_lines = [_MODULE_GAP_COPY[k] for k in ("LABOR", "INVENTORY", "MARKETING")
                            if _active[k] and not _module_data[k]]
        module_instruction = f"""

You MUST output exactly these lines and no others (plus HEADLINE and ACTION): {", ".join(required_lines)}.
- REVIEWS: the rating picture, any multi-week trend that carries a stated confidence, and any urgent reviewer named in the data above
- LABOR: state the labor % and whether it is trending up or down against prior weeks, using only the figures above
- INVENTORY: whether waste improved or worsened against last week with the % change given above, and the top waste item named above
- MARKETING: name the best-performing topic given above and say whether to push it further or change angle

Every module NOT in that list is either switched off for this client or reported no data this week. Write NO line for it. Do not infer what it might have said, do not suggest what it might show, and do not refer to it at all. A module with no data is handled outside this summary — inventing a sentence for it would be inventing a fact about this restaurant's week."""

        prompt = f"""You are the Cavnar AI Consultant writing a weekly digest for {restaurant_name}.

This week's data:
- Total reviews: {report.total_reviews}
- Average rating: {report.avg_rating}/5
- Positive: {pos}, Negative: {neg}
- Urgent reviews: {urgent_count}
- Top themes: {top_themes or "nothing notable"}
- Period: {report.period_start} to {report.period_end}{wow_context}{extra_context}{backlog_context}{module_instruction}

Today: {today_rpt}

Notable reviews:{specific_reviews}

Respond in EXACTLY this structure, one item per line, label followed by a colon, nothing else on the line before the colon. Output ONLY the lines listed above as required (plus HEADLINE and ACTION):

HEADLINE: one sentence — the single most important takeaway this week, addressed to the owner by name ("{greeting},")
REVIEWS: one short sentence on review performance this week
LABOR: one short sentence stating the labor % and whether it's trending up or down
INVENTORY: one short sentence on waste cost and the top waste item
MARKETING: one short sentence on best-performing content or a suggested content angle
ACTION: one specific, concrete next step the owner should take this week

Rules:
- Write a line ONLY for a module listed as required above. Never write a line for a module that is not on that list, for any reason.
- Each line is ONE sentence, plain text, no markdown, no bullets, no bold
- Always use $ signs before dollar amounts ($2,400 not 2400)
- State no figure — a dollar amount, a percentage, a count, a rating — that does not appear in the data above. Not one.
- Name a reviewer only from "Notable reviews" above, spelled as it is written there. Name no one if that section is empty.
- Where a ROOT-CAUSE DIAGNOSIS is given above, the ACTION line comes from its recommendation. Never substitute a cause or a fix of your own — this system cannot confirm one.
- Where a cross-module observation is given above, you may say the two things moved together. Never say one caused the other.
- The ACTION line must always be present and must be concrete (a specific call, message, schedule change, or order — not vague advice)"""

        msg = create_with_retry(
            client,
            model=model_for("reporter"),
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=restaurant_id,
            action="weekly_digest",
        )
        raw = extract_text(msg).strip()
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise ValueError("weekly digest was truncated")
        import re as _re_rpt
        parsed = {}
        for line in raw.split("\n"):
            line = line.strip()
            m = _re_rpt.match(r'^(HEADLINE|REVIEWS|LABOR|INVENTORY|MARKETING|ACTION):\s*(.+)$', line)
            if m:
                parsed[m.group(1).lower()] = m.group(2).strip()
        if not parsed.get("headline"):
            # This used to fall back to {"headline": raw} — so a response
            # whose format drifted put the model's entire output, preamble
            # included, at the top of an email to the client. An unparsed
            # digest is not a digest.
            raise ValueError("weekly digest did not match the expected LABEL: format")

        # Every figure the digest states has to be one it was handed. The
        # prompt says "be specific with real numbers from the data above",
        # which instructs the model to state figures and never checked that a
        # stated figure came from anywhere. A line that invents "$2,400
        # recoverable" is dropped rather than emailed.
        from ai_guard import unsupported_figures
        for key in list(parsed):
            bad = unsupported_figures(parsed[key], prompt)
            if bad:
                print(f"[digest] dropped {key} line — unsupported figures {bad}")
                try:
                    import ops
                    ops.capture(RuntimeError(f"digest {key} line stated {bad} — not in the input"),
                                job="weekly_digest", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
                parsed.pop(key)

        # A module line the prompt did not ask for is, by construction, a line
        # about a module with no data this week — exactly the fabrication the
        # instruction above was rewritten to prevent. Enforced here as well as
        # asked for, because a prompt rule is a request and this email goes
        # out with nobody reading it first.
        _allowed = {k.lower() for k in required_lines} | {"headline", "action"}
        for key in [k for k in parsed if k not in _allowed]:
            print(f"[digest] dropped {key} line — that module reported no data this week")
            try:
                import ops
                ops.capture(RuntimeError(f"digest wrote a {key} line for a module with no data"),
                            job="weekly_digest", context=f"restaurant_id={restaurant_id}")
            except Exception:
                pass
            parsed.pop(key)

        if not parsed.get("headline"):
            raise ValueError("weekly digest headline stated figures that were not in the data")
        # Modules the client pays for that reported nothing. Deterministic
        # copy, never generated — see the module_instruction comment.
        if module_gap_lines:
            parsed["_data_gaps"] = module_gap_lines
        # Carried out so render_html can show them even when the model
        # ignored them — they are measured, not generated.
        if signals:
            parsed["_correlations"] = signals
        return parsed
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="weekly_digest", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return {}

def _follow_through_sections(restaurant_id, owner_view=False):
    """The executive-review half of the weekly digest: what came of the
    changes the owner made, where the goals stand, whether issues got
    handled, and any comp/void pattern worth a look.

    Deterministic — every line is read from the module that measures it, so
    this half of the email cannot state a figure the data doesn't carry. The
    loss signal can name an approving manager, so it appears only when the
    caller vouches that the recipient is a principal login (`owner_view`) —
    the preview routes send to whoever is signed in, managers included. Each
    block is optional and independently guarded: a failure drops that block,
    never the email.
    """
    from emails import BRAND, report_eyebrow, report_paragraph
    out = []
    if not restaurant_id:
        return out

    def _list(lines):
        return "<br>".join(_html.escape(l) for l in lines)

    try:
        import outcomes
        res = outcomes.recent_results(restaurant_id, days=7) or []
        if res:
            out.append(report_eyebrow("What your changes did") + report_paragraph(
                _list(outcomes.summarise(r) for r in res[:4]))
                + report_paragraph(f'<span style="font-size:12.5px;color:{BRAND["muted"]}">'
                                   f'{_html.escape(outcomes.CAUSATION_CAVEAT)}</span>'))
    except Exception as e:
        log.warning("digest outcomes block failed: %s", e)
    try:
        import goals
        gl = [g for g in (goals.progress(restaurant_id) or []) if g.get("state") != "unknown"]
        if gl:
            out.append(report_eyebrow("Goals") + report_paragraph(_list(goals.summarise(g) for g in gl[:4])))
    except Exception as e:
        log.warning("digest goals block failed: %s", e)
    try:
        import issues
        sm = issues.summary(restaurant_id)
        if sm and (sm["open"] or sm["acknowledged"] or sm.get("resolved_last_7_days")):
            bits = [f"{sm.get('resolved_last_7_days', 0)} resolved this week",
                    f"{sm['acknowledged']} in hand", f"{sm['open']} not yet acknowledged"]
            out.append(report_eyebrow("Issues", BRAND["bad"] if sm["open"] else None)
                       + report_paragraph(_html.escape(", ".join(bits) + ".")))
    except Exception as e:
        log.warning("digest issues block failed: %s", e)
    try:
        import menu_intelligence
        sg = (menu_intelligence.reprice_suggestions(restaurant_id) or {}).get("suggestions") or []
        # Only the ones worth a conversation: a dollar a month of lost margin
        # is not a weekly-review item.
        worth = [x for x in sg if (x.get("monthly_margin_lost") or 0) >= 25][:3]
        if worth:
            out.append(report_eyebrow("Prices to revisit") + report_paragraph(_list(
                f"{x['dish']}: {(x.get('drivers') or [{}])[0].get('ingredient', 'ingredient costs')} "
                f"rose, about ${x['monthly_margin_lost']:,.0f}/month of margin — "
                f"${x['suggested_price']:.2f} restores the old food cost %"
                for x in worth if x.get("suggested_price"))))
    except Exception as e:
        log.warning("digest reprice block failed: %s", e)
    try:
        import demand
        slow = (demand.slow_days(restaurant_id) or {}).get("slow_days") or []
        if slow:
            d = slow[0]
            out.append(report_eyebrow("Your quietest day") + report_paragraph(_html.escape(
                f"{d['day']}s run about {abs(d['vs_average_pct'])}% under a normal day "
                f"({d['samples']} weeks of history). A text to the guest club aimed at that "
                f"day is the cheapest thing that moves it — Cavnar tracks what it does.")))
    except Exception as e:
        log.warning("digest slow-day block failed: %s", e)
    try:
        import loss_detection
        ls = (loss_detection.signals(restaurant_id) or {}) if owner_view else {}
        flagged = ls.get("flagged") or []
        if flagged:
            out.append(report_eyebrow("Comps & voids — worth a look", BRAND["bad"]) + report_paragraph(
                _list(f"{f['headline']}. Could also be {f['alternative']}." for f in flagged[:3]))
                + report_paragraph(f'<span style="font-size:12.5px;color:{BRAND["muted"]}">'
                                   f'{_html.escape(ls.get("note") or "")}</span>'))
    except Exception as e:
        log.warning("digest loss block failed: %s", e)
    return out


def _digest_parts(report: WeeklyReport, restaurant_name: str, owner_name: str = None,
                  restaurant_id: int = None, owner_view: bool = False) -> dict:
    """The weekly digest's sections for ONE restaurant, without the shell.

    Split out of render_html so a multi-location owner can get every
    location in one email (render_group_html) — the dedup-by-address in
    scheduler.run_weekly_digests used to solve "three identical-looking
    digests" by dropping two restaurants' weeks on the floor.

    The weekly digest, on the same layout as every other Cavnar AI email.

    Two things were wrong with this and both were structural. It rendered
    DARK while everything else Cavnar AI sends is a light card — not because
    anyone chose a dark email, but because the web dashboard quietly POSTs
    its own dark-mode switch to /api/theme, and this template read that
    column. A UI preference for the dashboard was deciding how outbound mail
    looked. And it stacked five separately bordered, separately tinted cards
    inside a sixth, each repeating the same label/pill/figures furniture, so
    a week with four modules arrived as a wall of boxes.

    Now: one card, hairline rules, one section per module, and the AI
    consultant's sentence about a module sits under that module's own
    numbers instead of in a second block that said it all over again.
    """
    from emails import (BRAND, report_shell, report_stats, report_eyebrow,
                        report_quote, report_action, report_paragraph)

    _SANS = "-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif"

    def snip(text, limit=150):
        """Cut a quote at a word boundary. A hard character slice left guests
        mid-word ("borderline r…"), which reads like the email broke."""
        text = (text or "").strip()
        if len(text) <= limit:
            return _html.escape(text)
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit * 0.6:
            cut = cut[:space]
        return _html.escape(cut.rstrip(" ,.;:-")) + "&hellip;"

    def note(text, top=14):
        """The consultant's one sentence about a module, set directly under that
        module's own figures — it used to live in a separate card above them and
        say the same things a second time."""
        if not text:
            return ""
        return (f'<div style="font-family:{_SANS};font-size:13.5px;color:{BRAND["body"]};'
                f'line-height:1.6;margin-top:{top}px">{_html.escape(text)}</div>')

    reviews = getattr(report, "_reviews", [])
    urgent = [r for r in reviews if r.urgency == "high"]
    pos_count = report.sentiment.get("positive", 0)
    neg_count = report.sentiment.get("negative", 0)
    first_name = (owner_name or "").split()[0] if owner_name else "there"

    rating = report.avg_rating or 0
    if rating >= 4.5:
        rating_color, rating_label = BRAND["good"], "Excellent"
    elif rating >= 4.0:
        rating_color, rating_label = BRAND["good"], "Good"
    elif rating >= 3.5:
        rating_color, rating_label = BRAND["warn"], "Fair"
    else:
        rating_color, rating_label = BRAND["bad"], "Needs work"

    _rest = None
    try:
        from models import get_restaurant as _gr_d
        _rest = _gr_d(restaurant_id or report.restaurant_id)
    except Exception:
        pass
    location_label = ""
    if _rest and getattr(_rest, "location_name", None):
        location_label = f" &middot; {_html.escape(_rest.location_name)}"

    ai_summary = generate_ai_digest_summary(report, restaurant_name, owner_name,
                                            restaurant_id=restaurant_id)
    # The AI headline when there is one. When the model call fails it
    # returns {}, and this used to fall back to "Here's the week at X,
    # Erik." — a greeting with no information, in the opening slot of the
    # most-sent email in the product. The deterministic weekly headline is
    # measured from the same data and says something.
    ai_headline = ai_summary.get("headline")
    if not ai_headline:
        try:
            import weekly_review as _wr
            ai_headline = _wr.headline(_wr.build(restaurant_id or report.restaurant_id))
        except Exception as _hl_err:
            print(f"[digest] deterministic headline failed: {_hl_err}")
            ai_headline = f"Here's the week at {restaurant_name}, {first_name}."

    sections = [report_paragraph(_html.escape(ai_headline))]

    # Cross-module co-movements. These are MEASURED (floats compared to
    # floats in generate_ai_digest_summary), not generated — they were only
    # ever passed to the model as prompt context, so a model failure threw
    # away the one part of this email that relates two modules to each
    # other. Rendered here so they survive it.
    _correlations = ai_summary.get("_correlations") or []
    if _correlations:
        sections.append(
            report_eyebrow("Moving together")
            + report_paragraph("<br><br>".join(_html.escape(c) for c in _correlations[:2]))
            + report_paragraph(f'<span style="font-size:12.5px;color:{BRAND["muted"]}">'
                               f'Co-movements, not proven causes — two things moved in the '
                               f'same weeks.</span>'))

    # ── The week as a business week ────────────────────────────────────────
    # This digest is the one thing Cavnar AI sends every single week, and it
    # opened with a review count — so the clearest weekly statement the
    # product made about what matters was "reviews". The monthly email was
    # rebuilt to read like a P&L; these blocks are the same four questions
    # over seven days, in the same voice, above the review content. Owner
    # view only: they carry money, and a manager's digest should not.
    if owner_view:
        try:
            from emails import _weekly_review_sections
            sections.extend(_weekly_review_sections(restaurant_id or report.restaurant_id))
        except Exception as e:
            print(f"[digest] weekly review sections failed: {e}")

    # ── Review Intelligence ────────────────────────────────────────────────
    sections.append(
        report_eyebrow("Review Intelligence", tag=rating_label, tag_color=rating_color) +
        report_stats([
            (f"{rating}&#9733;", "avg rating", rating_color),
            (report.total_reviews, "reviews"),
            (pos_count, "positive", BRAND["good"] if pos_count else None),
            (neg_count, "negative", BRAND["bad"] if neg_count else None),
            ((len(urgent), "urgent", BRAND["bad"]) if urgent else None),
        ]) + note(ai_summary.get("reviews"))
    )

    # ── Labor Optimizer ────────────────────────────────────────────────────
    try:
        if _rest and _rest.module_labor:
            from labor import analyse_shifts_for_restaurant
            labor_data = analyse_shifts_for_restaurant(restaurant_id)
            if labor_data:
                lp = labor_data.get("overall_labor_pct", 0)
                ls = labor_data.get("total_sales", 0)
                lc = labor_data.get("total_labor_cost", 0)
                l_color = BRAND["good"] if lp <= 32 else (BRAND["warn"] if lp <= 36 else BRAND["bad"])
                l_label = "On target" if lp <= 32 else ("Watch closely" if lp <= 36 else "Over budget")
                sections.append(
                    report_eyebrow("Labor Optimizer", tag=l_label, tag_color=l_color) +
                    report_stats([
                        (f"{lp}%", "labor ratio", l_color),
                        (f"${lc:,.0f}", "labor cost"),
                        (f"${ls:,.0f}", "in sales"),
                    ]) + note(ai_summary.get("labor"))
                )
    except Exception:
        pass

    # ── Food Cost Control ──────────────────────────────────────────────────
    try:
        from inventory import analysis_for
        items, _inv_live, inv = (None, False, {})
        if _rest and _rest.module_inventory:
            items, _inv_live, inv = analysis_for(restaurant_id)
        # Sample-pantry figures must never reach an outbound report.
        if items and _inv_live:
            waste = inv.get("total_waste_cost_week", 0)
            recoverable = inv.get("recoverable_monthly", 0)
            top_waste = inv.get("waste_items", [])
            i_color = BRAND["good"] if waste < 200 else (BRAND["warn"] if waste < 500 else BRAND["bad"])
            i_label = "Low waste" if waste < 200 else ("Moderate" if waste < 500 else "High waste")
            top_line = ""
            if top_waste:
                top_line = (f'<div style="font-family:{_SANS};font-size:12px;color:{BRAND["muted"]};'
                            f'margin-top:12px">Biggest single loss: '
                            f'<span style="color:{BRAND["ink"]};font-weight:600">'
                            f'{_html.escape(str(top_waste[0]["item"]))}</span></div>')
            sections.append(
                report_eyebrow("Food Cost Control", tag=i_label, tag_color=i_color) +
                report_stats([
                    (f"${waste:,.0f}", "waste this week", i_color),
                    (f"${recoverable:,.0f}", "recoverable / mo"),
                ]) + top_line + note(ai_summary.get("inventory"))
            )
    except Exception:
        pass

    # ── Marketing (no weekly figures to report — the sentence is the whole
    #    section, so it only appears when there is genuinely something) ─────
    if _rest and getattr(_rest, "module_marketing", False) and ai_summary.get("marketing"):
        sections.append(report_eyebrow("Marketing Autopilot")
                        + note(ai_summary.get("marketing"), top=0))

    # ── The reviews themselves ─────────────────────────────────────────────
    if urgent:
        quotes = ""
        for r in urgent[:2]:
            quotes += report_quote(
                _html.escape((r.author or "Guest")[:24]),
                "★" * r.rating, snip(r.text), BRAND["bad"])
        sections.append(report_eyebrow("Needs a reply", BRAND["bad"]) + quotes)

    top_pos = next((r for r in reviews if r.sentiment == "positive" and r.rating >= 4), None)
    if top_pos:
        sections.append(
            report_eyebrow("Highlight of the week", BRAND["good"]) +
            report_quote(
                _html.escape((top_pos.author or "Guest")[:24]),
                "★" * top_pos.rating, snip(top_pos.text), BRAND["good"]))

    sections.extend(_follow_through_sections(restaurant_id or report.restaurant_id, owner_view=owner_view))

    if ai_summary.get("action"):
        sections.append(report_action("This week's move", _html.escape(ai_summary["action"])))

    # Modules this client pays for that reported nothing this week. The digest
    # used to instruct the model to write a line for these anyway ("always
    # write something specific and useful for it"), which turned a missing
    # sync into a confident sentence about the restaurant's week. Saying so
    # plainly is both honest and more actionable — it names the thing the
    # owner can fix to get the module working.
    for gap in (ai_summary.get("_data_gaps") or []):
        sections.append(report_paragraph(_html.escape(gap)))

    from time_utils import restaurant_now_by_id as _rnbi_html
    week_label = (_rnbi_html(restaurant_id or report.restaurant_id)
                  .strftime("Week of %B %-d, %Y"))

    return {"sections": sections, "week_label": week_label, "location_label": location_label,
            "rating": report.avg_rating, "total": report.total_reviews}


def render_html(report: WeeklyReport, restaurant_name: str, owner_name: str = None,
                restaurant_id: int = None, owner_view: bool = False) -> str:
    """One restaurant's digest in the standard shell."""
    from emails import report_shell
    p = _digest_parts(report, restaurant_name, owner_name, restaurant_id, owner_view)
    return report_shell(
        kicker="Weekly Digest",
        title=_html.escape(restaurant_name),
        subtitle=f"{p['week_label']}{p['location_label']}",
        sections=p["sections"],
        cta_label="Open your dashboard →",
    )


def render_group_html(items: list, owner_name: str = None, group_name: str = None) -> str:
    """Every location an owner runs, in one email: a portfolio line on top,
    then each location's own sections under its own eyebrow.

    `items` is [(restaurant, WeeklyReport)]. The portfolio line is the same
    read home_brief's group brief makes — strongest and weakest by rating —
    so the email and the dashboard agree on which location needs the
    owner first.
    """
    from emails import BRAND, report_shell, report_eyebrow, report_rule, report_paragraph
    parts = []
    for rest, rep in items:
        parts.append((rest, _digest_parts(rep, rest.name, owner_name, rest.id, True)))
    rated = [(r, p) for r, p in parts if p.get("rating")]
    sections = []
    if len(rated) >= 2:
        best = max(rated, key=lambda x: x[1]["rating"])
        worst = min(rated, key=lambda x: x[1]["rating"])
        sections.append(report_paragraph(
            f"Across {len(items)} locations this week: "
            f"<strong>{_html.escape(best[0].location_name or best[0].name)}</strong> led at "
            f"{best[1]['rating']:.1f}&#9733;, "
            f"<strong>{_html.escape(worst[0].location_name or worst[0].name)}</strong> trailed at "
            f"{worst[1]['rating']:.1f}&#9733;."))
    for i, (rest, p) in enumerate(parts):
        if i:
            sections.append(report_rule(30))
        label = rest.location_name or rest.name
        tag = f"{p['rating']:.1f}&#9733;" if p.get("rating") else None
        sections.append(report_eyebrow(_html.escape(label), color=BRAND["ember"], tag=tag))
        sections.extend(p["sections"])
    week_label = parts[0][1]["week_label"] if parts else ""
    return report_shell(
        kicker=f"Weekly Digest · {len(items)} locations",
        title=_html.escape(group_name or items[0][0].name),
        subtitle=week_label,
        sections=sections,
        cta_label="Open your dashboard →",
    )


def print_console_report(report: WeeklyReport, restaurant_name: str):
    """Fallback when SMTP isn't configured — prints a readable summary."""
    reviews = getattr(report, "_reviews", [])
    print(f"\n{'═'*60}")
    print(f"  WEEKLY DIGEST — {restaurant_name}")
    print(f"  {report.period_start} – {report.period_end}")
    print(f"{'═'*60}")
    print(f"  Total reviews : {report.total_reviews}")
    print(f"  Avg rating    : {report.avg_rating} / 5.0")
    print(f"  Positive      : {report.sentiment.get('positive',0)}")
    print(f"  Neutral       : {report.sentiment.get('neutral',0)}")
    print(f"  Negative      : {report.sentiment.get('negative',0)}")
    if report.top_issues:
        issues = ", ".join(f"{c.replace('_',' ')} ({n})" for c, n in report.top_issues)
        print(f"  Top themes    : {issues}")
    print()

    urgent = [r for r in reviews if r.urgency == "high"]
    if urgent:
        print(f"  ⚠  URGENT ({len(urgent)} review{'s' if len(urgent)>1 else ''}):")
        for r in urgent:
            print(f"     [{r.id}] {_stars(r.rating)} {r.author}: {r.text[:80]}...")
        print()

    print("  ALL REVIEWS:")
    for r in reviews:
        sentiment_marker = {"positive":"✓","neutral":"–","negative":"✗"}.get(r.sentiment,"?")
        print(f"\n  {sentiment_marker} {_stars(r.rating)}  {r.author}  [{r.platform}]")
        print(f"    {r.text[:100]}{'...' if len(r.text)>100 else ''}")
        if r.draft_response:
            print(f"    → DRAFT: {r.draft_response[:120]}{'...' if len(r.draft_response)>120 else ''}")
    print(f"\n{'═'*60}\n")


def send_digest(report: WeeklyReport, restaurant_name: str, to_email: str):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_from = os.getenv("SMTP_FROM")

    if not all([smtp_host, smtp_user, smtp_pass, smtp_from]):
        print("  (SMTP not configured — printing to console instead)")
        print_console_report(report, restaurant_name)
        return

    html = render_html(report, restaurant_name)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your weekly reviews — {restaurant_name}"
    msg["From"]    = smtp_from
    msg["To"]      = to_email
    msg.attach(MIMEText(_html_doc(html), "html"))

    with smtplib.SMTP_SSL(smtp_host, 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_from, to_email, msg.as_string())
    print(f"  Digest sent to {to_email}")


# Patch for test/demo: allow custom db_path passthrough
def build_report_from_db(restaurant_id: int, restaurant_name: str,
                          days: int = 7, db_path: str = None) -> WeeklyReport:
    """Like build_report but accepts an explicit db_path for testing."""
    from models import get_reviews_since as _grs
    if db_path:
        reviews = _grs(restaurant_id,
                       (datetime.now() - timedelta(days=days)).isoformat(),
                       db_path=db_path)
    else:
        reviews = _grs(restaurant_id,
                       (datetime.now() - timedelta(days=days)).isoformat())

    report = WeeklyReport(
        restaurant_id=restaurant_id,
        period_start=(datetime.now() - timedelta(days=days)).strftime("%b %d"),
        period_end=datetime.now().strftime("%b %d, %Y"),
    )
    if not reviews:
        return report

    report.total_reviews = len(reviews)
    report.avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1)
    cat_counts: dict = {}
    for r in reviews:
        report.sentiment[r.sentiment or "neutral"] += 1
        for cat in (r.categories or []):
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
    report.top_issues = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    report._reviews = reviews
    return report
