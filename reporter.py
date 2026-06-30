import os, json, smtplib, html as _html
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from models import get_reviews_since, save_weekly_report, WeeklyReport

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
            from datetime import datetime, timedelta
            from models import get_reviews_since, get_conn as _gc_r
            from zoneinfo import ZoneInfo
            now_chi = datetime.now(ZoneInfo('America/Chicago'))
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
                    reviewer = (r.review_name or "A guest").split()[0]
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
        from datetime import datetime as _dt_rpt
        from zoneinfo import ZoneInfo as _ZI_rpt
        today_rpt = _dt_rpt.now(_ZI_rpt('America/Chicago')).strftime('%B %d, %Y')

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
        if has_inventory: modules_active.append("Inventory Control")
        if has_marketing: modules_active.append("Marketing Autopilot")

        # Build per-module instructions
        module_instruction = ""
        if has_all_four:
            module_instruction = """

This client has all 4 modules active. Write 4-5 sentences total covering ALL of these:
- Reviews: overall rating picture, call out any multi-week trend if present, mention any urgent reviewer by name
- Labor: state the labor % and explicitly note if it is trending up or down vs prior weeks (data provided above)
- Inventory: mention whether waste improved or worsened vs last week (% change if available), name the top waste item
- Marketing: if post performance data is available, name the best-performing topic and suggest the owner double down on it or try a specific new angle
Do NOT focus only on reviews. Every module must get a specific data point — no vague sentences."""
        elif modules_active:
            active_list = "Review Intelligence, " + ", ".join(modules_active)
            module_instruction = f"\n\nActive modules: {active_list}. Cover each active module — not just reviews."

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

Respond in EXACTLY this structure, one item per line, label followed by a colon, nothing else on the line before the colon:

HEADLINE: one sentence — the single most important takeaway this week, addressed to the owner by name ("{greeting},")
REVIEWS: one short sentence on review performance this week, only include this line if reviews data exists
LABOR: one short sentence stating the labor % and whether it's trending up or down, only include this line if labor data exists
INVENTORY: one short sentence on waste cost and the top waste item, only include this line if inventory data exists
MARKETING: one short sentence on best-performing content or a suggested angle, only include this line if marketing data exists
ACTION: one specific, concrete next step the owner should take this week

Rules:
- Omit a label entirely (do not write the line) if that module has no data — do not write "N/A" or filler
- Each line is ONE sentence, plain text, no markdown, no bullets, no bold
- Always use $ signs before dollar amounts ($2,400 not 2400)
- Be specific with real numbers from the data above
- Do not list every review — only mention a reviewer by name if they stand out
- The ACTION line must always be present and must be concrete (a specific call, message, schedule change, or order — not vague advice)"""

        msg = client.messages.create(
            model=os.getenv("CLAUDE_REPORTER_MODEL", "claude-sonnet-4-6"),
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        import re as _re_rpt
        parsed = {}
        for line in raw.split("\n"):
            line = line.strip()
            m = _re_rpt.match(r'^(HEADLINE|REVIEWS|LABOR|INVENTORY|MARKETING|ACTION):\s*(.+)$', line)
            if m:
                parsed[m.group(1).lower()] = m.group(2).strip()
        return parsed if parsed.get("headline") else {"headline": raw}
    except Exception as e:
        return {}

def render_html(report: WeeklyReport, restaurant_name: str, owner_name: str = None,
                restaurant_id: int = None) -> str:
    reviews = getattr(report, "_reviews", [])
    urgent  = [r for r in reviews if r.urgency == "high"]
    pos_count = report.sentiment.get("positive", 0)
    neg_count = report.sentiment.get("negative", 0)
    first_name = (owner_name or "").split()[0] if owner_name else "there"

    # Rating trend indicator
    rating = report.avg_rating or 0
    if rating >= 4.5:
        rating_color = "#16a34a"; rating_label = "Excellent"
    elif rating >= 4.0:
        rating_color = "#2d6a4f"; rating_label = "Good"
    elif rating >= 3.5:
        rating_color = "#d97706"; rating_label = "Fair"
    else:
        rating_color = "#dc2626"; rating_label = "Needs work"

    # AI consultant summary — structured dict: headline, reviews, labor, inventory, marketing, action
    ai_summary = generate_ai_digest_summary(report, restaurant_name, owner_name,
                                             restaurant_id=restaurant_id)
    _module_colors = {"reviews": "#ff8a65", "labor": "#6fcf97", "inventory": "#ffc266", "marketing": "#7fb8e6"}
    _module_labels = {"reviews": "Reviews", "labor": "Labor", "inventory": "Inventory", "marketing": "Marketing"}
    ai_headline = ai_summary.get("headline") or f"Hi {first_name}, here is your weekly summary for {restaurant_name}."
    ai_module_rows = ""
    for _key in ("reviews", "labor", "inventory", "marketing"):
        _val = ai_summary.get(_key)
        if _val:
            ai_module_rows += f"""
<tr><td style="padding:0 0 8px"><table cellpadding="0" cellspacing="0"><tr>
  <td valign="top" style="padding-right:10px;white-space:nowrap"><span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:{_module_colors[_key]}">{_module_labels[_key]}</span></td>
  <td style="font-size:13px;color:#f0ebe0;line-height:1.55">{_html.escape(_val)}</td>
</tr></table></td></tr>"""
    ai_action_block = ""
    if ai_summary.get("action"):
        ai_action_block = f"""
<div style="margin-top:14px;padding-top:12px;border-top:1px solid rgba(255,255,255,.12)">
  <span style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f">→ This week's action</span>
  <p style="margin:5px 0 0;font-size:13px;color:#f0ebe0;line-height:1.55;font-weight:600">{_html.escape(ai_summary.get("action"))}</p>
</div>"""

    # Pull labor and inventory data for module scorecards
    labor_card = ""
    inventory_card = ""
    try:
        from models import get_restaurant as _gr_d
        _rest = _gr_d(restaurant_id) if restaurant_id else None
        if _rest and _rest.module_labor:
            from labor import analyse_shifts_for_restaurant
            labor_data = analyse_shifts_for_restaurant(restaurant_id)
            if labor_data:
                lp = labor_data.get("overall_labor_pct", 0)
                ls = labor_data.get("total_sales", 0)
                lc = labor_data.get("total_labor_cost", 0)
                l_color = "#16a34a" if lp <= 32 else ("#d97706" if lp <= 36 else "#dc2626")
                l_label = "On target" if lp <= 32 else ("Watch closely" if lp <= 36 else "Over budget")
                labor_card = f"""
<tr><td style="padding:0 0 12px">
  <div style="background:#f9fafb;border-radius:8px;padding:16px;border-left:4px solid {l_color}">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280">Labor Optimizer</span>
      <span style="font-size:11px;font-weight:600;color:{l_color};background:{l_color}18;padding:2px 8px;border-radius:12px">{l_label}</span>
    </div>
    <div style="display:flex;gap:20px">
      <div><div style="font-size:26px;font-weight:700;color:{l_color}">{lp}%</div><div style="font-size:11px;color:#9ca3af">labor ratio</div></div>
      <div><div style="font-size:26px;font-weight:700;color:#111">${lc:,.0f}</div><div style="font-size:11px;color:#9ca3af">labor cost</div></div>
      <div><div style="font-size:26px;font-weight:700;color:#111">${ls:,.0f}</div><div style="font-size:11px;color:#9ca3af">in sales</div></div>
    </div>
  </div>
</td></tr>"""
        if _rest and _rest.module_inventory:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            items, _ = load_inventory_for_restaurant(restaurant_id)
            inv = analyse_inventory(items)
            waste = inv.get("total_waste_cost_week", 0)
            recoverable = inv.get("recoverable_monthly", 0)
            top_waste = inv.get("waste_items", [])
            top_item = top_waste[0]["item"] if top_waste else "None"
            i_color = "#16a34a" if waste < 200 else ("#d97706" if waste < 500 else "#dc2626")
            i_label = "Low waste" if waste < 200 else ("Moderate" if waste < 500 else "High waste")
            inventory_card = f"""
<tr><td style="padding:0 0 12px">
  <div style="background:#f9fafb;border-radius:8px;padding:16px;border-left:4px solid {i_color}">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280">Inventory Control</span>
      <span style="font-size:11px;font-weight:600;color:{i_color};background:{i_color}18;padding:2px 8px;border-radius:12px">{i_label}</span>
    </div>
    <div style="display:flex;gap:20px">
      <div><div style="font-size:26px;font-weight:700;color:{i_color}">${waste:,.0f}</div><div style="font-size:11px;color:#9ca3af">waste this week</div></div>
      <div><div style="font-size:26px;font-weight:700;color:#111">${recoverable:,.0f}</div><div style="font-size:11px;color:#9ca3af">recoverable/mo</div></div>
      <div style="max-width:120px"><div style="font-size:13px;font-weight:600;color:#111;padding-top:4px">{top_item}</div><div style="font-size:11px;color:#9ca3af">top waste item</div></div>
    </div>
  </div>
</td></tr>"""
    except Exception:
        pass

    # Urgent reviews section
    urgent_rows = ""
    if urgent:
        for r in urgent[:3]:
            stars = "★" * r.rating + "☆" * (5 - r.rating)
            name = _html.escape((r.author or "Guest")[:20])
            snippet = _html.escape((r.text or "")[:120])
            urgent_rows += f"""
<tr><td style="padding:0 0 8px">
  <div style="background:#fef2f2;border-radius:6px;padding:12px 14px;border-left:3px solid #dc2626">
    <div style="display:flex;justify-content:space-between;margin-bottom:4px">
      <span style="font-size:12px;font-weight:600">{name}</span>
      <span style="font-size:12px;color:#dc2626">{stars}</span>
    </div>
    <p style="font-size:12px;color:#374151;margin:0;line-height:1.5">"{snippet}{"..." if len(r.text or "") > 120 else ""}"</p>
  </div>
</td></tr>"""

    # Top positive review
    top_pos = next((r for r in reviews if r.sentiment == "positive" and r.rating >= 4), None)
    pos_row = ""
    if top_pos:
        stars = "★" * top_pos.rating
        name = _html.escape((top_pos.author or "Guest")[:20])
        snippet = _html.escape((top_pos.text or "")[:120])
        pos_row = f"""
<tr><td style="padding:0 0 8px">
  <div style="background:#f0fdf4;border-radius:6px;padding:12px 14px;border-left:3px solid #16a34a">
    <div style="display:flex;justify-content:space-between;margin-bottom:4px">
      <span style="font-size:12px;font-weight:600">{name}</span>
      <span style="font-size:12px;color:#16a34a">{stars}</span>
    </div>
    <p style="font-size:12px;color:#374151;margin:0;line-height:1.5">"{snippet}{"..." if len(top_pos.text or "") > 120 else ""}"</p>
  </div>
</td></tr>"""

    from datetime import datetime as _dt_html
    from zoneinfo import ZoneInfo as _ZI_html
    week_label = _dt_html.now(_ZI_html("America/Chicago")).strftime("Week of %B %d, %Y")

    # Pre-build conditional HTML sections to avoid f-string nesting issues
    urgent_section_html = ""
    if urgent:
        urgent_section_html = ('<tr><td style="padding:0 32px 12px">' +
            '<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#dc2626;margin-bottom:8px">⚠ Needs Immediate Response</div>' +
            '<table width="100%" cellpadding="0" cellspacing="0">' + urgent_rows + '</table></td></tr>')
    pos_section_html = ""
    if pos_row:
        pos_section_html = ('<tr><td style="padding:0 32px 12px">' +
            '<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#16a34a;margin-bottom:8px">★ Highlight of the Week</div>' +
            '<table width="100%" cellpadding="0" cellspacing="0">' + pos_row + '</table></td></tr>')
    urgent_stat = (f"<div><div style='font-size:26px;font-weight:700;color:#dc2626'>⚠ {len(urgent)}</div><div style='font-size:11px;color:#9ca3af'>urgent</div></div>" if urgent else "")

    return f"""<html>
<body style="margin:0;padding:0;background:#f5f3f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f3f0;padding:24px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">

<!-- HEADER -->
<tr><td style="background:#1a1410;padding:24px 32px">
  <table width="100%"><tr>
    <td><span style="font-family:Georgia,serif;font-size:22px;font-weight:400;color:#f0ebe0">Cavnar <em style="color:#c84b2f">AI</em></span></td>
    <td align="right"><span style="font-size:11px;color:#7a6f65;letter-spacing:.1em;text-transform:uppercase">Weekly Digest</span></td>
  </tr></table>
  <div style="margin-top:6px;font-size:13px;color:#9a8f85">{restaurant_name} &nbsp;·&nbsp; {week_label}</div>
</td></tr>

<!-- AI CONSULTANT SUMMARY -->
<tr><td style="padding:24px 32px 0">
  <div style="background:#1a1410;border-radius:8px;padding:18px 20px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#c84b2f;margin-bottom:10px">Cavnar AI Consultant</div>
    <p style="font-size:14px;font-weight:600;color:#f0ebe0;line-height:1.55;margin:0 0 12px">{_html.escape(ai_headline)}</p>
    {f'<table cellpadding="0" cellspacing="0" width="100%">{ai_module_rows}</table>' if ai_module_rows else ''}
    {ai_action_block}
  </div>
</td></tr>

<!-- REVIEW SCORECARD -->
<tr><td style="padding:24px 32px 12px">
  <table width="100%" cellpadding="0" cellspacing="0">
  <tr><td style="padding:0 0 12px">
    <div style="background:#f9fafb;border-radius:8px;padding:16px;border-left:4px solid {rating_color}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#6b7280">Review Intelligence</span>
        <span style="font-size:11px;font-weight:600;color:{rating_color};background:{rating_color}18;padding:2px 8px;border-radius:12px">{rating_label}</span>
      </div>
      <div style="display:flex;gap:20px">
        <div><div style="font-size:26px;font-weight:700;color:{rating_color}">{rating}★</div><div style="font-size:11px;color:#9ca3af">avg rating</div></div>
        <div><div style="font-size:26px;font-weight:700;color:#111">{report.total_reviews}</div><div style="font-size:11px;color:#9ca3af">total reviews</div></div>
        <div><div style="font-size:26px;font-weight:700;color:#16a34a">{pos_count}</div><div style="font-size:11px;color:#9ca3af">positive</div></div>
        <div><div style="font-size:26px;font-weight:700;color:#dc2626">{neg_count}</div><div style="font-size:11px;color:#9ca3af">negative</div></div>
        {urgent_stat}
      </div>
    </div>
  </td></tr>

  {labor_card}
  {inventory_card}
  </table>
</td></tr>

    {urgent_section_html}

    {pos_section_html}

<!-- CTA -->
<tr><td style="padding:0 32px 24px">
  <a href="https://dashboard.cavnar.ai" style="display:block;background:#c84b2f;color:white;text-align:center;padding:13px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;letter-spacing:.04em">View Dashboard & Approve Responses →</a>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#f9fafb;padding:16px 32px;border-top:1px solid #e5e7eb">
  <p style="font-size:11px;color:#9ca3af;margin:0;text-align:center">Cavnar AI &nbsp;·&nbsp; will@cavnar.ai &nbsp;·&nbsp; <a href="https://cavnar.ai" style="color:#9ca3af">cavnar.ai</a></p>
</td></tr>

</table>
</td></tr>
</table>
</body></html>"""


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
    msg.attach(MIMEText(html, "html"))

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
