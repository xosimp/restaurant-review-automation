import os, json, smtplib
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
    <strong style="font-size:14px">{r.author}</strong>
    <span style="color:#f59e0b;font-size:16px">{_stars(r.rating)}</span>
  </div>
  <p style="margin:0 0 6px;font-size:14px;color:#1f2937;line-height:1.5">{r.text}</p>
  <div style="font-size:11px;color:{color};text-transform:uppercase;letter-spacing:.04em">
    {r.sentiment} &nbsp;·&nbsp; {r.platform}</div>
  {draft_section}
</div>"""


def generate_ai_digest_summary(report, restaurant_name, owner_name=None):
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
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(report.restaurant_id)
            if labor and labor.get("summary"):
                lp = labor.get("labor_pct", 0)
                ot_risk = labor.get("overtime_risk", [])
                labor_context = f"Labor cost this week: {lp:.1f}% of revenue. Overtime risk: {len(ot_risk)} employee(s)." if lp else ""
        except Exception:
            pass
        try:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            inv = load_inventory_for_restaurant(report.restaurant_id)
            if inv:
                analysis = analyse_inventory(inv)
                waste = analysis.get("top_waste", [])
                low = analysis.get("critical_low", [])
                if waste or low:
                    inventory_context = f"Top food waste item: {waste[0][0] if waste else 'none'}. Critical low stock: {low[0][0] if low else 'none'}."
        except Exception:
            pass

        extra_context = ""
        if labor_context:
            extra_context += f"\n- {labor_context}"
        if inventory_context:
            extra_context += f"\n- {inventory_context}"

        greeting = f"Hi {owner_name}" if owner_name else "Hi"
        prompt = f"""You are the Cavnar AI Consultant writing a brief weekly summary for {restaurant_name}.

This week's data:
- Total reviews: {report.total_reviews}
- Average rating: {report.avg_rating}/5
- Positive: {pos}, Negative: {neg}
- Urgent reviews needing attention: {urgent_count}
- Top themes: {top_themes or "nothing notable"}
- Period: {report.period_start} to {report.period_end}{extra_context}

Today's actual date: {__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/Chicago')).strftime('%B %d, %Y') if True else ''}

Write 2-3 sentences starting with "{greeting}," that:
1. Give the honest overall picture covering the most important metric this week (reviews, labor, or inventory — whichever is most notable)
2. Call out the single most important thing to act on this week
3. End with one specific, actionable suggestion

Tone: warm, direct, like a trusted advisor. No markdown, no bullet points, plain sentences only."""

        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return ""

def render_html(report: WeeklyReport, restaurant_name: str, owner_name: str = None) -> str:
    reviews = getattr(report, "_reviews", [])
    urgent  = [r for r in reviews if r.urgency == "high"]
    normal  = [r for r in reviews if r.urgency != "high"]

    urgent_section = ""
    if urgent:
        cards = "".join(_review_card(r) for r in urgent)
        urgent_section = f"""
<h3 style="color:#dc2626;margin:24px 0 8px">Needs attention ({len(urgent)})</h3>
{cards}"""

    normal_section = "".join(_review_card(r) for r in normal)
    top_issues_html = "".join(
        f'<span style="background:#f3f4f6;border-radius:4px;padding:3px 8px;'
        f'font-size:12px;margin-right:6px">{cat.replace("_"," ")} ({n})</span>'
        for cat, n in report.top_issues
    ) if report.top_issues else "<em>—</em>"

    ai_summary = generate_ai_digest_summary(report, restaurant_name, owner_name)
    if ai_summary:
        ai_summary_block = (
            '<div style="background:linear-gradient(135deg,#1a1410,#2a1f1a);border-radius:8px;padding:16px 20px;margin-bottom:20px">' +
            '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#c84b2f;margin-bottom:8px">Cavnar AI Consultant</div>' +
            '<p style="font-size:14px;color:#f0ebe0;line-height:1.7;margin:0">' + ai_summary + '</p></div>'
        )
    else:
        ai_summary_block = ''

    return f"""
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  max-width:620px;margin:0 auto;color:#111827;padding:20px">

<div style="border-bottom:2px solid #f3f4f6;padding-bottom:12px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between">
  <h2 style="margin:0;font-family:Georgia,serif;font-size:22px;font-weight:400">
    Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
  </h2>
  <span style="font-size:11px;color:#9ca3af;letter-spacing:.08em;text-transform:uppercase">Weekly Review Digest</span>
</div>
<p style="color:#6b7280;margin:0 0 16px;font-size:13px">{restaurant_name} &nbsp;·&nbsp; {report.period_start} – {report.period_end}</p>

{ai_summary_block}

<div style="display:flex;gap:12px;margin-bottom:20px">
  <div style="flex:1;background:#f9fafb;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:28px;font-weight:600">{report.total_reviews}</div>
    <div style="font-size:12px;color:#6b7280">reviews</div>
  </div>
  <div style="flex:1;background:#f9fafb;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:28px;font-weight:600">{report.avg_rating}</div>
    <div style="font-size:12px;color:#6b7280">avg rating</div>
  </div>
  <div style="flex:1;background:#f0fdf4;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:28px;font-weight:600;color:#16a34a">
      {report.sentiment.get("positive",0)}</div>
    <div style="font-size:12px;color:#6b7280">positive</div>
  </div>
  <div style="flex:1;background:#fef2f2;border-radius:8px;padding:12px;text-align:center">
    <div style="font-size:28px;font-weight:600;color:#dc2626">
      {report.sentiment.get("negative",0)}</div>
    <div style="font-size:12px;color:#6b7280">negative</div>
  </div>
</div>

<div style="margin-bottom:20px">
  <strong style="font-size:13px">Top themes this week:</strong><br>
  <div style="margin-top:6px">{top_issues_html}</div>
</div>

{urgent_section}

<h3 style="margin:24px 0 8px">All reviews ({len(normal)})</h3>
{normal_section}

<p style="font-size:11px;color:#9ca3af;margin-top:32px;border-top:1px solid #f3f4f6;
  padding-top:12px">
  Generated by Cavnar AI &nbsp;·&nbsp;
  Approve responses before posting.</p>
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
