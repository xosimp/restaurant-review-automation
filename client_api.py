"""
client_api.py — Client-facing API routes and data endpoints
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect, send_file, Response, render_template
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

# ── Shared handler bodies ────────────────────────────────────────────────────
# Plain, Flask-independent helpers behind the web (client_bp) routes below.
# Each returns (payload_dict, status_code) so both the web view (jsonify(**p),
# status) and mobile_api.py's mobile views can call the exact same logic
# without duplicating it.

def _do_approve(rid, restaurant_id):
    # Determine response action before approving
    try:
        _ac = get_conn()
        _row = _ac.execute(
            "SELECT regenerate_count, draft_edited FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, restaurant_id)
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
            _ac2.execute("UPDATE reviews SET response_action=? WHERE id=? AND restaurant_id=?", (_action, rid, restaurant_id))
            _ac2.commit(); _ac2.close()
    except Exception as _ae:
        print(f"[approve] response_action error: {_ae}")
    approve_response(rid, restaurant_id=restaurant_id)
    try:
        from models import log_event
        log_event(restaurant_id, "review_approved", {"review_id": rid})
    except Exception:
        pass
    try:
        from webhooks import fire_webhook as _fw
        _fw(restaurant_id, "response.approved", {"review_id": rid})
    except Exception:
        pass
    # Auto-post to Google in background thread — don't block the response
    try:
        from gmb import is_connected
        conn = get_conn()
        row = conn.execute(
            "SELECT platform, draft_response, review_name FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, restaurant_id)
        ).fetchone()
        conn.close()
        if row and row["platform"] == "google" and row["review_name"] and row["draft_response"]:
            if is_connected(restaurant_id):
                import threading as _t_gmb
                _rid_capture = rid
                _rest_id_capture = restaurant_id
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
                return {"ok": True, "auto_posted": True}, 200
    except Exception as e:
        print(f"[GMB] approve auto-post error: {e}")
    return {"ok": True, "auto_posted": False}, 200


def _do_skip(rid, restaurant_id):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=? AND restaurant_id=?",
                 (rid, restaurant_id))
    conn.commit(); conn.close()
    return {"ok": True}, 200


def _do_undo(rid, restaurant_id):
    """Reverts a skip, or an approval that never actually got auto-posted,
    back to the actionable 'drafted' state — neither has any external
    footprint, so there's nothing to unwind besides our own status column.
    A 'posted' review has to go through _do_retract instead, since that one
    has a real, live side effect on Google that a plain status flip can't
    touch."""
    conn = get_conn()
    row = conn.execute(
        "SELECT response_status FROM reviews WHERE id=? AND restaurant_id=?",
        (rid, restaurant_id)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "Review not found"}, 404
    if row["response_status"] not in ("skipped", "approved"):
        return {"ok": False, "error": "Only a skipped review, or an approved review that hasn't posted, can be undone this way."}, 400
    from models import revert_to_drafted, log_event
    revert_to_drafted(rid, restaurant_id)
    try:
        log_event(restaurant_id, "review_undo", {"review_id": rid})
    except Exception:
        pass
    return {"ok": True}, 200


def _do_retract(rid, restaurant_id):
    """Undoes an auto-posted approval by actually deleting the live reply
    from Google via the Business Profile API, then reverting our own status
    back to 'drafted' once that succeeds — a genuine retraction, not a
    cosmetic status change, since the reply was really visible to the
    public until this ran."""
    conn = get_conn()
    row = conn.execute(
        "SELECT platform, response_status, review_name FROM reviews WHERE id=? AND restaurant_id=?",
        (rid, restaurant_id)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "Review not found"}, 404
    if row["response_status"] != "posted":
        return {"ok": False, "error": "This review hasn't been posted, so there's nothing to retract."}, 400
    if row["platform"] != "google" or not row["review_name"]:
        return {"ok": False, "error": "Retracting is only supported for auto-posted Google replies."}, 400

    from gmb import delete_reply
    result = delete_reply(restaurant_id, row["review_name"])
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}, 502

    from models import revert_to_drafted, log_event
    revert_to_drafted(rid, restaurant_id)
    try:
        log_event(restaurant_id, "review_retracted", {"review_id": rid})
    except Exception:
        pass
    try:
        from webhooks import fire_webhook as _fw
        _fw(restaurant_id, "response.retracted", {"review_id": rid})
    except Exception:
        pass
    return {"ok": True}, 200


@client_bp.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid, current_user):
    payload, status = _do_approve(rid, current_user["restaurant_id"])
    return jsonify(**payload), status

def _do_delete_review(rid, restaurant_id):
    conn = get_conn()
    conn.execute(
        "UPDATE reviews SET deleted_at=datetime('now') WHERE id=? AND restaurant_id=?",
        (rid, restaurant_id)
    )
    conn.commit(); conn.close()
    return {"ok": True}, 200


@client_bp.route("/api/reviews/<int:rid>/delete", methods=["POST"])
@login_required
def delete_review(rid, current_user):
    payload, status = _do_delete_review(rid, current_user["restaurant_id"])
    return jsonify(**payload), status

@client_bp.route("/skip/<int:rid>", methods=["POST"])
@login_required
def skip(rid, current_user):
    payload, status = _do_skip(rid, current_user["restaurant_id"])
    return jsonify(**payload), status


@client_bp.route("/undo/<int:rid>", methods=["POST"])
@login_required
def undo(rid, current_user):
    payload, status = _do_undo(rid, current_user["restaurant_id"])
    return jsonify(**payload), status


@client_bp.route("/retract/<int:rid>", methods=["POST"])
@login_required
def retract(rid, current_user):
    payload, status = _do_retract(rid, current_user["restaurant_id"])
    return jsonify(**payload), status

def parse_insight_sections(text):
    """Splits free-form AI consultant prose into (intro, recommendations,
    forecast) — the one place this parsing happens, shared by
    format_insight_html() (web, renders as HTML) and the mobile insight
    routes (return the same three fields as JSON for native rendering)."""
    import re as _re
    if not text:
        return "Analysis unavailable.", [], None

    # Pull out a trailing "FORECAST: ..." line before any other parsing, so
    # it's identified regardless of which branch below handles the rest.
    forecast = None
    fmatch = _re.search(r'(?im)^\s*forecast:\s*(.+)$', text)
    if fmatch:
        forecast = fmatch.group(1).strip() or None
        text = (text[:fmatch.start()] + text[fmatch.end():]).strip()

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
            # No structured recommendations found — hand back the whole
            # (forecast-stripped) text untouched, preserving original line
            # breaks for callers that care (the web's pre-wrap rendering).
            return text, [], forecast
        intro = ' '.join(para_lines).strip()
        recs = rec_lines

    clean_recs = []
    for rec in recs:
        clean = _re.sub(r'^[\d.\-)]+\s*', '', rec).strip()
        if clean:
            clean_recs.append(clean)
    return intro, clean_recs, forecast


def format_insight_html(text):
    if not text:
        return 'Analysis unavailable.'
    intro, recs, forecast = parse_insight_sections(text)

    forecast_html = ''
    if forecast:
        forecast_html = (
            '<div style="margin-top:10px;padding:10px 12px;background:rgba(200,75,47,.08);'
            'border-left:2px solid var(--ember);border-radius:0 6px 6px 0">'
            '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;'
            'color:var(--ember);margin-bottom:4px">\U0001f52e Forecast</div>'
            '<div style="font-style:italic;line-height:1.6">' + forecast + '</div></div>'
        )

    if not recs:
        return '<p style="margin:0;line-height:1.7">' + intro + '</p>' + forecast_html

    html = ''
    if intro:
        html += '<p style="margin:0 0 10px 0;line-height:1.7">' + intro + '</p>'
    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin-bottom:8px">Recommendations</div>'
    num = 1
    for clean in recs:
        html += ('<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
            '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">'
            + str(num) +
            '</span><span style="line-height:1.6;color:#b7791f;font-weight:500">' + clean + '</span></div>')
        num += 1
    return html + forecast_html

def _do_review_stats(restaurant_id):
    from models import get_review_stats as _grs
    try:
        stats = _grs(restaurant_id)
        return stats, 200
    except Exception as e:
        return {"error": str(e)}, 500


@client_bp.route("/api/review-stats")
@login_required
def review_stats_api(current_user):
    payload, status = _do_review_stats(current_user["restaurant_id"])
    return jsonify(**payload), status

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

@client_bp.route("/api/theme", methods=["POST"])
@login_required
def save_theme_api(current_user):
    from models import update_restaurant
    data = request.get_json() or {}
    theme = data.get("theme")
    if theme not in ("dark", "light"):
        return jsonify(ok=False, error="invalid theme"), 400
    update_restaurant(current_user["restaurant_id"], {"email_theme": theme})
    return jsonify(ok=True)

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
    increment_template_use(tid, restaurant_id=current_user["restaurant_id"])
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
    insight, status = _do_review_insight(current_user["restaurant_id"])
    return jsonify(**insight), status


def _do_review_insight(rid):
    """Shared by the web route above and mobile_api.py's own /reviews/insight —
    one AI-prompt implementation behind both surfaces, same cache."""
    cached = _cache_get("review-insight:" + str(rid))
    if cached:
        return {"insight": cached}, 200
    try:
        import os, json, anthropic as _anth
        from models import get_restaurant, get_review_stats, get_top_issues
        from zoneinfo import ZoneInfo as _ZI_ri
        from datetime import datetime as _dt_ri, timedelta as _td_ri
        _client_ri = _anth.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        restaurant = get_restaurant(rid)
        rstats = get_review_stats(rid)
        top_issues = get_top_issues(rid, days=90, limit=5)
        from time_utils import restaurant_now
        now_chi = restaurant_now(restaurant)
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
        has_trend = bool(trend_str)
        format_count = "4" if has_trend else "3"
        forecast_line = (
            "\n\U0001f52e Next week: [1 sentence predicting where the rating/negative-review "
            "trend is headed if it continues, based on the 4-week trend above.]"
        ) if has_trend else ""
        prompt = (
            f"You are a restaurant reputation assistant. Output ONLY a {format_count}-line snapshot.\n\n"
            f"Restaurant: {rest_name} | Today: {today_str}\n"
            f"Data: {rstats['total']} reviews | {rstats['avg_rating']}★ avg | "
            f"{rstats['positive']} pos / {rstats['negative']} neg / {rstats['neutral']} neutral | "
            f"{rstats['urgent']} urgent | response rate {rstats['response_rate']}%\n"
            f"Top topics: {issues_str} | {wow_str}\n"
            f"{trend_block}"
            f"Urgent excerpts: {urgent_texts}\n\n"
            f"Return EXACTLY this format — {format_count} lines:\n"
            "\U0001f4ca This week: [1 punchy sentence on the most important number or multi-week trend. Be specific.]\n"
            "\u26a0\ufe0f Watch: [1 sentence on the biggest risk — multi-week declining trend, recurring complaint, rising negative %, or urgent review. Skip if nothing notable.]\n"
            "\u2705 Do today: [1 concrete action — e.g. 'Respond to Amanda L.s 1-star review about cold food.' Never generic.]"
            f"{forecast_line}\n\n"
            "Rules: no markdown, no extra lines, no preamble. Each line max 20 words. Never invent data. Prioritize multi-week trends over single-week blips."
        )

        from ai_utils import create_with_retry, extract_text
        msg = create_with_retry(
            _client_ri,
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=260,
            temperature=0.2,
            messages=[{"role":"user","content":prompt}],
            restaurant_id=rid,
            action="review_insight",
        )
        insight = extract_text(msg).strip()
        # Strip any markdown
        import re as _re_ri
        insight = _re_ri.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), insight)
        insight = _re_ri.sub(r'\*(.+?)\*',   lambda m: m.group(1), insight)
        _cache_set("review-insight:" + str(rid), insight)
        return {"insight": insight}, 200
    except Exception as _re:
        import traceback
        print(f"[review-insight ERROR] {_re}\n{traceback.format_exc()}")
        stale = _insight_cache.get("review-insight:" + str(rid))
        if stale:
            return {"insight": stale[1]}, 200
        return {"insight": "Analysis unavailable — check back shortly.", "error": str(_re)}, 500

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
        return jsonify(ok=False, error=str(e))

@client_bp.route("/api/mkt-performance")
@login_required
def mkt_performance_api(current_user):
    """Aggregate real Meta post performance for the Marketing tab's analytics
    card: total reach/engagement across published posts and the single
    best-performing post. Reads the same reach/impressions/likes/comments/
    shares columns refresh_post_metrics() (social_routes.py) keeps updated —
    this endpoint never calls Meta itself, it just summarizes what's already
    in the DB, so it stays fast even if Meta is slow or down."""
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        for col in ("reach", "impressions", "engaged", "likes", "comments", "shares"):
            try:
                conn.execute(f"ALTER TABLE marketing_content_log ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        conn.commit()

        published = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL",
            (rid,)
        ).fetchone()[0] or 0

        totals = conn.execute("""
            SELECT COALESCE(SUM(reach),0) as reach, COALESCE(SUM(impressions),0) as impressions,
                   COALESCE(SUM(likes),0) as likes, COALESCE(SUM(comments),0) as comments,
                   COALESCE(SUM(shares),0) as shares
            FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL
        """, (rid,)).fetchone()

        rows = conn.execute("""
            SELECT topic, post_platform, reach, impressions, likes, comments, shares
            FROM marketing_content_log
            WHERE restaurant_id=? AND post_id IS NOT NULL
              AND (reach > 0 OR impressions > 0 OR likes > 0 OR comments > 0)
        """, (rid,)).fetchall()
        conn.close()

        top_post = None
        if rows:
            best = max(rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0))
            top_post = {
                "topic": best["topic"], "platform": best["post_platform"],
                "reach": best["reach"] or 0, "likes": best["likes"] or 0,
                "comments": best["comments"] or 0, "shares": best["shares"] or 0,
            }

        total_engagement = (totals["likes"] or 0) + (totals["comments"] or 0) + (totals["shares"] or 0)
        return jsonify(
            ok=True,
            published=published,
            has_data=bool(rows),
            total_reach=(totals["reach"] or 0) + (totals["impressions"] or 0),
            total_engagement=total_engagement,
            top_post=top_post,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e))

def _do_ask_cavnar(restaurant_id, question, history=None):
    """The AI copilot's shared body — answers a plain-English question about
    the restaurant's own live data (reviews/labor/food cost/marketing,
    whichever modules are active) instead of the owner having to piece it
    together across tabs. Used by both the web panel and the mobile app.

    `history` is the caller's own prior message list for this chat session
    (list of {"role", "content"} dicts, oldest first, NOT including
    `question`) — ask_cavnar.ask() sanitizes/caps it itself, so this is a
    thin passthrough, not a second place that needs to re-validate it."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Ask a question first."}, 400
    if len(question) > 500:
        return {"ok": False, "error": "That question is too long — try to keep it under 500 characters."}, 400
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"askcavnar:{restaurant_id}", max_calls=10, window_secs=60):
        return {"ok": False, "error": "Too many questions — please wait a moment and try again."}, 429
    try:
        from ask_cavnar import ask as _ask_cavnar
        restaurant = get_restaurant(restaurant_id)
        if not restaurant:
            return {"ok": False, "error": "Restaurant not found"}, 404
        answer = _ask_cavnar(restaurant, question, history=history)
        return {"ok": True, "answer": answer}, 200
    except Exception as e:
        import ops
        ops.capture(e, job="ask_cavnar", context=f"restaurant_id={restaurant_id}")
        return {"ok": False, "error": "Couldn't get an answer right now — try again in a moment."}, 500


@client_bp.route("/api/ask-cavnar", methods=["POST"])
@login_required
def ask_cavnar_api(current_user):
    rid = current_user["restaurant_id"]
    data = request.get_json() or {}
    payload, status = _do_ask_cavnar(rid, data.get("question"), history=data.get("history"))
    return jsonify(**payload), status

@client_bp.route("/api/mkt-insight")
@login_required
def mkt_insight_api(current_user):
    insight, status = _do_mkt_insight(current_user["restaurant_id"])
    return jsonify(**insight), status


def _do_mkt_insight(rid, raw=False):
    """Shared by the web route above and mobile_api.py. raw=True skips
    format_insight_html() and the web cache key, for a client that renders
    its own plain-text layout instead of parsing HTML."""
    cache_key = ("mobile-" if raw else "") + "mkt-insight:" + str(rid)
    cached = _cache_get(cache_key)
    if cached:
        return {"insight": cached}, 200
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
        from time_utils import restaurant_now
        now = restaurant_now(restaurant)
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
        _trend_lines = []
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
        has_trend = bool(_trend_lines)
        forecast_instruction = (
            '\n\nAfter the two paragraphs, add one final line starting with exactly "FORECAST:" '
            "— one sentence predicting next week's reach/engagement trajectory based on the trend "
            "above. Only include this if the trend is genuinely supported by the data given."
        ) if has_trend else ""
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

Tone: warm, direct, like a trusted advisor. Match the brand voice exactly. No corporate language. Under 120 words total. If multiple holidays are coming up, mention both briefly.{forecast_instruction}"""
        import anthropic as _anth
        from ai_utils import create_with_retry, extract_text
        _client = _anth.Anthropic(api_key=__import__("os").getenv("ANTHROPIC_API_KEY"))
        msg = create_with_retry(
            _client,
            model=__import__("os").getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=rid,
            action="marketing_insight",
        )
        insight = extract_text(msg).strip()
        result = insight if raw else format_insight_html(insight)
        _cache_set(cache_key, result)
        return {"insight": result}, 200
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[MktInsight] ERROR: {str(e)}")
        stale = _insight_cache.get(cache_key)
        if stale:
            return {"insight": stale[1]}, 200
        return {"insight": "Marketing brief unavailable — check back shortly."}, 500

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
        from marketing import get_upcoming_holidays
        restaurant = get_restaurant(current_user["restaurant_id"])
        items, _is_live = load_inventory_for_restaurant(current_user["restaurant_id"])
        analysis = analyse_inventory(
            items,
            delivery_days=restaurant.delivery_days if restaurant else None,
            upcoming_holidays=get_upcoming_holidays(),
        )
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
    from ai_utils import ai_rate_limited
    rid = current_user["restaurant_id"] if current_user else None
    if rid and ai_rate_limited(f"gencontent:{rid}", max_calls=8, window_secs=60):
        return jsonify(content="", error="Too many requests — please wait a moment and try again.")
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

def _do_regenerate_draft(review_id, restaurant_id):
    """Regenerate AI draft for a review — delegates to drafter.draft_response()
    so a regenerated draft gets the same quality/model/urgency-escalation as
    the original draft (this used to be a separate, drifted reimplementation)."""
    from models import get_conn, get_approved_examples
    from drafter import draft_response
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"regen:{restaurant_id}", max_calls=10, window_secs=60):
        return {"ok": False, "error": "Too many regenerations — please wait a moment and try again."}, 200
    conn = get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, restaurant_id)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "Review not found"}, 200
    r = dict(row)
    restaurant = get_restaurant(restaurant_id)
    try:
        examples = get_approved_examples(restaurant_id, limit=4)
        new_draft = draft_response(
            review_id, r.get("rating", 3), r["text"], r.get("sentiment", "neutral"),
            restaurant.name,
            voice_notes=restaurant.voice_notes or "",
            restaurant_id=restaurant_id,
            approved_examples=examples,
            sign_off=restaurant.sign_off_name or restaurant.name,
            never_say=restaurant.never_say or "",
            urgency=r.get("urgency", "normal"),
        )
        conn = get_conn()
        conn.execute(
            "UPDATE reviews SET response_status='drafted', regenerate_count=COALESCE(regenerate_count,0)+1 WHERE id=? AND restaurant_id=?",
            (review_id, restaurant_id)
        )
        conn.commit(); conn.close()
        return {"ok": True, "draft": new_draft}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 200


def _do_save_draft(review_id, restaurant_id, draft_text):
    from models import update_draft
    draft = (draft_text or "").strip()
    if not draft:
        return {"ok": False, "error": "Draft cannot be empty"}, 200
    conn = get_conn()
    row = conn.execute("SELECT id FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, restaurant_id)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "Review not found"}, 200
    update_draft(review_id, draft)
    conn = get_conn()
    conn.execute(
        "UPDATE reviews SET response_status='drafted', draft_edited=1 WHERE id=? AND restaurant_id=?",
        (review_id, restaurant_id)
    )
    conn.commit(); conn.close()
    return {"ok": True}, 200


@client_bp.route("/api/regenerate-draft/<int:review_id>", methods=["POST"])
@login_required
def regenerate_draft(review_id, current_user):
    payload, status = _do_regenerate_draft(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status

@client_bp.route("/api/save-draft/<int:review_id>", methods=["POST"])
@login_required
def save_draft(review_id, current_user):
    data = request.get_json()
    payload, status = _do_save_draft(review_id, current_user["restaurant_id"], (data or {}).get("draft", ""))
    return jsonify(**payload), status

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

    # Compute next week dates — the restaurant's week, not the server's
    from time_utils import restaurant_now
    today = restaurant_now(restaurant, naive=True)
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

    # Weather forecast for the schedule week — never blocks generation if
    # geocoding/NWS is unavailable (see weather.get_forecast_for_week).
    try:
        from weather import get_forecast_for_week
        weather_forecast = get_forecast_for_week(restaurant, next_week_dates)
    except Exception:
        weather_forecast = []

    # The actual most recent prior generation (not just historical shift
    # data) — see _summarize_schedule_csv_by_day_role's own docstring for
    # why this gives the AI something real to compare against for its
    # summary. None if this is the restaurant's first-ever generation.
    prior_schedule_summary = None
    try:
        from models import get_schedule_history, get_schedule_history_detail
        _prior_entries = get_schedule_history(restaurant_id, limit=1)
        if _prior_entries:
            _prior_detail = get_schedule_history_detail(_prior_entries[0]["id"], restaurant_id)
            if _prior_detail and _prior_detail.get("schedule_csv"):
                prior_schedule_summary = _summarize_schedule_csv_by_day_role(_prior_detail["schedule_csv"])
    except Exception:
        prior_schedule_summary = None

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
        tz_name=getattr(restaurant, 'timezone', None),
        restaurant_id=restaurant_id,
        weather_forecast=weather_forecast or None,
        prior_schedule_summary=prior_schedule_summary or None,
    )
    result["restaurant_name"] = restaurant.name if restaurant else "Restaurant"
    return result


_schedule_jobs = {}  # job_id -> {"status": "pending"|"done"|"error", "result": ...}

# Matches a clock time like "8:00am"/"3:00 pm" — a real role name (Server,
# Prep Cook, Carry Out, ...) never looks like this, which is what makes it a
# reliable signal that a row's `day` field was dropped and every field after
# `date` shifted one position left. See _run_schedule_job's row-repair logic.
_TIME_FIELD_RE = re.compile(r'^\d{1,2}:\d{2}\s*(am|pm)$', re.IGNORECASE)
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _parse_time_to_minutes(t: str):
    """Parses a "9:00pm"/"9:30 am" style string (the exact format the AI is
    instructed to always produce) to minutes-since-midnight, or None if it
    doesn't match that format at all."""
    if not t or not _TIME_FIELD_RE.match(t.strip()):
        return None
    t_clean = t.strip().lower().replace(" ", "")
    period, hm = t_clean[-2:], t_clean[:-2]
    try:
        hour_str, minute_str = hm.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, IndexError):
        return None
    if period == "pm" and hour != 12:
        hour += 12
    if period == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _format_minutes_to_time(total_minutes: int) -> str:
    """Inverse of _parse_time_to_minutes — "9:00pm" style."""
    total_minutes %= 24 * 60
    hour24, minute = divmod(total_minutes, 60)
    period = "am" if hour24 < 12 else "pm"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d}{period}"


def _summarize_schedule_csv_by_day_role(csv_text: str) -> dict:
    """Compact day -> {role: {"count": N, "hours": H}} rollup of a past
    generation's schedule_csv — enough detail (headcount and hours per
    role per day) for the AI to describe a genuine week-over-week
    difference in its next "what changed and why" summary, without
    feeding it the full 100-200 row CSV again. Used to give
    generate_optimized_schedule something concrete to compare against —
    previously the summary only ever compared against historical shift
    patterns (TYPICAL HEADCOUNT), never the restaurant's own actual prior
    generated schedule, which is what "what changed" should mean to an
    owner reading it week to week.
    """
    import csv as _csv_mod, io as _io_mod
    summary: dict = {}
    try:
        for row in _csv_mod.DictReader(_io_mod.StringIO(csv_text or "")):
            day = row.get("day", "")
            role = row.get("role", "")
            if not day or not role:
                continue
            try:
                hrs = float(row.get("scheduled_hours") or 0)
            except (ValueError, TypeError):
                hrs = 0.0
            bucket = summary.setdefault(day, {}).setdefault(role, {"count": 0, "hours": 0.0})
            bucket["count"] += 1
            bucket["hours"] += hrs
    except Exception:
        return {}
    return summary


def _safe_hours_sum(rows: list) -> float:
    """Total scheduled_hours across rows, skipping any row whose value
    isn't actually numeric instead of raising — a live generation
    occasionally has one malformed row out of 100+ (see
    test_schedule_row_repair.py) where scheduled_hours ends up holding a
    stray time string from a column shift. The original per-row parsing
    loop already tolerates this per-row; the backstop passes' own
    hours_scheduled recomputes need the same tolerance, since a single bad
    row previously aborted the recompute (and by extension the CSV
    rebuild) via an uncaught ValueError from float() inside sum()."""
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
    return round(total, 1)


def _enforce_close_time(row: dict, real_day: str, close_times: dict, role_buffers: dict) -> None:
    """Hard-caps a row's shift_end at that day's actual close time (plus any
    role-specific after-close allowance, e.g. a bartender's stated "stay 1h
    after close") — mutates `row` in place. A no-op for any restaurant that
    hasn't configured close_times_json, so this only ever applies where an
    owner has actually set real hours.

    Telling the model "the close time is 9pm, never schedule past it" in
    prose plateaued below 100% compliance on live testing (captured
    servers scheduled to 9:30-10pm on a night that closes at 9pm, likely
    from over-generalizing a "staff this day like a busy Friday" volume
    note to Friday's later close time too) — this is the deterministic
    backstop for a rule that must never be violated, not one more request
    the model can occasionally miss.
    """
    if not real_day or real_day not in close_times:
        return
    close_minutes = _parse_time_to_minutes(close_times[real_day])
    if close_minutes is None:
        return
    role = (row.get("role") or "").strip()
    ceiling = close_minutes + role_buffers.get(role, 0)

    end_minutes = _parse_time_to_minutes(row.get("shift_end", ""))
    if end_minutes is None or end_minutes <= ceiling:
        return

    start_minutes = _parse_time_to_minutes(row.get("shift_start", ""))
    if start_minutes is None or start_minutes >= ceiling:
        # Can't produce a sensible corrected shift (e.g. shift_start is
        # itself already past the ceiling) — don't fabricate a number,
        # flag it for a human instead.
        row["needs_review"] = True
        return

    row["shift_end"] = _format_minutes_to_time(ceiling)
    row["scheduled_hours"] = str(round((ceiling - start_minutes) / 60, 1))
    note = (row.get("notes") or "").strip()
    row["notes"] = f"{note} (auto-capped to close time)" if note else "auto-capped to close time"


def _row_fields_look_sane(row: dict) -> bool:
    """True when everything but `day` already looks individually
    well-formed — confirmed against two more live-captured failure shapes
    beyond the day-omitted-shift one: the model sometimes just misspells
    the day word itself ("Thursson" instead of "Thursday"), or duplicates
    the employee name into the day slot instead of writing a weekday
    ("Piper A.,Piper A.,Runner,..."). In both, employee/role/times/hours
    were all already in the right place — only the day label itself was
    wrong, which `date` already fixes. Flagging those for human review is
    just noise; this reserves the flag for rows where something beyond the
    day label can't be confirmed sane.
    """
    emp = (row.get("employee") or "").strip()
    role = (row.get("role") or "").strip()
    start = (row.get("shift_start") or "").strip()
    end = (row.get("shift_end") or "").strip()
    hours = (row.get("scheduled_hours") or "").strip()
    if not emp or emp in _WEEKDAYS or _TIME_FIELD_RE.match(emp):
        return False
    if not role or role in _WEEKDAYS or _TIME_FIELD_RE.match(role):
        return False
    if not _TIME_FIELD_RE.match(start) or not _TIME_FIELD_RE.match(end):
        return False
    try:
        float(hours)
    except (ValueError, TypeError):
        return False
    return True

def _top_up_hours_gap(preview_rows: list, daily_target_hours: dict, hours_budget: float,
                       hours_scheduled: float, restaurant_id: int,
                       close_times: dict, role_buffers: dict) -> tuple:
    """Deterministic post-generation pass that adds real shifts on the days
    furthest under their own per-day target when the AI's output lands well
    under the week's PAR hours budget.

    Prompt-only fixes for this (see par_block/_daily_targets in labor.py)
    plateaued around -240h under a ~1314h budget on live testing — asking
    the model to hit a number more insistently in prose has a ceiling. This
    is the same deterministic-backstop pattern as _enforce_close_time,
    applied to headcount instead of shift end times.

    Never invents an employee: only adds a shift for someone who already
    has other shifts this week in that exact role, on a day they aren't
    already working and haven't marked unavailable, picking whoever has
    the fewest hours so far to spread the addition fairly. Uses an existing
    same-day/role shift as a time template when one exists, else that
    role's typical time block anywhere in the week. Every added row is
    tagged in its notes so it's visible, never silent.

    Returns (preview_rows, hours_added, added_dates) where added_dates is
    {date: shifts_added_count}.
    """
    total_gap = hours_budget - hours_scheduled
    # Don't force an already-close result — only step in for a real miss.
    if not daily_target_hours or total_gap < max(20, hours_budget * 0.05):
        return preview_rows, 0.0, {}

    import datetime as _dt_topup
    import json as _json_avail
    from models import get_staff_availability

    _avail_rows = get_staff_availability(restaurant_id) or []
    unavailable_by_emp: dict = {}
    available_by_emp: dict = {}
    # staff_availability only has whole-day granularity in its structured
    # fields — a client who writes "only mornings" or "no closes" has to
    # put it in the freeform notes field instead, which the AI prompt does
    # read (see labor.py's _avail_block) but this deterministic code has
    # no reliable way to parse. Rather than risk scheduling a body into a
    # time window their notes explicitly rule out, exclude anyone with any
    # notes at all from automatic day-level additions -- conservative, but
    # a missed top-up is a far smaller problem than deterministically
    # violating a constraint a human specifically wrote down.
    notes_restricted: set = set()
    for a in _avail_rows:
        name = a.get("employee_name")
        if not name:
            continue
        try:
            unavailable_by_emp[name] = set(_json_avail.loads(a.get("unavailable_days") or "[]"))
        except Exception:
            unavailable_by_emp[name] = set()
        try:
            avail = _json_avail.loads(a.get("available_days") or "[]")
            if avail:
                available_by_emp[name] = set(avail)
        except Exception:
            pass
        if (a.get("notes") or "").strip():
            notes_restricted.add(name)

    # Roster + time templates from the AI's own output this week
    role_employees: dict = {}
    role_time_template: dict = {}
    date_role_template: dict = {}
    hours_by_employee: dict = {}
    working_on_date: dict = {}
    role_headcount_by_date: dict = {}
    by_date_hours: dict = {}

    for r in preview_rows:
        date, emp, role = r.get("date", ""), r.get("employee", ""), r.get("role", "")
        if not (date and emp and role):
            continue
        role_employees.setdefault(role, set()).add(emp)
        working_on_date.setdefault(date, set()).add(emp)
        try:
            hrs = float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            hrs = 0.0
        hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + hrs
        by_date_hours[date] = by_date_hours.get(date, 0.0) + hrs
        role_headcount_by_date[(date, role)] = role_headcount_by_date.get((date, role), 0) + 1
        if r.get("shift_start") and r.get("shift_end"):
            date_role_template.setdefault((date, role), (r["shift_start"], r["shift_end"]))
            role_time_template.setdefault(role, (r["shift_start"], r["shift_end"]))

    # Average headcount per role across the days it appears — used to spot
    # a thin day for a role that's normally better staffed, the same lens
    # the PAR prompt instruction already asks the model to apply, just
    # applied mechanically here instead of trusted to prose compliance.
    role_day_counts: dict = {}
    for (d, role), n in role_headcount_by_date.items():
        role_day_counts.setdefault(role, []).append(n)
    role_avg_headcount = {role: sum(v) / len(v) for role, v in role_day_counts.items() if v}

    daily_target_hours = dict(daily_target_hours)  # local copy — we mutate to drop exhausted dates
    hours_added = 0.0
    added_dates: dict = {}
    remaining_gap = total_gap
    MAX_ADDS = 100  # hard safety ceiling regardless of gap size

    for _pass in range(MAX_ADDS):
        if remaining_gap <= 0:
            break
        target_date = max(
            (d for d in daily_target_hours if daily_target_hours[d] - by_date_hours.get(d, 0.0) > 0),
            key=lambda d: daily_target_hours[d] - by_date_hours.get(d, 0.0),
            default=None,
        )
        if not target_date:
            break

        try:
            day_name = _dt_topup.datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        # Pick the role furthest below its own weekly-average headcount for
        # this day, among roles that actually have an available candidate.
        candidates_role = None
        best_shortfall = 0.0
        for role, avg_hc in role_avg_headcount.items():
            today_hc = role_headcount_by_date.get((target_date, role), 0)
            shortfall = avg_hc - today_hc
            if shortfall > best_shortfall:
                pool = role_employees.get(role, set()) - working_on_date.get(target_date, set())
                pool = {e for e in pool
                        if day_name not in unavailable_by_emp.get(e, set())
                        and (e not in available_by_emp or day_name in available_by_emp[e])
                        and e not in notes_restricted}
                if pool:
                    best_shortfall = shortfall
                    candidates_role = (role, pool)

        if not candidates_role:
            # No role on this date has a real, available candidate — can't
            # responsibly add here. Drop the date and move to the
            # next-neediest one instead of forcing a bad pick.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        role, pool = candidates_role
        employee = min(pool, key=lambda e: hours_by_employee.get(e, 0.0))
        start, end = date_role_template.get((target_date, role)) or role_time_template.get(role, ("11:00am", "5:00pm"))

        new_row = {
            "date": target_date, "day": day_name, "employee": employee, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": "0",
            "notes": "added — PAR hours top-up",
        }
        _enforce_close_time(new_row, day_name, close_times, role_buffers)
        if new_row.get("needs_review"):
            # Template (borrowed from another day) doesn't produce a sane
            # shift once capped to this day's close time — skip rather
            # than fabricate a number, same discipline _enforce_close_time
            # itself follows.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        s_min, e_min = _parse_time_to_minutes(new_row["shift_start"]), _parse_time_to_minutes(new_row["shift_end"])
        if s_min is None or e_min is None or e_min <= s_min:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        new_row["scheduled_hours"] = str(round((e_min - s_min) / 60, 1))
        hrs = float(new_row["scheduled_hours"])
        if hrs <= 0:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        preview_rows.append(new_row)
        hours_added += hrs
        remaining_gap -= hrs
        added_dates[target_date] = added_dates.get(target_date, 0) + 1

        by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + hrs
        working_on_date.setdefault(target_date, set()).add(employee)
        hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs
        role_headcount_by_date[(target_date, role)] = role_headcount_by_date.get((target_date, role), 0) + 1

    return preview_rows, round(hours_added, 1), added_dates


def _extend_shifts_to_close_gap(preview_rows: list, daily_target_hours: dict, hours_budget: float,
                                 hours_scheduled: float, restaurant_id: int,
                                 close_times: dict, role_buffers: dict) -> tuple:
    """Second-line deterministic top-up, run after _top_up_hours_gap. That
    pass stops adding to a day once every role on it has run out of a real,
    available, not-already-scheduled candidate — which is correct (it
    should never invent a person), but it means some days still end up
    under target purely because the roster is thin, not because the day
    doesn't need the hours. This closes more of that gap the only way left
    that doesn't compromise on "never invent a person": push an existing
    closer's shift_end a bit later, up to that day's own close-time
    ceiling — the exact same ceiling _enforce_close_time already caps
    shift_end at, just applied as a bounded increase instead of a decrease.

    Only ever extends one of that day's later finishers for its role (a
    closer staying a bit longer is plausible; turning a lunch-only opener
    into a closer isn't), and caps each row's extension at 2h so no single
    shift balloons into something unrealistic. Never invents a new row —
    every hour added here belongs to someone already scheduled that day.

    Returns (preview_rows, hours_added, extended_dates) where
    extended_dates is {date: rows_extended_count}.
    """
    total_gap = hours_budget - hours_scheduled
    if not daily_target_hours or total_gap <= 0:
        return preview_rows, 0.0, {}

    import datetime as _dt_ext
    from models import get_staff_availability as _gsa_ext
    MAX_EXTENSION_MINUTES = 120

    # Same conservative rule as the other passes: staff_availability's
    # structured fields can't express "only mornings"/"no closes" — that
    # only ever lives in freeform notes, which this code can't reliably
    # parse. A shift extension is a smaller violation than scheduling a
    # brand-new one, but still real (pushing someone's shift 2h later
    # could turn a stated "mornings only" into an afternoon), so anyone
    # with any notes at all is left alone here too.
    notes_restricted_ext: set = {
        a.get("employee_name") for a in (_gsa_ext(restaurant_id) or [])
        if a.get("employee_name") and (a.get("notes") or "").strip()
    }

    by_date_hours: dict = {}
    rows_by_date: dict = {}
    for r in preview_rows:
        d = r.get("date")
        if not d:
            continue
        try:
            hrs = float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            hrs = 0.0
        by_date_hours[d] = by_date_hours.get(d, 0.0) + hrs
        rows_by_date.setdefault(d, []).append(r)

    hours_added = 0.0
    extended_dates: dict = {}
    remaining_gap = total_gap

    dates_by_need = sorted(
        (d for d in daily_target_hours if daily_target_hours[d] - by_date_hours.get(d, 0.0) > 0),
        key=lambda d: daily_target_hours[d] - by_date_hours.get(d, 0.0),
        reverse=True,
    )

    for target_date in dates_by_need:
        if remaining_gap <= 0:
            break
        day_gap = daily_target_hours[target_date] - by_date_hours.get(target_date, 0.0)
        if day_gap <= 0:
            continue
        try:
            day_name = _dt_ext.datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue

        # Latest finishers first — the realistic "closer stays a little
        # longer" candidates, not openers or lunch-only shifts.
        day_rows_sorted = sorted(
            rows_by_date.get(target_date, []),
            key=lambda r: _parse_time_to_minutes(r.get("shift_end", "")) or -1,
            reverse=True,
        )

        for row in day_rows_sorted:
            if day_gap <= 0 or remaining_gap <= 0:
                break
            if row.get("employee") in notes_restricted_ext:
                continue
            end_min = _parse_time_to_minutes(row.get("shift_end", ""))
            start_min = _parse_time_to_minutes(row.get("shift_start", ""))
            if end_min is None or start_min is None or end_min <= start_min:
                continue

            extend_by = min(MAX_EXTENSION_MINUTES, int(day_gap * 60))
            if extend_by <= 0:
                continue
            original_end = row["shift_end"]
            row["shift_end"] = _format_minutes_to_time(end_min + extend_by)
            _enforce_close_time(row, day_name, close_times, role_buffers)  # clamps back down if past close
            new_end_min = _parse_time_to_minutes(row["shift_end"])
            if new_end_min is None or new_end_min <= end_min:
                row["shift_end"] = original_end  # at/past the close-time ceiling already — nothing gained
                continue

            added_hours = round((new_end_min - end_min) / 60, 1)
            if added_hours <= 0:
                row["shift_end"] = original_end
                continue
            row["scheduled_hours"] = str(round((new_end_min - start_min) / 60, 1))
            note = (row.get("notes") or "").strip()
            row["notes"] = f"{note} (extended — PAR hours top-up)" if note else "extended — PAR hours top-up"

            hours_added += added_hours
            remaining_gap -= added_hours
            day_gap -= added_hours
            by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + added_hours
            extended_dates[target_date] = extended_dates.get(target_date, 0) + 1

    return preview_rows, round(hours_added, 1), extended_dates


_SERVER_MAX_OVERLAP = 7


def _peak_server_overlap(day_rows: list) -> tuple:
    """Sweep-line peak concurrent Server headcount for one day's rows.
    Returns (peak_count, peak_time_minutes) — peak_time is None if there
    are no rows. Ends are processed before starts at the same instant, so
    a shift ending at 3pm and one starting at 3pm never count as
    overlapping at that exact minute."""
    events = []
    for r in day_rows:
        s = _parse_time_to_minutes(r.get("shift_start", ""))
        e = _parse_time_to_minutes(r.get("shift_end", ""))
        if s is None or e is None or e <= s:
            continue
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda ev: (ev[0], ev[1]))  # -1 (end) before +1 (start) at same minute
    running = 0
    peak = 0
    peak_time = None
    for t, delta in events:
        running += delta
        if running > peak:
            peak = running
            peak_time = t
    return peak, peak_time


def _trim_server_overlap_cap(preview_rows: list, close_times: dict, role_buffers: dict) -> tuple:
    """Deterministic backstop for the "never more than 7 servers at once"
    hard cap already stated in hours_notes. Live testing showed the AI
    missing this reliably at 190-220+ row scale, including via double
    shifts (a server working both morning and night) that were never
    subtracted from the night total before more closers got added on top —
    the exact reported bug (Monday: 6 correct night closers + 2 uncounted
    doubles = 8) plus worse cases found in verification (10 on one night).

    Only ever shortens a row's shift_end (never shift_start) — "cut early
    when overstaffed" is already this restaurant's own stated convention
    for servers (see hours_notes' SHIFT END / CLOSER RULES), not something
    invented here. Priority for which row absorbs the cut, most to least
    preferred: a row this file's own top-up/extension passes added (most
    discretionary), then the later leg of a double shift (the specific
    reported pattern), then whichever active row started latest (a
    "last in, first cut" tiebreak). Removes a row outright if trimming it
    below ~30min would leave a token sliver shift.

    Returns (preview_rows, rows_trimmed, trimmed_dates).
    """
    by_date: dict = {}
    for r in preview_rows:
        if (r.get("role") or "").strip().lower() == "server":
            by_date.setdefault(r.get("date"), []).append(r)

    trimmed_dates: dict = {}
    rows_trimmed = 0

    for date, day_rows in by_date.items():
        try:
            day_name = __import__("datetime").datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day_name = None

        # Which employees are working a double shift today (2+ rows) — the
        # later of their rows (by start time) is the "carryover" leg.
        rows_by_employee: dict = {}
        for r in day_rows:
            emp = r.get("employee")
            if emp:
                rows_by_employee.setdefault(emp, []).append(r)
        double_shift_second_legs = set()
        for emp, rows in rows_by_employee.items():
            if len(rows) > 1:
                rows_sorted = sorted(rows, key=lambda r: _parse_time_to_minutes(r.get("shift_start", "")) or 0)
                for r in rows_sorted[1:]:
                    double_shift_second_legs.add(id(r))

        for _pass in range(20):  # bounded — one trim per pass, per day
            peak, peak_time = _peak_server_overlap(day_rows)
            if peak <= _SERVER_MAX_OVERLAP or peak_time is None:
                break

            active = []
            for r in day_rows:
                s = _parse_time_to_minutes(r.get("shift_start", ""))
                e = _parse_time_to_minutes(r.get("shift_end", ""))
                if s is not None and e is not None and s <= peak_time < e:
                    active.append(r)

            def _priority(r):
                is_topup = "PAR hours top-up" in (r.get("notes") or "")
                is_second_leg = id(r) in double_shift_second_legs
                start = _parse_time_to_minutes(r.get("shift_start", "")) or 0
                return (0 if is_topup else 1, 0 if is_second_leg else 1, -start)

            candidate = min(active, key=_priority)
            start_min = _parse_time_to_minutes(candidate.get("shift_start", ""))
            if start_min is None:
                break
            new_end = peak_time
            if new_end - start_min < 30:
                day_rows.remove(candidate)
                preview_rows.remove(candidate)
            else:
                candidate["shift_end"] = _format_minutes_to_time(new_end)
                if day_name:
                    _enforce_close_time(candidate, day_name, close_times, role_buffers)
                final_end = _parse_time_to_minutes(candidate["shift_end"])
                candidate["scheduled_hours"] = str(round((final_end - start_min) / 60, 1))
                note = (candidate.get("notes") or "").strip()
                candidate["notes"] = f"{note} (trimmed — over the 7-server cap)" if note else "trimmed — over the 7-server cap"

            rows_trimmed += 1
            trimmed_dates[date] = trimmed_dates.get(date, 0) + 1

    return preview_rows, rows_trimmed, trimmed_dates


_PIZZA_MORNING_WINDOW = (_parse_time_to_minutes("11:30am"), _parse_time_to_minutes("1:30pm"))
_PIZZA_NIGHT_WINDOW = (_parse_time_to_minutes("6:30pm"), _parse_time_to_minutes("8:30pm"))
_PIZZA_BUSY_DAYS = {"Monday", "Friday", "Saturday", "Sunday"}  # matches the existing "Pizza Monday and weekends" scale-up already in hours_notes


def _window_overlap(row: dict, window: tuple) -> bool:
    s = _parse_time_to_minutes(row.get("shift_start", ""))
    e = _parse_time_to_minutes(row.get("shift_end", ""))
    return s is not None and e is not None and s < window[1] and e > window[0]


def _ensure_pizza_cook_coverage(preview_rows: list, week_dates: list, week_days: list, restaurant_id: int,
                                 close_times: dict, role_buffers: dict) -> tuple:
    """Deterministic backstop for "always at least 1 Pizza Cook on, morning
    and night, every day -- 2 on busy days" (Monday/weekends, matching the
    existing dinner-service Pizza Cook scale-up already in hours_notes).
    Same prompt-only rule was already in hours_notes and still missed
    entirely on 2 of 7 days in live testing (Tuesday and Thursday mornings
    both landed at zero) -- same plateau as every other per-daypart
    headcount rule this session, now given the same deterministic
    treatment as PAR-hours and the server cap.

    Only ever adds a shift for someone who already works Pizza Cook
    elsewhere this week, on a day/daypart they aren't already scheduled
    for any role and haven't marked unavailable -- never invents an
    employee. Times come from another Pizza Cook shift in the same
    daypart this week when one exists, else a generous placeholder that
    _enforce_close_time correctly bounds to that day's real close time.

    Returns (preview_rows, rows_added, added_dates).
    """
    from models import get_staff_availability
    import json as _json_avail

    _avail_rows = get_staff_availability(restaurant_id) or []
    unavailable_by_emp: dict = {}
    available_by_emp: dict = {}
    # Same conservative exclusion as _top_up_hours_gap's notes_restricted,
    # and more directly relevant here specifically: this function decides
    # MORNING vs NIGHT coverage, which is exactly the granularity
    # staff_availability's structured fields can't express -- a "only
    # mornings" note on someone whose available_days includes a day this
    # loop needs NIGHT coverage for would otherwise get silently ignored.
    notes_restricted: set = set()
    for a in _avail_rows:
        name = a.get("employee_name")
        if not name:
            continue
        try:
            unavailable_by_emp[name] = set(_json_avail.loads(a.get("unavailable_days") or "[]"))
        except Exception:
            unavailable_by_emp[name] = set()
        try:
            avail = _json_avail.loads(a.get("available_days") or "[]")
            if avail:
                available_by_emp[name] = set(avail)
        except Exception:
            pass
        if (a.get("notes") or "").strip():
            notes_restricted.add(name)

    pizza_cooks: set = set()
    working_on_date: dict = {}
    hours_by_employee: dict = {}
    morning_template = None
    night_template = None
    for r in preview_rows:
        emp, role, date = r.get("employee"), (r.get("role") or "").strip(), r.get("date")
        if not (emp and date):
            continue
        working_on_date.setdefault(date, set()).add(emp)
        try:
            hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
        if role.lower() == "pizza cook":
            pizza_cooks.add(emp)
            if morning_template is None and _window_overlap(r, _PIZZA_MORNING_WINDOW):
                morning_template = (r["shift_start"], r["shift_end"])
            if night_template is None and _window_overlap(r, _PIZZA_NIGHT_WINDOW):
                night_template = (r["shift_start"], r["shift_end"])

    rows_added = 0
    added_dates: dict = {}

    for date, day_name in zip(week_dates, week_days):
        target = 2 if day_name in _PIZZA_BUSY_DAYS else 1
        for window, template, fallback in (
            (_PIZZA_MORNING_WINDOW, morning_template, ("8:00am", "11:00pm")),
            (_PIZZA_NIGHT_WINDOW, night_template, ("4:00pm", "11:00pm")),
        ):
            current = [r for r in preview_rows if r.get("date") == date
                       and (r.get("role") or "").strip().lower() == "pizza cook" and _window_overlap(r, window)]
            need = target - len(current)
            for _ in range(max(0, need)):
                pool = pizza_cooks - working_on_date.get(date, set())
                pool = {e for e in pool
                        if day_name not in unavailable_by_emp.get(e, set())
                        and (e not in available_by_emp or day_name in available_by_emp[e])
                        and e not in notes_restricted}
                if not pool:
                    break  # no real, available Pizza Cook left this week -- don't invent one
                employee = min(pool, key=lambda e: hours_by_employee.get(e, 0.0))
                start, end = template or fallback
                new_row = {
                    "date": date, "day": day_name, "employee": employee, "role": "Pizza Cook",
                    "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                    "notes": "added — Pizza Cook coverage floor",
                }
                _enforce_close_time(new_row, day_name, close_times, role_buffers)
                s_min = _parse_time_to_minutes(new_row["shift_start"])
                e_min = _parse_time_to_minutes(new_row["shift_end"])
                if new_row.get("needs_review") or s_min is None or e_min is None or e_min <= s_min:
                    break
                new_row["scheduled_hours"] = str(round((e_min - s_min) / 60, 1))
                preview_rows.append(new_row)
                working_on_date.setdefault(date, set()).add(employee)
                hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + float(new_row["scheduled_hours"])
                rows_added += 1
                added_dates[date] = added_dates.get(date, 0) + 1

    return preview_rows, rows_added, added_dates


def _run_schedule_job(job_id, restaurant_id):
    import csv as _csv_mod, io as _io_sched, traceback as _tb, datetime as _dt_sched
    try:
        result = _build_schedule_result(restaurant_id)
        from models import get_staff_notes as _gsn_sched, get_close_times as _gct_sched, get_role_close_buffers as _grcb_sched
        _raw_notes = _gsn_sched(restaurant_id) or []
        staff_constraints = {n["employee_name"]: n["notes"] for n in _raw_notes if n.get("employee_name")}
        _close_times = _gct_sched(restaurant_id)
        _role_close_buffers = _grcb_sched(restaurant_id)
        preview_rows = []
        hours_scheduled = 0.0
        hours_added = 0.0
        added_dates = {}
        extended_dates = {}
        trimmed_dates = {}
        pizza_added_dates = {}
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

                # The model occasionally mis-writes one row per generation
                # (out of 60-100+) — always a non-routine addition (a food
                # runner, a "misfill check," an extra staff member) that
                # doesn't follow the same repeating pattern as the rest of
                # the week. `date` was correct in every malformed row
                # observed, so re-derive `day` from it instead of trusting
                # the model's own day text, and detect+repair the specific
                # failure shapes actually seen in captured live output
                # rather than guessing.
                try:
                    _real_day = _dt_sched.datetime.strptime(_row.get("date", ""), "%Y-%m-%d").strftime("%A")
                except (ValueError, TypeError):
                    _real_day = None
                if not _real_day:
                    # date itself didn't parse — can't verify or repair
                    # anything in this row, so flag it rather than let a
                    # possibly-bogus day value pass through unmarked.
                    _row["needs_review"] = True
                elif _row.get("day") != _real_day:
                    if _row.get("employee") == _real_day:
                        # Clean two-column swap: "...,Jamie L.,Friday,
                        # Server,..." instead of "...,Friday,Jamie L.,
                        # Server,...". Swap back for a full recovery.
                        _row["day"], _row["employee"] = _real_day, _row["day"]
                    elif _TIME_FIELD_RE.match(_row.get("role", "")):
                        # The far more common failure, confirmed against
                        # live captured output: the model drops the `day`
                        # field entirely for this one row (never reorders
                        # it — just omits it), which shifts every field
                        # after `date` one position left. The tell is that
                        # "role" ends up holding a clock time
                        # ("...,Farah A.,Prep Cook,8:00am,3:00pm,7,morning
                        # prep,,"), which a real role name never does —
                        # every field after `date` un-shifts one position
                        # right, recovering a fully sensible row instead of
                        # just flagging it.
                        _old_day, _old_employee, _old_role = _row.get("day", ""), _row.get("employee", ""), _row.get("role", "")
                        _old_start, _old_end, _old_hours = _row.get("shift_start", ""), _row.get("shift_end", ""), _row.get("scheduled_hours", "")
                        _row["day"] = _real_day
                        _row["employee"] = _old_day
                        _row["role"] = _old_employee
                        _row["shift_start"] = _old_role
                        _row["shift_end"] = _old_start
                        _row["scheduled_hours"] = _old_end
                        _row["notes"] = _old_hours
                    else:
                        _row["day"] = _real_day
                        if not _row_fields_look_sane(_row):
                            _row["needs_review"] = True

                _enforce_close_time(_row, _real_day, _close_times, _role_close_buffers)

                preview_rows.append(_row)
                try:
                    hours_scheduled += float(_row.get("scheduled_hours") or 0)
                except (ValueError, TypeError):
                    pass
            print(f"[schedule] parsed {len(preview_rows)} rows, first={preview_rows[0] if preview_rows else None}")

            preview_rows, pizza_rows_added, pizza_added_dates = _ensure_pizza_cook_coverage(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                restaurant_id, _close_times, _role_close_buffers,
            )
            if pizza_rows_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] pizza cook coverage added {pizza_rows_added} row(s) across {pizza_added_dates}")

            preview_rows, hours_added, added_dates = _top_up_hours_gap(
                preview_rows, result.get("daily_target_hours", {}),
                result.get("hours_budget", 0), hours_scheduled, restaurant_id,
                _close_times, _role_close_buffers,
            )
            if hours_added:
                hours_scheduled = round(hours_scheduled + hours_added, 1)
                print(f"[schedule] top-up added {hours_added}h across {added_dates}")

            preview_rows, hours_extended, extended_dates = _extend_shifts_to_close_gap(
                preview_rows, result.get("daily_target_hours", {}),
                result.get("hours_budget", 0), hours_scheduled, restaurant_id,
                _close_times, _role_close_buffers,
            )
            if hours_extended:
                hours_scheduled = round(hours_scheduled + hours_extended, 1)
                hours_added = round(hours_added + hours_extended, 1)
                print(f"[schedule] extension added {hours_extended}h across {extended_dates}")

            preview_rows, rows_trimmed, trimmed_dates = _trim_server_overlap_cap(
                preview_rows, _close_times, _role_close_buffers,
            )
            if rows_trimmed:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] trimmed {rows_trimmed} row(s) over the 7-server cap across {trimmed_dates}")

            # Rebuild schedule_csv from the (possibly repaired) rows so the
            # downloadable/shared CSV matches what the app displays instead
            # of shipping the pre-repair text out from under it.
            if preview_rows:
                _lines_out = [",".join(_COLS)]
                for _r in preview_rows:
                    _lines_out.append(",".join(_r.get(c, "") for c in _COLS))
                result["schedule_csv"] = "\n".join(_lines_out)
        except Exception as _csv_ex:
            print(f"[schedule] csv parse error: {_csv_ex}")
            pass

        try:
            from models import save_schedule_history
            _wd = result.get("week_dates", [])
            save_schedule_history(
                restaurant_id, _wd[0] if _wd else None, _wd[-1] if _wd else None,
                round(hours_scheduled, 1), result.get("hours_budget", 0), result.get("labor_target", 30),
                result["schedule_csv"], result.get("summary", []),
            )
        except Exception as _hist_ex:
            print(f"[schedule history] save error: {_hist_ex}")

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
                hours_added_by_backstop=hours_added,
                backstop_added_dates=added_dates,
                backstop_extended_dates=extended_dates,
                backstop_trimmed_dates=trimmed_dates,
                backstop_pizza_added_dates=pizza_added_dates,
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
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"schedule:{current_user['restaurant_id']}", max_calls=3, window_secs=60):
        return jsonify(ok=False, error="Too many schedule generations — please wait a moment and try again.")
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
        "alert_quiet_start":     getattr(r, "alert_quiet_start", None),
        "alert_quiet_end":       getattr(r, "alert_quiet_end", None),
        "alert_max_per_day":     getattr(r, "alert_max_per_day", 0),
    }
    return jsonify(ok=True, contacts=contacts, settings=settings)


@client_bp.route("/api/alert-settings", methods=["POST"])
@login_required
def save_alert_settings(current_user):
    from notify import get_alert_contacts, add_alert_contact, delete_alert_contact
    from models import update_restaurant
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]

    # SMS requires real, server-verified consent — the modal's checkbox is a
    # UX nicety, not enforcement, since anyone can call this API directly.
    # Turning SMS on without sms_consent=true in the payload is silently
    # downgraded to off rather than trusted on faith.
    sms_requested = bool(data.get("urgent_via_sms"))
    sms_consented = bool(data.get("sms_consent"))
    sms_on = sms_requested and sms_consented

    # Sync contacts — max 2
    new_contacts = (data.get("contacts") or [])[:2]
    existing = get_alert_contacts(rid)
    for ec in existing:
        delete_alert_contact(ec["id"])
    for nc in new_contacts:
        phone = _normalize_phone(nc.get("phone") or "")
        name  = (nc.get("name")  or "").strip()
        if phone:
            add_alert_contact(rid, name, phone, sms_consent=sms_on)

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
        "urgent_via_sms":        int(sms_on),
        "urgent_via_email":      int(bool(data.get("urgent_via_email"))),
        "alert_any_review":      int(bool(data.get("alert_any_review"))),
        "alert_resp_approved":   int(bool(data.get("alert_resp_approved"))),
        "digest_enabled":        int(bool(data.get("digest_enabled"))),
        "digest_day":            data.get("digest_day", "monday"),
        "alert_quiet_start":     data.get("alert_quiet_start") or None,
        "alert_quiet_end":       data.get("alert_quiet_end") or None,
        "alert_max_per_day":     int(data.get("alert_max_per_day") or 0),
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

@client_bp.route("/api/dismiss-onboarding", methods=["POST"])
@login_required
def dismiss_onboarding(current_user):
    """Hide the getting-started checklist permanently for this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {"onboarding_dismissed": 1})
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
                        "<div style='font-family:-apple-system,BlinkMacSystemFont,\"Helvetica Neue\",Arial,sans-serif;max-width:520px;margin:0 auto'>"
                        "<img src='https://dashboard.cavnar.ai/static/brand/lockup-dark-email.png' width='150' height='28' alt='Cavnar AI' style='display:block;width:150px;height:28px;border:0;outline:none;margin-bottom:16px'>"
                        "<div style='border-top:3px solid #e07040;padding-top:16px;margin-bottom:16px'>"
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

        # Genuinely check first-upload-of-this-type — the comment always
        # claimed this but nothing enforced it, so Will was emailed on
        # every single CSV upload, not just the first. Checked BEFORE
        # log_email() below inserts this upload's own row, using the
        # existing email_log table as the record of every prior upload.
        _conn_fu = get_conn()
        _is_first_upload = _conn_fu.execute(
            "SELECT 1 FROM email_log WHERE restaurant_id=? AND email_type=? LIMIT 1",
            (restaurant_id, label)
        ).fetchone() is None
        _conn_fu.close()

        log_email(restaurant_id, label, current_user.get("email",""), f"{label} — {r.name if r else ''}")

        import os as _os, resend as _resend
        _resend_key = _os.getenv("RESEND_API_KEY", "")
        _will_email = _os.getenv("WILL_EMAIL", "will@cavnar.ai")
        _from_email = _os.getenv("FROM_EMAIL", "will@cavnar.ai")
        if _is_first_upload and _resend_key and r:
            _resend.api_key = _resend_key
            _module = "shift schedule" if data_type == "shifts" else "inventory"
            _resend.Emails.send({
                "from": f"Cavnar AI Alerts <{_from_email}>",
                "to": [_will_email],
                "subject": f"📂 {r.name} uploaded their first {_module} data",
                "html": f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:500px;margin:0 auto">
                    <img src="https://dashboard.cavnar.ai/static/brand/lockup-dark-email.png" width="150" height="28" alt="Cavnar AI" style="display:block;width:150px;height:28px;border:0;outline:none;margin-bottom:16px">
                    <div style="border-top:3px solid #c84b2f;padding-top:16px;margin-bottom:16px">
                        <h3 style="color:#0e0c0a;margin:0">First data upload</h3>
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

def _do_food_cost_quickcount(restaurant_id, items):
    """Save Big-8 ingredient prices, compute week-over-week drift, return alerts."""
    import json as _json_fc
    from datetime import datetime as _dt_fc
    from models import get_client_data as _gcd, get_conn as _gcc

    if not items or not isinstance(items, list):
        return {"ok": False, "error": "No items provided"}, 400

    rid = restaurant_id
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
    return {
        "ok": True, "drift": drift, "total_weekly_impact": round(total_impact, 2),
        "submitted_at": now_str, "prev_submitted_at": prev.get("submitted_at") if prev else None,
    }, 200


def _do_save_food_cost_custom_item(restaurant_id, name, unit):
    """Persist a custom ingredient name+unit to food_cost_json so it appears on next load."""
    import json as _jci
    name = (name or "").strip()
    unit = (unit or "").strip()
    if not name:
        return {"ok": False, "error": "Name required"}, 400

    rid = restaurant_id
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

    return {"ok": True, "name": name, "unit": unit}, 200


@client_bp.route("/api/food-cost-quickcount", methods=["POST"])
@login_required
def food_cost_quickcount(current_user):
    data = request.get_json() or {}
    payload, status = _do_food_cost_quickcount(current_user["restaurant_id"], data.get("items", []))
    return jsonify(**payload), status


@client_bp.route("/api/food-cost/save-custom-item", methods=["POST"])
@login_required
def save_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _do_save_food_cost_custom_item(current_user["restaurant_id"], data.get("name"), data.get("unit"))
    return jsonify(**payload), status


def _do_delete_food_cost_custom_item(restaurant_id, name):
    """Remove a saved custom ingredient by name from food_cost_json."""
    import json as _jcd
    name = (name or "").strip().lower()
    if not name:
        return {"ok": False, "error": "Name required"}, 400

    rid = restaurant_id
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
    return {"ok": True}, 200


@client_bp.route("/api/food-cost/delete-custom-item", methods=["POST"])
@login_required
def delete_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _do_delete_food_cost_custom_item(current_user["restaurant_id"], data.get("name"))
    return jsonify(**payload), status


# ── Review request ────────────────────────────────────────────────────────────

@client_bp.route("/api/send-review-request", methods=["POST"])
@login_required
def send_review_request(current_user):
    payload, status = _do_send_review_request(current_user["restaurant_id"], request.get_json() or {})
    return jsonify(**payload), status


def _do_send_review_request(rid, data):
    """Shared by the web route above and mobile_api.py's own send-review-request."""
    try:
        customer_name  = (data.get("name") or "").strip()
        customer_email = (data.get("email") or "").strip().lower()
        customer_phone = (data.get("phone") or "").strip()
        guest_note     = (data.get("message") or "").strip()[:200]
        if not customer_email and not customer_phone:
            return {"ok": False, "error": "Email or phone required"}, 400
        if customer_email and "@" not in customer_email:
            return {"ok": False, "error": "Valid email address required"}, 400

        restaurant = get_restaurant(rid)
        if not restaurant:
            return {"ok": False, "error": "Restaurant not found"}, 404

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
                + (f"{guest_note} " if guest_note else "")
                + f"We'd love your feedback — leave us a Google review: {review_url}"
            )
            sent_sms = _send_sms(customer_phone, sms_text)
            if not sent_sms and not customer_email:
                return {"ok": False, "error": "SMS delivery failed — check Twilio config"}, 500

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
            return {"ok": True}, 200

        import resend as _resend
        _resend.api_key = os.getenv("RESEND_API_KEY", "")
        if not _resend.api_key:
            return {"ok": False, "error": "Email not configured"}, 500

        import html as _html_escape
        note_block = (
            f'<p style="font-size:15px;color:#3a3530;line-height:1.6;margin:0 0 24px;'
            f'padding:14px 16px;background:#f7f4ef;border-left:3px solid #c84b2f;border-radius:4px">'
            f'{_html_escape.escape(guest_note)}</p>'
            if guest_note else ""
        )
        html_body = f"""
        <div style="font-family:'DM Sans',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;background:#f7f4ef">
          <div style="background:white;border-radius:12px;padding:32px;border:1px solid #e0dbd0">
            <img src="https://dashboard.cavnar.ai/static/brand/lockup-dark-email.png" width="150" height="28" alt="Cavnar AI" style="display:block;width:150px;height:28px;border:0;outline:none;margin-bottom:4px">
            <div style="font-size:11px;color:#7a736a;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #e0dbd0">
              On behalf of {rest_name}
            </div>
            <p style="font-size:15px;color:#3a3530;line-height:1.6;margin:0 0 16px">
              Hi {first_name},
            </p>
            <p style="font-size:15px;color:#3a3530;line-height:1.6;margin:0 0 24px">
              Thank you for dining with us at <strong>{rest_name}</strong>. We hope you had a great experience — we'd love to hear your thoughts.
            </p>
            {note_block}
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
            "to":      [customer_email],
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

        return {"ok": True}, 200

    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


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
    payload, status = _do_ai_visibility(current_user["restaurant_id"])
    return jsonify(**payload), status


def _do_ai_visibility(rid):
    """Shared by the web route above and mobile_api.py's own ai-visibility."""
    try:
        return _do_ai_visibility_inner(rid)
    except Exception as e:
        return {"ok": False, "error": str(e)}, 200


def _do_ai_visibility_inner(rid):
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"aivis:{rid}", max_calls=3, window_secs=60):
        return {"ok": False, "error": "Too many visibility checks — please wait a moment and try again."}, 200
    r = get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Restaurant not found"}, 404

    name        = r.name or ""
    neighborhood = r.neighborhood or ""
    vibe        = r.vibe or ""
    known_for   = r.known_for or ""
    # Use just the city portion (before any em dash or comma-detail) for clean queries
    city = neighborhood.split("—")[0].split(",")[0].strip() if neighborhood else ""
    city_full = neighborhood.split("—")[0].strip() if neighborhood else ""
    # Short cuisine descriptor from known_for first word(s), fallback to "restaurant"
    cuisine = (known_for.split(",")[0].strip() if known_for else "") or "restaurant"

    # Was "Where can I find " + the full vibe sentence + " in [city]?" —
    # vibe is a paragraph-length internal profile description (e.g.
    # "Contemporary Italian pizza bar with wood-fired Neapolitan pizzas
    # and a lively bar scene"), and embedding it verbatim made this an
    # exact-match fingerprint of the restaurant's own profile text, not a
    # query a real person would ever type. Reuses the same short cuisine
    # phrase already extracted above (known_for's first comma-separated
    # item) instead, lowercased to read as a natural mid-sentence phrase —
    # specific enough to test real cuisine-keyword discoverability,
    # generic enough that it isn't just parroting the profile back.
    vibe_query = ("Where can I find good " + cuisine.lower() + " in " + city_full + "?") if (vibe and city) else None

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
        # cuisine falls back to the literal word "restaurant" when known_for
        # is empty (line ~3129) — blindly concatenating that into these two
        # produced "Top local restaurant restaurants" / "Best restaurant
        # restaurant near me" for any restaurant with an incomplete profile,
        # exactly the "not a query a real person would type" problem the
        # vibe_query fix above already solved once, just resurfacing here in
        # the no-city fallback path. Only insert the cuisine word when it's
        # a real, non-fallback value.
        has_cuisine = bool(known_for)
        queries = [
            (name + " restaurant") if name else "restaurant near me",
            ("Top local " + cuisine + " restaurants") if has_cuisine else "Top local restaurants",
            ("Best " + cuisine + " restaurant near me") if has_cuisine else "Best restaurant near me",
        ]

    import requests as _pplx_req
    import time as _pplx_time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _pplx_key = os.getenv("PERPLEXITY_API_KEY", "")
    appeared_count = 0

    def _norm(s):
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("’", "").replace("’", ""))

    norm_name = _norm(name)

    # LEADING patterns ported from dashboard.html's own client-side cleanup
    # (renderAIVisibility) — ONLY handled the start of the answer, never the
    # end, and only existed on web at all, not here or on iOS. Moved server-
    # side so both platforms get clean text from the same source instead of
    # duplicating this regex list in two languages, and extended with
    # TRAILING patterns for the specific complaint this was missing: a
    # chatty offer tacked onto the end ("I can check others if you want?")
    # that makes no sense to show — the user never typed this query
    # themselves, it's generated server-side, so there's no "you" for the
    # model to be replying to.
    _leading_ai_patterns = [
        re.compile(r"^If you (?:mean|are (?:looking|referring|asking))[^,.]{0,80}[,.]\s*", re.I),
        re.compile(r"^Based on (?:the |my )?(?:search results?|available (?:information|sources?|data)|results)[,.]\s*", re.I),
        re.compile(r"^According to (?:the |my )?(?:search results?|available (?:information|sources?|data)|sources?)[,.]\s*", re.I),
        re.compile(r"^From (?:the |my )?(?:search results?|available (?:information|sources?|data)|results)[,.]\s*", re.I),
        re.compile(r"^The (?:search )?results? (?:show|indicate|suggest|reveal)s?\s+", re.I),
        re.compile(r"^(?:Looking at|Reviewing) (?:the )?(?:search )?results?[,.]\s*", re.I),
        re.compile(r"^I (?:found|can see|notice|see) that\s+", re.I),
        re.compile(r"^While .{5,80} is (?:a suburb|located|situated|part of)[^.]+\.\s*", re.I),
        re.compile(r"^Note that\s+", re.I),
    ]
    # Trailing: a whole final sentence that's the model offering to do more
    # rather than answering — "I can check others if you'd like", "Let me
    # know if you want more options", "Would you like me to look into it
    # further?". Applied in a loop since the model sometimes stacks two.
    _trailing_ai_pattern = re.compile(
        r"\s*(?:I can|I could|I'd be happy to|I'm happy to|Would you like me to|"
        r"Let me know if|Just let me know if|Feel free to)\b[^.!?]*[.!?]?\s*$",
        re.I,
    )

    def _clean_ai_answer(text):
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", text or "")
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        cleaned = re.sub(r"\[\d+\]", "", cleaned)
        for pat in _leading_ai_patterns:
            cleaned = pat.sub("", cleaned)
        while True:
            trimmed = _trailing_ai_pattern.sub("", cleaned)
            if trimmed == cleaned or not trimmed.strip():
                break
            cleaned = trimmed
        cleaned = cleaned.strip()
        return cleaned[0].upper() + cleaned[1:] if cleaned else cleaned

    # Firing all 3 queries at once via the ThreadPoolExecutor below
    # reliably trips Perplexity's rate limit on this key's tier — verified
    # directly: the same 3 queries run sequentially all succeed, but
    # 2 of 3 silently come back empty when fired simultaneously, every
    # time. One retry after a short delay (letting whatever per-second
    # window the limit uses clear) recovers those without giving up the
    # speed of parallelizing the common case where the limit isn't hit.
    def _run_query(q, _retry=True):
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
            if not answer and _retry:
                _pplx_time.sleep(2)
                return _run_query(q, _retry=False)
            appeared = bool(norm_name) and bool(answer) and norm_name in _norm(answer)
            # Was answer[:400] — the system prompt already asks for "under
            # 80 words" (~440 chars including spaces), so a 400-char cap
            # sat BELOW what a compliant response typically needs and was
            # cutting real content off before iOS's own press-and-hold
            # "read the full answer" feature ever saw it. max_tokens: 300
            # on the API call above already bounds the raw response size —
            # this extra truncation was redundant on top of that, not a
            # real safety net.
            return {"query": q, "answer": _clean_ai_answer(answer), "appeared": appeared}
        except Exception:
            if _retry:
                _pplx_time.sleep(2)
                return _run_query(q, _retry=False)
            return {"query": q, "answer": "Could not fetch answer.", "appeared": False}

    # Run all queries in parallel, but staggered — caps total time at ~10s
    # instead of 30s+, while avoiding the true root cause of the rate-limit
    # failures: all 3 requests landing in the same instant. The retry
    # inside _run_query alone wasn't reliable enough (retries can still
    # collide with each other); starting each submission 0.6s after the
    # last spreads the burst without giving up most of the parallel-speed
    # benefit (~1.2s of stagger vs ~3-4s per request either way).
    query_results = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=3) as _pool:
        _futures = {}
        for _i, _q in enumerate(queries):
            if _i > 0:
                _pplx_time.sleep(0.6)
            _futures[_pool.submit(_run_query, _q)] = _i
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

    # 1. Google Place ID — lets AI tools index the right location
    if bool(r.google_place_id):
        checklist.append({"label": "Google Place ID connected", "done": True, "pts": 10,
                          "action": "Done — AI tools can find your location", "needs_gmb": False})
    else:
        checklist.append({"label": "Add your Google Place ID", "done": False, "pts": 10,
                          "action": "Go to Account → paste your Google Place ID so AI tools can index you",
                          "needs_gmb": False})

    # 2. Yelp profile linked — Perplexity and ChatGPT pull heavily from Yelp
    if bool(r.yelp_business_id):
        checklist.append({"label": "Yelp profile linked", "done": True, "pts": 10,
                          "action": "Done — Perplexity indexes Yelp heavily", "needs_gmb": False})
    else:
        checklist.append({"label": "Link your Yelp business profile", "done": False, "pts": 10,
                          "action": "Go to Account → add your Yelp business ID (find it in your Yelp URL)",
                          "needs_gmb": False})

    # 3. Menu URL — admin sets this; silently included if present, hidden if not
    if bool(r.menu_url):
        checklist.append({"label": "Menu URL added", "done": True, "pts": 10,
                          "action": "Done — AI tools can surface your menu in results", "needs_gmb": False})

    # 4. Restaurant profile — vibe + known_for + neighborhood power all AI queries
    has_full_profile = bool(r.neighborhood and r.vibe and r.known_for)
    if has_full_profile:
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
        checklist.append({"label": "50+ Google reviews", "done": True, "pts": 10,
                          "action": "Done — strong review volume boosts AI ranking", "needs_gmb": False})
    elif review_total >= 20:
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "done": False, "pts": 10,
                          "action": "Send review requests to recent customers — 50+ reviews is the AI visibility threshold",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "done": False, "pts": 10,
                          "action": "Send review requests after every visit — this is the #1 driver of AI search ranking",
                          "needs_gmb": False})

    # 6. Review response rate — active engagement signals a healthy business to AI tools
    if resp_rate >= 75:
        checklist.append({"label": "Excellent review response rate (" + str(resp_rate) + "%)", "done": True, "pts": 10,
                          "action": "Done — responding to reviews signals an active, trusted business", "needs_gmb": False})
    elif resp_rate >= 40:
        checklist.append({"label": "Increase response rate to 75%+ (currently " + str(resp_rate) + "%)", "done": False, "pts": 10,
                          "action": "Use the Reviews tab to draft and post responses — AI tools reward active owner engagement",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Start responding to Google reviews (currently " + str(resp_rate) + "%)", "done": False, "pts": 10,
                          "action": "Go to Reviews → use AI-drafted responses to reply — aim for 75%+ response rate",
                          "needs_gmb": False})

    # 7. GBP OAuth connected — unlocks real-time profile data and future auto-posting
    if gbp_connected:
        checklist.append({"label": "Google Business Profile connected", "done": True, "pts": 10,
                          "action": "Done — real-time GBP data is active", "needs_gmb": False})
    else:
        checklist.append({"label": "Connect Google Business Profile (OAuth)", "done": False, "pts": 10,
                          "action": "Go to Account → Connect GBP to unlock live profile editing and Google Posts",
                          "needs_gmb": True})

    # 8. Business description — keyword-rich descriptions are indexed by every AI search tool
    desc = gbp_data.get("description", "")
    if desc and len(desc) >= 150:
        checklist.append({"label": "Business description written (" + str(len(desc)) + " chars)", "done": True, "pts": 10,
                          "action": "Done — description feeds AI search results directly", "needs_gmb": False})
    elif desc:
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

    # 11. Business hours in GBP — AI tools answer "is it open right now"
    # directly from this field; without it, that whole class of query can't
    # be answered about this restaurant at all, regardless of how complete
    # everything else is. get_gbp_listing's readMask now requests
    # regularHours alongside the fields it already fetched (gmb.py).
    has_hours = bool(gbp_data.get("has_hours"))
    if gbp_connected and has_hours:
        checklist.append({"label": "Hours listed in GBP", "done": True, "pts": 10,
                          "action": "Done — AI tools can answer \"is it open now\" directly", "needs_gmb": False})
    elif gbp_connected and not has_hours:
        checklist.append({"label": "Add hours to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Hours: set your regular hours",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add hours to GBP", "done": False, "pts": 10,
                          "action": "In Google Business Profile → Info → Hours: set your regular hours",
                          "needs_gmb": True})

    # 12. Recent review activity — volume (#5) and response rate (#6) alone
    # don't catch a restaurant that's stopped getting NEW reviews; a
    # steady, current review stream is its own distinct signal AI systems
    # weigh over one that simply peaked at some point in the past. Pulled
    # from our own reviews table — no GMB dependency, same as items
    # 1/2/4/5/6.
    _rconn = get_conn()
    recent_reviews = _rconn.execute(
        "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL AND fetched_at >= datetime('now','-30 days')",
        (rid,)
    ).fetchone()[0] or 0
    _rconn.close()
    if recent_reviews >= 3:
        checklist.append({"label": "Active review stream (" + str(recent_reviews) + " in last 30 days)", "done": True, "pts": 10,
                          "action": "Done — a steady, current review stream signals an active business", "needs_gmb": False})
    elif recent_reviews >= 1:
        checklist.append({"label": "Build a steadier review stream (" + str(recent_reviews) + " in last 30 days)", "done": False, "pts": 10,
                          "action": "Send review requests regularly — a handful of new reviews each month keeps your listing looking active",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "No reviews in the last 30 days", "done": False, "pts": 10,
                          "action": "Send review requests to recent customers — an active, current stream matters as much as total volume",
                          "needs_gmb": False})

    # gbp_score is a straight doneCount/totalCount percentage, not a
    # weighted point sum — the old scheme (raw points per item, uneven
    # partial-credit branches, clamped to 100 to guard against the silent
    # bonus items) was exactly why this could disagree with the checklist
    # grid's own "X/Y done" count (reported directly: 5/11 done showing as
    # 50%, which was the old 5/10 math, stale the moment an 11th item
    # existed). This is always self-consistent with whatever the checklist
    # actually ends up being for this restaurant — currently 11 or 12 items
    # depending on whether menu_url is set — with no special-casing needed
    # for that; a new item just changes the denominator automatically.
    _gbp_done = sum(1 for item in checklist if item["done"])
    gbp_score = round(_gbp_done / len(checklist) * 100) if checklist else 0

    # Social posting cadence — deliberately NOT a checklist item / part of
    # gbp_score (this isn't a Google Business Profile field, it's marketing
    # activity within this app), returned as its own field so the roadmap's
    # "Post consistently on social" card can auto-complete instead of
    # always showing not-done. marketing_content_log only proves content
    # was drafted through this app, not confirmed-posted to a platform —
    # an imperfect signal, but a real, live one rather than none at all.
    # Same table-creation pattern mobile_api.py's own home-KPI query uses
    # for this table, since this is the first read of it from client_api.py.
    _conn = get_conn()
    _conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    social_posts_30d = _conn.execute(
        "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','-30 days')",
        (rid,)
    ).fetchone()[0] or 0
    _conn.close()

    ai_score = round((appeared_count / len(queries)) * 100) if queries else 0

    return {
        "ok": True,
        "restaurant_name": name,
        "neighborhood": neighborhood,
        "queries": query_results,
        "appeared_count": appeared_count,
        "total_queries": len(queries),
        "ai_score": ai_score,
        "gbp_score": gbp_score,
        "checklist": checklist,
        "gbp_connected": gbp_connected,
        "social_posts_30d": social_posts_30d,
        # review_total/resp_rate were already computed above (item 5/6's own
        # thresholds use them) but never left this function — the roadmap
        # only ever saw a boolean done flag per checklist item, which is why
        # its copy could only ever be generic done/not-done text instead of
        # this restaurant's own actual numbers. Exposed directly so the
        # roadmap can build copy like "38 of 50 reviews" instead of a
        # static "get more reviews" for every restaurant regardless of
        # where they actually stand.
        "review_total": review_total,
        "resp_rate": resp_rate,
    }, 200


@client_bp.route("/api/webhook", methods=["GET"])
@login_required
def webhook_get(current_user):
    from webhooks import get_webhook
    import json
    # get_webhook() only returns is_active=1 rows, so an auto-disabled webhook
    # (is_active=0) wouldn't show up here at all — the client would just see
    # "no webhook configured" with no explanation of why it disappeared.
    # Look it up directly so a disabled-but-still-configured webhook is
    # visible, with disabled_reason explaining what happened.
    from models import get_conn
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM webhooks WHERE restaurant_id=? LIMIT 1",
        (current_user["restaurant_id"],)
    ).fetchone()
    conn.close()
    wh = dict(row) if row else None
    if not wh:
        return jsonify(ok=True, webhook=None)
    return jsonify(ok=True, webhook={
        "url":                  wh["url"],
        "secret":               wh["secret"],
        "events":               json.loads(wh.get("events") or "[]"),
        "last_fired":           wh.get("last_fired_at"),
        "last_status":          wh.get("last_status"),
        "is_active":            bool(wh.get("is_active")),
        "consecutive_failures": wh.get("consecutive_failures") or 0,
        "disabled_reason":      wh.get("disabled_reason"),
    })

@client_bp.route("/api/webhook/deliveries", methods=["GET"])
@login_required
def webhook_deliveries_route(current_user):
    from webhooks import get_webhook_deliveries
    return jsonify(ok=True, deliveries=get_webhook_deliveries(current_user["restaurant_id"], limit=20))

@client_bp.route("/api/webhook/reactivate", methods=["POST"])
@login_required
def webhook_reactivate(current_user):
    from webhooks import reactivate_webhook
    reactivate_webhook(current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/webhook", methods=["POST"])
@login_required
def webhook_save(current_user):
    from webhooks import save_webhook, InvalidWebhookURL
    import json
    data   = request.get_json()
    url    = (data.get("url") or "").strip()
    events = data.get("events") or ["review.received", "alert.fired", "response.approved"]
    if not url.startswith("http"):
        return jsonify(ok=False, error="Invalid URL")
    try:
        secret = save_webhook(current_user["restaurant_id"], url, events)
    except InvalidWebhookURL as e:
        return jsonify(ok=False, error=str(e))
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
    result = _deliver(wh, "test", {
        "message": "This is a test webhook from Cavnar AI",
        "restaurant_id": current_user["restaurant_id"],
    })
    if result and result.get("ok"):
        return jsonify(ok=True)
    status = result.get("status") if result else 0
    error = result.get("error") if result else None
    if status:
        return jsonify(ok=False, error=f"Endpoint responded with status {status} — check it's returning a 2xx.")
    return jsonify(ok=False, error=error or "Could not reach that URL — check it's correct and publicly reachable.")


# ── Guest SMS lifecycle marketing — Marketing-module clients only ───────────
# Every other module gate in this app is UI-only (the tab/button is hidden,
# but the API route itself doesn't check). This one actually enforces it
# server-side too, because unlike generating marketing copy, sending a guest
# campaign has a real per-message Twilio cost — a client without the module
# hitting the API directly would be a real, billable abuse path, not just a
# UI inconsistency.

def _restaurant_has_marketing_module(restaurant_id):
    r = get_restaurant(restaurant_id)
    return bool(r and r.module_marketing)

_NO_MARKETING_MODULE_ERROR = "Guest text club requires the Marketing module — contact will@cavnar.ai to add it."

@client_bp.route("/api/guest-contacts", methods=["GET"])
@login_required
def guest_contacts_list(current_user):
    if not _restaurant_has_marketing_module(current_user["restaurant_id"]):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import get_guest_contacts
    return jsonify(ok=True, contacts=get_guest_contacts(current_user["restaurant_id"]))

@client_bp.route("/api/guest-contacts", methods=["POST"])
@login_required
def guest_contacts_add(current_user):
    """Owner adding a number manually — never consented (see
    guest_marketing.add_guest_contact_manual's docstring for why)."""
    if not _restaurant_has_marketing_module(current_user["restaurant_id"]):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import add_guest_contact_manual
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    if not phone:
        return jsonify(ok=False, error="Phone number required"), 400
    contact_id = add_guest_contact_manual(current_user["restaurant_id"], phone, name=name)
    return jsonify(ok=True, id=contact_id)

@client_bp.route("/api/guest-contacts/<int:contact_id>", methods=["DELETE"])
@login_required
def guest_contacts_delete(contact_id, current_user):
    if not _restaurant_has_marketing_module(current_user["restaurant_id"]):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import delete_guest_contact
    delete_guest_contact(contact_id, current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/guest-contacts/<int:contact_id>/mark-visit", methods=["POST"])
@login_required
def guest_contacts_mark_visit(contact_id, current_user):
    """Manual visit signal for contacts without a natural opt-in-scan moment —
    starts the automated post-visit review-request countdown (see
    guest_marketing.run_review_request_followups)."""
    if not _restaurant_has_marketing_module(current_user["restaurant_id"]):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import mark_guest_visit
    mark_guest_visit(contact_id, current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/guest-campaign/draft", methods=["POST"])
@login_required
def guest_campaign_draft(current_user):
    rid = current_user["restaurant_id"]
    if not _restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"guestcampaign:{rid}", max_calls=8, window_secs=60):
        return jsonify(ok=False, error="Too many requests — please wait a moment and try again."), 429
    data = request.get_json() or {}
    try:
        from guest_marketing import draft_campaign_message
        restaurant = get_restaurant(rid)
        message = draft_campaign_message(restaurant, campaign_type=data.get("type", "general"), topic=data.get("topic", ""))
        return jsonify(ok=True, message=message)
    except Exception as e:
        import ops
        ops.capture(e, job="guest_campaign_draft", context=f"restaurant_id={rid}")
        return jsonify(ok=False, error="Couldn't draft a message right now — try again in a moment."), 500

@client_bp.route("/api/guest-campaign/send", methods=["POST"])
@login_required
def guest_campaign_send(current_user):
    rid = current_user["restaurant_id"]
    if not _restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Message required"), 400
    if ai_rate_limited(f"guestcampaignsend:{rid}", max_calls=3, window_secs=300):
        return jsonify(ok=False, error="Too many campaigns sent recently — please wait a few minutes."), 429
    try:
        from guest_marketing import send_campaign
        result = send_campaign(rid, message)
        return jsonify(ok=True, **result)
    except Exception as e:
        import ops
        ops.capture(e, job="guest_campaign_send", context=f"restaurant_id={rid}")
        return jsonify(ok=False, error="Couldn't send the campaign — try again in a moment."), 500


@client_bp.route("/api/guest-qr")
@login_required
def guest_qr_code(current_user):
    """Downloadable PNG QR code encoding the guest join link — the actual
    guest-facing artifact has to leave this screen (printed on a table
    tent, receipt footer, etc.); nobody scans a laptop in the office."""
    rid = current_user["restaurant_id"]
    if not _restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    import qrcode, io
    join_url = request.url_root.rstrip("/") + f"/join/{rid}"
    img = qrcode.make(join_url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    download = request.args.get("download") == "1"
    return send_file(buf, mimetype="image/png", as_attachment=download,
                      download_name="guest-text-club-qr.png" if download else None)


# ── Public guest opt-in page — no login, printed on a table tent / QR code ──
# Also module-gated: if a client's Marketing module is later removed, their
# old join link/QR code (already printed, already in the wild) must stop
# accepting new signups rather than keep working for free.

@client_bp.route("/join/<int:restaurant_id>")
def guest_optin_page(restaurant_id):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    if not restaurant.module_marketing:
        return "This text club isn't active right now.", 404
    return render_template("guest_optin.html", restaurant_name=restaurant.name)

@client_bp.route("/api/public/guest-optin/<int:restaurant_id>", methods=["POST"])
def guest_optin_submit(restaurant_id):
    from ai_utils import ai_rate_limited
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"
    if ai_rate_limited(f"guestoptin:{ip}", max_calls=5, window_secs=300):
        return jsonify(ok=False, error="Too many attempts — please wait a few minutes and try again."), 429
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if not restaurant.module_marketing:
        return jsonify(ok=False, error="This text club isn't active right now."), 404
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        return jsonify(ok=False, error="Enter a valid phone number"), 400
    if not data.get("consent"):
        return jsonify(ok=False, error="Consent is required to join"), 400
    from guest_marketing import add_guest_contact_public_optin
    add_guest_contact_public_optin(restaurant_id, phone, name=name)
    return jsonify(ok=True)


def _do_switch_location(current_user, target_id, token):
    if current_user.get("role") != "owner":
        return {"ok": False, "error": "Not an owner account"}, 403
    if not target_id:
        return {"ok": False, "error": "Missing restaurant_id"}, 400
    # Validate target is in same group as base restaurant
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return {"ok": False, "error": "No location group configured"}, 400
    group = get_location_group(base.location_group)
    valid_ids = [r["id"] for r in group]
    if target_id not in valid_ids:
        return {"ok": False, "error": "Location not in your group"}, 403
    from auth import switch_active_restaurant
    switch_active_restaurant(token, target_id)
    target = get_restaurant(target_id)
    return {"ok": True, "restaurant_name": target.name, "restaurant_id": target_id}, 200


def _do_group_locations(current_user):
    if current_user.get("role") != "owner":
        return {"ok": True, "locations": []}, 200
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return {"ok": True, "locations": []}, 200
    group = get_location_group(base.location_group)
    active_id = current_user["restaurant_id"]
    locs = [{"id": r["id"], "name": r.get("location_name") or r["name"],
              "active": r["id"] == active_id} for r in group]
    return {"ok": True, "locations": locs, "group_name": base.location_group}, 200


# Notification/alert type → human label. Keys must match the UNPREFIXED
# alert_type strings notify.py._log_alert() actually writes ("1star", not
# "alert_1star") — a prior version of this dict used prefixed keys and so
# never matched anything, silently falling back to the raw internal string.
_NOTIFICATION_LABELS = {
    "1star":            "1★ review received",
    "2star":            "2★ review received",
    "5star":            "5★ review received",
    "health":           "Health/safety mention",
    "neg_spike":        "Negative review spike",
    "negative_trend":   "Rating declining trend",
    "no_response":      "Unresponded review (48h)",
    "rating_threshold": "Rating below threshold",
    "labor_over":       "Labor % over target",
}

# Which module a notification's "view" action should open — every alert
# type is review/rating-driven except labor_over. Review-specific types
# also carry a review_id (see the SELECT below) so the client can jump
# straight to that review instead of just the Reviews tab in general.
_NOTIFICATION_MODULE = {
    "1star": "reviews", "2star": "reviews", "5star": "reviews", "health": "reviews",
    "neg_spike": "reviews", "no_response": "reviews",
    "negative_trend": "reviews", "rating_threshold": "reviews",
    "labor_over": "labor",
}


def _do_get_notifications(restaurant_id):
    try:
        conn = get_conn()
        rows = conn.execute(
            """SELECT alert_type, review_id, fired_at FROM alert_log
               WHERE restaurant_id=?
               ORDER BY fired_at DESC LIMIT 20""",
            (restaurant_id,)
        ).fetchall()
        conn.close()
        items = [{"type": r["alert_type"],
                  "label": _NOTIFICATION_LABELS.get(r["alert_type"], r["alert_type"]),
                  "fired_at": r["fired_at"],
                  "review_id": r["review_id"],
                  "module": _NOTIFICATION_MODULE.get(r["alert_type"], "reviews")} for r in rows]
        return {"ok": True, "notifications": items}, 200
    except Exception as e:
        return {"ok": False, "notifications": [], "error": "Couldn't load notifications right now."}, 200


@client_bp.route("/api/switch-location", methods=["POST"])
@login_required
def switch_location(current_user):
    data = request.get_json() or {}
    target_id = int(data.get("restaurant_id", 0))
    token = request.cookies.get("session_token")
    payload, status = _do_switch_location(current_user, target_id, token)
    return jsonify(**payload), status


@client_bp.route("/api/group-locations")
@login_required
def group_locations(current_user):
    payload, status = _do_group_locations(current_user)
    return jsonify(**payload), status


@client_bp.route("/api/notifications")
@login_required
def get_notifications(current_user):
    payload, status = _do_get_notifications(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Ryan seed (module-level — runs under Gunicorn AND direct python) ─────────


