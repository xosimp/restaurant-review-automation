"""
client_api.py — Client-facing API routes and data endpoints
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect, send_file, Response
import os, json, re
from datetime import datetime

from models import (get_conn, get_restaurant, update_restaurant, approve_response,
                    get_review_stats, get_reviews_data, get_sentiment_trend,
                    get_top_issues, get_platform_breakdown, get_topic_heatmap)
from auth import login_required

client_bp = Blueprint('client', __name__)

# Simple in-memory insight cache: {cache_key: (timestamp, value)}
_insight_cache = {}
_INSIGHT_TTL = 300  # 5 minutes

def _cache_get(key):
    entry = _insight_cache.get(key)
    if entry and (datetime.utcnow() - entry[0]).total_seconds() < _INSIGHT_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    _insight_cache[key] = (datetime.utcnow(), value)

@client_bp.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid, current_user):
    # Determine response action before approving
    try:
        _ac = get_conn()
        _row = _ac.execute(
            "SELECT regenerate_count, draft_edited FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, current_user["restaurant_id"])
        ).fetchone()
        _ac.close()
        if _row:
            if (_row["regenerate_count"] or 0) > 0:
                _action = "regenerated"
            elif (_row["draft_edited"] or 0) == 1:
                _action = "edited"
            else:
                _action = "approved_as_is"
            _ac2 = get_conn()
            _ac2.execute("UPDATE reviews SET response_action=? WHERE id=?", (_action, rid))
            _ac2.commit(); _ac2.close()
    except Exception as _ae:
        print(f"[approve] response_action error: {_ae}")
    approve_response(rid)
    try:
        from models import log_event
        log_event(current_user["restaurant_id"], "review_approved", {"review_id": rid})
    except Exception:
        pass
    try:
        from webhooks import fire_webhook as _fw
        _fw(current_user["restaurant_id"], "response.approved", {"review_id": rid})
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
                            try:
                                from webhooks import fire_webhook as _fw2
                                _fw2(_rest_id_capture, "response.posted", {
                                    "review_id": _rid_capture,
                                    "platform": "google",
                                    "author": _review_name,
                                })
                            except Exception:
                                pass
                        else:
                            print(f"[GMB] Auto-post failed for review {_rid_capture}: {result['error']}")
                    except Exception as _ge:
                        print(f"[GMB] Background post error: {_ge}")
                _t_gmb.Thread(target=_post_gmb_bg, daemon=True).start()
                return jsonify(ok=True, auto_posted=True)
    except Exception as e:
        print(f"[GMB] approve auto-post error: {e}")
    return jsonify(ok=True, auto_posted=False)

@client_bp.route("/api/reviews/<int:rid>/delete", methods=["POST"])
@login_required
def delete_review(rid, current_user):
    conn = get_conn()
    conn.execute(
        "UPDATE reviews SET deleted_at=datetime('now') WHERE id=? AND restaurant_id=?",
        (rid, current_user["restaurant_id"])
    )
    conn.commit(); conn.close()
    return jsonify(ok=True)

@client_bp.route("/skip/<int:rid>", methods=["POST"])
@login_required
def skip(rid, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=? AND restaurant_id=?",
                 (rid, current_user["restaurant_id"]))
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

@client_bp.route("/api/topic-heatmap")
@login_required
def topic_heatmap_api(current_user):
    try:
        days = int(request.args.get("days", 90))
        if days not in (30, 60, 90, 180):
            days = 90
        data = get_topic_heatmap(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@client_bp.route("/api/changelog")
@login_required
def changelog_api(current_user):
    from models import get_changelog, get_restaurant, update_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    entries = get_changelog()
    # Mark as seen — stamp now
    update_restaurant(current_user["restaurant_id"], {"changelog_seen_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")})
    return jsonify(ok=True, entries=entries)

@client_bp.route("/api/changelog/unread-count")
@login_required
def changelog_unread_count(current_user):
    from models import get_changelog, get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    since = restaurant.changelog_seen_at if restaurant else None
    unread = get_changelog(since=since) if since else get_changelog()
    return jsonify(ok=True, count=len(unread))

@client_bp.route("/api/templates", methods=["GET"])
@login_required
def list_templates(current_user):
    from models import get_response_templates
    return jsonify(ok=True, templates=get_response_templates(current_user["restaurant_id"]))

@client_bp.route("/api/templates", methods=["POST"])
@login_required
def create_template(current_user):
    from models import create_response_template
    data  = request.get_json() or {}
    title = (data.get("title") or "").strip()
    body  = (data.get("body") or "").strip()
    if not title or not body:
        return jsonify(ok=False, error="Title and body required"), 400
    if len(title) > 120:
        return jsonify(ok=False, error="Title too long (120 chars max)"), 400
    category = data.get("category", "general")
    if category not in ("general", "positive", "negative", "neutral"):
        category = "general"
    tid = create_response_template(current_user["restaurant_id"], title, body, category)
    return jsonify(ok=True, id=tid)

@client_bp.route("/api/templates/<int:tid>", methods=["DELETE"])
@login_required
def delete_template(tid, current_user):
    from models import delete_response_template
    delete_response_template(tid, current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/templates/<int:tid>/use", methods=["POST"])
@login_required
def use_template(tid, current_user):
    from models import increment_template_use
    increment_template_use(tid)
    return jsonify(ok=True)

@client_bp.route("/api/import-tripadvisor", methods=["POST"])
@login_required
def import_tripadvisor(current_user):
    import io, csv as _csv
    from models import Review, save_reviews
    # Admin can pass restaurant_id explicitly; clients always use their own
    admin_rid = request.form.get("restaurant_id")
    if admin_rid and current_user.get("is_admin"):
        rid = int(admin_rid)
    else:
        rid = current_user["restaurant_id"]
    f    = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="No file uploaded"), 400
    try:
        content = f.read().decode("utf-8-sig")  # handle BOM
    except Exception:
        return jsonify(ok=False, error="Could not read file — make sure it's a UTF-8 CSV"), 400
    if not content.strip():
        return jsonify(ok=False, error="File is empty"), 400
    try:
        rows = list(_csv.DictReader(io.StringIO(content)))
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}"), 400
    if not rows:
        return jsonify(ok=False, error="No data rows found"), 400

    # Normalise column names (lowercase, strip spaces)
    def _get(row, *keys):
        for k in keys:
            for rk in row:
                if rk.strip().lower() == k:
                    return (row[rk] or "").strip()
        return ""

    reviews = []
    for i, row in enumerate(rows):
        text   = _get(row, "text", "review", "body", "comment", "review text")
        rating_raw = _get(row, "rating", "stars", "score", "bubble")
        author = _get(row, "author", "reviewer", "name", "user", "username")
        date   = _get(row, "date", "review date", "published", "visited")
        title  = _get(row, "title", "review title", "headline")
        if not text or not rating_raw:
            continue
        try:
            rating = int(float(rating_raw))
        except Exception:
            continue
        if rating < 1 or rating > 5:
            continue
        full_text = (title + " — " + text) if title else text
        reviews.append(Review(
            restaurant_id=rid,
            platform="tripadvisor",
            external_id=f"ta_import_{i}_{hash(text[:40])}",
            author=author or "TripAdvisor Guest",
            rating=rating,
            text=full_text,
            review_date=date or None,
        ))
    if not reviews:
        return jsonify(ok=False, error="No valid reviews found — check column names (rating, text required)"), 400

    # Correct platform label from form override
    plat_override = (request.form.get("platform") or "").strip().lower()
    allowed_platforms = ("tripadvisor", "doordash", "ubereats")
    if plat_override in allowed_platforms:
        for rv in reviews:
            rv.platform = plat_override

    new_count, new_objs = save_reviews(reviews)
    # Trigger AI processing in background
    if new_objs:
        try:
            import threading as _t
            from analyser import process_new_reviews as _proc
            _t.Thread(target=_proc, args=(new_objs,), daemon=True).start()
        except Exception:
            pass
    return jsonify(ok=True, imported=len(reviews), new=new_count)

@client_bp.route("/api/response-performance")
@login_required
def response_performance_api(current_user):
    try:
        from models import get_response_performance
        days = int(request.args.get("days", 90))
        if days not in (30, 60, 90, 180):
            days = 90
        data = get_response_performance(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

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
    rid = current_user["restaurant_id"]
    cached = _cache_get("review-insight:" + str(rid))
    if cached:
        return jsonify(insight=cached)
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
        from models import get_conn as _gc_ri
        _conn_ri = _gc_ri()
        # 4-week rolling trend
        weekly_rows = _conn_ri.execute("""
            SELECT strftime('%Y-W%W', fetched_at) as week,
                   COUNT(*) as cnt,
                   ROUND(AVG(rating),2) as avg_r,
                   ROUND(SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END)*100.0/COUNT(*),1) as neg_pct
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-28 days')
            GROUP BY week ORDER BY week
        """, (rid,)).fetchall()
        this_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as neg,
                   SUM(CASE WHEN urgency='high' AND response_status NOT IN ('posted','skipped') THEN 1 ELSE 0 END) as urgent
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-7 days')
        """, (rid,)).fetchone()
        last_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-14 days')
              AND fetched_at < datetime('now','-7 days')
        """, (rid,)).fetchone()
        # Topic persistence — issues appearing in 2+ of the last 4 weeks
        # categories is a JSON array; pull raw rows and parse in Python
        topic_rows = _conn_ri.execute("""
            SELECT categories, strftime('%Y-W%W', fetched_at) as week
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-28 days')
              AND categories IS NOT NULL AND categories != '' AND categories != '[]'
        """, (rid,)).fetchall()
        import json as _json_ri
        topic_weeks = []
        for row in topic_rows:
            try:
                cats = _json_ri.loads(row["categories"]) if row["categories"] else []
            except Exception:
                cats = []
            for cat in cats:
                topic_weeks.append({"category": cat, "week": row["week"]})
        urgent_rows = _conn_ri.execute("""
            SELECT text FROM reviews
            WHERE restaurant_id=? AND urgency='high'
              AND response_status NOT IN ('posted','skipped')
            ORDER BY fetched_at DESC LIMIT 2
        """, (rid,)).fetchall()
        _conn_ri.close()

        # Build week-over-week string
        wow_str = ""
        if last_week and last_week["cnt"] > 0 and this_week and this_week["cnt"] > 0:
            diff = (this_week["cnt"] or 0) - last_week["cnt"]
            rdiff = round(((this_week["avg_r"] or 0) - (last_week["avg_r"] or 0)), 1)
            wow_str = f"vs last week: {'+' if diff>=0 else ''}{diff} reviews, avg rating {'up' if rdiff>0 else 'down' if rdiff<0 else 'unchanged'} {abs(rdiff) if rdiff!=0 else ''}."

        # Build 4-week rating trend string
        trend_str = ""
        if len(weekly_rows) >= 3:
            ratings = [r["avg_r"] for r in weekly_rows if r["avg_r"]]
            if len(ratings) >= 3:
                if all(ratings[i] <= ratings[i+1] for i in range(len(ratings)-1)):
                    trend_str = f"Rating IMPROVING {len(ratings)} weeks straight ({ratings[0]}★ → {ratings[-1]}★)."
                elif all(ratings[i] >= ratings[i+1] for i in range(len(ratings)-1)):
                    trend_str = f"Rating DECLINING {len(ratings)} weeks straight ({ratings[0]}★ → {ratings[-1]}★). Flag this."
                else:
                    trend_str = f"Rating unstable last {len(ratings)} weeks: {' → '.join(str(r) + '★' for r in ratings)}."
            neg_pcts = [r["neg_pct"] for r in weekly_rows if r["neg_pct"] is not None]
            if len(neg_pcts) >= 3 and neg_pcts[-1] > neg_pcts[0] + 5:
                trend_str += f" Negative % rising: {neg_pcts[0]}% → {neg_pcts[-1]}%."

        # Persistent topics (same issue 2+ weeks in a row)
        persist_str = ""
        from collections import defaultdict as _dd_ri
        _topic_map = _dd_ri(set)
        for row in topic_weeks:
            _topic_map[row["category"]].add(row["week"])
        persistent = [t for t, wks in _topic_map.items() if len(wks) >= 2]
        if persistent:
            persist_str = f"Recurring complaints (2+ weeks): {', '.join(persistent[:3])}."

        urgent_texts = "; ".join(f'"{r["text"][:80]}"' for r in urgent_rows) if urgent_rows else "none"
        issues_str = ", ".join(f"{i['label']} ({i['count']})" for i in top_issues) if top_issues else "no data"
        owner_name = restaurant.owner_name if restaurant else None
        rest_name  = restaurant.name if restaurant else "this restaurant"
        name_line  = f"Owner: {owner_name}" if owner_name else ""
        trend_block = (f"4-week trend: {trend_str}\n" if trend_str else "") + (f"Persistent issues: {persist_str}\n" if persist_str else "")
        prompt = (
            f"You are a restaurant reputation assistant. Output ONLY a 3-line snapshot.\n\n"
            f"Restaurant: {rest_name} | Today: {today_str}\n"
            f"Data: {rstats['total']} reviews | {rstats['avg_rating']}★ avg | "
            f"{rstats['positive']} pos / {rstats['negative']} neg / {rstats['neutral']} neutral | "
            f"{rstats['urgent']} urgent | response rate {rstats['response_rate']}%\n"
            f"Top topics: {issues_str} | {wow_str}\n"
            f"{trend_block}"
            f"Urgent excerpts: {urgent_texts}\n\n"
            "Return EXACTLY this format — 3 lines:\n"
            "\U0001f4ca This week: [1 punchy sentence on the most important number or multi-week trend. Be specific.]\n"
            "\u26a0\ufe0f Watch: [1 sentence on the biggest risk — multi-week declining trend, recurring complaint, rising negative %, or urgent review. Skip if nothing notable.]\n"
            "\u2705 Do today: [1 concrete action — e.g. 'Respond to Amanda L.s 1-star review about cold food.' Never generic.]\n\n"
            "Rules: no markdown, no extra lines, no preamble. Each line max 20 words. Never invent data. Prioritize multi-week trends over single-week blips."
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
        _cache_set("review-insight:" + str(rid), insight)
        return jsonify(insight=insight)
    except Exception as _re:
        import traceback
        print(f"[review-insight ERROR] {_re}\n{traceback.format_exc()}")
        stale = _insight_cache.get("review-insight:" + str(rid))
        if stale:
            return jsonify(insight=stale[1])
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
        topic_map = {}
        topic_order = []
        for r in rows:
            t = r["topic"]
            if not t:
                continue
            if t not in topic_map:
                topic_map[t] = {"topic": t, "posted": False, "platforms": [], "metrics": {}}
                topic_order.append(t)
            entry = topic_map[t]
            if r["post_id"]:
                entry["posted"] = True
                plat = (r["post_platform"] or "").strip()
                if plat and plat not in entry["platforms"]:
                    entry["platforms"].append(plat)
            m = entry["metrics"]
            if not m.get("reach") and r["reach"]:       m["reach"]       = int(r["reach"] or 0)
            if not m.get("impressions") and r["impressions"]: m["impressions"] = int(r["impressions"] or 0)
            if not m.get("likes") and r["likes"]:       m["likes"]       = int(r["likes"] or 0)
            if not m.get("comments") and r["comments"]: m["comments"]    = int(r["comments"] or 0)

        seen = []
        for t in topic_order:
            entry = topic_map[t]
            platforms = entry["platforms"]
            if len(platforms) > 1:
                entry["platform"] = " + ".join(p.replace("facebook","FB").replace("instagram","IG") for p in platforms)
            elif platforms:
                entry["platform"] = platforms[0]
            else:
                entry["platform"] = ""
            del entry["platforms"]
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
    rid = current_user["restaurant_id"]
    cached = _cache_get("mkt-insight:" + str(rid))
    if cached:
        return jsonify(insight=cached)
    try:
        from marketing import get_profile_for_restaurant, get_recent_content, get_upcoming_holidays, generate_content
        from models import get_restaurant
        from datetime import datetime
        from zoneinfo import ZoneInfo
        restaurant = get_restaurant(rid)
        name = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
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
        # Pull post performance with weekly trend detection
        perf_clause = ""
        try:
            from models import get_conn as _gc
            _conn = _gc()
            _perf_rows = _conn.execute(
                """SELECT topic, post_platform, reach, impressions, engaged, likes, comments
                   FROM marketing_content_log
                   WHERE restaurant_id=? AND post_id IS NOT NULL
                     AND (reach > 0 OR impressions > 0 OR likes > 0)
                   ORDER BY created_at DESC LIMIT 20""",
                (rid,)
            ).fetchall()
            _weekly = _conn.execute(
                """SELECT strftime('%Y-W%W', created_at) as week,
                          ROUND(AVG(CASE WHEN reach > 0 THEN reach END), 0) as avg_reach,
                          ROUND(AVG(CASE WHEN impressions > 0 THEN impressions END), 0) as avg_imp,
                          COUNT(*) as posts
                   FROM marketing_content_log
                   WHERE restaurant_id=? AND post_id IS NOT NULL
                     AND created_at >= datetime('now', '-56 days')
                   GROUP BY week ORDER BY week""",
                (rid,)
            ).fetchall()
            _conn.close()
            _perf_lines = []
            if _perf_rows:
                _sorted = sorted(_perf_rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0), reverse=True)
                for _r in _sorted[:3]:
                    _parts = []
                    if _r["reach"]:       _parts.append(str(int(_r["reach"])) + " reach")
                    if _r["impressions"]: _parts.append(str(int(_r["impressions"])) + " impr")
                    if _r["likes"]:       _parts.append(str(int(_r["likes"])) + " likes")
                    if _parts:
                        _perf_lines.append("BEST: " + _r["topic"] + " (" + _r["post_platform"] + "): " + ", ".join(_parts))
                for _r in _sorted[-3:]:
                    _parts = []
                    if _r["reach"]:       _parts.append(str(int(_r["reach"])) + " reach")
                    if _r["impressions"]: _parts.append(str(int(_r["impressions"])) + " impr")
                    if _parts:
                        _perf_lines.append("WEAK: " + _r["topic"] + " (" + _r["post_platform"] + "): " + ", ".join(_parts))
            _trend_lines = []
            if len(_weekly) >= 3:
                _reach_vals = [w["avg_reach"] for w in _weekly if w["avg_reach"]]
                if len(_reach_vals) >= 3:
                    if all(_reach_vals[i] >= _reach_vals[i+1] for i in range(len(_reach_vals)-1)):
                        _trend_lines.append("Reach DECLINING " + str(len(_reach_vals)) + " weeks straight (" + str(int(_reach_vals[0])) + " to " + str(int(_reach_vals[-1])) + ") — strategy pivot needed.")
                    elif all(_reach_vals[i] <= _reach_vals[i+1] for i in range(len(_reach_vals)-1)):
                        _trend_lines.append("Reach GROWING " + str(len(_reach_vals)) + " weeks straight (" + str(int(_reach_vals[0])) + " to " + str(int(_reach_vals[-1])) + ") — double down on what's working.")
                    else:
                        _diff_pct = round((_reach_vals[-1] - _reach_vals[0]) / max(_reach_vals[0], 1) * 100)
                        if abs(_diff_pct) > 20:
                            _trend_lines.append("Reach " + ("up" if _diff_pct > 0 else "down") + " " + str(abs(int(_diff_pct))) + "% over last " + str(len(_reach_vals)) + " weeks.")
            if _perf_lines or _trend_lines:
                perf_clause = "\n\nSocial performance data:"
                if _trend_lines:
                    perf_clause += "\nTrend: " + " ".join(_trend_lines)
                if _perf_lines:
                    perf_clause += "\n" + "\n".join(_perf_lines)
                perf_clause += "\nDouble down on BEST topics. Rethink or avoid WEAK ones. Reference the trend when advising."
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
        formatted = format_insight_html(insight)
        _cache_set("mkt-insight:" + str(rid), formatted)
        return jsonify(insight=formatted)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[MktInsight] ERROR: {str(e)}")
        stale = _insight_cache.get("mkt-insight:" + str(rid))
        if stale:
            return jsonify(insight=stale[1])
        return jsonify(insight="Marketing brief unavailable — check back shortly.")

@client_bp.route("/api/labor-insight")
@login_required
def labor_insight_api(current_user):
    rid = current_user["restaurant_id"]
    cached = _cache_get("labor-insight:" + str(rid))
    if cached:
        return jsonify(insight=cached)
    try:
        from labor import analyse_shifts_for_restaurant, get_claude_insights
        from models import get_restaurant
        restaurant = get_restaurant(rid)
        name  = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        analysis = analyse_shifts_for_restaurant(rid)
        from models import get_staff_notes as _gsn_labor
        _staff_notes_labor = _gsn_labor(rid)
        insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner,
                                      restaurant_id=rid,
                                      staff_notes=_staff_notes_labor if _staff_notes_labor else None)
        formatted = format_insight_html(insight)
        _cache_set("labor-insight:" + str(rid), formatted)
        return jsonify(insight=formatted)
    except Exception as e:
        import traceback; traceback.print_exc()
        stale = _insight_cache.get("labor-insight:" + str(rid))
        if stale:
            return jsonify(insight=stale[1])
        return jsonify(insight="Unable to load analysis — check back shortly.")

@client_bp.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    try:
        from inventory import load_inventory_for_restaurant, analyse_inventory, get_claude_insights
        restaurant = get_restaurant(current_user["restaurant_id"])
        items, _is_live = load_inventory_for_restaurant(current_user["restaurant_id"])
        analysis = analyse_inventory(items)
        owner_name = restaurant.owner_name if restaurant else None
        insight = get_claude_insights(analysis, owner_name=owner_name, restaurant_name=restaurant.name if restaurant else None, restaurant_id=current_user["restaurant_id"], items=items)
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
        conn = get_conn()
        conn.execute(
            "UPDATE reviews SET response_status='drafted', regenerate_count=COALESCE(regenerate_count,0)+1 WHERE id=? AND restaurant_id=?",
            (review_id, current_user["restaurant_id"])
        )
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
    conn.execute(
        "UPDATE reviews SET response_status='drafted', draft_edited=1 WHERE id=? AND restaurant_id=?",
        (review_id, current_user["restaurant_id"])
    )
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
        resp = jsonify(weeks=weeks)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
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

def _build_schedule_result(restaurant_id):
    """Shared logic for both schedule endpoints."""
    from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                       generate_optimized_schedule, get_hourly_rate)
    from models import get_restaurant, get_staff_notes, get_yoy_schedule_context
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI

    restaurant = get_restaurant(restaurant_id)
    shifts = load_shifts_for_restaurant(restaurant_id)
    if not shifts:
        raise ValueError("No shift data available — upload shifts CSV first")
    analysis = analyse_shifts_for_restaurant(restaurant_id)
    # Use blended rate from per-role rates if available, otherwise flat rate
    rate = analysis.get("blended_rate") or get_hourly_rate(restaurant_id)
    target   = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    owner    = restaurant.owner_name if restaurant else None
    staff_notes = get_staff_notes(restaurant_id) or None

    # Employee availability
    from models import get_staff_availability as _gsa, init_staff_availability as _isa
    try:
        _isa()
        staff_availability = _gsa(restaurant_id) or []
    except Exception:
        staff_availability = []

    # Compute next week dates
    today = _dt.now(_ZI('America/Chicago')).replace(tzinfo=None)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + _td(days=days_ahead)
    next_week_dates = [(monday + _td(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    # Revenue override from restaurant target (takes priority over YoY sum)
    monthly_rev_target = float(getattr(restaurant, 'monthly_revenue_target', 0) or 0)

    # YoY context — same day last year
    yoy_ctx = get_yoy_schedule_context(restaurant_id, next_week_dates)

    # Flag holiday matches in YoY context
    try:
        from marketing import get_upcoming_holidays as _guh_sched
        import re as _re_h
        _hol_str = _guh_sched(today)
        if _hol_str:
            _hol_this_week = {}
            for chunk in _hol_str.split(", "):
                m = _re_h.search(r'\((\w+ \d+)\)$', chunk)
                if m:
                    try:
                        hdate = _dt.strptime(m.group(1) + " " + str(today.year), "%b %d %Y")
                        for nd in next_week_dates:
                            if hdate.strftime("%Y-%m-%d") == nd:
                                _hol_this_week[nd] = chunk[:chunk.rfind("(")].strip()
                    except Exception:
                        pass
            for row in yoy_ctx:
                nd = row.get("next_week_date", "")
                if nd in _hol_this_week:
                    row["is_holiday"] = True
                    row["holiday_name"] = _hol_this_week[nd]
    except Exception:
        pass

    # Upcoming events for the schedule banner
    upcoming_events = []
    try:
        from marketing import get_upcoming_holidays as _guh2
        import re as _re_ev
        _ev_str = _guh2(today)
        if _ev_str:
            for chunk in _ev_str.split(", "):
                m = _re_ev.search(r'\((\w+ \d+)\)$', chunk)
                if m:
                    try:
                        edate = _dt.strptime(m.group(1) + " " + str(today.year), "%b %d %Y")
                        days_away = (edate - today).days
                        if 0 <= days_away <= 21:
                            upcoming_events.append({
                                "name": chunk[:chunk.rfind("(")].strip(),
                                "date_str": m.group(1),
                                "days_away": days_away
                            })
                    except Exception:
                        pass
    except Exception:
        pass

    result = generate_optimized_schedule(
        analysis, shifts,
        restaurant_name=restaurant.name if restaurant else "Restaurant",
        hourly_rate=rate,
        owner_name=owner,
        staff_notes=staff_notes,
        labor_target=target,
        yoy_context=yoy_ctx,
        upcoming_events=upcoming_events if upcoming_events else None,
        monthly_revenue_target=monthly_rev_target,
        hours_notes=getattr(restaurant, 'hours_notes', None),
        role_rates=analysis.get("role_rates") or {},
        section_count=getattr(restaurant, 'section_count', None),
        daypart_split=getattr(restaurant, 'daypart_split', None),
        delivery_pct=getattr(restaurant, 'delivery_pct', None),
        role_minimums_json=getattr(restaurant, 'role_minimums_json', None),
        sched_notes=getattr(restaurant, 'sched_notes', None),
        staff_availability=staff_availability or None,
    )
    result["restaurant_name"] = restaurant.name if restaurant else "Restaurant"
    return result


_schedule_jobs = {}  # job_id -> {"status": "pending"|"done"|"error", "result": ...}

def _run_schedule_job(job_id, restaurant_id):
    import csv as _csv_mod, io as _io_sched, traceback as _tb
    try:
        result = _build_schedule_result(restaurant_id)
        from models import get_staff_notes as _gsn_sched
        _raw_notes = _gsn_sched(restaurant_id) or []
        staff_constraints = {n["employee_name"]: n["notes"] for n in _raw_notes if n.get("employee_name")}
        preview_rows = []
        hours_scheduled = 0.0
        try:
            _COLS = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
            _csv_lines = result["schedule_csv"].split("\n")
            print(f"[schedule] csv lines={len(_csv_lines)} first3={_csv_lines[:3]}")
            for _line in _csv_lines[1:]:  # skip header
                _line = _line.strip()
                if not _line:
                    continue
                _parts = _line.split(",", 7)  # max 7 splits — notes gets remainder
                if len(_parts) < 6:
                    continue
                # Strip outer quotes Sonnet sometimes adds around field values
                _row = {_COLS[i]: _parts[i].strip().strip('"').strip() for i in range(min(len(_parts), 8))}
                # Keep rows that have a non-empty employee name — skips header repeats and prose
                if not _row.get("employee", "").strip() or _row.get("employee", "").lower() == "employee":
                    continue
                preview_rows.append(_row)
                try:
                    hours_scheduled += float(_row.get("scheduled_hours") or 0)
                except (ValueError, TypeError):
                    pass
            print(f"[schedule] parsed {len(preview_rows)} rows, first={preview_rows[0] if preview_rows else None}")
        except Exception as _csv_ex:
            print(f"[schedule] csv parse error: {_csv_ex}")
            pass
        _schedule_jobs[job_id] = {
            "status": "done",
            "result": dict(
                ok=True,
                schedule_csv=result["schedule_csv"],
                summary=result.get("summary", []),
                preview_rows=preview_rows,
                week_dates=result.get("week_dates", []),
                week_days=result.get("week_days", []),
                projected_revenue=result.get("projected_revenue", 0),
                hours_budget=result.get("hours_budget", 0),
                labor_budget_dollars=result.get("labor_budget_dollars", 0),
                hours_scheduled=round(hours_scheduled, 1),
                labor_target=result.get("labor_target", 30),
                staff_constraints=staff_constraints,
            )
        }
    except Exception as e:
        tb = _tb.format_exc()
        print(f"[schedule job] FAILED:\n{tb}")
        _schedule_jobs[job_id] = {"status": "error", "result": {"ok": False, "error": str(e), "traceback": tb}}


@client_bp.route("/api/generate-schedule", methods=["GET"])
@login_required
def generate_schedule_json(current_user):
    """Start async schedule generation. Returns job_id for polling."""
    import threading, uuid
    job_id = str(uuid.uuid4())
    _schedule_jobs[job_id] = {"status": "pending", "result": None}
    t = threading.Thread(target=_run_schedule_job, args=(job_id, current_user["restaurant_id"]), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@client_bp.route("/api/schedule-status/<job_id>", methods=["GET"])
def schedule_status(job_id):
    """Poll for schedule generation result. No login_required — job_id is an unguessable UUID."""
    job = _schedule_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "status": "error", "error": "Job not found"}), 404
    if job["status"] == "pending":
        return jsonify({"ok": True, "status": "pending"})
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        _schedule_jobs.pop(job_id, None)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "status": "error", "error": str(e)}), 500


@client_bp.route("/api/download-schedule")
@login_required
def download_schedule(current_user):
    import io
    try:
        result = _build_schedule_result(current_user["restaurant_id"])
        csv_clean = result["schedule_csv"]
        name = result.get("restaurant_name", "Restaurant").replace(" ", "_")
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

def _normalize_phone(raw):
    import re
    digits = re.sub(r'\D', '', raw or '')
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) > 7:
        return '+' + digits
    return None


@client_bp.route("/api/alert-settings", methods=["GET"])
@login_required
def get_alert_settings(current_user):
    from notify import get_alert_contacts
    from models import get_restaurant
    rid = current_user["restaurant_id"]
    contacts = get_alert_contacts(rid)
    r = get_restaurant(rid)
    settings = {
        "alert_1star":           r.alert_1star,
        "alert_2star":           r.alert_2star,
        "alert_health":          r.alert_health,
        "alert_neg_spike":       r.alert_neg_spike,
        "alert_negative_trend":  r.alert_negative_trend,
        "alert_no_response":     r.alert_no_response,
        "alert_5star":           r.alert_5star,
        "alert_rating_threshold": r.alert_rating_threshold,
        "alert_rating_floor":    r.alert_rating_floor,
        "alert_labor_over":      r.alert_labor_over,
        "alert_any_review":      getattr(r, "alert_any_review", 0),
        "alert_resp_approved":   getattr(r, "alert_resp_approved", 0),
        "urgent_via_sms":        getattr(r, "urgent_via_sms", 0),
        "urgent_via_email":      getattr(r, "urgent_via_email", 0),
        "digest_enabled":        getattr(r, "digest_enabled", 1),
        "digest_day":            getattr(r, "digest_day", "monday"),
    }
    return jsonify(ok=True, contacts=contacts, settings=settings)


@client_bp.route("/api/alert-settings", methods=["POST"])
@login_required
def save_alert_settings(current_user):
    from notify import get_alert_contacts, add_alert_contact, delete_alert_contact
    from models import update_restaurant
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]

    # Sync contacts — max 2
    new_contacts = (data.get("contacts") or [])[:2]
    existing = get_alert_contacts(rid)
    for ec in existing:
        delete_alert_contact(ec["id"])
    for nc in new_contacts:
        phone = _normalize_phone(nc.get("phone") or "")
        name  = (nc.get("name")  or "").strip()
        if phone:
            add_alert_contact(rid, name, phone)

    update_restaurant(rid, {
        "alert_1star":           int(bool(data.get("alert_1star"))),
        "alert_2star":           int(bool(data.get("alert_2star"))),
        "alert_health":          int(bool(data.get("alert_health"))),
        "alert_neg_spike":       int(bool(data.get("alert_neg_spike"))),
        "alert_negative_trend":  int(bool(data.get("alert_negative_trend"))),
        "alert_no_response":     int(bool(data.get("alert_no_response"))),
        "alert_5star":           int(bool(data.get("alert_5star"))),
        "alert_rating_threshold": int(bool(data.get("alert_rating_threshold"))),
        "alert_rating_floor":    float(data.get("alert_rating_floor") or 4.0),
        "alert_labor_over":      int(bool(data.get("alert_labor_over"))),
        "urgent_via_sms":        int(bool(data.get("urgent_via_sms"))),
        "urgent_via_email":      int(bool(data.get("urgent_via_email"))),
        "alert_any_review":      int(bool(data.get("alert_any_review"))),
        "alert_resp_approved":   int(bool(data.get("alert_resp_approved"))),
        "digest_enabled":        int(bool(data.get("digest_enabled"))),
        "digest_day":            data.get("digest_day", "monday"),
    })
    return jsonify(ok=True)


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
    _ot_flags = []
    try:
        if data_type == "shifts":
            from labor import analyse_shifts_for_restaurant, get_hourly_rate as _ghr
            _shift_analysis = analyse_shifts_for_restaurant(restaurant_id)
            _ot_flags = [f for f in _shift_analysis.get("overtime_risk", []) if f.get("status") == "overtime"]
            # Persist per-day breakdown for YoY schedule generation
            try:
                from models import save_labor_daily_history as _sldh
                _sldh(restaurant_id, _shift_analysis.get("by_day", {}))
            except Exception as _dh_e:
                print(f"[daily history] {_dh_e}")
            # Persist this upload as a labor_history snapshot so trend chart is immediately correct
            try:
                from models import save_labor_snapshot as _sls
                _dr = _shift_analysis.get("date_range", {})
                if _dr.get("start") and _dr.get("end"):
                    _sls(restaurant_id, _dr["start"], _dr["end"],
                         _shift_analysis["overall_labor_pct"],
                         _shift_analysis["total_labor_cost"],
                         _shift_analysis["total_sales"])
            except Exception as _snap_e:
                print(f"[labor snapshot] {_snap_e}")
            try:
                from webhooks import fire_webhook as _fw_labor
                _fw_labor(restaurant_id, "labor.updated", {
                    "labor_pct": _shift_analysis.get("labor_pct"),
                    "total_hours": _shift_analysis.get("total_hours"),
                })
            except Exception:
                pass
        elif data_type == "inventory":
            import threading as _t_inv
            _rid_inv = restaurant_id
            def _inv_trend_bg():
                try:
                    from inventory import load_inventory_for_restaurant as _lif, analyse_inventory as _ai, compute_item_trends as _cit
                    from webhooks import fire_webhook as _fw_inv
                    _items, _ = _lif(_rid_inv)
                    _analysis = _ai(_items)
                    _trends = _cit(_rid_inv, _items)
                    _fw_inv(_rid_inv, "inventory.updated", {
                        "waste_rate_pct": _analysis.get("waste_rate_pct"),
                        "benchmark": _analysis.get("benchmark_label"),
                        "total_waste_cost": _analysis.get("total_waste_cost_week"),
                    })
                    if (_analysis.get("waste_rate_pct") or 0) > 8:
                        _fw_inv(_rid_inv, "inventory.cost_alert", {
                            "waste_rate_pct": _analysis.get("waste_rate_pct"),
                            "benchmark": _analysis.get("benchmark_label"),
                            "monthly_projection": _analysis.get("monthly_waste_projection"),
                        })
                    for _pa in _trends["price_alerts"]:
                        _fw_inv(_rid_inv, "food_cost.price_increase", _pa)
                    for _ta in _trends["trend_alerts"]:
                        _fw_inv(_rid_inv, "food_cost.price_trend", _ta)
                except Exception as _ie:
                    print(f"[inv trend bg] {_ie}")
            _t_inv.Thread(target=_inv_trend_bg, daemon=True).start()
    except Exception:
        pass  # non-fatal — data is saved, analysis will run on next load

    # Overtime alert — email owner immediately when upload reveals an overtime employee
    if _ot_flags:
        try:
            import os as _os_ot, resend as _resend_ot
            from models import get_restaurant as _gr_ot
            _r_ot = _gr_ot(restaurant_id)
            _key_ot = _os_ot.getenv("RESEND_API_KEY", "")
            _from_ot = _os_ot.getenv("FROM_EMAIL", "will@cavnar.ai")
            if _key_ot and _r_ot and _r_ot.owner_email:
                _resend_ot.api_key = _key_ot
                _ot_rows = "".join(
                    "<tr><td style='padding:6px 10px;border-bottom:1px solid #e0dbd0'><strong>" +
                    f["employee"] + "</strong></td><td style='padding:6px 10px;border-bottom:1px solid #e0dbd0'>" +
                    str(f["hours"]) + "h — week of " + f["week"] + "</td></tr>"
                    for f in _ot_flags
                )
                _resend_ot.Emails.send({
                    "from": "Cavnar AI Labor Alerts <" + _from_ot + ">",
                    "to": [_r_ot.owner_email],
                    "subject": "⚠ Overtime detected — " + _r_ot.name,
                    "html": (
                        "<div style='font-family:sans-serif;max-width:520px;margin:0 auto'>"
                        "<div style='border-top:3px solid #e07040;padding-top:20px;margin-bottom:16px'>"
                        "<h3 style='color:#0e0c0a;margin:0'>Overtime Alert</h3>"
                        "<p style='font-size:13px;color:#7a736a;margin:4px 0 0'>Cavnar AI Labor Monitor</p>"
                        "</div>"
                        "<p style='font-size:15px;line-height:1.6;color:#0e0c0a'>Your latest shift upload shows "
                        + str(len(_ot_flags)) + " employee(s) in overtime this week:</p>"
                        "<table style='width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px'>"
                        "<thead><tr style='background:#f7f4ef'>"
                        "<th style='padding:6px 10px;text-align:left;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#7a736a'>Employee</th>"
                        "<th style='padding:6px 10px;text-align:left;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#7a736a'>Hours</th>"
                        "</tr></thead><tbody>" + _ot_rows + "</tbody></table>"
                        "<p style='font-size:13px;color:#7a736a'>Hours over 40 are billed at 1.5× — "
                        "consider adjusting next week's schedule to avoid repeat overtime.</p>"
                        "<hr style='border:none;border-top:1px solid #e0dbd0;margin:16px 0'/>"
                        "<p style='font-size:11px;color:#7a736a'>Cavnar AI — dashboard.cavnar.ai</p>"
                        "</div>"
                    )
                })
        except Exception:
            pass

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


# ── Food cost quick count ─────────────────────────────────────────────────────

@client_bp.route("/api/food-cost-quickcount", methods=["POST"])
@login_required
def food_cost_quickcount(current_user):
    """Save Big-8 ingredient prices, compute week-over-week drift, return alerts."""
    import json as _json_fc
    from datetime import datetime as _dt_fc
    from models import get_client_data as _gcd, get_conn as _gcc

    data = request.get_json() or {}
    items = data.get("items", [])
    if not items or not isinstance(items, list):
        return jsonify(ok=False, error="No items provided"), 400

    rid = current_user["restaurant_id"]
    now_str = _dt_fc.now().strftime("%Y-%m-%d")

    # Load existing saved data
    existing_raw = _gcd(rid)
    existing_fc = {}
    if existing_raw and existing_raw.get("food_cost_json"):
        try:
            existing_fc = _json_fc.loads(existing_raw["food_cost_json"])
        except Exception:
            existing_fc = {}

    prev = existing_fc.get("current")  # rotate current → previous
    new_current = {"submitted_at": now_str, "items": items}

    # Compute price drift vs previous submission
    drift = []
    if prev and prev.get("items"):
        prev_map = {i["name"].lower(): i for i in prev["items"] if i.get("name")}
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            prev_item = prev_map.get(name.lower())
            if not prev_item:
                continue
            try:
                curr_price = float(item.get("price") or 0)
                prev_price = float(prev_item.get("price") or 0)
                if prev_price > 0 and curr_price > 0:
                    pct = round((curr_price - prev_price) / prev_price * 100, 1)
                    if abs(pct) >= 3:  # only flag meaningful changes
                        weekly_usage = float(item.get("usage") or 0)
                        weekly_impact = round((curr_price - prev_price) * weekly_usage, 2)
                        drift.append({
                            "name": name,
                            "prev_price": prev_price,
                            "curr_price": curr_price,
                            "pct_change": pct,
                            "weekly_impact": weekly_impact,
                            "direction": "up" if pct > 0 else "down"
                        })
            except Exception:
                pass
    drift.sort(key=lambda x: abs(x["weekly_impact"]), reverse=True)

    # Save new data
    save_payload = _json_fc.dumps({"current": new_current, "previous": prev or {}})
    conn = _gcc()
    existing_row = conn.execute("SELECT id FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
    if existing_row:
        conn.execute("UPDATE client_data SET food_cost_json=?, updated_at=datetime('now') WHERE restaurant_id=?",
                     (save_payload, rid))
    else:
        conn.execute("INSERT INTO client_data (restaurant_id, food_cost_json) VALUES (?, ?)",
                     (rid, save_payload))
    conn.commit()
    conn.close()

    total_impact = sum(d["weekly_impact"] for d in drift if d["direction"] == "up")
    return jsonify(ok=True, drift=drift, total_weekly_impact=round(total_impact, 2),
                   submitted_at=now_str, prev_submitted_at=prev.get("submitted_at") if prev else None)


@client_bp.route("/api/food-cost/save-custom-item", methods=["POST"])
@login_required
def save_food_cost_custom_item(current_user):
    """Persist a custom ingredient name+unit to food_cost_json so it appears on next load."""
    import json as _jci
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400

    rid = current_user["restaurant_id"]
    from models import get_client_data as _gcd_ci, get_conn as _gcc_ci
    existing_raw = _gcd_ci(rid)
    fc = {}
    if existing_raw and existing_raw.get("food_cost_json"):
        try:
            fc = _jci.loads(existing_raw["food_cost_json"])
        except Exception:
            fc = {}

    custom_items = fc.get("custom_items", [])
    existing_names = [ci["name"].lower() for ci in custom_items if ci.get("name")]
    if name.lower() not in existing_names:
        custom_items.append({"name": name, "unit": unit})
        fc["custom_items"] = custom_items
        payload = _jci.dumps(fc)
        conn = _gcc_ci()
        row = conn.execute("SELECT id FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
        if row:
            conn.execute("UPDATE client_data SET food_cost_json=?, updated_at=datetime('now') WHERE restaurant_id=?",
                         (payload, rid))
        else:
            conn.execute("INSERT INTO client_data (restaurant_id, food_cost_json) VALUES (?, ?)", (rid, payload))
        conn.commit()
        conn.close()

    return jsonify(ok=True, name=name, unit=unit)


@client_bp.route("/api/food-cost/delete-custom-item", methods=["POST"])
@login_required
def delete_food_cost_custom_item(current_user):
    """Remove a saved custom ingredient by name from food_cost_json."""
    import json as _jcd
    data = request.get_json() or {}
    name = (data.get("name") or "").strip().lower()
    if not name:
        return jsonify(ok=False, error="Name required"), 400

    rid = current_user["restaurant_id"]
    from models import get_client_data as _gcd_d, get_conn as _gcc_d
    existing_raw = _gcd_d(rid)
    fc = {}
    if existing_raw and existing_raw.get("food_cost_json"):
        try:
            fc = _jcd.loads(existing_raw["food_cost_json"])
        except Exception:
            fc = {}

    custom_items = fc.get("custom_items", [])
    fc["custom_items"] = [ci for ci in custom_items if (ci.get("name") or "").lower() != name]
    payload = _jcd.dumps(fc)
    conn = _gcc_d()
    conn.execute("UPDATE client_data SET food_cost_json=?, updated_at=datetime('now') WHERE restaurant_id=?",
                 (payload, rid))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ── Review request ────────────────────────────────────────────────────────────

@client_bp.route("/api/send-review-request", methods=["POST"])
@login_required
def send_review_request(current_user):
    try:
        data          = request.get_json() or {}
        customer_name  = (data.get("name") or "").strip()
        customer_email = (data.get("email") or "").strip().lower()
        customer_phone = (data.get("phone") or "").strip()
        if not customer_email and not customer_phone:
            return jsonify(ok=False, error="Email or phone required"), 400
        if customer_email and "@" not in customer_email:
            return jsonify(ok=False, error="Valid email address required"), 400

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

        # Send via SMS if phone provided
        if customer_phone:
            from notify import send_sms as _send_sms
            sms_text = (
                f"Hi {first_name}, thanks for dining at {rest_name}! "
                f"We'd love your feedback — leave us a Google review: {review_url}"
            )
            sent_sms = _send_sms(customer_phone, sms_text)
            if not sent_sms and not customer_email:
                return jsonify(ok=False, error="SMS delivery failed — check Twilio config"), 500

        # Send via Resend if email provided
        if not customer_email:
            # SMS-only path — skip email block
            from models import get_conn as _gc
            conn = _gc()
            conn.execute(
                "INSERT INTO review_requests (restaurant_id, customer_name, customer_email, customer_phone, method) VALUES (?,?,?,?,?)",
                (rid, customer_name, "", customer_phone, "sms")
            )
            conn.commit()
            conn.close()
            return jsonify(ok=True)

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
        method = "both" if customer_phone else "email"
        conn.execute(
            "INSERT INTO review_requests (restaurant_id, customer_name, customer_email, customer_phone, method) VALUES (?,?,?,?,?)",
            (rid, customer_name, customer_email, customer_phone or None, method)
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
    try:
        return _ai_visibility_inner(current_user)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200

def _ai_visibility_inner(current_user):
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404

    name        = r.name or ""
    neighborhood = r.neighborhood or ""
    vibe        = r.vibe or ""
    known_for   = r.known_for or ""
    # Use just the city portion (before any em dash or comma-detail) for clean queries
    city = neighborhood.split("—")[0].split(",")[0].strip() if neighborhood else ""
    city_full = neighborhood.split("—")[0].strip() if neighborhood else ""
    # Short cuisine descriptor from known_for first word(s), fallback to "restaurant"
    cuisine = (known_for.split(",")[0].strip() if known_for else "") or "restaurant"

    vibe_query = ("Where can I find " + vibe.strip() + " in " + city_full + "?") if (vibe and city) else None

    if vibe and city:
        vibe_l = vibe.lower()
        if any(w in vibe_l for w in ["bar", "lively", "cocktail", "drinks", "nightlife"]):
            occasion = "a night out"
        elif any(w in vibe_l for w in ["romantic", "intimate", "date"]):
            occasion = "date night"
        elif any(w in vibe_l for w in ["family", "kids", "casual"]):
            occasion = "family dinner"
        elif any(w in vibe_l for w in ["brunch", "breakfast", "morning"]):
            occasion = "brunch"
        else:
            occasion = "dinner"
        q3 = "Best restaurants for " + occasion + " in " + city_full
    else:
        q3 = "Best " + cuisine + " in " + city_full

    if city:
        queries = [
            vibe_query or (name + " restaurant in " + city_full),
            "Top restaurants in " + city_full,
            q3,
        ]
    else:
        queries = [
            name + " restaurant",
            "Top local " + cuisine + " restaurants",
            "Best " + cuisine + " restaurant near me",
        ]

    import requests as _pplx_req
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _pplx_key = os.getenv("PERPLEXITY_API_KEY", "")
    appeared_count = 0

    def _norm(s):
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("’", "").replace("’", ""))

    norm_name = _norm(name)

    def _run_query(q):
        try:
            resp = _pplx_req.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {_pplx_key}", "Content-Type": "application/json"},
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "Answer in under 80 words. Recommend specific restaurants by name. Do not include citations, footnotes, or markdown formatting."},
                        {"role": "user", "content": q},
                    ],
                    "max_tokens": 300,
                },
                timeout=10
            )
            answer = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") if resp.status_code == 200 else ""
            appeared = bool(norm_name) and bool(answer) and norm_name in _norm(answer)
            return {"query": q, "answer": answer[:400], "appeared": appeared}
        except Exception:
            return {"query": q, "answer": "Could not fetch answer.", "appeared": False}

    # Run all queries in parallel — caps total time at ~10s instead of 30s+
    query_results = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=3) as _pool:
        _futures = {_pool.submit(_run_query, q): i for i, q in enumerate(queries)}
        for _fut in as_completed(_futures):
            i = _futures[_fut]
            try:
                query_results[i] = _fut.result()
            except Exception:
                query_results[i] = {"query": queries[i], "answer": "Could not fetch answer.", "appeared": False}
    appeared_count = sum(1 for r in query_results if r and r.get("appeared"))

    # GBP completeness score — 10 items x 10 pts = 100
    # Items 1-6: checkable from our own DB (no GMB OAuth needed)
    # Items 7-10: require GMB OAuth connection
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

    # 1. Google Place ID — lets AI tools index the right location
    if bool(r.google_place_id):
        gbp_score += 10
        checklist.append({"label": "Google Place ID connected", "done": True, "pts": 10,
                          "action": "Done — AI tools can find your location", "needs_gmb": False})
    else:
        checklist.append({"label": "Add your Google Place ID", "done": False, "pts": 10,
                          "action": "Go to Account → paste your Google Place ID so AI tools can index you",
                          "needs_gmb": False})

    # 2. Yelp profile linked — Perplexity and ChatGPT pull heavily from Yelp
    if bool(r.yelp_business_id):
        gbp_score += 10
        checklist.append({"label": "Yelp profile linked", "done": True, "pts": 10,
                          "action": "Done — Perplexity indexes Yelp heavily", "needs_gmb": False})
    else:
        checklist.append({"label": "Link your Yelp business profile", "done": False, "pts": 10,
                          "action": "Go to Account → add your Yelp business ID (find it in your Yelp URL)",
                          "needs_gmb": False})

    # 3. Menu URL — admin sets this; silently award pts if present, hide if not
    if bool(r.menu_url):
        gbp_score += 10
        checklist.append({"label": "Menu URL added", "done": True, "pts": 10,
                          "action": "Done — AI tools can surface your menu in results", "needs_gmb": False})

    # 4. Restaurant profile — vibe + known_for + neighborhood power all AI queries
    has_full_profile = bool(r.neighborhood and r.vibe and r.known_for)
    if has_full_profile:
        gbp_score += 10
        checklist.append({"label": "Restaurant profile fully filled in", "done": True, "pts": 10,
                          "action": "Done — neighborhood, vibe, and specialties all set", "needs_gmb": False})
    else:
        missing = [f for f, v in [("neighborhood", r.neighborhood), ("vibe", r.vibe), ("known for", r.known_for)] if not v]
        checklist.append({"label": "Complete restaurant profile (" + ", ".join(missing) + " missing)", "done": False, "pts": 10,
                          "action": "Go to Account → fill in neighborhood, vibe, and what you're known for",
                          "needs_gmb": False})

    # 5. Review volume — AI systems rank by review count; 50+ is the threshold for appearing
    rstats = get_review_stats(rid)
    resp_rate = rstats.get("response_rate", 0) if rstats else 0
    review_total = rstats.get("total", 0) if rstats else 0
    if review_total >= 50:
        gbp_score += 10
        checklist.append({"label": "50+ Google reviews", "done": True, "pts": 10,
                          "action": "Done — strong review volume boosts AI ranking", "needs_gmb": False})
    elif review_total >= 20:
        gbp_score += 5
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "done": False, "pts": 10,
                          "action": "Send review requests to recent customers — 50+ reviews is the AI visibility threshold",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "done": False, "pts": 10,
                          "action": "Send review requests after every visit — this is the #1 driver of AI search ranking",
                          "needs_gmb": False})

    # 6. Review response rate — active engagement signals a healthy business to AI tools
    if resp_rate >= 75:
        gbp_score += 10
        checklist.append({"label": "Excellent review response rate (" + str(resp_rate) + "%)", "done": True, "pts": 10,
                          "action": "Done — responding to reviews signals an active, trusted business", "needs_gmb": False})
    elif resp_rate >= 40:
        gbp_score += 5
        checklist.append({"label": "Increase response rate to 75%+ (currently " + str(resp_rate) + "%)", "done": False, "pts": 10,
                          "action": "Use the Reviews tab to draft and post responses — AI tools reward active owner engagement",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Start responding to Google reviews (currently " + str(resp_rate) + "%)", "done": False, "pts": 10,
                          "action": "Go to Reviews → use AI-drafted responses to reply — aim for 75%+ response rate",
                          "needs_gmb": False})

    # 7. GBP OAuth connected — unlocks real-time profile data and future auto-posting
    if gbp_connected:
        gbp_score += 10
        checklist.append({"label": "Google Business Profile connected", "done": True, "pts": 10,
                          "action": "Done — real-time GBP data is active", "needs_gmb": False})
    else:
        checklist.append({"label": "Connect Google Business Profile (OAuth)", "done": False, "pts": 10,
                          "action": "Go to Account → Connect GBP to unlock live profile editing and Google Posts",
                          "needs_gmb": True})

    # 8. Business description — keyword-rich descriptions are indexed by every AI search tool
    desc = gbp_data.get("description", "")
    if desc and len(desc) >= 150:
        gbp_score += 10
        checklist.append({"label": "Business description written (" + str(len(desc)) + " chars)", "done": True, "pts": 10,
                          "action": "Done — description feeds AI search results directly", "needs_gmb": False})
    elif desc:
        gbp_score += 4
        checklist.append({"label": "Expand GBP description to 150+ chars (currently " + str(len(desc)) + ")", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Description: add cuisine type, atmosphere, and signature dishes",
                          "needs_gmb": True})
    else:
        checklist.append({"label": "Write a keyword-rich GBP business description", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Description: mention cuisine, ambiance, and top dishes (150+ chars)",
                          "needs_gmb": True})

    # 9. Phone number in GBP — basic trust signal; missing phone = incomplete listing
    has_phone = bool(gbp_data.get("phone"))
    if gbp_connected and has_phone:
        gbp_score += 10
        checklist.append({"label": "Phone number in GBP", "done": True, "pts": 10,
                          "action": "Done", "needs_gmb": False})
    elif gbp_connected and not has_phone:
        checklist.append({"label": "Add phone number to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Phone: add your primary number",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add phone number to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Phone: add your primary number",
                          "needs_gmb": True})

    # 10. Website linked in GBP — AI tools follow the website link to gather more context
    has_website = bool(gbp_data.get("website"))
    if gbp_connected and has_website:
        gbp_score += 10
        checklist.append({"label": "Website linked in GBP", "done": True, "pts": 10,
                          "action": "Done — AI tools crawl your website for menu and about content", "needs_gmb": False})
    elif gbp_connected and not has_website:
        checklist.append({"label": "Add website URL to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Website: add your restaurant's website",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add website URL to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Website: add your restaurant's website",
                          "needs_gmb": True})

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


@client_bp.route("/api/webhook", methods=["GET"])
@login_required
def webhook_get(current_user):
    from webhooks import get_webhook
    import json
    wh = get_webhook(current_user["restaurant_id"])
    if not wh:
        return jsonify(ok=True, webhook=None)
    return jsonify(ok=True, webhook={
        "url":         wh["url"],
        "secret":      wh["secret"],
        "events":      json.loads(wh.get("events") or "[]"),
        "last_fired":  wh.get("last_fired_at"),
        "last_status": wh.get("last_status"),
    })

@client_bp.route("/api/webhook", methods=["POST"])
@login_required
def webhook_save(current_user):
    from webhooks import save_webhook
    import json
    data   = request.get_json()
    url    = (data.get("url") or "").strip()
    events = data.get("events") or ["review.received", "alert.fired", "response.approved"]
    if not url.startswith("http"):
        return jsonify(ok=False, error="Invalid URL")
    secret = save_webhook(current_user["restaurant_id"], url, events)
    return jsonify(ok=True, secret=secret)

@client_bp.route("/api/webhook", methods=["DELETE"])
@login_required
def webhook_delete(current_user):
    from webhooks import delete_webhook
    delete_webhook(current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/webhook/test", methods=["POST"])
@login_required
def webhook_test(current_user):
    from webhooks import get_webhook, _deliver
    wh = get_webhook(current_user["restaurant_id"])
    if not wh:
        return jsonify(ok=False, error="No webhook configured")
    _deliver(wh, "test", {
        "message": "This is a test webhook from Cavnar AI",
        "restaurant_id": current_user["restaurant_id"],
    })
    return jsonify(ok=True)


@client_bp.route("/api/switch-location", methods=["POST"])
@login_required
def switch_location(current_user):
    if current_user.get("role") != "owner":
        return jsonify(ok=False, error="Not an owner account"), 403
    data = request.get_json()
    target_id = int(data.get("restaurant_id", 0))
    if not target_id:
        return jsonify(ok=False, error="Missing restaurant_id"), 400
    # Validate target is in same group as base restaurant
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return jsonify(ok=False, error="No location group configured"), 400
    group = get_location_group(base.location_group)
    valid_ids = [r["id"] for r in group]
    if target_id not in valid_ids:
        return jsonify(ok=False, error="Location not in your group"), 403
    from auth import switch_active_restaurant
    token = request.cookies.get("session_token")
    switch_active_restaurant(token, target_id)
    target = get_restaurant(target_id)
    return jsonify(ok=True, restaurant_name=target.name, restaurant_id=target_id)


@client_bp.route("/api/group-locations")
@login_required
def group_locations(current_user):
    if current_user.get("role") != "owner":
        return jsonify(ok=False, locations=[])
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return jsonify(ok=True, locations=[])
    group = get_location_group(base.location_group)
    active_id = current_user["restaurant_id"]
    locs = [{"id": r["id"], "name": r.get("location_name") or r["name"],
              "active": r["id"] == active_id} for r in group]
    return jsonify(ok=True, locations=locs, group_name=base.location_group)


@client_bp.route("/api/notifications")
@login_required
def get_notifications(current_user):
    rid = current_user["restaurant_id"]
    LABELS = {
        "alert_1star":          "1★ review received",
        "alert_2star":          "2★ review received",
        "alert_5star":          "5★ review received",
        "alert_health":         "Health/safety mention",
        "alert_neg_spike":      "Negative review spike",
        "alert_negative_trend": "Rating declining trend",
        "alert_no_response":    "Unresponded review (48h)",
        "alert_rating_threshold": "Rating below threshold",
        "alert_labor_over":     "Labor % over target",
    }
    try:
        conn = get_conn()
        rows = conn.execute(
            """SELECT alert_type, fired_at FROM alert_log
               WHERE restaurant_id=?
               ORDER BY fired_at DESC LIMIT 20""",
            (rid,)
        ).fetchall()
        conn.close()
        items = [{"type": r["alert_type"],
                  "label": LABELS.get(r["alert_type"], r["alert_type"]),
                  "fired_at": r["fired_at"]} for r in rows]
        return jsonify(ok=True, notifications=items)
    except Exception as e:
        return jsonify(ok=False, notifications=[])


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Ryan seed (module-level — runs under Gunicorn AND direct python) ─────────


