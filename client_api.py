"""
client_api.py — Client-facing API routes and data endpoints
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect, send_file, Response
import os, json

from models import (get_conn, get_restaurant, update_restaurant, approve_response,
                    get_review_stats, get_reviews_data, get_sentiment_trend,
                    get_top_issues, get_platform_breakdown)
from auth import login_required

client_bp = Blueprint('client', __name__)

@client_bp.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid, current_user):
    approve_response(rid)
    try:
        from models import log_event
        log_event(current_user["restaurant_id"], "review_approved", {"review_id": rid})
    except Exception:
        pass
    # Auto-post to Google in background thread — don't block the response
    try:
        from gmb import is_connected
        conn = get_conn()
        row = conn.execute(
            "SELECT platform, draft_response, review_name FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, current_user["restaurant_id"])
        ).fetchone()
        conn.close()
        if row and row["platform"] == "google" and row["review_name"] and row["draft_response"]:
            if is_connected(current_user["restaurant_id"]):
                import threading as _t_gmb
                _rid_capture = rid
                _rest_id_capture = current_user["restaurant_id"]
                _review_name = row["review_name"]
                _draft = row["draft_response"]
                def _post_gmb_bg():
                    try:
                        from gmb import post_reply
                        result = post_reply(_rest_id_capture, _review_name, _draft)
                        if result["ok"]:
                            from models import mark_posted
                            mark_posted(_rid_capture)
                            print(f"[GMB] Auto-posted review {_rid_capture} ✓")
                        else:
                            print(f"[GMB] Auto-post failed for review {_rid_capture}: {result['error']}")
                    except Exception as _ge:
                        print(f"[GMB] Background post error: {_ge}")
                _t_gmb.Thread(target=_post_gmb_bg, daemon=True).start()
                return jsonify(ok=True, auto_posted=True)
    except Exception as e:
        print(f"[GMB] approve auto-post error: {e}")
    return jsonify(ok=True, auto_posted=False)

@client_bp.route("/skip/<int:rid>", methods=["POST"])
@login_required
def skip(rid, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

def format_insight_html(text):
    import re as _re
    if not text:
        return 'Analysis unavailable.'
    # Try splitting on explicit Recommendations: heading first
    parts = _re.split(r'(?i)recommendations?:', text, maxsplit=1)
    if len(parts) == 2:
        intro = parts[0].strip()
        recs_raw = parts[1].strip()
        recs = [r.strip() for r in _re.split(r'\n+', recs_raw) if r.strip()]
    else:
        # Look for lines that start with 1. 2. 3. or are standalone short sentences after a paragraph
        lines = text.strip().split('\n')
        para_lines = []
        rec_lines = []
        in_recs = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if _re.match(r'^[123][\.\)]\s+', line):
                in_recs = True
                rec_lines.append(line)
            elif in_recs and _re.match(r'^[0-9][\.\)]\s+', line):
                rec_lines.append(line)
            elif in_recs:
                # Stop - closing sentence or non-numbered line after recs
                in_recs = False
                para_lines.append(line)
            else:
                para_lines.append(line)
        if not rec_lines:
            return '<p style="margin:0;line-height:1.7">' + text + '</p>'
        intro = ' '.join(para_lines).strip()
        recs = rec_lines
    html = ''
    if intro:
        html += '<p style="margin:0 0 10px 0;line-height:1.7">' + intro + '</p>'
    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin-bottom:8px">Recommendations</div>'
    num = 1
    for rec in recs:
        clean = _re.sub(r'^[\d.\-)]+\s*', '', rec).strip()
        if not clean:
            continue
        html += ('<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
            '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">'
            + str(num) +
            '</span><span style="line-height:1.6;color:#b7791f;font-weight:500">' + clean + '</span></div>')
        num += 1
    return html

@client_bp.route("/api/review-stats")
@login_required
def review_stats_api(current_user):
    from models import get_review_stats as _grs
    try:
        stats = _grs(current_user["restaurant_id"])
        return jsonify(**stats)
    except Exception as e:
        return jsonify(error=str(e)), 500

@client_bp.route("/api/sentiment-trend")
@login_required
def sentiment_trend_api(current_user):
    from models import get_sentiment_trend as _gst
    try:
        data = _gst(current_user["restaurant_id"], weeks=8)
        return jsonify(weeks=data)
    except Exception as e:
        return jsonify(weeks=[], error=str(e))

@client_bp.route("/api/review-insight")
@login_required
def review_insight_api(current_user):
    try:
        import os, json, anthropic as _anth
        from models import get_restaurant, get_review_stats, get_top_issues
        from zoneinfo import ZoneInfo as _ZI_ri
        from datetime import datetime as _dt_ri, timedelta as _td_ri
        _client_ri = _anth.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        rid = current_user["restaurant_id"]
        restaurant = get_restaurant(rid)
        rstats = get_review_stats(rid)
        top_issues = get_top_issues(rid, days=90, limit=5)
        now_chi = _dt_ri.now(_ZI_ri('America/Chicago'))
        today_str = now_chi.strftime("%B %d, %Y")
        # Week-over-week comparison
        from models import get_conn as _gc_ri
        _conn_ri = _gc_ri()
        last_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(sentiment='negative') as neg
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-14 days')
              AND fetched_at < datetime('now','-7 days')
        """, (rid,)).fetchone()
        this_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(sentiment='negative') as neg,
                   SUM(urgency='high' AND response_status NOT IN ('posted','skipped')) as urgent
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-7 days')
        """, (rid,)).fetchone()
        # Recent urgent reviews text
        urgent_rows = _conn_ri.execute("""
            SELECT text FROM reviews
            WHERE restaurant_id=? AND urgency='high'
              AND response_status NOT IN ('posted','skipped')
            ORDER BY fetched_at DESC LIMIT 2
        """, (rid,)).fetchall()
        _conn_ri.close()
        wow_str = ""
        if last_week and last_week["cnt"] > 0 and this_week and this_week["cnt"] > 0:
            diff = (this_week["cnt"] or 0) - last_week["cnt"]
            rdiff = round(((this_week["avg_r"] or 0) - (last_week["avg_r"] or 0)), 1)
            wow_str = f"vs last week: {'+' if diff>=0 else ''}{diff} reviews, avg rating {'up' if rdiff>0 else 'down' if rdiff<0 else 'unchanged'} {abs(rdiff) if rdiff!=0 else ''}."
        urgent_texts = "; ".join(f'"{r["text"][:80]}"' for r in urgent_rows) if urgent_rows else "none"
        issues_str = ", ".join(f"{i['label']} ({i['count']})" for i in top_issues) if top_issues else "no data"
        owner_name = restaurant.owner_name if restaurant else None
        rest_name  = restaurant.name if restaurant else "this restaurant"
        name_line  = f"Owner: {owner_name}" if owner_name else ""
        prompt = (
            f"You are a restaurant reputation assistant. Output ONLY a 3-line snapshot.\n\n"
            f"Restaurant: {rest_name} | Today: {today_str}\n"
            f"Data: {rstats['total']} reviews | {rstats['avg_rating']}★ avg | "
            f"{rstats['positive']} pos / {rstats['negative']} neg / {rstats['neutral']} neutral | "
            f"{rstats['urgent']} urgent | response rate {rstats['response_rate']}%\n"
            f"Top topics: {issues_str} | {wow_str}\n"
            f"Urgent excerpts: {urgent_texts}\n\n"
            "Return EXACTLY this format — 3 lines:\n"
            "\U0001f4ca This week: [1 punchy sentence on the most important number. Be specific.]\n"
            "\u26a0\ufe0f Watch: [1 sentence on the biggest risk — urgent review theme, low response rate, or negative pattern. Skip if nothing urgent.]\n"
            "\u2705 Do today: [1 concrete action — e.g. 'Respond to Amanda L.s 1-star review about cold food.' Never generic.]\n\n"
            "Rules: no markdown, no extra lines, no preamble. Each line max 20 words. Never invent data."
        )

        msg = _client_ri.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role":"user","content":prompt}]
        )
        insight = msg.content[0].text.strip()
        # Strip any markdown
        import re as _re_ri
        insight = _re_ri.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), insight)
        insight = _re_ri.sub(r'\*(.+?)\*',   lambda m: m.group(1), insight)
        return jsonify(insight=insight)
    except Exception as _re:
        import traceback
        print(f"[review-insight ERROR] {_re}\n{traceback.format_exc()}")
        return jsonify(insight="Analysis unavailable — check back shortly.", error=str(_re)), 500

@client_bp.route("/api/recent-topics")
@login_required
def recent_topics_api(current_user):
    try:
        from models import get_conn
        rid = current_user["restaurant_id"]
        conn = get_conn()
        for col in ("post_id", "post_platform", "reach", "impressions", "likes", "comments"):
            try:
                conn.execute("ALTER TABLE marketing_content_log ADD COLUMN " + col + " TEXT")
                conn.commit()
            except Exception:
                pass
        rows = conn.execute(
            """SELECT topic, post_id, post_platform, reach, impressions, likes, comments
               FROM marketing_content_log
               WHERE restaurant_id=? ORDER BY created_at DESC LIMIT 16""",
            (rid,)
        ).fetchall()
        conn.close()
        seen = []
        seen_topics = set()
        for r in rows:
            t = r["topic"]
            if not t or t in seen_topics:
                continue
            seen_topics.add(t)
            entry = {"topic": t, "posted": bool(r["post_id"]), "platform": r["post_platform"] or ""}
            m = {}
            reach = (r["reach"] or 0)
            impressions = (r["impressions"] or 0)
            likes = (r["likes"] or 0)
            comments = (r["comments"] or 0)
            if reach:       m["reach"]       = reach
            if impressions: m["impressions"] = impressions
            if likes:       m["likes"]       = likes
            if comments:    m["comments"]    = comments
            entry["metrics"] = m
            seen.append(entry)
            if len(seen) >= 8:
                break
        return jsonify(topics=seen)
    except Exception as e:
        return jsonify(topics=[])

@client_bp.route("/api/mkt-stats")
@login_required
def mkt_stats_api(current_user):
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        gen   = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (rid,)).fetchone()[0] or 0
        pub   = conn.execute("SELECT COUNT(DISTINCT topic) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)).fetchone()[0] or 0
        month = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)).fetchone()[0] or 0
        conn.close()
        return jsonify(ok=True, generated=gen, published=pub, this_month=month)
    except Exception as e:
        return jsonify(ok=False, generated=0, published=0, this_month=0)

@client_bp.route("/api/mkt-insight")
@login_required
def mkt_insight_api(current_user):
    try:
        from marketing import get_profile_for_restaurant, get_recent_content, get_upcoming_holidays, generate_content
        from models import get_restaurant
        from datetime import datetime
        from zoneinfo import ZoneInfo
        restaurant = get_restaurant(current_user["restaurant_id"])
        name = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        rid = current_user["restaurant_id"]
        p = get_profile_for_restaurant(rid)
        recent = get_recent_content(rid, limit=5)
        now = datetime.now(ZoneInfo("America/Chicago"))
        upcoming = get_upcoming_holidays(now.replace(tzinfo=None))
        recent_str = ", ".join(r["topic"] for r in recent) if recent else "none yet"
        greeting = f"{owner}," if owner else "Hi,"
        never_clause = f"Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
        menu_clause = f"Current menu/specials: {p['menu_notes']}." if p.get("menu_notes") else ""
        skip_h = [h.strip().lower() for h in (p.get("skip_holidays") or "").split(",") if h.strip()]
        if skip_h and upcoming:
            upcoming = ", ".join(h for h in upcoming.split(", ") if not any(s in h.lower() for s in skip_h)) or None
        # Pull post performance data for trend context
        perf_clause = ""
        try:
            from models import get_conn as _gc
            _conn = _gc()
            _perf_rows = _conn.execute(
                """SELECT topic, post_platform, reach, impressions, engaged, likes, comments
                   FROM marketing_content_log
                   WHERE restaurant_id=? AND post_id IS NOT NULL
                     AND (reach > 0 OR impressions > 0 OR likes > 0)
                   ORDER BY created_at DESC LIMIT 10""",
                (rid,)
            ).fetchall()
            _conn.close()
            if _perf_rows:
                _lines = []
                for _r in _perf_rows:
                    _parts = []
                    if _r["reach"]:       _parts.append(str(_r["reach"]) + " reach")
                    if _r["impressions"]: _parts.append(str(_r["impressions"]) + " impressions")
                    if _r["engaged"]:     _parts.append(str(_r["engaged"]) + " engaged")
                    if _r["likes"]:       _parts.append(str(_r["likes"]) + " likes")
                    if _r["comments"]:    _parts.append(str(_r["comments"]) + " comments")
                    if _parts:
                        _lines.append(_r["topic"] + " (" + _r["post_platform"] + "): " + ", ".join(_parts))
                if _lines:
                    perf_clause = "\n\nRecent post performance data:\n" + "\n".join(_lines) + "\nUse this to identify what content resonates most and suggest doubling down on high-performing topics or formats."
        except Exception:
            pass
        prompt = f"""You are the Cavnar AI Marketing Consultant for {name}.
Write a short, punchy weekly marketing brief for {owner or "the owner"} — 3-4 sentences max.

Restaurant: {p["name"]} in {p["neighborhood"]}.
Vibe: {p["vibe"]}.
Known for: {p["known_for"]}.
Brand voice: {p["voice"]}.
{menu_clause}
{never_clause}
ALL upcoming holidays in next 30 days (mention ALL of them, not just one): {upcoming if upcoming else "none"}.
Recent content generated (do NOT repeat these): {recent_str}.{perf_clause}

Structure exactly like this — no headers, no bullets, just two short paragraphs:
Paragraph 1: Start with "{greeting}" then give 1 specific marketing opportunity this week tied to the season, upcoming holidays, or a gap in recent content. If post performance data is available, mention what's working.
Paragraph 2: One concrete content suggestion with a specific angle. Reference real menu items if provided. End with a one-line encouragement.

Tone: warm, direct, like a trusted advisor. Match the brand voice exactly. No corporate language. Under 120 words total. If multiple holidays are coming up, mention both briefly."""
        import anthropic as _anth
        _client = _anth.Anthropic(api_key=__import__("os").getenv("ANTHROPIC_API_KEY"))
        msg = _client.messages.create(
            model=__import__("os").getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        insight = msg.content[0].text.strip()
        return jsonify(insight=format_insight_html(insight))
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[MktInsight] ERROR: {str(e)}")
        return jsonify(insight=f"Marketing brief unavailable — check back shortly.")

@client_bp.route("/api/labor-insight")
@login_required
def labor_insight_api(current_user):
    try:
        from labor import analyse_shifts_for_restaurant, get_claude_insights
        from models import get_restaurant
        restaurant = get_restaurant(current_user["restaurant_id"])
        name  = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        from models import get_staff_notes as _gsn_labor
        _staff_notes_labor = _gsn_labor(current_user["restaurant_id"])
        insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner,
                                      restaurant_id=current_user["restaurant_id"],
                                      staff_notes=_staff_notes_labor if _staff_notes_labor else None)
        return jsonify(insight=format_insight_html(insight))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(insight=f"Unable to load analysis. Error: {str(e)[:100]}")

@client_bp.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    try:
        from inventory import load_inventory_for_restaurant, analyse_inventory, get_claude_insights
        restaurant = get_restaurant(current_user["restaurant_id"])
        items, _is_live = load_inventory_for_restaurant(current_user["restaurant_id"])
        analysis = analyse_inventory(items)
        owner_name = restaurant.owner_name if restaurant else None
        insight = get_claude_insights(analysis, owner_name=owner_name, restaurant_name=restaurant.name if restaurant else None, restaurant_id=current_user["restaurant_id"])
        return jsonify(insight=format_insight_html(insight))
    except Exception as _inv_e:
        import traceback
        print(f"[inv-insight ERROR] {_inv_e}\n{traceback.format_exc()}")
        return jsonify(insight="Analysis unavailable — check server logs.", error=str(_inv_e)), 500

@client_bp.route("/api/generate-content", methods=["POST"])
@login_required
def gen_content(current_user):
    data = request.get_json()
    from marketing import generate_content, mark_calendar_idea_used
    rid = current_user["restaurant_id"] if current_user else None
    content_type = data.get("type","instagram_post")
    topic = data.get("topic","")
    result = generate_content(content_type, topic, restaurant_id=rid)
    if data.get("from_calendar") and rid:
        try:
            mark_calendar_idea_used(rid, content_type, topic)
        except Exception:
            pass
    return jsonify(content=result)

@client_bp.route("/api/content-calendar")
@login_required
def content_calendar(current_user):
    from marketing import get_content_calendar_ideas
    return jsonify(ideas=get_content_calendar_ideas(
        restaurant_id=current_user["restaurant_id"]))

@client_bp.route("/api/regenerate-draft/<int:review_id>", methods=["POST"])
@login_required
def regenerate_draft(review_id, current_user):
    """Regenerate AI draft for a review."""
    from models import get_conn, update_draft
    conn = get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, current_user["restaurant_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="Review not found")
    r = dict(row)
    restaurant = get_restaurant(current_user["restaurant_id"])
    try:
        import anthropic
        client = anthropic.Anthropic()
        sentiment_note = {"positive":"positive","negative":"negative","neutral":"neutral"}.get(r.get("sentiment","neutral"),"neutral")

        # Pull approved examples to teach the AI this owner's style
        from models import get_approved_examples
        examples = get_approved_examples(current_user["restaurant_id"], limit=4)
        examples_block = ""
        if examples:
            ex_lines = "\n".join([
                f"  Review ({e['rating']}★): \"{e['review']}\"\n  Response: \"{e['response']}\""
                for e in examples
            ])
            examples_block = f"\n\nHere are {len(examples)} recent responses this owner approved — match this exact tone and style:\n{ex_lines}"

        # Extract reviewer first name if available
        reviewer_name = ""
        if r.get("review_name"):
            first = r["review_name"].strip().split()[0]
            # Only use if it looks like a real name (not "A Google User" etc)
            if len(first) > 1 and first.lower() not in ("a","an","the","anonymous","user","google","yelp"):
                reviewer_name = first

        # Pull recurring negative themes to address patterns
        theme_context = ""
        if sentiment_note == "negative":
            try:
                from models import get_conn as _gc_r
                _conn_r = _gc_r()
                recent_neg = _conn_r.execute("""
                    SELECT text FROM reviews
                    WHERE restaurant_id=? AND sentiment='negative'
                    AND response_status NOT IN ('skipped')
                    ORDER BY fetched_at DESC LIMIT 5
                """, (current_user["restaurant_id"],)).fetchall()
                _conn_r.close()
                if len(recent_neg) >= 3:
                    theme_context = f"\n\nNote: This restaurant has had {len(recent_neg)} negative reviews recently. If this review shares themes with common complaints, acknowledge the pattern and note that it's being actively addressed."
            except Exception:
                pass

        # Platform-specific guidance
        platform = r.get("platform", "google")
        if platform == "google":
            platform_guidance = "This is a Google review — naturally include the restaurant name once for SEO. Keep it professional and inviting."
        elif platform == "yelp":
            platform_guidance = "This is a Yelp review — be conversational and genuine. Do NOT include the restaurant name repeatedly."
        else:
            platform_guidance = "Keep the response professional and genuine."

        # Length calibration by rating
        rating = r.get("rating", 3)
        if rating >= 4:
            length_guidance = "Keep it brief and warm — 25-40 words is ideal for a positive review. Don't over-explain."
        elif rating == 3:
            length_guidance = "Keep it 40-60 words — acknowledge both the positives and address any concerns mentioned."
        else:
            length_guidance = "This needs a fuller response — 60-80 words. Acknowledge specific complaints mentioned, apologize sincerely, and explain what will be done differently."

        # Build reviewer address
        reviewer_line = f"Address the reviewer as {reviewer_name} by name at the start." if reviewer_name else "Do not invent a name — start without one."

        prompt = f"""Write a professional, warm restaurant response to this {sentiment_note} review.

Restaurant: {restaurant.name}
Platform: {platform_guidance}
Voice guidance: {restaurant.voice_notes or "Warm, genuine, never corporate. Always invite guests back."}
Sign off as: {restaurant.sign_off_name or restaurant.name}
Never use: {restaurant.never_say or ""}{examples_block}
{reviewer_line}
Length: {length_guidance}

IMPORTANT: If the reviewer mentions specific complaints (cold food, slow service, wrong order, etc.) — address each one directly by name. Do not give a generic apology.{theme_context}

Review (rating: {r["rating"]}/5):
{r["text"]}

Write ONLY the response, no preamble. Sound like a real person, not a PR firm."""

        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=300,
            messages=[{"role":"user","content":prompt}]
        )
        new_draft = msg.content[0].text.strip()
        update_draft(review_id, new_draft)
        # Reset status to drafted
        conn = get_conn()
        conn.execute("UPDATE reviews SET response_status='drafted' WHERE id=?", (review_id,))
        conn.commit(); conn.close()
        return jsonify(ok=True, draft=new_draft)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@client_bp.route("/api/save-draft/<int:review_id>", methods=["POST"])
@login_required
def save_draft(review_id, current_user):
    """Save a manually edited draft."""
    from models import update_draft
    data = request.get_json()
    draft = data.get("draft","").strip()
    if not draft:
        return jsonify(ok=False, error="Draft cannot be empty")
    conn = get_conn()
    row = conn.execute("SELECT id FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, current_user["restaurant_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="Review not found")
    update_draft(review_id, draft)
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='drafted' WHERE id=?", (review_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@client_bp.route("/api/labor-trend")
@login_required
def labor_trend_api(current_user):
    """Return labor % history for the trend chart."""
    try:
        from models import get_labor_history
        history = get_labor_history(current_user["restaurant_id"], limit=8)
        if not history:
            return jsonify(weeks=[])
        weeks = []
        for h in history[::-1]:  # oldest first = left to right
            try:
                start = datetime.strptime(h["period_start"], "%Y-%m-%d")
                label = start.strftime("%-m/%-d")
            except Exception:
                label = h.get("period_start", "")[:5]
            weeks.append({
                "label": label,
                "pct": round(h["labor_pct"], 1),
                "labor": h["total_labor"],
                "sales": h["total_sales"],
            })
        return jsonify(weeks=weeks)
    except Exception as e:
        return jsonify(weeks=[], error=str(e))

@client_bp.route("/api/labor-gap")
@login_required
def labor_gap_api(current_user):
    try:
        from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        gap = calculate_monthly_gap(analysis)
        return jsonify(gap)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e), over_target=False, monthly_gap=0,
                      current_pct=0, target_pct=30)

@client_bp.route("/api/download-schedule")
@login_required
def download_schedule(current_user):
    import io
    try:
        from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                           generate_optimized_schedule, get_hourly_rate)
        from models import get_restaurant
        restaurant = get_restaurant(current_user["restaurant_id"])
        shifts   = load_shifts_for_restaurant(current_user["restaurant_id"])
        if not shifts:
            return jsonify(ok=False, error="No shift data available — upload shifts CSV first"), 400
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        rate     = get_hourly_rate(current_user["restaurant_id"])
        owner    = restaurant.owner_name if restaurant and restaurant.owner_name else None
        target   = restaurant.labor_target_pct if restaurant else 30.0
        from models import get_staff_notes
        staff_notes = get_staff_notes(current_user["restaurant_id"])
        csv_text = generate_optimized_schedule(
            analysis, shifts,
            restaurant_name=restaurant.name if restaurant else "Restaurant",
            hourly_rate=rate,
            owner_name=owner,
            staff_notes=staff_notes if staff_notes else None,
            labor_target=target
        )
        # Clean up any markdown Claude might add
        lines = [l for l in csv_text.split("\n") if l.strip() and not l.startswith("#") and not l.startswith("```")]
        csv_clean = "\n".join(lines)
        name = (restaurant.name if restaurant else "Restaurant").replace(" ","_")
        return send_file(
            io.BytesIO(csv_clean.encode()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"optimized_schedule_{name}.csv"
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500

@client_bp.route("/api/billing-info")
@login_required
def billing_info(current_user):
    """Fetch billing status from Stripe for the current client."""
    import stripe as _stripe
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.stripe_customer_id:
        return jsonify(ok=False, reason="no_customer")

    stripe_key = os.getenv("STRIPE_SECRET_KEY","")
    if not stripe_key:
        return jsonify(ok=False, reason="no_key")

    try:
        _stripe.api_key = stripe_key
        # Get active subscriptions for this customer
        subs = _stripe.Subscription.list(
            customer=restaurant.stripe_customer_id,
            status="active",
            limit=5
        )
        if not subs.data:
            # Check for trialing
            subs = _stripe.Subscription.list(
                customer=restaurant.stripe_customer_id,
                status="trialing",
                limit=5
            )

        if not subs.data:
            return jsonify(ok=True, status="inactive", message="No active subscription found")

        sub = subs.data[0]
        from datetime import datetime
        next_date = datetime.fromtimestamp(sub.current_period_end).strftime("%-m/%-d/%Y")
        amount    = sum(i.price.unit_amount for i in sub["items"].data) / 100
        status    = sub.status  # active, trialing, past_due, canceled

        # Get payment method
        pm_desc = "Card on file"
        try:
            customer = _stripe.Customer.retrieve(
                restaurant.stripe_customer_id,
                expand=["invoice_settings.default_payment_method"]
            )
            pm = customer.invoice_settings.default_payment_method
            if pm and pm.card:
                pm_desc = f"{pm.card.brand.title()} ending {pm.card.last4}"
        except Exception:
            pass

        # Customer portal link
        try:
            portal = _stripe.billing_portal.Session.create(
                customer=restaurant.stripe_customer_id,
                return_url="https://dashboard.cavnar.ai"
            )
            portal_url = portal.url
        except Exception:
            portal_url = None

        return jsonify(
            ok=True,
            status=status,
            next_date=next_date,
            amount=f"${amount:,.0f}/mo",
            payment_method=pm_desc,
            portal_url=portal_url,
            trial_end=datetime.fromtimestamp(sub.trial_end).strftime("%-m/%-d/%Y") if sub.trial_end else None,
        )
    except Exception as e:
        print(f"Stripe billing info error: {e}")
        return jsonify(ok=False, reason="stripe_error", error=str(e))

@client_bp.route("/api/update-digest-day", methods=["POST"])
@login_required
def update_digest_day(current_user):
    """Let client update their own weekly digest day."""
    data = request.get_json()
    day  = data.get("day","monday").lower()
    valid = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    if day not in valid:
        return jsonify(ok=False, error="Invalid day")
    update_restaurant(current_user["restaurant_id"], {
        "digest_day": day,
        "digest_enabled": int(data.get("enabled", 1))
    })
    return jsonify(ok=True)

@client_bp.route("/api/dismiss-welcome", methods=["POST"])
@login_required
def dismiss_welcome(current_user):
    """Mark user as having seen welcome banner by updating last_login."""
    try:
        from auth import update_last_login
        update_last_login(current_user["id"])
    except Exception as _e:
        print(f"[dismiss-welcome] update_last_login error: {_e}")
    try:
        from models import log_event
        log_event(current_user["restaurant_id"], "login")
    except Exception:
        pass
    return jsonify(ok=True)


@client_bp.route("/client/sample-template/<template_type>")
@login_required
def download_sample_template(current_user, template_type):
    """Serve sample CSV templates for clients to download."""
    from flask import Response
    if template_type == "shifts":
        csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
        csv += "2026-06-01,Monday,Jane Smith,Server,11:00,17:00,6,6.0,4200,\n"
        csv += "2026-06-01,Monday,Mark Jones,Cook,10:00,18:00,8,8.2,4200,\n"
        csv += "2026-06-02,Tuesday,Jane Smith,Server,17:00,23:00,6,5.8,4800,\n"
        csv += "2026-06-02,Tuesday,Mark Jones,Cook,10:00,18:00,8,8.0,4800,\n"
        return Response(csv, mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=sample_shifts_template.csv"})
    elif template_type == "inventory":
        csv = "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week\n"
        csv += "Chicken Breast,Protein,30,22,5.80,6.0,30,3.5\n"
        csv += "Romaine Lettuce,Produce,20,28,2.50,3.5,25,8.0\n"
        csv += "Heavy Cream,Dairy,12,9,3.80,1.8,12,1.5\n"
        csv += "Pasta Rigatoni,Pantry,15,19,2.80,2.2,15,1.8\n"
        return Response(csv, mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=sample_inventory_template.csv"})
    return "Template not found", 404


# ── Client self-serve data upload ────────────────────────────────────────────
@client_bp.route("/client/upload-data", methods=["POST"])
@login_required
def client_upload_data(current_user):
    """
    Client-facing upload endpoint. Validates CSV, saves it, triggers re-analysis.
    login_required (not admin_required) so clients can upload their own data.
    """
    import io, csv as _csv
    from models import save_client_data, log_email

    # File size limit: 5MB max
    file = request.files.get("file")
    if file and file.content_length and file.content_length > 5 * 1024 * 1024:
        return jsonify(ok=False, error="File too large. Maximum size is 5MB."), 413

    restaurant_id = current_user["restaurant_id"]
    data_type     = request.form.get("data_type")  # "shifts" or "inventory"

    if data_type not in ("shifts", "inventory"):
        return jsonify(ok=False, error="Invalid data type")

    f = request.files.get("csv_file")
    if not f:
        return jsonify(ok=False, error="No file uploaded")

    try:
        csv_content = f.read().decode("utf-8")
    except Exception:
        return jsonify(ok=False, error="Could not read file — make sure it's a CSV")

    if not csv_content.strip():
        return jsonify(ok=False, error="File appears empty")

    # Validate it parses
    try:
        rows = list(_csv.DictReader(io.StringIO(csv_content)))
        if not rows:
            return jsonify(ok=False, error="CSV has no data rows")
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}")

    # Validate required columns exist
    headers = [h.strip().lower() for h in (rows[0].keys() if rows else [])]

    if data_type == "shifts":
        required = ["date", "employee", "actual_hours"]
        optional_sales = ["sales", "sales_that_day", "revenue"]
        missing = [c for c in required if c not in headers]
        has_sales = any(c in headers for c in optional_sales)
        if missing:
            return jsonify(ok=False, error=(
                f"Your shifts CSV is missing required columns: {', '.join(missing)}. "
                f"Required columns are: date, employee, actual_hours. "
                f"Also recommended: sales (daily revenue for that date). "
                f"Download the sample template from the Labor tab for reference."
            ))
        if not has_sales:
            # Warn but don't block — labor % just won't show
            pass

    elif data_type == "inventory":
        required = ["item", "current_stock", "par_level", "unit_cost", "waste_last_week"]
        missing = [c for c in required if c not in headers]
        if missing:
            return jsonify(ok=False, error=(
                f"Your inventory CSV is missing required columns: {', '.join(missing)}. "
                f"Required columns are: item, current_stock, par_level, unit_cost, waste_last_week. "
                f"Also recommended: avg_daily_usage, last_order_qty. "
                f"Download the sample template from the Inventory tab for reference."
            ))

    # Save it
    save_client_data(restaurant_id, data_type, csv_content, source="upload")

    # Trigger immediate re-analysis so dashboard reflects new data right away
    try:
        if data_type == "shifts":
            from labor import analyse_shifts_for_restaurant
            analyse_shifts_for_restaurant(restaurant_id)
        elif data_type == "inventory":
            from models import get_restaurant
            r = get_restaurant(restaurant_id)
            if r:
                pass  # inventory analysis runs on next dashboard load
    except Exception as e:
        pass  # non-fatal — data is saved, analysis will run on next load

    # Log it and notify Will on first-ever upload
    try:
        from models import get_restaurant, get_client_data
        r = get_restaurant(restaurant_id)
        label = "Labor CSV upload" if data_type == "shifts" else "Inventory CSV upload"
        log_email(restaurant_id, label, current_user.get("email",""), f"{label} — {r.name if r else ''}")

        # Check if this is the client's first upload of this type
        import os as _os, resend as _resend
        _resend_key = _os.getenv("RESEND_API_KEY", "")
        _will_email = _os.getenv("WILL_EMAIL", "will@cavnar.ai")
        _from_email = _os.getenv("FROM_EMAIL", "will@cavnar.ai")
        if _resend_key and r:
            _resend.api_key = _resend_key
            _module = "shift schedule" if data_type == "shifts" else "inventory"
            _resend.Emails.send({
                "from": f"Cavnar AI Alerts <{_from_email}>",
                "to": [_will_email],
                "subject": f"📂 {r.name} just uploaded their {_module} data",
                "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                    <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
                        <h3 style="color:#0e0c0a;margin:0">Client data uploaded</h3>
                    </div>
                    <p style="font-size:15px;line-height:1.6">
                        <strong>{r.name}</strong> just uploaded their {_module} CSV ({len(rows)} rows).<br><br>
                        Good time to check their dashboard looks right and send a quick note.
                    </p>
                    <hr style="border:none;border-top:1px solid #e0dbd0;margin:16px 0"/>
                    <p style="font-size:11px;color:#7a736a">
                        <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">View in admin →</a>
                    </p>
                </div>"""
            })
    except Exception:
        pass

    return jsonify(ok=True, rows=len(rows), message=f"{len(rows)} rows loaded successfully")


# ── Review request ────────────────────────────────────────────────────────────

@client_bp.route("/api/send-review-request", methods=["POST"])
@login_required
def send_review_request(current_user):
    try:
        data          = request.get_json() or {}
        customer_name  = (data.get("name") or "").strip()
        customer_email = (data.get("email") or "").strip().lower()
        if not customer_email or "@" not in customer_email:
            return jsonify(ok=False, error="Valid email required"), 400

        rid        = current_user["restaurant_id"]
        restaurant = get_restaurant(rid)
        if not restaurant:
            return jsonify(ok=False, error="Restaurant not found"), 404

        # Build Google review link
        place_id    = restaurant.google_place_id or ""
        review_url  = (f"https://search.google.com/local/writereview?placeid={place_id}"
                       if place_id else "https://g.page/r/review")
        first_name  = customer_name.split()[0] if customer_name else "there"
        rest_name   = restaurant.name or "us"

        # Send via Resend
        import resend as _resend
        _resend.api_key = os.getenv("RESEND_API_KEY", "")
        if not _resend.api_key:
            return jsonify(ok=False, error="Email not configured"), 500

        html_body = f"""
        <div style="font-family:'DM Sans',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;background:#f7f4ef">
          <div style="background:white;border-radius:12px;padding:32px;border:1px solid #e0dbd0">
            <div style="font-family:Georgia,serif;font-size:22px;color:#0e0c0a;margin-bottom:4px">
              Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
            </div>
            <div style="font-size:11px;color:#7a736a;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #e0dbd0">
              On behalf of {rest_name}
            </div>
            <p style="font-size:15px;color:#3a3530;line-height:1.6;margin:0 0 16px">
              Hi {first_name},
            </p>
            <p style="font-size:15px;color:#3a3530;line-height:1.6;margin:0 0 24px">
              Thank you for dining with us at <strong>{rest_name}</strong>. We hope you had a great experience — we'd love to hear your thoughts.
            </p>
            <a href="{review_url}" style="display:inline-block;background:#c84b2f;color:white;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:600;letter-spacing:.3px">
              Leave a Google review →
            </a>
            <p style="font-size:12px;color:#7a736a;margin-top:24px;line-height:1.5">
              It only takes 60 seconds and helps other guests find us. We read every review.
            </p>
          </div>
          <p style="font-size:10px;color:#a09080;text-align:center;margin-top:16px">
            Sent via Cavnar AI · <a href="https://dashboard.cavnar.ai" style="color:#a09080">cavnar.ai</a>
          </p>
        </div>"""

        _resend.Emails.send({
            "from":    "reviews@cavnar.ai",
            "to":      customer_email,
            "subject": f"How was your visit to {rest_name}?",
            "html":    html_body,
        })

        # Log the request
        from models import get_conn as _gc
        conn = _gc()
        conn.execute(
            "INSERT INTO review_requests (restaurant_id, customer_name, customer_email) VALUES (?,?,?)",
            (rid, customer_name, customer_email)
        )
        conn.commit()
        conn.close()

        return jsonify(ok=True)

    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@client_bp.route("/api/review-request-stats")
@login_required
def review_request_stats(current_user):
    from models import get_review_request_stats
    return jsonify(**get_review_request_stats(current_user["restaurant_id"]))


# ── GBP Listings ──────────────────────────────────────────────────────────────

@client_bp.route("/api/gbp-debug")
@login_required
def gbp_debug(current_user):
    import requests as _req
    from gmb import get_valid_token, get_gmb_account_id
    from models import get_restaurant
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    out = {
        "has_refresh_token": bool(r.gmb_refresh_token),
        "has_location_id":   bool(r.gmb_location_id),
        "stored_location_id": r.gmb_location_id or None,
        "stored_account_id":  r.gmb_account_id or None,
        "google_place_id":    r.google_place_id or None,
    }
    token = get_valid_token(rid)
    out["token_ok"] = bool(token)
    if token:
        # Raw accounts call
        try:
            resp = _req.get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
                            headers={"Authorization": "Bearer " + token}, timeout=10)
            out["accounts_status"] = resp.status_code
            out["accounts_body"]   = resp.json()
        except Exception as e:
            out["accounts_error"] = str(e)
        # Raw locations call if we have account_id
        acct = r.gmb_account_id or get_gmb_account_id(token)
        if acct:
            try:
                resp2 = _req.get(
                    "https://mybusinessbusinessinformation.googleapis.com/v1/" + acct + "/locations",
                    headers={"Authorization": "Bearer " + token},
                    params={"readMask": "name,title,phoneNumbers,websiteUri,profile"},
                    timeout=10)
                out["locations_status"] = resp2.status_code
                out["locations_body"]   = resp2.json()
            except Exception as e:
                out["locations_error"] = str(e)
    return jsonify(out)


@client_bp.route("/api/gbp-listing", methods=["GET"])
@login_required
def gbp_listing_get(current_user):
    from gmb import get_gbp_listing, get_valid_token, get_gmb_account_id, get_gmb_location_id
    from models import get_restaurant, update_restaurant
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    # Token present but location missing — try to discover it now
    if r and r.gmb_refresh_token and not r.gmb_location_id:
        try:
            token = get_valid_token(rid)
            if token:
                account_id = get_gmb_account_id(token)
                if account_id:
                    location_id = get_gmb_location_id(token, account_id, r.google_place_id or "")
                    if location_id:
                        update_restaurant(rid, {
                            "gmb_account_id":  account_id,
                            "gmb_location_id": location_id,
                        })
        except Exception as e:
            print(f"[GBP] auto-discover location failed: {e}")
    return jsonify(**get_gbp_listing(rid))


@client_bp.route("/api/gbp-listing", methods=["POST"])
@login_required
def gbp_listing_update(current_user):
    from gmb import update_gbp_listing
    data = request.get_json() or {}
    fields = {}
    if "phone"       in data: fields["phone"]       = data["phone"].strip()
    if "website"     in data: fields["website"]     = data["website"].strip()
    if "description" in data: fields["description"] = data["description"].strip()
    result = update_gbp_listing(current_user["restaurant_id"], fields)
    return jsonify(**result)


# ── AI Visibility ─────────────────────────────────────────────────────────────

@client_bp.route("/api/ai-visibility")
@login_required
def ai_visibility(current_user):
    import anthropic as _anth
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404

    name        = r.name or ""
    neighborhood = r.neighborhood or ""
    vibe        = r.vibe or ""
    known_for   = r.known_for or ""
    descriptor  = vibe or known_for or "restaurant"
    location    = neighborhood or "the area"

    if neighborhood:
        queries = [
            "Best " + descriptor + " in " + neighborhood,
            "Top restaurants in " + neighborhood,
            "Where to eat in " + neighborhood + " tonight",
        ]
    else:
        queries = [
            "Best " + descriptor + " restaurant near me",
            "Top local restaurants for " + (known_for or "dinner"),
            "Highly rated " + descriptor + " spots to visit",
        ]

    _cl = _anth.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    query_results = []
    appeared_count = 0

    for q in queries:
        try:
            msg = _cl.messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": (
                        "You are a helpful local dining guide. Answer the following question as if "
                        "recommending restaurants from your knowledge. Name specific places if you know "
                        "them. Keep your answer under 120 words. Question: " + q
                    )
                }]
            )
            answer = msg.content[0].text if msg.content else ""
            appeared = name.lower().strip() in answer.lower() if name else False
            if appeared:
                appeared_count += 1
            query_results.append({"query": q, "answer": answer[:300], "appeared": appeared})
        except Exception as e:
            query_results.append({"query": q, "answer": "Could not fetch answer.", "appeared": False})

    # GBP completeness score
    gbp_data = {}
    gbp_connected = bool(r.gmb_refresh_token and r.gmb_location_id)
    if gbp_connected:
        try:
            from gmb import get_gbp_listing
            gbp_result = get_gbp_listing(rid)
            if gbp_result.get("ok"):
                gbp_data = gbp_result
        except Exception:
            pass

    checklist = []
    gbp_score = 0

    if gbp_connected:
        gbp_score += 20
        checklist.append({"label": "Google Business Profile connected", "done": True, "pts": 20})
    else:
        checklist.append({"label": "Connect Google Business Profile", "done": False, "pts": 20})

    has_phone = bool(gbp_data.get("phone"))
    if has_phone:
        gbp_score += 10
        checklist.append({"label": "Phone number in GBP", "done": True, "pts": 10})
    else:
        checklist.append({"label": "Add phone number to GBP", "done": False, "pts": 10})

    has_website = bool(gbp_data.get("website"))
    if has_website:
        gbp_score += 15
        checklist.append({"label": "Website linked in GBP", "done": True, "pts": 15})
    else:
        checklist.append({"label": "Add website URL to GBP", "done": False, "pts": 15})

    desc = gbp_data.get("description", "")
    if desc and len(desc) >= 50:
        gbp_score += 25
        checklist.append({"label": "Business description written (50+ chars)", "done": True, "pts": 25})
    elif desc:
        gbp_score += 10
        checklist.append({"label": "Expand GBP description — currently too short", "done": False, "pts": 15})
    else:
        checklist.append({"label": "Write a business description in GBP", "done": False, "pts": 25})

    rstats = get_review_stats(rid)
    resp_rate = rstats.get("response_rate", 0) if rstats else 0
    if resp_rate >= 50:
        gbp_score += 20
        checklist.append({"label": "Strong review response rate (50%+)", "done": True, "pts": 20})
    elif resp_rate >= 20:
        gbp_score += 10
        checklist.append({"label": "Increase review response rate to 50%+", "done": False, "pts": 10})
    else:
        checklist.append({"label": "Start responding to Google reviews", "done": False, "pts": 20})

    has_profile = bool(r.vibe or r.known_for or r.neighborhood)
    if has_profile:
        gbp_score += 10
        checklist.append({"label": "Restaurant profile complete (neighborhood, vibe, known for)", "done": True, "pts": 10})
    else:
        checklist.append({"label": "Complete restaurant profile in Account settings", "done": False, "pts": 10})

    ai_score = round((appeared_count / len(queries)) * 100) if queries else 0

    return jsonify(
        ok=True,
        restaurant_name=name,
        neighborhood=neighborhood,
        queries=query_results,
        appeared_count=appeared_count,
        total_queries=len(queries),
        ai_score=ai_score,
        gbp_score=gbp_score,
        checklist=checklist,
        gbp_connected=gbp_connected,
    )


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Ryan seed (module-level — runs under Gunicorn AND direct python) ─────────


