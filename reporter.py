import os, json, smtplib, html as _html
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from models import get_reviews_since, save_weekly_report, WeeklyReport


def _html_doc(fragment, bg="#f7f4ef"):
    """Wrap a bare fragment in a real HTML document so its background fills
    the mail client's viewport instead of stopping at the content's height
    (the half-cut-off look). Imported lazily: emails.py reads RESEND_API_KEY
    at module scope, and a module-level import here could bind it before
    load_dotenv() runs. See emails._html_document."""
    from emails import html_document
    return html_document(fragment, bg)



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
    draft_section = (
        f'<div style="background:#f8fafc;border-radius:6px;padding:10px 12px;'
        f'font-size:13px;margin-top:8px"><strong>Suggested reply:</strong><br>'
        f'<span style="color:#374151">{r.draft_response}</span></div>'
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
        from ai_utils import create_with_retry, extract_text
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        reviews = getattr(report, "_reviews", [])
        pos = report.sentiment.get("positive", 0)
        neg = report.sentiment.get("negative", 0)
        urgent_count = sum(1 for r in reviews if r.urgency == "high")
        top_themes = ", ".join(cat.replace("_"," ") for cat, n in (report.top_issues or [])[:3])
        # Pull labor and inventory context if available
        labor_context = ""
        inventory_context = ""
        marketing_context = ""
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(report.restaurant_id)
            if labor and labor.get("overall_labor_pct"):
                lp = labor.get("overall_labor_pct", 0)
                ot_risk = labor.get("overtime_risk", [])
                labor_context = f"Labor: {lp:.1f}% of revenue this week"
                if ot_risk:
                    labor_context += f", {len(ot_risk)} overtime risk"
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
                        if _vals[-1] > _vals[0] + 1.5:
                            labor_context += f" — trending UP from {_vals[0]:.1f}% ({len(_vals)} weeks)"
                        elif _vals[-1] < _vals[0] - 1.5:
                            labor_context += f" — trending DOWN from {_vals[0]:.1f}% ({len(_vals)} weeks, improving)"
                except Exception:
                    pass
                labor_context += "."
        except Exception:
            pass
        try:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            inv, _ = load_inventory_for_restaurant(report.restaurant_id)
            if inv:
                analysis = analyse_inventory(inv)
                waste = analysis.get("waste_items", [])
                low = analysis.get("critical_low", [])
                top_waste = waste[0]["item"] if waste else None
                inventory_context = f"Inventory: top waste item is {top_waste}" if top_waste else ""
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
        except Exception:
            pass

        # Cross-module correlation — find patterns that span multiple modules
        correlation_context = ""
        try:
            signals = []
            # Labor up + food quality complaints = understaffed kitchen signal
            _labor_up = "trending UP" in labor_context
            _food_complaints = any(
                (i[0] if isinstance(i, tuple) else i.get("label","")).lower()
                in ("food_quality", "wait_time", "service")
                for i in (report.top_issues or [])[:5]
            )
            if _labor_up and _food_complaints:
                signals.append("Labor % is rising the same period food quality/wait complaints increased — possible understaffing causing kitchen pressure. Worth investigating connection.")

            # Inventory waste up + negative reviews both rising = volume spike signal
            _waste_up = "UP" in inventory_context
            _neg_rising = neg > pos * 0.4 if (pos + neg) > 3 else False
            if _waste_up and _neg_rising:
                signals.append("Food waste and negative reviews are both elevated this week — higher-than-expected volume may be the common cause (over-ordering met by service strain).")

            # Labor trending down + review sentiment improving = scheduling optimization working
            _labor_down = "trending DOWN" in labor_context
            if _labor_down and pos > neg * 2 and pos >= 3:
                signals.append("Labor costs are improving AND guest sentiment is strong — the scheduling adjustments appear to be working without hurting the guest experience.")

            # Marketing performance up + no corresponding review volume increase = awareness not converting
            _mkt_good = marketing_context and "reach+impr" in marketing_context
            _reviews_low = report.total_reviews < 3
            if _mkt_good and _reviews_low:
                signals.append("Social posts are getting good reach but review volume is low — guests are seeing the content but not being prompted to leave reviews. Consider adding a review ask to post captions.")

            if signals:
                correlation_context = "\n\nCross-module patterns detected (mention the most relevant one in your summary):\n" + "\n".join(f"- {s}" for s in signals)
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

        # Pull last week's stats for comparison
        wow_context = ""
        try:
            from datetime import timedelta
            from models import get_reviews_since, get_conn as _gc_r
            from time_utils import restaurant_now_by_id
            now_chi = restaurant_now_by_id(restaurant_id or report.restaurant_id)
            last_week_start = (now_chi - timedelta(days=14)).isoformat()
            last_week_end = (now_chi - timedelta(days=7)).isoformat()
            _conn_r = _gc_r()
            last_week = _conn_r.execute(
                """SELECT COUNT(*) as cnt, AVG(rating) as avg_r FROM reviews
                   WHERE restaurant_id=? AND fetched_at >= ? AND fetched_at < ?""",
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
                   WHERE restaurant_id=? AND response_status IN ('pending','drafted')
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

        # Build per-module instructions — every ACTIVE module must produce a line, even with thin data
        required_lines = ["REVIEWS"]
        if has_labor: required_lines.append("LABOR")
        if has_inventory: required_lines.append("INVENTORY")
        if has_marketing: required_lines.append("MARKETING")
        module_instruction = f"""

Active modules for this client: {", ".join(required_lines)}. You MUST output exactly these lines (plus HEADLINE and ACTION) — never skip an active module even if the data section above is thin or missing for it.
- REVIEWS: overall rating picture, call out any multi-week trend if present, mention any urgent reviewer by name
- LABOR (if active): state the labor % and whether it's trending up or down vs prior weeks; if no prior-week comparison is available, state the current % and that it's the first week of tracking
- INVENTORY (if active): mention whether waste improved or worsened vs last week (% change if available), name the top waste item; if no waste data at all, state that inventory is tracking clean with no waste flagged
- MARKETING (if active): if post performance data is available, name the best-performing topic and suggest doubling down or trying a new angle; if no performance data exists yet, suggest one concrete content idea based on what guests are saying in reviews this week
Do NOT omit an active module's line just because its data section above is empty — always write something specific and useful for it."""

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

Respond in EXACTLY this structure, one item per line, label followed by a colon, nothing else on the line before the colon. Output ONLY the lines listed as required in "Active modules" above (plus HEADLINE and ACTION) — do not add lines for inactive modules, and do not skip lines for active ones:

HEADLINE: one sentence — the single most important takeaway this week, addressed to the owner by name ("{greeting},")
REVIEWS: one short sentence on review performance this week
LABOR: one short sentence stating the labor % and whether it's trending up or down
INVENTORY: one short sentence on waste cost and the top waste item
MARKETING: one short sentence on best-performing content or a suggested content angle
ACTION: one specific, concrete next step the owner should take this week

Rules:
- Only write lines for modules listed as active above — never write a line for an inactive module, and never skip a line for an active one, regardless of how much data is available
- Each line is ONE sentence, plain text, no markdown, no bullets, no bold
- Always use $ signs before dollar amounts ($2,400 not 2400)
- Be specific with real numbers from the data above when available
- Do not list every review — only mention a reviewer by name if they stand out
- The ACTION line must always be present and must be concrete (a specific call, message, schedule change, or order — not vague advice)"""

        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_REPORTER_MODEL", "claude-sonnet-5"),
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
        if not parsed.get("headline"):
            raise ValueError("weekly digest headline stated figures that were not in the data")
        return parsed
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="weekly_digest", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return {}

def render_html(report: WeeklyReport, restaurant_name: str, owner_name: str = None,
                restaurant_id: int = None) -> str:
    """The weekly digest, on the same layout as every other Cavnar AI email.

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
    ai_headline = (ai_summary.get("headline")
                   or f"Here's the week at {restaurant_name}, {first_name}.")

    sections = [report_paragraph(_html.escape(ai_headline))]

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
        if _rest and _rest.module_inventory:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            items, _ = load_inventory_for_restaurant(restaurant_id)
            inv = analyse_inventory(items)
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

    if ai_summary.get("action"):
        sections.append(report_action("This week's move", _html.escape(ai_summary["action"])))

    from time_utils import restaurant_now_by_id as _rnbi_html
    week_label = (_rnbi_html(restaurant_id or report.restaurant_id)
                  .strftime("Week of %B %-d, %Y"))

    return report_shell(
        kicker="Weekly Digest",
        title=_html.escape(restaurant_name),
        subtitle=f"{week_label}{location_label}",
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
    import models as _m
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
