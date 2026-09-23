"""
client_api.py — Client-facing API routes and data endpoints
Registered as a Flask Blueprint in hosted_dashboard.py
"""
import config
from flask import Blueprint, request, jsonify, redirect, send_file, Response, render_template, make_response
import os, json, re, time, threading
from datetime import datetime

from models import get_restaurant, update_restaurant, approve_response, get_review_stats, get_reviews_data, get_top_issues, get_topic_heatmap
import models as _models_mod

def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard. `from models import get_conn` bound the function object at
    import, so a test's monkeypatch of models.get_conn never reached the
    bare get_conn() calls in this module and they opened ./reviews.db."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()
from auth import login_required


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

client_bp = Blueprint('client', __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(client_bp)

# Exception text handed to a client, with credentials stripped — a
# requests error carries the failing URL, and a Places URL carries key=.
from ai_guard import safe_error as _safe_err

# Simple in-memory insight cache: {cache_key: (timestamp, value)}
_insight_cache = {}
_INSIGHT_TTL = 300  # 5 minutes

# The most rows one CSV import will process inline. A year of shifts for a
# 40-person restaurant is roughly 15,000 rows, so this is generous for real
# data and still bounded: parse, analyse and store all run synchronously in
# the request, and the deployment has four request threads in total.
MAX_CSV_ROWS = 25_000

# How many Ask answers may be generated at once in this process. Each holds a
# daemon thread and an open model conversation that can run several tool
# rounds. Sized above gunicorn's --threads 4 so it never rejects a request
# the server could actually serve today, while still being a real ceiling if
# the worker count changes.
# Each open Ask stream holds a request thread for its whole tool loop. The
# ceiling must stay under gunicorn's 4 threads, or four people asking at
# once leave nothing to serve login or /health (AI-1).
ASK_MAX_CONCURRENT = min(int(os.getenv("ASK_MAX_CONCURRENT", "2")), 3)
_ASK_SLOTS = threading.BoundedSemaphore(ASK_MAX_CONCURRENT)

def _cache_get(key):
    entry = _insight_cache.get(key)
    if entry and (datetime.utcnow() - entry[0]).total_seconds() < _INSIGHT_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    _insight_cache[key] = (datetime.utcnow(), value)


def _analysis_fingerprint(analysis) -> str:
    """A short hash of the figures an insight is written from, so a cached
    narrative can never outlive the numbers it describes."""
    import hashlib as _hl, json as _jfp
    keys = ("total_waste_cost_week", "monthly_waste_projection", "recoverable_monthly",
            "total_stock_value", "waste_rate_pct", "benchmark_label", "total_items")
    shape = {k: analysis.get(k) for k in keys}
    shape["waste_items"] = [(x.get("item"), x.get("waste_cost")) for x in (analysis.get("waste_items") or [])]
    shape["critical_low"] = [x.get("item") for x in (analysis.get("critical_low") or [])]
    return _hl.sha256(_jfp.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()[:12]


def invalidate_insight_cache(restaurant_id, prefixes=None):
    """Drop cached AI insight for one restaurant.

    Called when the underlying data changes. Without it a five-minute-old
    narrative sits next to freshly uploaded numbers and contradicts them.
    The food-cost keys carry a fingerprint of the analysis after the
    restaurant id, so they are matched by prefix rather than by equality.
    """
    prefixes = prefixes or ("labor-insight:", "mobile-labor-insight:",
                            "inv-insight:", "mobile-inv-insight:",
                            # Was missing, so a freshly-approved reply or a
                            # new review left the Reviews read stale for the
                            # rest of its TTL — and Home reads that same
                            # cache entry.
                            "review-insight:")
    suffix = str(restaurant_id)
    for key in [k for k in _insight_cache
                if any(k == p + suffix or k.startswith(p + suffix + ":") for p in prefixes)]:
        _insight_cache.pop(key, None)

# ── Shared handler bodies ────────────────────────────────────────────────────
# Plain, Flask-independent helpers behind the web (client_bp) routes below.
# Each returns (payload_dict, status_code) so both the web view (jsonify(**p),
# status) and mobile_api.py's mobile views can call the exact same logic
# without duplicating it.

def _do_approve(rid, restaurant_id):
    # The approve itself first, as a compare-and-set: only this restaurant's
    # live, drafted reply, and only once however many approves arrive
    # together (MOD-REV-4, MOD-REV-5). Nothing below — the action label, the
    # webhook, the Google post, the confirmation — happens for a loser.
    from models import claim_approval
    if not claim_approval(rid, restaurant_id):
        _gc = get_conn()
        _cur = _gc.execute("SELECT response_status, deleted_at FROM reviews WHERE id=? AND restaurant_id=?",
                           (rid, restaurant_id)).fetchone()
        _gc.close()
        if not _cur:
            return {"ok": False, "error": "Review not found"}, 404
        if _cur["deleted_at"]:
            return {"ok": False, "error": "That review was removed."}, 409
        if _cur["response_status"] in ("approved", "posted"):
            return {"ok": False, "error": "That reply has already been approved."}, 409
        return {"ok": False, "error": "There's no drafted reply to approve on that review."}, 409
    # Determine response action
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
    auto_posted, post_error = _attempt_google_post(rid, restaurant_id)
    try:
        from notify import fire_response_approved_alert
        fire_response_approved_alert(restaurant_id, rid, posted=auto_posted)
    except Exception:
        pass
    payload = {"ok": True, "auto_posted": auto_posted}
    if post_error:
        payload["post_error"] = post_error
    return payload, 200


def _attempt_google_post(rid, restaurant_id):
    """Synchronously tries to post a review's approved draft to Google.

    Used right after approving, and again from the "Retry posting" button
    on a review whose first attempt failed. Used to run in a background
    thread that fired-and-forgot the result — the HTTP response went out
    with auto_posted:True before Google had actually been asked, so the UI
    showed "Posted to Google" optimistically, and a failure (bad token,
    Google API error) was never seen by anyone, just printed to server
    logs. A review stuck at 'approved' then showed a static "Posting to
    Google" label forever with no way to tell "still working" from
    "silently failed" and no way to retry.

    Returns (auto_posted, post_error). auto_posted is True only once
    Google has actually accepted the reply. post_error carries the reason
    when an attempt was made and failed; it's None both on success and
    when nothing was attempted (not a Google review, no draft, or GBP
    isn't connected yet — none of those are failures).
    """
    try:
        from gmb import is_connected, post_reply
        conn = get_conn()
        row = conn.execute(
            "SELECT platform, draft_response, review_name FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, restaurant_id)
        ).fetchone()
        conn.close()
        if not (row and row["platform"] == "google" and row["review_name"] and row["draft_response"]):
            return False, None
        if not is_connected(restaurant_id):
            return False, None
        result = post_reply(restaurant_id, row["review_name"], row["draft_response"])
        if result["ok"]:
            from models import mark_posted
            mark_posted(rid)
            print(f"[GMB] Auto-posted review {rid} ✓")
            try:
                from webhooks import fire_webhook as _fw2
                _fw2(restaurant_id, "response.posted", {
                    "review_id": rid,
                    "platform": "google",
                    "author": row["review_name"],
                })
            except Exception:
                pass
            return True, None
        print(f"[GMB] Auto-post failed for review {rid}: {result['error']}")
        if result.get("removed"):
            # Google answered 404: the review is gone, so it leaves the queue
            # and the stats rather than sitting 'approved' forever (MOD-REV-14).
            try:
                _dc = get_conn()
                _dc.execute("UPDATE reviews SET deleted_at=datetime('now') WHERE id=? AND restaurant_id=?",
                            (rid, restaurant_id))
                _dc.commit()
                _dc.close()
            except Exception as _de:
                print(f"[GMB] could not retire removed review {rid}: {_de}")
        return False, result["error"]
    except Exception as e:
        # The owner reads post_error; the exception text (a connection pool
        # repr, a URL) goes to the failure digest instead (MOD-REV-15).
        print(f"[GMB] approve auto-post error: {e}")
        try:
            import ops
            ops.capture(e, job="review_post", context=f"restaurant_id={restaurant_id} review_id={rid}")
        except Exception:
            pass
        return False, ("Couldn't reach Google to post this reply. Nothing was lost — "
                       "use Retry posting in a few minutes.")


def _do_retry_post(rid, restaurant_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT response_status, platform FROM reviews WHERE id=? AND restaurant_id=?",
        (rid, restaurant_id)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "Review not found"}, 404
    if row["response_status"] != "approved" or row["platform"] != "google":
        return {"ok": False, "error": "Only an approved Google reply that hasn't posted yet can be retried."}, 400
    auto_posted, post_error = _attempt_google_post(rid, restaurant_id)
    payload = {"ok": True, "auto_posted": auto_posted}
    if post_error:
        payload["post_error"] = post_error
    return payload, 200


def _do_approve_all(restaurant_id, limit=25):
    """Publish every drafted reply in one go.

    Each review goes through the same _do_approve path a single approve
    uses — response_action, activity log, webhook, background Google post —
    so a bulk publish is never a shortcut. Capped at 25 per call; a bigger
    backlog takes another run.

    Lives here rather than in mobile_api so the web route and Ask Cavnar's
    proposal share one implementation. It was mobile-only, which meant the
    assistant's approve-all card pointed at a web URL that did not exist.
    """
    try:
        limit = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit = 25
    conn = get_conn()
    rows = conn.execute("""
        SELECT id FROM reviews
        WHERE restaurant_id=? AND response_status='drafted' AND deleted_at IS NULL
          AND draft_response IS NOT NULL AND TRIM(draft_response) != ''
        ORDER BY (urgency='high') DESC, review_date DESC, id DESC
        LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()

    approved = posted = failed = 0
    for row in rows:
        try:
            payload, status = _do_approve(row["id"], restaurant_id)
            if status == 200 and payload.get("ok"):
                approved += 1
                if payload.get("auto_posted"):
                    posted += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    remaining = 0
    try:
        conn = get_conn()
        remaining = conn.execute("""
            SELECT COUNT(*) FROM reviews
            WHERE restaurant_id=? AND response_status='drafted' AND deleted_at IS NULL
              AND draft_response IS NOT NULL AND TRIM(draft_response) != ''
        """, (restaurant_id,)).fetchone()[0] or 0
        conn.close()
    except Exception:
        pass

    if approved:
        try:
            from models import log_event
            log_event(restaurant_id, "reviews_bulk_approved", {"count": approved, "posted": posted})
        except Exception:
            pass
    return {"ok": True, "approved": approved, "posted": posted,
            "failed": failed, "remaining": int(remaining)}, 200



def _emails_mod():
    """Lazy import — emails.py reads RESEND_API_KEY at call time and importing
    it at module scope here would re-introduce the frozen-key bug."""
    import emails
    return emails


@client_bp.route("/api/reviews/approve-all", methods=["POST"])
@login_required
def approve_all_reviews_api(current_user):
    try:
        import home_brief; home_brief.invalidate(current_user["restaurant_id"])
    except Exception:
        pass
    data = request.get_json(silent=True) or {}
    payload, status = _do_approve_all(current_user["restaurant_id"], data.get("limit", 25))
    return jsonify(**payload), status


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
    try:
        # The reply is already gone from Google at this point — a failure
        # here (a locked db under concurrent writes, say) used to be an
        # unhandled exception straight to Flask's generic HTML error page,
        # which the client's fetch().then(r => r.json()) can't parse, so
        # every retract that hit this surfaced as a flat "Network error"
        # toast with nothing to go on, even though Google had already
        # accepted the retraction. Our own status just failed to catch up.
        revert_to_drafted(rid, restaurant_id)
    except Exception as e:
        return {"ok": False, "error": "Retracted from Google, but couldn't update our own status — refresh the page. (" + str(e) + ")"}, 500
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


@client_bp.route("/api/reviews/page")
@login_required
def reviews_page_api(current_user):
    """One page of the inbox, for the web's "Load more".

    The dashboard used to render every review the restaurant had ever
    received into the initial HTML document; this is what lets it render a
    page at a time instead. Returns rendered-ready dicts, the running
    offset and whether more remain.
    """
    from models import get_reviews_data, REVIEWS_PAGE_SIZE
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    rows, total = get_reviews_data(
        current_user["restaurant_id"],
        request.args.get("filter", "all"),
        request.args.get("search", ""),
        category=request.args.get("category") or None,
        platform=request.args.get("platform") or None,
        limit=REVIEWS_PAGE_SIZE, offset=offset, include_total=True,
    )
    restaurant = get_restaurant(current_user["restaurant_id"])
    html = "".join(
        render_template("_review_card.html", r=r, restaurant=restaurant, delay=0)
        for r in rows
    )
    return jsonify(ok=True, html=html, count=len(rows), total=total,
                   offset=offset + len(rows),
                   has_more=(offset + len(rows)) < total)


@client_bp.route("/api/reviews/<int:rid>/retry-post", methods=["POST"])
@login_required
def retry_post_review(rid, current_user):
    payload, status = _do_retry_post(rid, current_user["restaurant_id"])
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
    forecast, unverified) — the one place this parsing happens, shared by
    format_insight_html() (web, renders as HTML) and the mobile insight
    routes (return the same fields as JSON for native rendering)."""
    import re as _re
    if not text:
        return "Analysis unavailable.", [], None, None

    # Pull out a trailing "UNVERIFIED: ..." line before anything else.
    # ai_guard.verify_figures() appends this (via labor.py / competitor.py)
    # when the model stated a figure not backed by the numbers it was
    # given — it's a caveat about the passage as a whole, not a fourth
    # recommendation. Left in place, the numbered-recs split below has no
    # way to tell the difference and renders it as recommendation "4.".
    unverified = None
    umatch = _re.search(r'(?is)\n*unverified:\s*(.+)$', text)
    if umatch:
        unverified = umatch.group(1).strip().rstrip('.') or None
        text = text[:umatch.start()].strip()

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
            return text, [], forecast, unverified
        intro = ' '.join(para_lines).strip()
        recs = rec_lines

    clean_recs = []
    for rec in recs:
        clean = _re.sub(r'^[\d.\-)]+\s*', '', rec).strip()
        if clean:
            clean_recs.append(clean)
    return intro, clean_recs, forecast, unverified


def format_insight_html(text):
    if not text:
        return 'Analysis unavailable.'
    intro, recs, forecast, unverified = parse_insight_sections(text)

    forecast_html = ''
    if forecast:
        forecast_html = (
            '<div style="margin-top:10px;padding:10px 12px;background:rgba(200,75,47,.08);'
            'border-left:2px solid var(--ember);border-radius:0 6px 6px 0">'
            '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;'
            'color:var(--ember);margin-bottom:4px">Forecast</div>'
            '<div style="font-style:italic;line-height:1.6">' + forecast + '</div></div>'
        )

    # A caveat about the passage as a whole (a figure the model stated that
    # its own input didn't back up) — never a numbered recommendation, and
    # deliberately not styled like one, so it can't be mistaken for a
    # suggestion to act on.
    unverified_html = ''
    if unverified:
        unverified_html = (
            '<div style="margin-top:10px;padding:10px 12px;background:rgba(184,127,31,.08);'
            'border-left:2px solid var(--amber);border-radius:0 6px 6px 0">'
            '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;'
            'color:var(--amber);margin-bottom:4px">⚠ Unverified</div>'
            '<div style="line-height:1.6;color:var(--ink2)">Could not confirm ' + unverified
            + ' against your actual numbers.</div></div>'
        )

    if not recs:
        return '<p style="margin:0;line-height:1.7">' + intro + '</p>' + forecast_html + unverified_html

    html = ''
    if intro:
        html += '<p style="margin:0 0 10px 0;line-height:1.7">' + intro + '</p>'
    html += '<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin-bottom:8px">Recommendations</div>'
    num = 1
    for clean in recs:
        html += ('<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
            '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">'
            + str(num) +
            '</span><span style="line-height:1.6;color:#b7791f;font-weight:500">' + clean + '</span></div>')
        num += 1
    return html + forecast_html + unverified_html

def _do_review_stats(restaurant_id):
    from models import get_review_stats as _grs, get_restaurant as _gr
    try:
        stats = _grs(restaurant_id)
        # avg_rating is the average of the reviews Cavnar AI HOLDS, which on
        # a Google Business connection is the most recent page of them, not
        # the restaurant's whole history. gbp_rating is Google's own figure
        # over every review ever left. Both were being shown with neither
        # labelled, so two different ratings sat on one screen and an owner
        # had no way to tell which was their real one. They travel together
        # now, with the count each is computed over.
        r = _gr(restaurant_id)
        official = getattr(r, "gbp_rating", None) if r else None
        official_count = getattr(r, "gbp_review_count", None) if r else None
        stats["official_rating"] = float(official) if official else None
        stats["official_review_count"] = int(official_count) if official_count else None
        stats["official_rating_updated_at"] = getattr(r, "gbp_rating_updated_at", None) if r else None
        stats["sample_size"] = stats.get("total", 0)
        # True when we hold every review Google says exists.
        stats["is_full_history"] = bool(
            official_count and stats.get("total", 0) >= int(official_count))
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
        return jsonify(ok=False, error=_safe_err(e))

@client_bp.route("/api/changelog")
@login_required
def changelog_api(current_user):
    """Web twin — the one body is mobile_api.mobile_changelog."""
    return _m("mobile_changelog")(current_user)

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
    """Persists the DASHBOARD's dark-mode preference. It no longer affects
    email: reporter.py and notify.py used to read this column, so a dark
    dashboard silently produced dark digests and alerts while every other
    Cavnar AI email stayed a light card. Emails have one design now."""
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
    """Web twin — the one body is mobile_api.mobile_create_template."""
    return _m("mobile_create_template")(current_user)

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
        return jsonify(ok=False, error=_safe_err(e))

@client_bp.route("/api/sentiment-trend")
@login_required
def sentiment_trend_api(current_user):
    from models import get_sentiment_trend as _gst
    try:
        data = _gst(current_user["restaurant_id"], weeks=8)
        return jsonify(weeks=data)
    except Exception as e:
        return jsonify(weeks=[], error=_safe_err(e))

@client_bp.route("/api/review-insight")
@login_required
def review_insight_api(current_user):
    insight, status = _do_review_insight(current_user["restaurant_id"])
    return jsonify(**insight), status


def _verify_named_entities(generated: str, context: str) -> list:
    """Capitalised names in generated text that were never in its input.

    The `Do today` line's own worked example is "Respond to Amanda L.s 1-star
    review about cold food" — a guest named by name. The urgent-excerpt query
    selected `text` and not `author`, so no guest name reached the prompt at
    all, and the model was shown a demonstration of naming a person while
    holding no people to name. `verify_figures` could not catch it: a
    fabricated name is not a figure.

    Deliberately narrow. It matches a capitalised word only where the prose
    treats it as a person or a place — after a preposition or a possessive,
    or followed by a surname initial — so ordinary sentence-initial
    capitalisation and the platform names the prompt always carries do not
    trip it.
    """
    import re as _re_n
    known = {w.lower() for w in _re_n.findall(r"[A-Za-z][\w'-]+", context or "")}
    # Words that are capitalised in ordinary prose and are never a guest.
    _SKIP = {"google", "yelp", "monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday", "january", "february", "march",
             "april", "may", "june", "july", "august", "september", "october",
             "november", "december", "cavnar", "respond", "review", "reviews"}
    out = []
    patterns = (
        r"\b(?:from|by|to|for|with)\s+([A-Z][a-z]{2,})\b",   # "respond to Amanda"
        r"\b([A-Z][a-z]{2,})\s+[A-Z]\.",                       # "Amanda L."
        r"\b([A-Z][a-z]{2,})'s\b",                             # "Amanda's review"
    )
    for pat in patterns:
        for m in _re_n.finditer(pat, generated or ""):
            name = m.group(1)
            low = name.lower()
            if low in _SKIP or low in known or name in out:
                continue
            out.append(name)
    return out


def _do_review_insight(rid):
    """The Reviews module's AI read — shared by the web route above and
    mobile_api.py's own /reviews/insight.

    This used to be a summariser wearing a consultant's voice. Every
    substantive claim in its output had already been computed in Python before
    the model was called — `trend_str`, `persist_str`, `wow_str`, `issues_str`
    — so the model's whole remaining job was to rephrase four pre-formed
    strings at twenty words each. The deterministic pre-computation was good
    work, and it is still here; what was missing was the step after it.

    Three things changed:

      * The model is now handed the DIAGNOSIS (review_intelligence.diagnose) —
        a stored, evidence-cited root cause with an alternative explanation and
        a way to tell them apart — and the operational figures from the other
        modules, and is asked to connect them. It is reasoning over evidence
        rather than restating a sentence it was given.
      * Every claim carries a kind (ai_guard.CLAIM_KINDS) and the trend carries
        a real confidence (waste_trend's slope-agreement scorer), so a measured
        figure, an inference and a forecast stop arriving as three identical
        lines.
      * Guest names are now IN the prompt, and a name in the output that was
        not in the prompt is caught and flagged, the way an unsupported figure
        already was.

    The whole payload is cached, not just the insight string — the caveat
    flags used to be computed fresh and then thrown away on every cache hit,
    so the warning showed once and silently vanished for the next five
    minutes.
    """
    cached = _cache_get("review-insight:" + str(rid))
    if cached:
        return (dict(cached) if isinstance(cached, dict) else {"insight": cached}), 200
    try:
        import os, json
        import ai_utils as _aiu_ri
        from models import get_restaurant, get_review_stats, get_top_issues
        # The shared, bounded client (AI-1): a bare anthropic.Anthropic here
        # ran on the SDK's 600 s timeout and its own retries.
        _client_ri = _aiu_ri.get_client()
        restaurant = get_restaurant(rid)
        rstats = get_review_stats(rid)
        # sentiment=None: this line is labelled "Top topics" in the prompt,
        # not "complaints" — see get_top_issues' docstring.
        top_issues = get_top_issues(rid, days=90, limit=5, sentiment=None)
        from time_utils import restaurant_now
        now_chi = restaurant_now(restaurant)
        today_str = now_chi.strftime("%B %d, %Y")
        from models import get_conn as _gc_ri
        _conn_ri = _gc_ri()
        # Every window and every bucket key below reads the time a GUEST
        # wrote the review — COALESCE(NULLIF(review_date,''), fetched_at) —
        # not fetched_at, which is when Cavnar pulled it. On a first connect
        # an entire multi-year history arrives stamped with one fetched_at,
        # so "this week" was the whole history and the 4-week buckets were
        # one bar. Soft-deleted rows are excluded here too; every other
        # review query already excludes them, so the AI passage was the one
        # surface still describing reviews the owner had removed.
        from models import REVIEW_TIME_AXIS_BARE as _AXIS
        weekly_rows = _conn_ri.execute(f"""
            SELECT strftime('%Y-W%W', {_AXIS}) as week,
                   COUNT(*) as cnt,
                   ROUND(AVG(rating),2) as avg_r,
                   ROUND(SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END)*100.0/COUNT(*),1) as neg_pct
            FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL
              AND {_AXIS} >= datetime('now','-28 days')
            GROUP BY week ORDER BY week
        """, (rid,)).fetchall()
        this_week = _conn_ri.execute(f"""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as neg,
                   SUM(CASE WHEN urgency='high' AND response_status NOT IN ('posted','skipped') THEN 1 ELSE 0 END) as urgent
            FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL
              AND {_AXIS} >= datetime('now','-7 days')
        """, (rid,)).fetchone()
        last_week = _conn_ri.execute(f"""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r
            FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL
              AND {_AXIS} >= datetime('now','-14 days')
              AND {_AXIS} < datetime('now','-7 days')
        """, (rid,)).fetchone()
        # Topic persistence — issues appearing in 2+ of the last 4 weeks
        # categories is a JSON array; pull raw rows and parse in Python
        topic_rows = _conn_ri.execute(f"""
            SELECT categories, strftime('%Y-W%W', {_AXIS}) as week
            FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL
              AND {_AXIS} >= datetime('now','-28 days')
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
        # `author` and `id` travel with the text now. Without them the prompt
        # held no guest names at all while its own worked example named one,
        # and the model's only way to satisfy "never generic" was to make a
        # name up. See _verify_named_entities above.
        urgent_rows = _conn_ri.execute(f"""
            SELECT id, author, rating, text FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL AND urgency='high'
              AND response_status NOT IN ('posted','skipped')
            ORDER BY {_AXIS} DESC LIMIT 2
        """, (rid,)).fetchall()
        _conn_ri.close()

        # Build week-over-week string.
        # One shared floor for "is this enough data to call a direction"
        # (notify.MIN_TREND_REVIEWS_PER_WEEK), used by the week-over-week
        # line, the 4-week trend below it and the daily trend alert.
        from notify import MIN_TREND_REVIEWS_PER_WEEK as _MIN_WK
        wow_str = ""
        if (last_week and this_week
                and (last_week["cnt"] or 0) >= _MIN_WK and (this_week["cnt"] or 0) >= _MIN_WK):
            diff = (this_week["cnt"] or 0) - last_week["cnt"]
            rdiff = round(((this_week["avg_r"] or 0) - (last_week["avg_r"] or 0)), 1)
            wow_str = f"vs last week: {'+' if diff>=0 else ''}{diff} reviews, avg rating {'up' if rdiff>0 else 'down' if rdiff<0 else 'unchanged'} {abs(rdiff) if rdiff!=0 else ''}."

        # Build 4-week rating trend string.
        #
        # Only weeks with enough reviews behind them count. Without this a
        # week holding a single 5-star review followed by a week holding a
        # single 3-star one produced "Rating DECLINING 3 weeks straight —
        # flag this", which is a direction read off three data points that
        # each represent one guest. cnt was already selected above and was
        # simply never looked at.
        trend_weeks = [r for r in weekly_rows if (r["cnt"] or 0) >= _MIN_WK]
        trend_str = ""
        if len(trend_weeks) >= 3:
            ratings = [r["avg_r"] for r in trend_weeks if r["avg_r"]]
            if len(ratings) >= 3:
                if all(ratings[i] <= ratings[i+1] for i in range(len(ratings)-1)):
                    trend_str = f"Rating IMPROVING {len(ratings)} weeks straight ({ratings[0]}★ → {ratings[-1]}★)."
                elif all(ratings[i] >= ratings[i+1] for i in range(len(ratings)-1)):
                    trend_str = f"Rating DECLINING {len(ratings)} weeks straight ({ratings[0]}★ → {ratings[-1]}★). Flag this."
                else:
                    trend_str = f"Rating unstable last {len(ratings)} weeks: {' → '.join(str(r) + '★' for r in ratings)}."
            neg_pcts = [r["neg_pct"] for r in trend_weeks if r["neg_pct"] is not None]
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

        # Urgent reviews now reach the prompt with the guest's first name and
        # the review id, so "respond to X about Y" can name a real person.
        def _first_name(raw):
            parts = (raw or "").strip().split()
            first = parts[0] if parts else ""
            return first if len(first) > 1 and first.lower() not in (
                "a", "an", "the", "anonymous", "user", "google", "yelp", "local") else ""
        # The name and the excerpt are both guest-written, so both travel
        # inside the untrusted fence the note above the prompt describes
        # (AI-15) — they were quoted raw under a note about a fence that was
        # not there.
        from ai_guard import wrap_untrusted as _wrap_ri
        _urgent_lines = []
        for r in urgent_rows:
            who = _first_name(r["author"]) or "an unnamed guest"
            _urgent_lines.append(f'#{r["id"]} ({r["rating"]}★), guest name then review excerpt:\n'
                                 + _wrap_ri(f'{who}\n{(r["text"] or "")[:110]}'))
        urgent_texts = ("\n" + "\n".join(_urgent_lines)) if _urgent_lines else "none"
        issues_str = ", ".join(f"{i['label']} ({i['count']})" for i in top_issues) if top_issues else "no data"
        rest_name  = restaurant.name if restaurant else "this restaurant"

        # ── The consultant's evidence pack ──────────────────────────────────
        #
        # Everything below is the step the module never took. The old prompt
        # was handed four pre-written sentences (trend_str, persist_str,
        # wow_str, issues_str) and asked to rephrase them in twenty words
        # each; this one is handed the evidence those sentences were written
        # from, plus what the other modules recorded over the same period,
        # plus a stored root-cause diagnosis that had to cite real review ids
        # to exist at all.
        import review_intelligence as _ri
        _trend = _ri.rating_trend(rid)
        _ops_ctx = _ri.operational_context(rid)
        _money = _ri.revenue_at_risk(rid)
        _bench = _ri.competitor_benchmark(rid)
        _sev = _ri.severity_breakdown(rid)
        _parts = _ri.daypart_breakdown(rid)
        _locs = _ri.location_comparison(rid)
        # Diagnoses are READ here, not generated — generating would put a
        # Sonnet call per cluster on the critical path of every tab open. The
        # scheduler refreshes them daily; this reads whatever is current,
        # stale ones included, because a stale cause beats no cause and it
        # carries its own age.
        _diags = _ri.get_diagnoses(rid, include_stale=True)

        _ev = []
        if _trend["direction"]:
            _ev.append(f"Rating {_trend['direction']} {_trend['first']}★ to {_trend['latest']}★ "
                       f"across {_trend['weeks_above_floor']} weeks that clear the "
                       f"{_trend['min_reviews_per_week']}-review floor "
                       f"({_trend['confidence']} confidence).")
        elif _trend["reason"]:
            _ev.append(f"No rating direction: {_trend['reason']}.")
        for a in _trend["anomalies"][:2]:
            _ev.append(f"Week {a['week']} sat outside the series ({a['kind']}, "
                       f"{a['avg_rating']}★ on {a['count']} reviews).")
        if persist_str:
            _ev.append(persist_str)
        _open_sev = [t for t in _sev["tiers"] if t["open"] > 0
                     and t["key"] in ("safety", "legal", "operational")]
        if _open_sev:
            _ev.append("Open by severity: " + ", ".join(
                f"{t['open']} {t['label'].lower()}" for t in _open_sev) + ".")
        _hot_days = [d for d in _parts["by_weekday"] if d["negative_pct"] is not None][:2]
        if _hot_days:
            _ev.append("Worst weekdays by negative share: " + ", ".join(
                f"{d['weekday']} {d['negative_pct']}% of {d['total']}" for d in _hot_days) + ".")
        _hot_parts = [d for d in _parts["by_daypart"] if d["negative_pct"] is not None][:2]
        if _hot_parts:
            _ev.append("By daypart: " + ", ".join(
                f"{d['daypart'].replace('_',' ')} {d['negative_pct']}% of {d['total']}"
                for d in _hot_parts) + ".")
        if _bench.get("available") and _bench.get("gap_vs_median") is not None:
            _ev.append(f"Against the {_bench['competitor_count']} competitors Intel tracks: "
                       f"you are {_bench['our_rating_90d']}★ over 90 days vs a "
                       f"{_bench['competitor_median']}★ median "
                       f"({_bench['gap_vs_median']:+.2f}), intel as of {_bench.get('as_of') or 'unknown'}.")
        if _locs.get("available") and _locs.get("outlier_themes"):
            _o = _locs["outlier_themes"][0]
            _ev.append(f"Across your locations, {_o['category'].replace('_',' ')} is "
                       f"{int(_o['our_share']*100)}% of this location's complaints vs "
                       f"{int(_o['peer_share']*100)}% at the others.")
        if _money.get("available"):
            _ev.append(f"Revenue implication of the {_money['rating_delta']:+.2f}-star 30-day move: "
                       f"${abs(_money['monthly_low']):,} to ${abs(_money['monthly_high']):,} a month "
                       f"{'at risk' if _money['direction']=='at_risk' else 'of upside'}, "
                       f"on {_money['sales_source']}. A forecast from a published range, not a measurement.")
        evidence_block = "\n".join(f"- {e}" for e in _ev) if _ev else "- (nothing above the evidence floor)"

        ops_block = _ri._operational_block(_ops_ctx)

        if _diags:
            _d = _diags[0]
            diag_block = (
                f"Theme: {_d['category'].replace('_',' ')} ({_d['mention_count']} negative reviews)\n"
                f"Most likely cause: {_d['cause']}\n"
                f"Alternative: {_d['alternative_cause'] or 'none offered'}\n"
                f"How to tell them apart: {_d['what_would_confirm'] or 'not established'}\n"
                f"Recommended: {_d['recommended_action'] or 'none'}\n"
                f"Confidence: {_d['confidence']} | rests on reviews "
                + ", ".join("#" + str(i) for i in _d['evidence_review_ids'][:5])
                + (f" | produced {int(_d['age_hours'])}h ago" if _d.get("age_hours") is not None else ""))
        else:
            diag_block = ("(No root-cause diagnosis exists yet - either no complaint cluster "
                          "clears the evidence floor, or the diagnosis pass has not run. Do NOT "
                          "invent a cause. Say what the reviews show and stop.)")

        has_trend = bool(_trend["direction"] in ("improving", "declining")
                         and _trend["confidence"] in ("high", "medium"))
        has_diag = bool(_diags)
        forecast_line = (
            "\n\U0001f52e Next week: [1 sentence on where the rating trend is headed IF it "
            "continues. Say 'if nothing changes'. This is a projection, not a measurement.]"
        ) if has_trend else ""
        why_line = (
            "\n\U0001f50d Why: [1-2 sentences naming the most likely OPERATIONAL cause from the "
            "DIAGNOSIS block, what else it could be, and the one thing that would tell them "
            "apart. Use that diagnosis - do not substitute a different cause.]"
        ) if has_diag else ""
        from ai_guard import UNTRUSTED_NOTE as _UN_RI
        prompt = (
            "You are an experienced restaurant operations consultant writing the daily read on "
            "this restaurant's reviews. You are not a summariser: the owner can already see their "
            "counts and their star average on the same screen. Your value is the step after the "
            "count - what it means operationally, how sure you are, and what to do about it.\n\n"
            f"{_UN_RI}\n\n"
            f"Restaurant: {rest_name} | Today: {today_str}\n\n"
            "MEASURED (read from the database - these are facts):\n"
            f"- {rstats['total']} reviews all time, {rstats['avg_rating']} star lifetime average\n"
            f"- {rstats['positive']} positive / {rstats['negative']} negative / "
            f"{rstats['neutral']} neutral of {rstats['classified']} analysed"
            + (f" ({rstats['unanalysed']} not yet analysed, so the split covers less than the total)"
               if rstats.get("unanalysed") else "") + "\n"
            f"- {rstats['urgent']} urgent and unresolved | response rate {rstats['response_rate']}%\n"
            f"- Topics guests raise: {issues_str}\n"
            + (f"- {wow_str}\n" if wow_str else "")
            + f"- Urgent reviews awaiting a reply: {urgent_texts}\n\n"
            "DERIVED (computed from those facts, each carrying its own confidence):\n"
            f"{evidence_block}\n\n"
            "WHAT THE OTHER MODULES RECORDED OVER THE SAME PERIOD:\n"
            f"{ops_block}\n\n"
            "DIAGNOSIS (a stored root-cause pass over the largest complaint cluster):\n"
            f"{diag_block}\n\n"
            "EVIDENCE RULES - these bound what you may claim:\n"
            "- State no figure that does not appear above. Not a dollar amount, not a percentage, "
            "not a count, not a rating.\n"
            "- Name a guest ONLY from the urgent-review list above, using the name exactly as it "
            "is written there. If that list says 'none', name no one at all.\n"
            "- You may connect reviews to another module's figure ONLY by naming that figure. If "
            "the other-modules section says there is no data, you have no operational evidence - "
            "say what the reviews show and stop there.\n"
            "- Never assert a cause that is not in the DIAGNOSIS block. If there is no diagnosis, "
            "there is no cause for you to state.\n"
            "- Keep measured, inferred and projected apart. A confidence level given above travels "
            "with the claim it belongs to: if a trend is low confidence, say so rather than "
            "stating it flat.\n"
            "- Prioritise a multi-week pattern over a single-week blip, and a serious complaint "
            "over a merely frequent one.\n\n"
            "Return EXACTLY these lines, in this order, no markdown, no preamble, no extra lines:\n"
            "\U0001f4ca This week: [1 sentence on the most important MEASURED fact. Max 22 words.]"
            f"{why_line}\n"
            "\u26a0\ufe0f Watch: [1 sentence on the biggest risk and how confident you are in it. "
            "Omit this line entirely if nothing clears the evidence floor. Max 22 words.]\n"
            "\u2705 Do today: [1 concrete action a manager can start this shift with the staff and "
            "menu they already have. If a guest is named above, name them. Never generic. Max 22 words.]"
            f"{forecast_line}"
        )

        from ai_utils import create_with_retry, extract_text, model_for
        msg = create_with_retry(
            _client_ri,
            # A consultant's read is worth a bigger model than a rephrase was.
            # Haiku was adequate when the job was restating four pre-written
            # sentences; connecting a complaint cluster to a labor figure and
            # saying how sure it is, is not that job.
            model=model_for("review_insight"),
            max_tokens=520,
            messages=[{"role":"user","content":prompt}],
            restaurant_id=rid,
            action="review_insight",
        )
        insight = extract_text(msg).strip()
        # Strip any markdown
        import re as _re_ri
        insight = _re_ri.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), insight)
        insight = _re_ri.sub(r'\*(.+?)\*',   lambda m: m.group(1), insight)
        # Figures the model states have to be figures it was handed. Kept
        # rather than dropped — this is on-screen text the owner is reading
        # now, so it carries a flag instead of a hole — but the flag is what
        # lets the UI stop presenting an unverified number as a fact.
        from ai_guard import verify_figures, CLAIM_KINDS
        _unsupported = verify_figures(insight, prompt, "review_insight", rid)
        # A name the model wrote that was never in its input. verify_figures
        # cannot see this — a fabricated guest is not a figure — and it is the
        # most damaging thing this passage can get wrong, because the whole
        # premise of the panel is that Cavnar has read the reviews.
        _invented_names = _verify_named_entities(insight, prompt)
        if _invented_names:
            try:
                import ops as _ops_ri
                _ops_ri.capture(
                    RuntimeError(f"review_insight named {_invented_names[:3]} — not in its input"),
                    job="review_insight", context=f"restaurant_id={rid}")
            except Exception:
                pass

        # What kind of claim each part of this payload is making.
        # ai_guard.CLAIM_KINDS was written for exactly this and is already
        # shipped by Intel and Food Cost; Reviews sent a measured figure, an
        # inference and a projection as three identical lines of prose.
        _kinds = {"this_week": "measured", "watch": "inferred",
                  "do_today": "suggestion", "rating_trend": "computed",
                  "severity_breakdown": "measured", "dayparts": "measured"}
        if has_diag:
            _kinds["why"] = "inferred"
        if has_trend:
            _kinds["next_week"] = "forecast"
        if _money.get("available"):
            _kinds["revenue_at_risk"] = "forecast"
        if _bench.get("available"):
            _kinds["benchmark"] = "measured"
        _kinds = {k: v for k, v in _kinds.items() if v in CLAIM_KINDS}

        payload = {
            "insight": insight,
            "figures_verified": not _unsupported,
            "unsupported_figures": _unsupported,
            "names_verified": not _invented_names,
            "unsupported_names": _invented_names,
            "claim_kinds": _kinds,
            "confidence": _trend.get("confidence"),
            "trend": {k: _trend[k] for k in
                      ("direction", "confidence", "change", "first", "latest",
                       "weeks_above_floor", "reason", "anomalies")},
            "diagnosis": _diags[0] if _diags else None,
            "diagnoses": _diags[:3],
            "revenue_at_risk": _money,
            "benchmark": _bench,
            "severity": _sev,
            "dayparts": _parts,
            "locations": _locs,
            "operational_context": _ops_ctx,
            "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
            "stale": False,
        }
        # The WHOLE payload is cached now, not just the insight string.
        # Caching the string meant every caveat — unverified figures,
        # unverified names, the trend's confidence, the diagnosis's own age —
        # was computed, rendered once, and then silently dropped for the next
        # five minutes while the text it qualified kept being shown.
        _cache_set("review-insight:" + str(rid), payload)
        return payload, 200
    except Exception as _re:
        import traceback
        print(f"[review-insight ERROR] {_re}\n{traceback.format_exc()}")
        stale = _insight_cache.get("review-insight:" + str(rid))
        if stale:
            # This path deliberately bypasses the TTL — a stale read beats no
            # read — so it has to say how old it is. ai_guard.freshness exists
            # for exactly this: text written some time ago read identically to
            # text written this morning, because the claims never carried a
            # date. stale_after_days=0 because anything on this path is, by
            # definition, past its window.
            from ai_guard import freshness as _fresh_ri
            body = stale[1]
            out = dict(body) if isinstance(body, dict) else {"insight": body}
            out.update(_fresh_ri(stale[0].isoformat(timespec="seconds"), stale_after_days=0))
            out["stale"] = True
            return out, 200
        # The owner reads `insight`: a budget stop or an outage says so rather
        # than "check back shortly" (AI-11), and no raw exception text — a
        # provider error body with its request id — reaches the client (AI-31).
        from ai_utils import insight_error as _insight_err_ri
        _msg_ri, _status_ri = _insight_err_ri(_re)
        return {"insight": _msg_ri, "error": _msg_ri}, _status_ri

def _do_recent_topics(rid):
    """The last few things this restaurant generated, folded by topic, with
    whether each one actually got posted and what it did. Shared with
    mobile_api.py — the app's Analytics tab had no equivalent at all, so a
    post's real numbers were only ever visible on the web.

    The per-request ALTER TABLEs that used to sit here added reach/likes/
    comments as TEXT, contradicting the canonical INTEGER schema in models.py
    (see the comment above marketing_content_log there). They were no-ops on
    every database that had already been migrated, and wrong on any that
    hadn't — models.py owns this table's shape."""
    try:
        from models import get_conn
        conn = get_conn()
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
        return {"topics": seen}
    except Exception:
        return {"topics": []}


@client_bp.route("/api/recent-topics")
@login_required
def recent_topics_api(current_user):
    return jsonify(**_do_recent_topics(current_user["restaurant_id"]))

@client_bp.route("/api/mkt-stats")
@login_required
def mkt_stats_api(current_user):
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        gen   = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (rid,)).fetchone()[0] or 0
        pub   = conn.execute("SELECT COUNT(DISTINCT topic) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)).fetchone()[0] or 0
        month = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)).fetchone()[0] or 0
        conn.close()
        return jsonify(ok=True, generated=gen, published=pub, this_month=month)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))

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
        return jsonify(ok=False, error=_safe_err(e))

def _parse_conversation_id(raw):
    """A conversation id from a request body: a positive int, or None when
    absent. Anything else (a string, zero, a float) is treated as absent
    rather than rejected — the chat then lands in the current conversation."""
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None
    return cid if cid > 0 else None


def _resolve_ask_conversation(restaurant_id, conversation_id, new_conversation=False, user_id=None):
    """For a question: which chat it belongs in. An explicit id must be one
    of this restaurant's own AND one this person can see (404 otherwise — the
    caller never learns which of the two it failed); `new_conversation` starts
    a fresh chat for this question (the app's "New chat" — created here, on
    the first question, so an untouched New chat never leaves an empty row in
    the history); no id means "my current chat, or a fresh one if there are
    none yet". Returns (conversation_id, error_payload).

    `user_id` is the viewer, and it is passed through to models as viewer_id
    rather than only being stamped on new rows. list/get/delete were already
    viewer-scoped; this path was not, so an invited teammate who supplied a
    conversation_id got the owner's chat resolved, replayed into the prompt,
    and answered back to them — the exact leak _viewer_clause exists to stop,
    through the one door that never asked it. Ask transcripts carry labor
    cost, food cost and revenue in plain text.
    """
    from models import get_ask_conversation, current_ask_conversation_id, create_ask_conversation
    if new_conversation:
        return create_ask_conversation(restaurant_id, user_id=user_id), None
    if conversation_id is not None:
        if get_ask_conversation(restaurant_id, conversation_id, viewer_id=user_id) is None:
            return None, ({"ok": False, "error": "That conversation doesn't exist."}, 404)
        return conversation_id, None
    return current_ask_conversation_id(restaurant_id, viewer_id=user_id), None


def _ask_rate_key(restaurant_id, user_id):
    """One bucket per person. Falls back to the restaurant when a caller has
    no user id, which is the old behaviour and still bounded."""
    return f"askcavnar:{restaurant_id}:{user_id}" if user_id else f"askcavnar:{restaurant_id}"


def _ask_meta(meta):
    """The answer's provenance, in the shape both clients read.

    Kept to what a UI can actually act on: which modules were consulted (an
    evidence panel and a drill-down), how confident the answer is, and
    whether any figure in it failed verification against what the model was
    handed. `unverified_figures` being non-empty is the signal to caveat the
    numbers on screen rather than to hide the answer — see ai_guard's note on
    interactive vs unattended text.
    """
    meta = meta or {}
    return {
        "modules_consulted": meta.get("modules_consulted") or [],
        "tools_used": meta.get("tools_used") or [],
        "confidence": meta.get("confidence") or "unknown",
        "unverified_figures": meta.get("unverified_figures") or [],
        "depth": meta.get("depth") or "standard",
    }


def _do_ask_cavnar(restaurant_id, question, history=None, user_id=None, conversation_id=None,
                   new_conversation=False, brief=False, user=None):
    """The AI copilot's shared body — answers a plain-English question about
    the restaurant's own live data (reviews/labor/food cost/marketing,
    whichever modules are active) instead of the owner having to piece it
    together across tabs. Used by both the web panel and the mobile app.

    `history` is the caller's own prior message list for this chat session
    (list of {"role", "content"} dicts, oldest first, NOT including
    `question`) — ask_with_tools sanitizes/caps it itself, so this is a thin
    passthrough, not a second place that needs to re-validate it.

    `conversation_id` picks the chat the turn belongs to (the app's chat
    history); None means the restaurant's current chat."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Ask a question first."}, 400
    if len(question) > 2000:
        return {"ok": False, "error": "That question is too long — try to keep it under 2000 characters."}, 400
    from ai_utils import ai_rate_limited
    # Keyed per PERSON, not per restaurant. A restaurant-wide bucket meant an
    # owner and an invited teammate asking at the same time throttled each
    # other, and the bucket is a spend guard on one human's typing speed, not
    # on the account. The account-level ceiling is ai_budget_exceeded, which
    # create_with_retry already enforces on every call.
    if ai_rate_limited(_ask_rate_key(restaurant_id, user_id), max_calls=5, window_secs=60):
        return {"ok": False, "error": "Too many questions — please wait a moment and try again."}, 429
    # After the rate limit, so a hammered "New chat" can't mint 50 empty rows.
    conversation_id, err = _resolve_ask_conversation(restaurant_id, conversation_id,
                                                     new_conversation=new_conversation, user_id=user_id)
    if err:
        return err
    try:
        from ask_cavnar import ask_with_tools
        from models import save_ask_message, get_ask_history, log_ask_action
        restaurant = get_restaurant(restaurant_id)
        if not restaurant:
            return {"ok": False, "error": "Restaurant not found"}, 404

        # Stored history is the source of truth so the conversation survives
        # a reload or an app restart; a client-supplied list is still honoured
        # for callers that haven't migrated.
        if history is None:
            # viewer_id, not just conversation_id: replaying a transcript into
            # the prompt is a read of it, and it has to obey the same scoping
            # the history endpoint does.
            history = [{"role": h["role"], "content": h["content"]}
                       for h in get_ask_history(restaurant_id, conversation_id=conversation_id,
                                                viewer_id=user_id)]

        # `user` scopes what the answer may draw on to what this login's role
        # can read — a manager never gets food cost through Ask either.
        answer, truncated, proposals, meta = ask_with_tools(
            restaurant, question, history=history, user=user, **({'brief': True} if brief else {}))

        try:
            conversation_id = save_ask_message(restaurant_id, "user", question, user_id=user_id,
                                               conversation_id=conversation_id)
            save_ask_message(restaurant_id, "assistant", answer,
                             proposals=proposals or None, user_id=user_id,
                             conversation_id=conversation_id)
            for p in (proposals or []):
                log_ask_action(restaurant_id, p["action"], summary=p.get("summary"),
                               body=p.get("body"), outcome="proposed", user_id=user_id)
        except Exception as e:
            # Never fail a good answer because the transcript couldn't be written.
            import ops
            ops.capture(e, job="ask_cavnar_persist", context=f"restaurant_id={restaurant_id}")

        # `truncated` tells the client the answer stopped at max_tokens rather
        # than finishing, so it can say so instead of presenting a half
        # sentence as complete advice. `proposals` are confirm cards — actions
        # the assistant wants to take and deliberately cannot take itself.
        # `conversation_id` tells a client that started a fresh chat which
        # one it is now in. `modules_consulted` / `unverified_figures` /
        # `confidence` are what the answer rests on — without them on the wire
        # no client could ever render an evidence panel or a confidence chip,
        # however good the answer was.
        return {"ok": True, "answer": answer, "truncated": truncated,
                "proposals": proposals or [], "conversation_id": conversation_id,
                **_ask_meta(meta)}, 200
    except Exception as e:
        from ai_utils import AIBudgetExceeded, AIRefused, user_facing_error
        msg, status = user_facing_error(e)
        # A budget stop is a decision this product made on purpose, not a
        # fault — it does not belong in the failure digest beside real ones.
        # Nor does a refusal: nothing broke, and it is not saved as an answer.
        if not isinstance(e, (AIBudgetExceeded, AIRefused)):
            import ops
            ops.capture(e, job="ask_cavnar", context=f"restaurant_id={restaurant_id}")
        return {"ok": False, "error": msg}, status


@client_bp.route("/api/ask-cavnar", methods=["POST"])
@login_required
def ask_cavnar_api(current_user):
    rid = current_user["restaurant_id"]
    data = request.get_json() or {}
    payload, status = _do_ask_cavnar(rid, data.get("question"), history=data.get("history"), brief=(data.get("surface") == "home"),
                                     user_id=current_user.get("id"),
                                     conversation_id=_parse_conversation_id(data.get("conversation_id")),
                                     new_conversation=bool(data.get("new_conversation")),
                                     user=current_user)
    return jsonify(**payload), status


def _ask_cavnar_stream_response(rid, uid, question, conversation_id=None, new_conversation=False, brief=False,
                                user=None):
    """Server-sent events: progress while tools run, then the answer.

    Shared by the web and mobile stream routes so iOS gets the same live
    "Reading your reviews" progress the web client already shows, instead
    of a spinner for however long the tool loop takes.

    The tool loop can take several round trips, and a silent spinner for
    that long reads as broken. Streaming the tool labels shows the work
    rather than token-by-token text, which is both more useful here and far
    simpler than threading a token stream through a loop that pauses for
    tool results.
    """
    import queue
    import threading

    question = (question or "").strip()
    if not question:
        return jsonify(ok=False, error="Ask a question first."), 400
    if len(question) > 2000:
        return jsonify(ok=False, error="That question is too long — keep it under 2000 characters."), 400
    from ai_utils import ai_rate_limited
    if ai_rate_limited(_ask_rate_key(rid, uid), max_calls=5, window_secs=60):
        return jsonify(ok=False, error="Too many questions — please wait a moment."), 429
    conversation_id, err = _resolve_ask_conversation(rid, conversation_id,
                                                     new_conversation=new_conversation, user_id=uid)
    if err:
        payload, status = err
        return jsonify(**payload), status

    events = queue.Queue()

    def work():
        cid = conversation_id
        try:
            from ask_cavnar import ask_with_tools
            from models import save_ask_message, get_ask_history, log_ask_action
            restaurant = get_restaurant(rid)
            if not restaurant:
                events.put({"type": "error", "error": "Restaurant not found"})
                return
            history = [{"role": h["role"], "content": h["content"]}
                       for h in get_ask_history(rid, conversation_id=cid, viewer_id=uid)]
            answer, truncated, proposals, meta = ask_with_tools(
                restaurant, question, history=history, user=user,
                on_progress=lambda label, state: events.put(
                    {"type": "progress", "label": label, "state": state}),
                **({"brief": True} if brief else {}))
            try:
                cid = save_ask_message(rid, "user", question, user_id=uid, conversation_id=cid)
                save_ask_message(rid, "assistant", answer, proposals=proposals or None,
                                 user_id=uid, conversation_id=cid)
                for p in (proposals or []):
                    log_ask_action(rid, p["action"], summary=p.get("summary"),
                                   body=p.get("body"), outcome="proposed", user_id=uid)
            except Exception:
                pass
            events.put({"type": "answer", "answer": answer,
                        "truncated": truncated, "proposals": proposals or [],
                        "conversation_id": cid, **_ask_meta(meta)})
        except Exception as e:
            from ai_utils import AIBudgetExceeded, AIRefused, user_facing_error
            msg, _status = user_facing_error(e, "Couldn't get an answer right now — try again.")
            if not isinstance(e, (AIBudgetExceeded, AIRefused)):
                import ops
                ops.capture(e, job="ask_cavnar_stream", context=f"restaurant_id={rid}")
            events.put({"type": "error", "error": msg})
        finally:
            _ASK_SLOTS.release()
            events.put(None)

    # Bounded. Every Ask request used to spawn an unbounded daemon thread;
    # concurrency was limited only incidentally, by gunicorn's four request
    # threads. That coupling is not a design — raise --threads, or add a
    # client that opens several streams, and thread creation is unbounded
    # against a model API with its own rate limits. The semaphore makes the
    # ceiling explicit and releases in the worker's finally.
    if not _ASK_SLOTS.acquire(blocking=False):
        def _busy():
            events.put({"type": "error", "error": (
                "Cavnar is answering a few other questions right now — "
                "try again in a moment.")})
            events.put(None)
        threading.Thread(target=_busy, daemon=True).start()
    else:
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as _te:
            # Thread creation itself failing is the memory-pressure case this
            # semaphore exists for, and it is the one path where the slot is
            # held by a worker that will never reach its finally.
            _ASK_SLOTS.release()
            import ops
            ops.capture(_te, job="ask_cavnar_stream", context=f"restaurant_id={rid}")

            def _failed():
                events.put({"type": "error", "error": (
                    "Cavnar is under heavy load right now — try again in a moment.")})
                events.put(None)
            threading.Thread(target=_failed, daemon=True).start()

    def generate():
        while True:
            item = events.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@client_bp.route("/api/ask-cavnar/stream", methods=["POST"])
@login_required
def ask_cavnar_stream(current_user):
    data = request.get_json(silent=True) or {}
    return _ask_cavnar_stream_response(
        current_user["restaurant_id"], current_user.get("id"), data.get("question"),
        conversation_id=_parse_conversation_id(data.get("conversation_id")),
        new_conversation=bool(data.get("new_conversation")),
        brief=(data.get("surface") == "home"), user=current_user)


@client_bp.route("/api/ask-cavnar/history")
@login_required
def ask_cavnar_history(current_user):
    from models import get_ask_history
    return jsonify(ok=True, messages=get_ask_history(current_user["restaurant_id"],
                                                     viewer_id=current_user.get("id")))


@client_bp.route("/api/ask-cavnar/history", methods=["DELETE"])
@login_required
def ask_cavnar_clear_history(current_user):
    from models import clear_ask_history
    clear_ask_history(current_user["restaurant_id"])
    return jsonify(ok=True)


# ── Chat history: one row per conversation ────────────────────────────────
#
# Shared bodies, so the web and mobile routes can't drift. Every one is
# scoped to the caller's restaurant inside models — an id that belongs to
# someone else reads as "doesn't exist", never as theirs.

def _do_list_ask_conversations(restaurant_id, viewer_id=None):
    from models import list_ask_conversations
    return {"ok": True,
            "conversations": list_ask_conversations(restaurant_id, viewer_id=viewer_id)}, 200


def _do_create_ask_conversation(restaurant_id, user_id=None):
    from models import create_ask_conversation
    cid = create_ask_conversation(restaurant_id, user_id=user_id)
    return {"ok": True, "conversation_id": cid}, 200


def _do_get_ask_conversation(restaurant_id, conversation_id, viewer_id=None):
    from models import get_ask_conversation, get_ask_history, _ASK_TRANSCRIPT_KEEP
    convo = get_ask_conversation(restaurant_id, conversation_id, viewer_id=viewer_id)
    if convo is None:
        return {"ok": False, "error": "That conversation doesn't exist."}, 404
    messages = get_ask_history(restaurant_id, limit=_ASK_TRANSCRIPT_KEEP,
                               conversation_id=conversation_id, viewer_id=viewer_id)
    return {"ok": True, "conversation": convo, "messages": messages}, 200


def _do_delete_ask_conversation(restaurant_id, conversation_id, viewer_id=None):
    from models import delete_ask_conversation
    if not delete_ask_conversation(restaurant_id, conversation_id, viewer_id=viewer_id):
        return {"ok": False, "error": "That conversation doesn't exist."}, 404
    return {"ok": True}, 200


@client_bp.route("/api/ask-cavnar/conversations")
@login_required
def ask_cavnar_conversations(current_user):
    payload, status = _do_list_ask_conversations(current_user["restaurant_id"],
                                                 viewer_id=current_user.get("id"))
    return jsonify(**payload), status


@client_bp.route("/api/ask-cavnar/conversations", methods=["POST"])
@login_required
def ask_cavnar_new_conversation(current_user):
    payload, status = _do_create_ask_conversation(current_user["restaurant_id"], current_user.get("id"))
    return jsonify(**payload), status


@client_bp.route("/api/ask-cavnar/conversations/<int:conversation_id>")
@login_required
def ask_cavnar_conversation(current_user, conversation_id):
    payload, status = _do_get_ask_conversation(current_user["restaurant_id"], conversation_id,
                                               viewer_id=current_user.get("id"))
    return jsonify(**payload), status


@client_bp.route("/api/ask-cavnar/conversations/<int:conversation_id>", methods=["DELETE"])
@login_required
def ask_cavnar_delete_conversation(current_user, conversation_id):
    payload, status = _do_delete_ask_conversation(current_user["restaurant_id"], conversation_id,
                                                  viewer_id=current_user.get("id"))
    return jsonify(**payload), status


def _do_record_ask_action(restaurant_id, user_id, data):
    """Record what the owner did with a proposal.

    The confirmed action itself is executed by the client calling the same
    route its button already uses — this only writes the audit line, so
    there is still exactly one code path that can send a supplier order.
    """
    from models import log_ask_action, save_ask_message
    action = (data.get("action") or "").strip()
    outcome = (data.get("outcome") or "").strip()
    if not action or outcome not in ("confirmed", "dismissed"):
        return {"ok": False, "error": "action and outcome (confirmed|dismissed) are required"}, 400
    log_ask_action(restaurant_id, action, summary=data.get("summary"),
                   body=data.get("body"), outcome=outcome, user_id=user_id)
    # The snapshot is cached for a minute and now carries what has already
    # been proposed — so confirming an order and immediately asking "did that
    # go out?" would otherwise be answered from a context assembled before
    # the confirmation existed. Same reason home_brief invalidates on dismiss.
    try:
        import ask_cavnar
        ask_cavnar.invalidate_context(restaurant_id)
    except Exception:
        pass

    # Also write it into the transcript. Without this the model never learns
    # what happened to its own proposal: asked "did that order go out?" after
    # the owner confirmed, it answered "no, nothing has been sent" — stating a
    # false fact about the account with complete confidence. Recorded as a
    # user turn because confirming genuinely is the owner's action, and it
    # keeps the user/assistant alternation the Messages API expects. Lands in
    # the chat the proposal came from when the client says which.
    try:
        label = data.get("summary") or action.replace("_", " ")
        verb = "Confirmed" if outcome == "confirmed" else "Dismissed"
        save_ask_message(restaurant_id, "user", f"[{verb}: {label}]", user_id=user_id,
                         conversation_id=_parse_conversation_id(data.get("conversation_id")))
    except Exception:
        pass
    return {"ok": True}, 200


@client_bp.route("/api/ask-cavnar/action", methods=["POST"])
@login_required
def ask_cavnar_record_action(current_user):
    data = request.get_json(silent=True) or {}
    payload, status = _do_record_ask_action(current_user["restaurant_id"], current_user.get("id"), data)
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
            '\nFORECAST: one short sentence on where reach is heading, based only on the trend above.'
        ) if has_trend else ""
        # Written to the shape parse_insight_sections() actually reads: a
        # one-line intro, then numbered recommendations, then an optional
        # FORECAST line. It used to ask for "two short paragraphs", which the
        # parser has nowhere to put — the whole brief landed in `intro` and
        # rendered as a wall of prose filling the sheet. The point of the
        # consultant is a glance, not a read.
        prompt = f"""You are the Cavnar AI Marketing Consultant for {name}.

Restaurant: {p["name"]} in {p["neighborhood"]}.
Vibe: {p["vibe"]}.
Known for: {p["known_for"]}.
Brand voice: {p["voice"]}.
{menu_clause}
{never_clause}
Upcoming holidays in the next 30 days: {upcoming if upcoming else "none"}.
Recent content already generated (do NOT repeat these): {recent_str}.{perf_clause}

Return EXACTLY this shape and nothing else:

Line 1: "{greeting}" followed by ONE sentence, 20 words maximum, naming the single
biggest marketing opportunity this week. This line is read on its own on a
small screen — it has to stand alone.

Then a blank line, then 2 recommendations, numbered "1." and "2.", each ONE
sentence of 15 words or less. Each is a concrete thing to post or do, with a
specific angle — reference a real menu item or a named holiday where it fits.
No preamble on them, no closing encouragement, no sign-off.{forecast_instruction}

Tone: warm, direct, a trusted advisor who knows the owner is busy. Match the
brand voice. No corporate language. The whole brief must be under 60 words."""
        import anthropic as _anth
        from ai_utils import create_with_retry, extract_text, model_for
        _client = _anth.Anthropic(api_key=__import__("os").getenv("ANTHROPIC_API_KEY"))
        msg = create_with_retry(
            _client,
            model=model_for("marketing_insight"),
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=rid,
            action="marketing_insight",
        )
        insight = extract_text(msg).strip()
        from ai_guard import verify_figures
        _unsupported = verify_figures(insight, prompt, "marketing_insight", rid)
        result = insight if raw else format_insight_html(insight)
        _cache_set(cache_key, result)
        return {"insight": result, "figures_verified": not _unsupported,
                "unsupported_figures": _unsupported}, 200
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[MktInsight] ERROR: {str(e)}")
        stale = _insight_cache.get(cache_key)
        if stale:
            return {"insight": stale[1]}, 200
        # A budget stop or outage says so (AI-11); anything else keeps the
        # retry wording.
        from ai_utils import insight_error as _insight_err_mkt
        _msg_mkt, _status_mkt = _insight_err_mkt(e, "Marketing brief unavailable — check back shortly.")
        return {"insight": _msg_mkt}, _status_mkt

def _labor_diagnosis_safe(rid, analysis=None):
    """labor.diagnose over the current analysis — deterministic, so it is
    never cached with the model's prose and never fails the route."""
    try:
        from labor import analyse_shifts_for_restaurant, diagnose
        return diagnose(analysis or analyse_shifts_for_restaurant(rid))
    except Exception:
        return {"available": False, "reason": "could not read the shifts"}


@client_bp.route("/api/labor-insight")
@login_required
def labor_insight_api(current_user):
    rid = current_user["restaurant_id"]
    cached = _cache_get("labor-insight:" + str(rid))
    if cached:
        return jsonify(insight=cached, diagnosis=_labor_diagnosis_safe(rid))
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
        return jsonify(insight=formatted, diagnosis=_labor_diagnosis_safe(rid, analysis))
    except Exception as e:
        import traceback; traceback.print_exc()
        stale = _insight_cache.get("labor-insight:" + str(rid))
        if stale:
            return jsonify(insight=stale[1])
        from ai_utils import insight_error as _insight_err_lab
        _msg_lab, _status_lab = _insight_err_lab(e, "Unable to load analysis — check back shortly.")
        # 200 as before for an ordinary failure; a pause or outage carries its
        # own status (AI-11).
        return jsonify(insight=_msg_lab), (200 if _status_lab == 500 else _status_lab)

@client_bp.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    try:
        from inventory import analysis_for, get_claude_insights
        rid = current_user["restaurant_id"]
        restaurant = get_restaurant(rid)
        items, is_live, analysis = analysis_for(rid)
        # Keyed on the figures, not just the restaurant. The mobile twin has
        # cached this for a while and the web route did not, so every Food
        # Cost page load was a fresh paid LLM call; and invalidate_insight_cache
        # listed an "inv-insight:" key nothing ever wrote. Hashing the numbers
        # means a quick count or a new delivery invalidates the narrative by
        # construction, rather than leaving a five-minute-old story beside
        # figures that have already moved.
        cache_key = "inv-insight:%s:%s" % (rid, _analysis_fingerprint(analysis))
        insight = _cache_get(cache_key)
        if insight is None:
            owner_name = restaurant.owner_name if restaurant else None
            insight = get_claude_insights(analysis, owner_name=owner_name,
                                          restaurant_name=restaurant.name if restaurant else None,
                                          restaurant_id=rid, items=items,
                                          is_live=is_live)
            _cache_set(cache_key, insight)
        # is_live travels with the insight so the UI can say whose numbers
        # these are instead of presenting example data as the owner's own.
        return jsonify(insight=format_insight_html(insight), is_live=bool(is_live))
    except Exception as _inv_e:
        import traceback
        print(f"[inv-insight ERROR] {_inv_e}\n{traceback.format_exc()}")
        # safe_error, not str(e): a requests failure carries the URL it was
        # calling and a Places URL carries key=.
        from ai_utils import insight_error as _insight_err_inv
        _msg_inv, _status_inv = _insight_err_inv(_inv_e)
        return jsonify(insight=_msg_inv, error=_msg_inv), _status_inv


@client_bp.route("/api/food-cost/waste-trend")
@login_required
def food_cost_waste_trend(current_user):
    """The Waste Trend card: the ISO-week series plus every derived figure
    and observation, computed once server-side (waste_trend.py). The live
    analysis is passed in so the target line reflects this week's real
    purchases rather than an average of history."""
    from inventory import load_inventory_for_restaurant, analysis_for
    from waste_trend import build_waste_trend
    rid = current_user["restaurant_id"]
    range_key = (request.args.get("range") or "8w").lower()
    analysis = None
    is_live = True
    try:
        restaurant = get_restaurant(rid)
        items, is_live = load_inventory_for_restaurant(rid)
        # A sample pantry's purchases would put the target line somewhere
        # the owner's real history never bought — without a live count the
        # target comes from the newest week that recorded purchases, or
        # not at all.
        if is_live:
            _, _, analysis = analysis_for(rid, items=items, is_live=is_live)
    except Exception:
        analysis = None
    try:
        return jsonify(**build_waste_trend(rid, range_key, analysis=analysis, is_live=bool(is_live)))
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=_safe_err(e)), 500

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
    try:
        result = generate_content(content_type, topic, restaurant_id=rid)
    except Exception as e:
        # There was no except here at all, so a budget stop became a bare 500
        # with no message (AI-11). The phone twin already said "paused".
        from ai_utils import AIBudgetExceeded, user_facing_error
        msg, status = user_facing_error(e, "Couldn't write that right now — try again in a moment.")
        if not isinstance(e, AIBudgetExceeded):
            import ops
            ops.capture(e, job="generate_content", context=f"restaurant_id={rid}")
        return jsonify(content="", error=msg), status
    if data.get("from_calendar") and rid:
        try:
            mark_calendar_idea_used(rid, content_type, topic)
        except Exception:
            pass
    return jsonify(content=result, tags=_post_tags_safe(rid, topic, result))


def _post_tags_safe(rid, topic, body):
    """What the post is about (marketing_tags.infer), so the compose screen
    can say "Margherita · game day" and the owner can correct it. Never
    fails a generation."""
    try:
        import marketing_tags
        t = marketing_tags.infer(rid, topic, body)
        t["label"] = marketing_tags.label(t)
        return t
    except Exception:
        return None

# ── Marketing: media, scheduling, drafts, links, analytics ────────────────
# The web halves of everything mobile_api.py now exposes, so a feature isn't
# quietly phone-only the way Post to Google was.

@client_bp.route("/api/marketing/media", methods=["GET", "POST"])
@login_required
def marketing_media_api(current_user):
    rid = current_user["restaurant_id"]
    if not _restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    from marketing_media import store_image, list_media, media_url, MediaError
    if request.method == "GET":
        items = list_media(rid)
        for item in items:
            item["url"] = media_url(request.url_root, item["token"])
        return jsonify(ok=True, media=items)

    upload = request.files.get("file") if request.files else None
    if not upload:
        return jsonify(ok=False, error="No photo was attached."), 400
    try:
        stored = store_image(rid, upload.read(), upload.mimetype or "")
    except MediaError as e:
        return jsonify(ok=False, error=_safe_err(e)), 400
    except Exception:
        return jsonify(ok=False, error="Couldn't process that photo."), 500
    return jsonify(ok=True, media_id=stored["id"], token=stored["token"],
                   url=media_url(request.url_root, stored["token"]))


@client_bp.route("/api/marketing/media/<int:media_id>", methods=["DELETE"])
@login_required
def marketing_media_delete(media_id, current_user):
    from marketing_media import delete_media
    delete_media(media_id, current_user["restaurant_id"])
    return jsonify(ok=True)


@client_bp.route("/api/marketing/schedule", methods=["GET", "POST"])
@login_required
def marketing_schedule_api(current_user):
    """Web twin — the one body is mobile_api.mobile_schedule."""
    return _m("mobile_schedule")(current_user)


@client_bp.route("/api/marketing/schedule/<int:post_id>", methods=["DELETE"])
@login_required
def marketing_schedule_cancel(post_id, current_user):
    import marketing_publish as _mp
    result = _mp.cancel_scheduled(post_id, current_user["restaurant_id"])
    return jsonify(**result), (200 if result.get("ok") else 400)


@client_bp.route("/api/marketing/drafts", methods=["GET", "POST"])
@login_required
def marketing_drafts_api(current_user):
    """Web twin — the one body is mobile_api.mobile_drafts."""
    return _m("mobile_drafts")(current_user)


@client_bp.route("/api/marketing/drafts/<int:draft_id>/approve", methods=["POST"])
@login_required
def marketing_draft_approve(draft_id, current_user):
    import marketing_drafts as _md
    result = _md.approve_draft(draft_id, current_user["restaurant_id"],
                               user_id=current_user.get("id"), role=current_user.get("role"))
    return jsonify(**result), (200 if result.get("ok") else 403)


@client_bp.route("/api/marketing/drafts/<int:draft_id>", methods=["DELETE"])
@login_required
def marketing_draft_delete(draft_id, current_user):
    import marketing_drafts as _md
    return jsonify(**_md.delete_draft(draft_id, current_user["restaurant_id"]))


@client_bp.route("/api/marketing/performance-window")
@login_required
def marketing_performance_window(current_user):
    from marketing_signals import performance_window
    try:
        days = max(7, min(int(request.args.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30
    return jsonify(ok=True, **performance_window(current_user["restaurant_id"], days=days))


@client_bp.route("/api/marketing/attribution")
@login_required
def marketing_attribution_api(current_user):
    from marketing_signals import attribution_summary
    return jsonify(**attribution_summary(current_user["restaurant_id"]))


@client_bp.route("/api/marketing/links", methods=["GET", "POST"])
@login_required
def marketing_links_api(current_user):
    """Web twin — the one body is mobile_api.mobile_links."""
    return _m("mobile_links")(current_user)


@client_bp.route("/api/guest-segments")
@login_required
def guest_segments_api(current_user):
    """Web twin — the one body is mobile_api.mobile_guest_segments."""
    return _m("mobile_guest_segments")(current_user)


@client_bp.route("/api/guest-campaigns")
@login_required
def guest_campaigns_api(current_user):
    from guest_marketing import campaign_history, consent_ledger
    rid = current_user["restaurant_id"]
    if not _restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_NO_MARKETING_MODULE_ERROR), 403
    return jsonify(ok=True, campaigns=campaign_history(rid), ledger=consent_ledger(rid))


@client_bp.route("/api/guest-newsletter", methods=["GET", "POST"])
@login_required
def guest_newsletter_api(current_user):
    """Web twin — the one body is mobile_api.mobile_guest_newsletter."""
    return _m("mobile_guest_newsletter")(current_user)


@client_bp.route("/api/marketing/preview", methods=["POST"])
@login_required
def marketing_preview_api(current_user):
    """Web twin — the one body is mobile_api.mobile_marketing_preview."""
    return _m("mobile_marketing_preview")(current_user)


@client_bp.route("/api/post-to-google", methods=["POST"])
@login_required
def post_to_google(current_user):
    """Publish generated copy to the connected Google Business Profile.

    Marketing has always written `google_promo` copy and the dashboard has
    always offered Instagram and Facebook buttons next to it — with no way to
    put a Google post on Google. Mobile got this route first
    (mobile_api.mobile_create_google_post); this is the web half."""
    import gmb as _gmb
    rid = current_user["restaurant_id"]
    data = request.get_json() or {}
    if not _gmb.is_connected(rid):
        return jsonify(ok=False, error="Connect Google Business first — Settings → Connections."), 200
    summary = (data.get("summary") or "").strip()
    if not summary:
        return jsonify(ok=False, error="Post text is required"), 400
    result = _gmb.create_local_post(
        rid, summary,
        cta_type=(data.get("cta_type") or "").strip() or None,
        cta_url=(data.get("cta_url") or "").strip() or None,
    )
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error") or "Google rejected the post"), 200
    try:
        from marketing import log_content
        log_content(rid, "google_promo", (data.get("topic") or summary)[:80],
                    post_id=result.get("name") or None, post_platform="google")
    except Exception:
        pass
    return jsonify(ok=True, post_id=result.get("name"))


@client_bp.route("/api/content-calendar")
@login_required
def content_calendar(current_user):
    """?force=1 is the "Generate week" button asking for a fresh draw; a plain
    read returns this week's cached calendar (see get_content_calendar_ideas).
    The web tab has always driven this from an explicit press, so it forces —
    what changed is that the result is now kept."""
    from marketing import (get_content_calendar_ideas, get_cached_calendar,
                           RECENT_CALENDAR_SECONDS)
    from ai_utils import ai_rate_limited
    rid = current_user["restaurant_id"]
    force = request.args.get("force") not in (None, "", "0", "false")
    if force:
        # A retry after a slow first attempt is answered before the limiter
        # sees it — it counts attempts, not generations.
        just_made = get_cached_calendar(rid, max_age_seconds=RECENT_CALENDAR_SECONDS)
        if just_made:
            return jsonify(ideas=just_made)
        if ai_rate_limited(f"calendar:{rid}", max_calls=4, window_secs=300):
            return jsonify(ideas=[], error="Too many calendar regenerations — try again in a few minutes."), 429
    # An empty week always comes with a reason (AI-26): a cut-off or
    # unreadable draw, a budget pause, an outage. The phone twin already said
    # so; the web tab got ideas=[] and nothing else.
    try:
        ideas = get_content_calendar_ideas(restaurant_id=rid, force=force)
    except Exception as e:
        import ops
        from ai_utils import AIBudgetExceeded, insight_error
        if not isinstance(e, AIBudgetExceeded):
            ops.capture(e, job="content_calendar", context=f"restaurant_id={rid}")
        msg, status = insight_error(e, "Couldn't build this week's calendar — try Generate again.")
        return jsonify(ideas=[], error=msg), status
    if not ideas:
        return jsonify(ideas=[], error="Couldn't build this week's calendar — try Generate again.")
    return jsonify(ideas=ideas)

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
            language=getattr(restaurant, "response_language", None) or None,
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
        return {"ok": False, "error": _safe_err(e)}, 200


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
        return jsonify(weeks=[], error=_safe_err(e))

@client_bp.route("/api/labor-gap")
@login_required
def labor_gap_api(current_user):
    try:
        from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        if not analysis.get("is_live"):
            # The sample week is not the owner's overspend.
            return jsonify(ok=True, is_live=False, projectable=False, over_target=False,
                           monthly_gap=0, current_pct=None, target_pct=analysis.get("labor_target"))
        gap = calculate_monthly_gap(analysis)
        return jsonify(gap)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=_safe_err(e), over_target=False, monthly_gap=0,
                      current_pct=0, target_pct=30)

# Async schedule generation is tracked in ops.async_jobs (a table); the routes
# below and the admin console reach the store through this alias.
import ops as _ops  # noqa: E402

# The schedule engine lives in schedule_engine.py (see its docstring). These
# names are re-exported so the routes below, the tests and the other modules
# that historically reached them here keep one address.
from schedule_engine import (  # noqa: E402,F401
    _no_shift_data_message,
    _build_schedule_result,
    _parse_role_minimums,
    _reconcile_scheduled_hours,
    _parse_time_to_minutes,
    _format_minutes_to_time,
    _summarize_schedule_csv_by_day_role,
    _safe_hours_sum,
    _enforce_close_time,
    _row_fields_look_sane,
    _top_up_hours_gap,
    _extend_shifts_to_close_gap,
    _peak_server_overlap,
    _trim_server_overlap_cap,
    _window_overlap,
    _ensure_role_floors,
    _MORNING_WINDOW,
    _NIGHT_WINDOW,
    replacement_is_legal,
    _rows_to_csv_text,
    quality_inputs_from_db,
    _prior_week_assignments,
    _quality_signals,
    _score_schedule_quality,
    _sched_notes_with_findings,
    _run_schedule_job,
    _TIME_FIELD_RE,
    _WEEKDAYS,
    _WEEKLY_HOURS_CEILING,
    _SERVER_MAX_OVERLAP,
)


@client_bp.route("/api/generate-schedule", methods=["GET", "POST"])
@login_required
def generate_schedule_json(current_user):
    """Start async schedule generation. Returns job_id for polling.

    GET is kept because the dashboard's Labor tab has always called it that
    way; POST is accepted so Ask Cavnar's confirm card can use one verb for
    both surfaces (the mobile twin is POST-only).
    """
    import threading, uuid
    from ai_utils import ai_rate_limited
    from permissions import has_permission, SCHEDULE_DRAFT
    if not (current_user.get("is_admin") or has_permission(current_user, SCHEDULE_DRAFT)):
        return jsonify(ok=False, error="Your login can view labor but not draft a schedule."), 403
    # One generation at a time per restaurant: a second press joins the
    # running job rather than paying for a second model call.
    running = _ops.active_job("schedule", current_user["restaurant_id"])
    if running:
        return jsonify(ok=True, job_id=running, joined=True)
    if ai_rate_limited(f"schedule:{current_user['restaurant_id']}", max_calls=3, window_secs=60):
        return jsonify(ok=False, error="Too many schedule generations — please wait a moment and try again.")
    body = request.get_json(silent=True) or {}
    week_start = (body.get("week_start") or request.args.get("week_start") or "").strip()[:10] or None
    from schedule_engine import check_week_start as _cws
    week_start, _ws_err = _cws(current_user["restaurant_id"], week_start)
    if _ws_err:
        return jsonify(ok=False, error=_ws_err), 400
    dates = [str(d)[:10] for d in (body.get("dates") or []) if str(d)[:10]] or None
    base_history_id = body.get("history_id") if dates else None
    if dates and not base_history_id:
        return jsonify(ok=False, error="Regenerating some days needs the draft they belong to (history_id)."), 400
    # Checked and started in one transaction: two presses at the same instant
    # get one job (SCHED-25).
    job_id, joined = _ops.claim_async_job(str(uuid.uuid4()), "schedule", current_user["restaurant_id"])
    if joined:
        return jsonify(ok=True, job_id=job_id, joined=True)
    t = threading.Thread(target=_run_schedule_job, args=(job_id, current_user["restaurant_id"]),
                         kwargs={"week_start": week_start, "dates": dates, "base_history_id": base_history_id}, daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@client_bp.route("/api/schedule-status/<job_id>", methods=["GET"])
@login_required
def schedule_status(current_user, job_id):
    """Poll for schedule generation result. Scoped to the caller's own
    restaurant — this used to be unauthenticated on the reasoning that the
    job id is an unguessable UUID, which is true but left the result readable
    by anyone who saw the id in a log or a shared screen."""
    job = _ops.read_async_job(job_id, restaurant_id=current_user["restaurant_id"])
    if not job:
        return jsonify({"ok": False, "status": "error", "error": "Job not found"}), 404
    if job["status"] == "pending":
        return jsonify({"ok": True, "status": "pending"})
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "status": "error", "error": str(e)}), 500


@client_bp.route("/api/download-schedule")
@login_required
def download_schedule(current_user):
    """Serve the schedule that was generated, reviewed and possibly edited.

    This used to call _build_schedule_result, which runs the generator
    again — so the CSV an owner printed was a different week from the one
    on their screen, carried none of the quality review they had just
    worked through, and billed a second model call every press.
    """
    import io
    from models import get_schedule_history, get_schedule_history_detail
    try:
        rid = current_user["restaurant_id"]
        restaurant = get_restaurant(rid)
        entries = get_schedule_history(rid, limit=1)
        if not entries:
            return jsonify(ok=False,
                           error="Generate a schedule first — there's nothing to download yet."), 400
        detail = get_schedule_history_detail(entries[0]["id"], rid) or {}
        csv_clean = (detail.get("schedule_csv") or "").strip()
        if not csv_clean:
            return jsonify(ok=False, error="That schedule has no rows to download."), 400
        name = (restaurant.name if restaurant else "Restaurant").replace(" ", "_")
        return send_file(
            io.BytesIO(csv_clean.encode()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"optimized_schedule_{name}.csv"
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=_safe_err(e)), 500

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
        _stripe = config.stripe_api(stripe_key)
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
        return jsonify(ok=False, reason="stripe_error", error=_safe_err(e))

def _normalize_phone_lenient(raw):
    import re
    digits = re.sub(r'\D', '', raw or '')
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) > 7:
        return '+' + digits
    return None


# The per-alert-type push switches, in the order the settings screens list
# them. deliver_alert reads these columns; both clients now write them.
_PUSH_COLUMNS = ("al_1star_push", "al_2star_push", "al_5star_push",
                 "al_health_push", "al_spike_push", "al_unres_push")


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
    # Per-type push switches. These columns have existed and been honoured by
    # deliver_alert since push shipped, and iOS has had switches for them —
    # the web dashboard had none, so an owner who manages Cavnar from a
    # laptop could not turn a single push off.
    for col in _PUSH_COLUMNS:
        settings[col] = 1 if getattr(r, col, 1) is None else int(getattr(r, col, 1))
    settings["push_sound"] = 1 if getattr(r, "push_sound", 1) is None else int(getattr(r, "push_sound", 1))
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

    # Sync contacts — max 2. A real error instead of silently dropping the
    # extras — the client already hides its own "+ Add" past 2, but a
    # direct API call or a stale build should hear why it failed.
    raw_contacts = data.get("contacts") or []
    if len(raw_contacts) > 2:
        return jsonify(ok=False, error="Alert contacts are limited to 2."), 400
    new_contacts = raw_contacts[:2]
    existing = get_alert_contacts(rid)
    for ec in existing:
        delete_alert_contact(ec["id"])
    for nc in new_contacts:
        phone = _normalize_phone_lenient(nc.get("phone") or "")
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
        **{col: int(bool(data.get(col, True))) for col in _PUSH_COLUMNS},
        "push_sound":            0 if data.get("push_sound") is False else 1,
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

    restaurant_id = current_user["restaurant_id"]
    data_type     = request.form.get("data_type")  # "shifts" or "inventory"

    if data_type not in ("shifts", "inventory"):
        return jsonify(ok=False, error="Invalid data type")

    # The dataset belongs to a module, so replacing it takes that module's
    # access. This path is outside auth._MODULE_PREFIXES (one route serves
    # both datasets), so a manager without food-cost access could replace the
    # inventory data the owner's margins are computed from (SEC-24).
    from permissions import has_permission, FOOD_COST_VIEW, LABOR_VIEW
    if not has_permission(current_user, FOOD_COST_VIEW if data_type == "inventory" else LABOR_VIEW):
        return jsonify(ok=False, error="Your login doesn't have access to that module's data."), 403

    f = request.files.get("csv_file")
    if not f:
        return jsonify(ok=False, error="No file uploaded")

    # Excel's "CSV UTF-8" starts with a BOM and its plain "CSV" is cp1252;
    # both are CSVs an owner will reasonably upload.
    _raw_csv = f.read()
    try:
        csv_content = _raw_csv.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            csv_content = _raw_csv.decode("cp1252")
        except Exception:
            return jsonify(ok=False, error="Could not read file — make sure it's a CSV")

    if not csv_content.strip():
        return jsonify(ok=False, error="File appears empty")

    # Validate it parses.
    #
    # The size guard that used to sit at the top of this function read
    # request.files.get("file") while the upload arrives as "csv_file", so it
    # never fired on anything; Flask's MAX_CONTENT_LENGTH (5 MB, set in
    # hosted_dashboard) was doing the whole job silently. That leaves the
    # real limit: 5 MB of valid CSV is roughly 100,000 shift rows, and
    # everything downstream of here — parse, analyse, store — runs
    # SYNCHRONOUSLY inside the request, on a deployment with four request
    # threads in total and a 120-second gunicorn timeout. A single large
    # upload is a quarter of the platform for as long as it takes.
    try:
        rows = list(_csv.DictReader(io.StringIO(csv_content)))
        if not rows:
            return jsonify(ok=False, error="CSV has no data rows")
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}")

    if len(rows) > MAX_CSV_ROWS:
        return jsonify(ok=False, error=(
            f"That file has {len(rows):,} rows — this import handles up to "
            f"{MAX_CSV_ROWS:,} at a time. Split it by date range and upload "
            f"the parts, or send it to will@cavnar.ai and we'll load it for you."
        )), 413

    # Validate required columns exist
    headers = [h.strip().lower() for h in (rows[0].keys() if rows else [])]

    if data_type == "shifts":
        # Validated and stored in the shape the analysis reads — the same
        # normaliser every read goes through (SEC-16, MOD-LAB-10/11/12).
        # A refused file changes nothing: the previous dataset stays.
        from labor import validate_shifts_csv
        _shift_rows, _shift_errors = validate_shifts_csv(csv_content)
        if _shift_errors:
            _more = len(_shift_errors) - 5
            return jsonify(ok=False, row_errors=_shift_errors[:50], error=(
                "Some rows couldn't be read, so nothing was saved: " + " ".join(_shift_errors[:5])
                + (f" …and {_more} more." if _more > 0 else "")))
        headers = list(_shift_rows[0].keys())
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
        # Every row through the same parser the reads use. Only the headers
        # were checked, so "$1.80", a blank cell, title-case headers or a nan
        # were saved and then made every Food Cost read raise under a green
        # "uploaded" (MOD-FC-5). Refused here, with the rows named.
        from inventory import parse_inventory_rows
        _inv_items, _inv_errors = parse_inventory_rows(rows)
        if _inv_errors:
            _more = len(_inv_errors) - 5
            return jsonify(ok=False, row_errors=_inv_errors[:50], error=(
                "Some rows couldn't be read, so nothing was saved: " + " ".join(_inv_errors[:5])
                + (f" …and {_more} more." if _more > 0 else "")))
        if not _inv_items:
            return jsonify(ok=False, error="CSV has no data rows")

    # Save it
    save_client_data(restaurant_id, data_type, csv_content, source="upload")
    # The AI insight is cached for five minutes with no invalidation, so a
    # fresh upload showed the previous data's narrative beside the new
    # data's numbers on the same screen. Drop it on write.
    invalidate_insight_cache(restaurant_id)

    # Trigger immediate re-analysis so dashboard reflects new data right away
    _ot_flags = []
    try:
        if data_type == "shifts":
            from labor import analyse_shifts_for_restaurant
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
                # analyse_shifts returns overall_labor_pct and per-day hours;
                # the keys read here never existed, so every payload was
                # nulls (MOD-LAB-19).
                _fw_labor(restaurant_id, "labor.updated", {
                    "labor_pct": _shift_analysis.get("overall_labor_pct"),
                    "total_hours": round(sum(float((d or {}).get("actual") or 0)
                                             for d in (_shift_analysis.get("by_day") or {}).values()), 1),
                    "total_labor_cost": _shift_analysis.get("total_labor_cost"),
                    "total_sales": _shift_analysis.get("total_sales"),
                })
            except Exception:
                pass
        elif data_type == "inventory":
            import threading as _t_inv
            _rid_inv = restaurant_id
            def _inv_trend_bg():
                try:
                    from inventory import analysis_for as _afor, compute_item_trends as _cit
                    from webhooks import fire_webhook as _fw_inv
                    _items, _live_inv, _analysis = _afor(_rid_inv)
                    if not (_items and _live_inv):
                        return  # never fan out sample-pantry figures to a customer's webhook
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

    # Overtime alert — email owner immediately when upload reveals an overtime
    # employee. Only this week and last (an upload of last year's history is
    # not "in overtime this week"), and each person-week once: re-uploading
    # the same file re-sent the email (MOD-LAB-17).
    if _ot_flags:
        from datetime import date as _d_ot, timedelta as _td_ot
        _cut_ot = (_d_ot.today() - _td_ot(days=14)).isoformat()
        _recent_ot = [f for f in _ot_flags if str(f.get("week_start") or "") >= _cut_ot]
        _ot_flags = []
        for _f in _recent_ot:
            try:
                if _ops.claim_period(f"overtime_email:{restaurant_id}", f"{_f['employee'].casefold()}:{_f.get('week_start')}"):
                    _ot_flags.append(_f)
            except Exception:
                _ot_flags.append(_f)
    if _ot_flags:
        try:
            import os as _os_ot, resend as _resend_ot
            from html import escape as _html_escape_ot   # names come from the CSV
            from models import get_restaurant as _gr_ot
            _r_ot = _gr_ot(restaurant_id)
            _key_ot = _os_ot.getenv("RESEND_API_KEY", "")
            _from_ot = config.from_email()
            if _key_ot and _r_ot and _r_ot.owner_email:
                _resend_ot.api_key = _key_ot
                _ot_rows = "".join(
                    "<tr><td style='padding:6px 10px;border-bottom:1px solid #e0dbd0'><strong>" +
                    _html_escape_ot(f["employee"]) + "</strong></td><td style='padding:6px 10px;border-bottom:1px solid #e0dbd0'>" +
                    str(f["hours"]) + "h — week of " + _html_escape_ot(f["week"]) + "</td></tr>"
                    for f in _ot_flags
                )
                _resend_ot.Emails.send({
                    "from": _emails_mod().sender("ops"),
                    "to": [_r_ot.owner_email],
                    "subject": "⚠ Overtime detected — " + _r_ot.name,
                    "html": _html_doc(
                        "<div style='background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box'>"
                        "<div style='font-family:-apple-system,BlinkMacSystemFont,\"Helvetica Neue\",Arial,sans-serif;max-width:520px;margin:0 auto;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box'>"
                        "<img src='https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png' width='150' height='26' alt='Cavnar AI' style='display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:16px'>"
                        "<div style='border-top:3px solid #e07040;padding-top:16px;margin-bottom:16px'>"
                        "<h3 style='color:#0e0c0a;margin:0'>Overtime Alert</h3>"
                        "<p style='font-size:13px;color:#7a736a;margin:4px 0 0'>Cavnar AI Labor Monitor</p>"
                        "</div>"
                        "<p style='font-size:15px;line-height:1.6;color:#0e0c0a'>Your latest shift upload shows "
                        + str(len(_ot_flags)) + " overtime week" + ("" if len(_ot_flags) == 1 else "s") + " in the last two weeks:</p>"
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
        _will_email = config.will_email()
        _from_email = config.from_email()
        if _is_first_upload and _resend_key and r:
            _resend.api_key = _resend_key
            _module = "shift schedule" if data_type == "shifts" else "inventory"
            _resend.Emails.send({
                "from": _emails_mod().sender("ops"),
                "to": [_will_email],
                "subject": f"📂 {r.name} uploaded their first {_module} data",
                "html": _html_doc(f"""<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:500px;margin:0 auto;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
                    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:16px">
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
                </div>
                    </div>"""),
            })
    except Exception:
        pass

    return jsonify(ok=True, rows=len(rows), message=f"{len(rows)} rows loaded successfully")


# ── Food cost quick count ─────────────────────────────────────────────────────

def _clean_quickcount_items(items):
    """Coerce a submitted quick count into storable rows.

    Returns (clean, rejected). A row needs a name and a finite, non-negative
    price; usage is optional. Anything else is reported back by name rather
    than silently stored as a zero that becomes next week's baseline and
    suppresses that ingredient's drift alert forever.
    """
    import math as _math_fc
    clean, rejected = [], []
    for raw in items:
        if not isinstance(raw, dict):
            rejected.append({"name": None, "why": "not an item"})
            continue
        name = str(raw.get("name") or "").strip()[:120]
        if not name:
            rejected.append({"name": None, "why": "no name"})
            continue

        def _num(key, required):
            v = raw.get(key)
            if v in (None, ""):
                return None if required else 0.0
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if not _math_fc.isfinite(f) or f < 0:
                return None
            return round(f, 4)

        price = _num("price", True)
        if price is None:
            rejected.append({"name": name, "why": "price is missing or not a number"})
            continue
        usage = _num("usage", False)
        if usage is None:
            rejected.append({"name": name, "why": "usage is not a number"})
            continue
        clean.append({"name": name, "unit": str(raw.get("unit") or "")[:40],
                      "price": price, "usage": usage})
    return clean, rejected


def _do_food_cost_quickcount(restaurant_id, items):
    """Save Big-8 ingredient prices, compute week-over-week drift, return alerts."""
    import json as _json_fc
    from datetime import datetime as _dt_fc
    from models import get_client_data as _gcd, get_conn as _gcc

    if not items or not isinstance(items, list):
        return {"ok": False, "error": "No items provided"}, 400
    if len(items) > 200:
        return {"ok": False, "error": "Too many items in one count."}, 400

    # Validate and coerce before anything is persisted. The raw array used to
    # be written verbatim; on the NEXT submission it became `prev`, and
    # prev_map's i["name"].lower() raised outside any try — a 500 that saved
    # nothing, so the poisoned blob stayed and every future count 500'd for
    # that restaurant permanently.
    items, rejected = _clean_quickcount_items(items)
    if not items:
        return {"ok": False, "error": "No usable items — each needs a name and a valid price.",
                "rejected": rejected}, 400

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
    if prev and isinstance(prev.get("items"), list):
        prev_map = {str(i["name"]).lower(): i for i in prev["items"]
                    if isinstance(i, dict) and i.get("name")}
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

    # Merge, don't replace. Rebuilding the blob from scratch dropped the
    # custom_items key that _do_save_food_cost_custom_item writes and the
    # template renders — so every quick count silently deleted every custom
    # ingredient the owner had added.
    existing_fc["current"] = new_current
    existing_fc["previous"] = prev or {}
    save_payload = _json_fc.dumps(existing_fc)
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
        # Rows that couldn't be stored are named rather than dropped quietly:
        # a blank price used to be written as $0.00 and then suppressed that
        # ingredient's drift alert the following week.
        "rejected": rejected,
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

        # Build Google review link. There is no generic fallback: the old
        # one was https://g.page/r/review, which points at no particular
        # business — a guest who tapped it landed nowhere useful, having
        # been sent there by name by the restaurant.
        place_id    = restaurant.google_place_id or ""
        if not place_id:
            return {"ok": False,
                    "error": "This restaurant has no Google Place ID on file, so there is no "
                             "review link to send yet. Add it in settings first."}, 400
        review_url  = f"https://search.google.com/local/writereview?placeid={place_id}"
        first_name  = customer_name.split()[0] if customer_name else "there"
        rest_name   = restaurant.name or "us"

        # Send via SMS if phone provided
        if customer_phone:
            # Alerts go only to contacts who consented (get_alert_contacts'
            # sms_consent_only). This path texted whatever number was typed
            # in, with no record that the guest agreed to be messaged — the
            # one outbound SMS in the product that skipped the consent model
            # the product already has.
            if not (data.get("sms_consent") or data.get("consent")):
                return {"ok": False,
                        "error": "Confirm the guest agreed to be texted before sending a "
                                 "review request by SMS."}, 400
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
        <div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
        <div style="font-family:'DM Sans',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;background:#f7f4ef">
          <div style="background:white;border-radius:12px;padding:32px;border:1px solid #e0dbd0">
            <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:4px">
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
        </div>
        </div>"""

        _resend.Emails.send({
            "from":    "reviews@cavnar.ai",
            "to":      [customer_email],
            "subject": f"How was your visit to {rest_name}?",
            "html":    _html_doc(html_body),
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
        return {"ok": False, "error": _safe_err(e)}, 500


@client_bp.route("/api/review-request-stats")
@login_required
def review_request_stats(current_user):
    from models import get_review_request_stats
    return jsonify(**get_review_request_stats(current_user["restaurant_id"]))


# ── GBP Listings ──────────────────────────────────────────────────────────────

@client_bp.route("/api/gbp-debug")
@login_required
def gbp_debug(current_user):
    # Raw Google Business Profile API bodies (account ids, location names):
    # Cavnar staff only. Any logged-in login, a teammate included, could read
    # them (SEC audit "debug endpoints"). Candidate for removal after
    # verifying nothing else calls it.
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Not found"), 404
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
    from gmb import get_gbp_listing, get_valid_token, find_gmb_location
    from models import get_restaurant, update_restaurant
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    # Token present but location missing — try to discover it now
    if r and r.gmb_refresh_token and not r.gmb_location_id:
        try:
            token = get_valid_token(rid)
            if token:
                _m = find_gmb_location(token, r.google_place_id or "")
                if _m.get("ok"):
                    update_restaurant(rid, {
                        "gmb_account_id":  _m["account"],
                        "gmb_location_id": _m["location"],
                    })
                else:
                    print(f"[GBP] auto-discover declined for rid={rid}: {_m.get('error')}")
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
        return {"ok": False, "error": _safe_err(e)}, 200


# A visibility check asks the same three questions about the same restaurant
# and gets near-identical answers within a day, so re-running it inside this
# window buys nothing and costs three sonar queries. There was no cache at all
# — only a burst limit, which caps nine queries a minute rather than the month
# (the same distinction ai_utils makes about rate limits versus budgets).
# The one AI system this module actually queries. Everything the interface
# says about "AI search" is a statement about this vendor and this model,
# and both travel with the payload so no surface has to guess.
AIVIS_PLATFORM = "Perplexity"
AIVIS_MODEL = os.getenv("AI_VISIBILITY_MODEL", "sonar")

# Confirmed against production's ai_usage log: every "could not fetch answer"
# was an HTTP 429 from Perplexity, never a timeout or any other failure.
# This key sits on Perplexity's Tier 0 (no lifetime spend yet), which caps
# sonar at 50 requests/minute — and the old per-run stagger (submit every
# 0.6s, 3 workers, one 2s-delay retry) only paced one restaurant's own 8
# queries against each other. It never accounted for: (a) that burst alone,
# with its retries, already lands ~13 calls inside 9 seconds — well over
# 50 RPM by itself — and (b) the limit is per API key, not per restaurant,
# so run_weekly_ai_visibility() walking restaurants back-to-back (scheduler.py)
# stacks each restaurant's burst on the same shared budget with no gap
# between them. Pacing has to live at the one place every send actually
# passes through, not at each call site.
_PPLX_MIN_INTERVAL = float(os.getenv("PPLX_MIN_REQUEST_INTERVAL", "1.3"))  # 50 RPM = 1.2s; small margin
_pplx_pace_lock = threading.Lock()
_pplx_last_sent_at = [0.0]


def _pplx_wait_turn():
    """Block until it's safe to send the next Perplexity request.

    Process-wide and thread-safe: every actual send — any restaurant, any
    thread, any retry — queues through this one gate, so concurrent workers
    within a run and back-to-back restaurants in the scheduler both respect
    the same 50 RPM ceiling instead of only the queries within one run.

    A no-op under pytest — this paces real network sends, and the test
    suite's mocked ones don't touch Perplexity's actual rate limit, so
    there's nothing here for a real request's timing to protect.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    with _pplx_pace_lock:
        now = time.monotonic()
        wait = _pplx_last_sent_at[0] + _PPLX_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _pplx_last_sent_at[0] = time.monotonic()


_AIVIS_CACHE_SECS = int(os.getenv("AI_VISIBILITY_CACHE_SECS", "21600"))  # 6 hours
_aivis_cache = {}


# Cached per process because a restaurant's address does not normally
# change and this is a billed Places call on every check. It never expired,
# though, so a restaurant that relocated or had its Place ID corrected kept
# the old city until a redeploy — and the city gates every appearance match,
# so a wrong one silently scores zero forever.
_CITY_CACHE_SECS = 86400
_city_cache = {}


def _city_from_place_id(place_id: str) -> str:
    """The city Google has on file for this listing, or "".

    Cached per process: the address of a restaurant does not change, and
    this would otherwise be a billed Places call on every visibility check.
    """
    if not place_id:
        return ""
    _hit = _city_cache.get(place_id)
    if _hit and (datetime.utcnow() - _hit[0]).total_seconds() < _CITY_CACHE_SECS:
        return _hit[1]
    city = ""
    settled = False     # an answer worth remembering, found or not
    try:
        import requests as _req
        key = config.google_places_key()
        if key:
            resp = _req.get("https://maps.googleapis.com/maps/api/place/details/json",
                            params={"place_id": place_id, "fields": "address_component",
                                    "key": key}, timeout=8)
            data = resp.json()
            status = data.get("status")
            if status == "OK":
                settled = True
                for comp in (data.get("result", {}).get("address_components") or []):
                    types = comp.get("types") or []
                    if "locality" in types:
                        city = comp.get("long_name") or ""
                        break
                    if not city and "postal_town" in types:
                        city = comp.get("long_name") or ""
            elif status in ("NOT_FOUND", "INVALID_REQUEST", "ZERO_RESULTS"):
                settled = True   # a fact about this Place ID, not a blip
    except Exception as e:
        print(f"[aivis] city lookup failed for {place_id}: {e}")
    # Only a settled answer is cached. A timeout or a quota refusal was
    # cached as "no city" for a day, which scored every run in it as zero
    # (MOD-INT-4); it is retried on the next call instead.
    if settled:
        _city_cache[place_id] = (datetime.utcnow(), city)
    return city


def _do_ai_visibility_inner(rid, force=False):
    from ai_utils import ai_rate_limited, ai_budget_exceeded
    if ai_rate_limited(f"aivis:{rid}", max_calls=3, window_secs=60):
        return {"ok": False, "error": "Too many visibility checks — please wait a moment and try again."}, 200

    # The restaurant is looked up BEFORE the cache is read. The cache lives
    # six hours in process memory, so a restaurant that was deleted or
    # deactivated kept being served its own intelligence — a 200 with a full
    # payload — until the entry aged out. Whether a restaurant exists is not
    # something a cache of its results can answer.
    r = get_restaurant(rid)
    if not r:
        _aivis_cache.pop(rid, None)
        return {"ok": False, "error": "Restaurant not found"}, 404

    if not force:
        _hit = _aivis_cache.get(rid)
        if _hit and (datetime.utcnow() - _hit[0]).total_seconds() < _AIVIS_CACHE_SECS:
            _cached = dict(_hit[1])
            _cached["cached"] = True
            return _cached, 200
        # The process cache is gone after every deploy; the recorded run is
        # still the answer. Serving it beats eight live Perplexity queries on
        # a request thread (MOD-INT-5). The weekly job keeps it current; the
        # Refresh button forces a new run.
        try:
            from models import latest_ai_visibility_payload
            _stored = latest_ai_visibility_payload(rid)
        except Exception:
            _stored = None
        if _stored:
            _cached = dict(_stored[0])
            _cached["cached"] = True
            _cached["measured_at"] = _stored[1]
            _aivis_cache[rid] = (datetime.utcnow(), dict(_cached))
            return _cached, 200

    # Perplexity is a paid dependency like any other, so it answers to the
    # same ceiling. It used to be exempt purely because it wasn't Claude.
    _over = ai_budget_exceeded(rid)
    if _over:
        return {"ok": False, "error": f"AI visibility is paused — {_over} reached."}, 200

    name        = r.name or ""
    neighborhood = r.neighborhood or ""
    vibe        = r.vibe or ""
    known_for   = r.known_for or ""
    # The city comes from Google's own address for this Place ID, not from
    # the free-text `neighborhood` profile field. An owner who typed "West
    # Loop" or "Downtown" there was producing queries like "Top restaurants
    # in West Loop" — and worse, norm_city gates every appearance match, so
    # a neighbourhood-only profile could never register a mention and scored
    # zero for a reason that had nothing to do with the restaurant. The
    # profile field stays as the fallback for a restaurant with no Place ID.
    resolved_city = _city_from_place_id(getattr(r, "google_place_id", None))
    city_source = "google" if resolved_city else ("profile" if neighborhood else "")
    if resolved_city:
        city = city_full = resolved_city
    else:
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
        # Six queries, not three. The score is appearances over queries, so
        # three of them gave it exactly four possible values — 0, 33, 67,
        # 100 — and a single query flipping moved it 33 points. Perplexity
        # is non-deterministic, so that flip happens on its own. Six halves
        # the quantum and widens the angles a guest might actually ask from.
        # Every query used to be a superlative local-discovery question —
        # "best/top restaurants in X" in six shapes. A model answering those
        # leans on aggregator listicles and high-review-count venues, so a
        # small independent scored zero largely because of how the questions
        # were written, and the roadmap then told them reviews would fix it.
        #
        # Two changes. The intents now spread across discovery, cuisine,
        # occasion, and practical questions a guest actually asks. And each
        # carries a kind: BRANDED asks about this restaurant by name, which
        # is a different question from whether it surfaces in an open
        # search, and blending the two into one number answered neither.
        queries = [
            {"q": vibe_query or ("Where can I find good " + cuisine.lower() + " in " + city_full + "?"),
             "kind": "cuisine"},
            {"q": "Top restaurants in " + city_full, "kind": "discovery"},
            {"q": q3, "kind": "occasion"},
            {"q": "Where should I eat in " + city_full + " tonight?", "kind": "discovery"},
            {"q": "Best " + cuisine.lower() + " restaurants near " + city_full, "kind": "cuisine"},
            {"q": "Highly rated local restaurants in " + city_full, "kind": "discovery"},
            # Practical intent — a guest who already has a shortlist.
            {"q": "Which restaurants in " + city_full + " are good for a group?", "kind": "practical"},
            # Branded recall: does the system know this restaurant at all?
            # Scored separately; it is not evidence of discoverability.
            {"q": "Tell me about " + name + " in " + city_full, "kind": "branded"},
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
            {"q": (name + " restaurant") if name else "restaurant near me", "kind": "branded"},
            {"q": ("Top local " + cuisine + " restaurants") if has_cuisine else "Top local restaurants",
             "kind": "discovery"},
            {"q": ("Best " + cuisine + " restaurant near me") if has_cuisine else "Best restaurant near me",
             "kind": "cuisine"},
        ]

    import requests as _pplx_req
    import time as _pplx_time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _pplx_key = os.getenv("PERPLEXITY_API_KEY", "")
    appeared_count = 0

    def _norm(s):
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("’", "").replace("’", ""))

    norm_name = _norm(name)

    # The competitor set this restaurant already has on file, so an answer
    # naming one of them is recorded as such. Read once per run.
    def _known_competitors():
        try:
            import json as _jc
            blob = getattr(r, "competitor_intel", None)
            if not blob:
                return []
            data = _jc.loads(blob) if isinstance(blob, str) else blob
            return [c.get("name") for c in (data.get("competitors") or []) if c.get("name")]
        except Exception:
            return []

    _competitor_names = _known_competitors()

    def _competitors_in(answer):
        """Which of this restaurant's known competitors the answer named.

        Whole-phrase, same discipline as _mentions_this_restaurant — a
        substring test would match "Mia" inside "Gia Mia".
        """
        if not answer or not _competitor_names:
            return []
        na = _norm(answer)
        out = []
        for cname in _competitor_names:
            nc = _norm(cname)
            if not nc or len(nc) < 4:
                continue
            if re.search(r"(?:^|\s)" + re.escape(nc) + r"(?:\s|$)", na):
                out.append(cname)
        return out
    norm_city = _norm(city) if city else ""

    def _mentions_this_restaurant(answer):
        """Did the answer name THIS restaurant, or one that shares its name?

        `norm_name in _norm(answer)` on its own is a substring test. Gia Mia
        has locations in St. Charles, Geneva and Wheaton; a recommendation of
        any of them counted as this one appearing. A short name ("Bar", "The
        Table") matched almost every answer outright.

        So: the name has to appear as a whole phrase, and when we know the
        city, the sentence carrying the name has to carry the city too —
        which is why the system prompt now asks for the city alongside each
        recommendation. Without a city on the profile we cannot tell the
        locations apart at all, and say so rather than claiming a match.
        """
        if not norm_name or not answer:
            return False
        norm_answer = _norm(answer)
        # Whole-phrase, not substring: "mia" must not match "Gia Mia".
        if not re.search(r"(?:^|\s)" + re.escape(norm_name) + r"(?:\s|$)", norm_answer):
            return False
        if not norm_city:
            return False
        # The city has to sit next to the name, not merely somewhere in an
        # answer that also lists five other towns. Proximity rather than
        # sentence-splitting: "St. Charles" contains a period, so splitting
        # on punctuation tears the city in half and never matches.
        for m in re.finditer(r"(?:^|\s)" + re.escape(norm_name) + r"(?:\s|$)", norm_answer):
            window = norm_answer[max(0, m.start() - 40):m.end() + 80]
            if norm_city in window:
                return True
        return False

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

        re.compile(r"^(?:Looking at|Reviewing) (?:the )?(?:search )?results?[,.]\s*", re.I),

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

    # Concurrency here is for latency (up to 3 responses in flight at once),
    # not for send order — _pplx_wait_turn() below is what actually keeps
    # this key under its 50 RPM ceiling, across every worker and every
    # restaurant in the process, not just the queries in this one run.
    def _run_query(spec, _retry=True):
        q = spec["q"] if isinstance(spec, dict) else spec
        kind = spec.get("kind", "discovery") if isinstance(spec, dict) else "discovery"
        try:
            _pplx_wait_turn()
            resp = _pplx_req.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {_pplx_key}", "Content-Type": "application/json"},
                json={
                    "model": AIVIS_MODEL,
                    # Citations are no longer suppressed. Perplexity's whole
                    # value here is that its claims are grounded, and the old
                    # prompt asked it to strip exactly that — leaving an
                    # unverifiable assertion stored against the restaurant.
                    # The inline [n] markers are still cleaned out of the
                    # display text; the URLs come back separately below.
                    "messages": [
                        {"role": "system", "content": "Answer in under 80 words. Recommend specific restaurants by name, and include the city or neighbourhood each one is in. Do not use markdown formatting."},
                        {"role": "user", "content": q},
                    ],
                    "max_tokens": 300,
                },
                timeout=10
            )
            body = resp.json() if resp.status_code == 200 else {}
            answer = body.get("choices", [{}])[0].get("message", {}).get("content", "") if body else ""
            sources = [c for c in (body.get("citations") or []) if isinstance(c, str)][:6]

            # Meter it. Perplexity used to sit entirely outside the ledger and
            # the budget — the $10/day and $1,500/month ceilings bound Claude
            # only, so nine sonar queries a minute per restaurant were both
            # unbounded and invisible. Same table, same budget, same admin view.
            try:
                from ai_utils import log_api_call as _lac
                _u = (body.get("usage") or {}) if isinstance(body, dict) else {}
                _lac(rid, "ai_visibility", "perplexity-search",
                     calls=1,
                     input_tokens=int(_u.get("prompt_tokens") or 0),
                     output_tokens=int(_u.get("completion_tokens") or 0),
                     status="ok" if answer else "error",
                     error=None if answer else f"HTTP {resp.status_code}, no answer")
            except Exception:
                pass
            if not answer and _retry:
                # _pplx_wait_turn() already keeps sends under the 50 RPM
                # ceiling, so a 429 here means Perplexity's own window
                # hasn't cleared yet — honor its Retry-After when it sends
                # one instead of guessing a flat delay.
                _delay = 2.0
                if resp.status_code == 429:
                    try:
                        _delay = max(_delay, float(resp.headers.get("Retry-After", _delay)))
                    except (TypeError, ValueError):
                        pass
                _pplx_time.sleep(_delay)
                return _run_query(spec, _retry=False)
            if not answer:
                # Perplexity did not answer. That is an outage on our side,
                # not evidence the restaurant is invisible — scoring it zero
                # is how a rate limit became a permanent dip in the owner's
                # visibility trend.
                return {"query": q, "kind": kind, "answer": "Could not fetch answer.",
                        "appeared": False, "ok": False, "sources": [],
                        "competitors_named": []}
            appeared = _mentions_this_restaurant(answer)
            # Was answer[:400] — the system prompt already asks for "under
            # 80 words" (~440 chars including spaces), so a 400-char cap
            # sat BELOW what a compliant response typically needs and was
            # cutting real content off before iOS's own press-and-hold
            # "read the full answer" feature ever saw it. max_tokens: 300
            # on the API call above already bounds the raw response size —
            # this extra truncation was redundant on top of that, not a
            # real safety net.
            # The answers ARE ranked lists of restaurants, and the
            # competitor set is already validated with Place IDs two modules
            # away. Nothing cross-referenced them, so the one comparison an
            # owner most wants — did my competitors come up instead of me —
            # was a pass over data already in memory that nobody made.
            return {"query": q, "kind": kind, "answer": _clean_ai_answer(answer),
                    "appeared": appeared, "ok": True, "sources": sources,
                    "competitors_named": _competitors_in(answer)}
        except Exception:
            if _retry:
                _pplx_time.sleep(2)
                return _run_query(spec, _retry=False)
            return {"query": q, "kind": kind, "answer": "Could not fetch answer.",
                    "appeared": False, "ok": False, "sources": [],
                    "competitors_named": []}

    # Submit all queries at once — up to 3 run concurrently for latency,
    # but each one blocks on _pplx_wait_turn() before it actually sends,
    # so send order (not submission order) is what respects the 50 RPM
    # ceiling. A manual pre-submission stagger used to try to approximate
    # this here; it's gone now that the real gate lives in _run_query.
    query_results = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=3) as _pool:
        _futures = {_pool.submit(_run_query, _q): _i for _i, _q in enumerate(queries)}
        for _fut in as_completed(_futures):
            i = _futures[_fut]
            try:
                query_results[i] = _fut.result()
            except Exception:
                query_results[i] = {"query": queries[i]["q"], "kind": queries[i].get("kind"),
                                    "answer": "Could not fetch answer.", "appeared": False,
                                    "ok": False, "sources": [], "competitors_named": []}
    answered = [r for r in query_results if r and r.get("ok")]
    # Branded recall — "tell me about X" — is not evidence that a guest
    # searching openly would find you. It was being counted in the same
    # number as discovery, which answered neither question. Scored apart.
    discovery = [r for r in answered if r.get("kind") != "branded"]
    branded = [r for r in answered if r.get("kind") == "branded"]
    appeared_count = sum(1 for r in discovery if r.get("appeared"))
    branded_appeared = sum(1 for r in branded if r.get("appeared"))
    branded_score = round(branded_appeared / len(branded) * 100) if branded else None

    # Which competitors came up across this run, and how often. The answers
    # are ranked restaurant lists; this is the comparison the product goal
    # asks for and nothing was doing.
    from collections import Counter as _Counter
    _comp_hits = _Counter()
    for _r in answered:
        for _c in (_r.get("competitors_named") or []):
            _comp_hits[_c] += 1
    competitor_appearances = [
        {"name": n, "queries": c,
         "share": round(c / len(discovery) * 100) if discovery else 0}
        for n, c in _comp_hits.most_common(8)
    ]

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
        checklist.append({"label": "Google Place ID connected", "effort": "minutes", "why_it_matters": "lets us read your listing at all", "done": True, "kind": "setup", "pts": 10,
                          "action": "Done — your listing is linked", "needs_gmb": False})
    else:
        checklist.append({"label": "Add your Google Place ID", "effort": "minutes", "why_it_matters": "lets us read your listing at all", "done": False, "kind": "setup", "pts": 10,
                          "action": "Go to Account → paste your Google Place ID so we can read your listing",
                          "needs_gmb": False})

    # 2. Yelp profile linked — Perplexity and ChatGPT pull heavily from Yelp
    if bool(r.yelp_business_id):
        checklist.append({"label": "Yelp profile linked", "effort": "minutes", "why_it_matters": "lets us read your Yelp listing", "done": True, "kind": "setup", "pts": 10,
                          "action": "Done — your Yelp listing is linked", "needs_gmb": False})
    else:
        checklist.append({"label": "Link your Yelp business profile", "effort": "minutes", "why_it_matters": "lets us read your Yelp listing", "done": False, "kind": "setup", "pts": 10,
                          "action": "Go to Account → add your Yelp business ID (find it in your Yelp URL)",
                          "needs_gmb": False})

    # 3. Menu URL — admin sets this; silently included if present, hidden if not
    if bool(r.menu_url):
        checklist.append({"label": "Menu URL added", "effort": "minutes", "why_it_matters": "publishes your menu at a fixed address", "done": True, "kind": "setup", "pts": 10,
                          "action": "Done — your menu is published at a public URL", "needs_gmb": False})

    # 4. Restaurant profile — vibe + known_for + neighborhood power all AI queries
    has_full_profile = bool(r.neighborhood and r.vibe and r.known_for)
    if has_full_profile:
        checklist.append({"label": "Restaurant profile fully filled in", "effort": "minutes", "why_it_matters": "shapes the questions we ask on your behalf", "done": True, "kind": "setup", "pts": 10,
                          "action": "Done — neighborhood, vibe, and specialties all set", "needs_gmb": False})
    else:
        missing = [f for f, v in [("neighborhood", r.neighborhood), ("vibe", r.vibe), ("known for", r.known_for)] if not v]
        checklist.append({"label": "Complete restaurant profile (" + ", ".join(missing) + " missing)", "effort": "minutes", "why_it_matters": "shapes the questions we ask on your behalf", "done": False, "kind": "setup", "pts": 10,
                          "action": "Go to Account → fill in neighborhood, vibe, and what you're known for",
                          "needs_gmb": False})

    # 5. Review volume — AI systems rank by review count; 50+ is the threshold for appearing
    rstats = get_review_stats(rid)
    resp_rate = rstats.get("response_rate", 0) if rstats else 0
    review_total = rstats.get("total", 0) if rstats else 0
    if review_total >= 50:
        checklist.append({"label": "50+ Google reviews", "effort": "months", "why_it_matters": "the slowest signal to build and the hardest to fake", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — 50+ reviews is a strong public signal", "needs_gmb": False})
    elif review_total >= 20:
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "effort": "months", "why_it_matters": "the slowest signal to build and the hardest to fake", "done": False, "kind": "presence", "pts": 10,
                          "action": "Send review requests to recent customers — more reviews is a stronger public signal",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Build to 50+ Google reviews (" + str(review_total) + " so far)", "effort": "months", "why_it_matters": "the slowest signal to build and the hardest to fake", "done": False, "kind": "presence", "pts": 10,
                          "action": "Send review requests after every visit — review volume is the slowest signal to build",
                          "needs_gmb": False})

    # 6. Review response rate — active engagement signals a healthy business to AI tools
    if resp_rate >= 75:
        checklist.append({"label": "Excellent review response rate (" + str(resp_rate) + "%)", "effort": "weeks", "why_it_matters": "visible on your listing to anyone reading it", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — replies are visible on your public listing", "needs_gmb": False})
    elif resp_rate >= 40:
        checklist.append({"label": "Increase response rate to 75%+ (currently " + str(resp_rate) + "%)", "effort": "weeks", "why_it_matters": "visible on your listing to anyone reading it", "done": False, "kind": "presence", "pts": 10,
                          "action": "Use the Reviews tab to draft and post responses — replies show on your public listing",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Start responding to Google reviews (currently " + str(resp_rate) + "%)", "effort": "months", "why_it_matters": "the slowest signal to build and the hardest to fake", "done": False, "kind": "presence", "pts": 10,
                          "action": "Go to Reviews → use AI-drafted responses to reply — aim for 75%+ response rate",
                          "needs_gmb": False})

    # 7. GBP OAuth connected — unlocks real-time profile data and future auto-posting
    if gbp_connected:
        checklist.append({"label": "Google Business Profile connected", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": True, "kind": "setup", "pts": 10,
                          "action": "Done — real-time GBP data is active", "needs_gmb": False})
    else:
        checklist.append({"label": "Connect Google Business Profile (OAuth)", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "setup", "pts": 10,
                          "action": "Go to Account → Connect GBP to unlock live profile editing and Google Posts",
                          "needs_gmb": True})

    # 8. Business description — keyword-rich descriptions are indexed by every AI search tool
    desc = gbp_data.get("description", "")
    if desc and len(desc) >= 150:
        checklist.append({"label": "Business description written (" + str(len(desc)) + " chars)", "effort": "minutes", "why_it_matters": "the text a reader sees under your name", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — your description is published on your listing", "needs_gmb": False})
    elif desc:
        checklist.append({"label": "Expand GBP description to 150+ chars (currently " + str(len(desc)) + ")", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Description: add cuisine type, atmosphere, and signature dishes",
                          "needs_gmb": True})
    else:
        checklist.append({"label": "Write a keyword-rich GBP business description", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Description: mention cuisine, ambiance, and top dishes (150+ chars)",
                          "needs_gmb": True})

    # 9. Phone number in GBP — basic trust signal; missing phone = incomplete listing
    has_phone = bool(gbp_data.get("phone"))
    if gbp_connected and has_phone:
        checklist.append({"label": "Phone number in GBP", "effort": "minutes", "why_it_matters": "a listing without one looks abandoned", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done", "needs_gmb": False})
    elif gbp_connected and not has_phone:
        checklist.append({"label": "Add phone number to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Phone: add your primary number",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add phone number to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Phone: add your primary number",
                          "needs_gmb": True})

    # 10. Website linked in GBP — AI tools follow the website link to gather more context
    has_website = bool(gbp_data.get("website"))
    if gbp_connected and has_website:
        checklist.append({"label": "Website linked in GBP", "effort": "minutes", "why_it_matters": "the one link you control end to end", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — AI tools crawl your website for menu and about content", "needs_gmb": False})
    elif gbp_connected and not has_website:
        checklist.append({"label": "Add website URL to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Website: add your restaurant's website",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add website URL to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Website: add your restaurant's website",
                          "needs_gmb": True})

    # 11. Business hours in GBP — AI tools answer "is it open right now"
    # directly from this field; without it, that whole class of query can't
    # be answered about this restaurant at all, regardless of how complete
    # everything else is. get_gbp_listing's readMask now requests
    # regularHours alongside the fields it already fetched (gmb.py).
    has_hours = bool(gbp_data.get("has_hours"))
    if gbp_connected and has_hours:
        checklist.append({"label": "Hours listed in GBP", "effort": "minutes", "why_it_matters": "the single most-read field on a listing", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — AI tools can answer \"is it open now\" directly", "needs_gmb": False})
    elif gbp_connected and not has_hours:
        checklist.append({"label": "Add hours to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
                          "action": "In Google Business Profile → Info → Hours: set your regular hours",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "Add hours to GBP", "effort": "minutes", "why_it_matters": "unlocks live listing data and Google Posts", "done": False, "kind": "presence", "pts": 10,
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
        # On the guest's own axis: a first connect stamps an entire multi-year
        # history with one fetched_at, which would read here as thirty days of
        # a "steady, current review stream" that in fact stopped years ago.
        "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL "
        "AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now','-30 days')",
        (rid,)
    ).fetchone()[0] or 0
    _rconn.close()
    if recent_reviews >= 3:
        checklist.append({"label": "Active review stream (" + str(recent_reviews) + " in last 30 days)", "effort": "weeks", "why_it_matters": "recency, separate from total volume", "done": True, "kind": "presence", "pts": 10,
                          "action": "Done — a steady, current review stream", "needs_gmb": False})
    elif recent_reviews >= 1:
        checklist.append({"label": "Build a steadier review stream (" + str(recent_reviews) + " in last 30 days)", "effort": "weeks", "why_it_matters": "recency, separate from total volume", "done": False, "kind": "presence", "pts": 10,
                          "action": "Send review requests regularly — a handful of new reviews each month keeps the stream current",
                          "needs_gmb": False})
    else:
        checklist.append({"label": "No reviews in the last 30 days", "effort": "weeks", "why_it_matters": "recency, separate from total volume", "done": False, "kind": "presence", "pts": 10,
                          "action": "Send review requests to recent customers — recency is its own signal, separate from total volume",
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
    # Two scores, because these were two different things under one name.
    #
    # gbp_score counted "is a Yelp ID typed into Cavnar AI" in the same
    # percentage as "does your Google listing have opening hours on it",
    # and called the result AI visibility. Typing a Yelp ID into a settings
    # box does not change anything a guest or a search engine can see; it
    # was worth ten points on the headline number of this screen.
    #
    # presence_score covers only what is actually true of the restaurant's
    # public listing and review record: review volume, response rate,
    # recency, description, phone, website, hours. setup_items are the
    # Cavnar-side connections, listed and counted but never scored, because
    # they describe this product's configuration rather than the
    # restaurant's standing anywhere.
    # An item with no kind is a bug, not a category. Defaulting it into
    # either bucket hides the mistake; naming it makes the next one obvious.
    _untagged = [i["label"] for i in checklist
                 if i.get("kind") not in ("presence", "setup") or not i.get("effort")]
    if _untagged:
        print(f"[aivis] checklist items missing kind or effort: {_untagged}")
        try:
            import ops as _ops_aiv
            _ops_aiv.capture(RuntimeError(f"untagged AI-visibility checklist items: {_untagged}"),
                             job="ai_visibility", context=f"restaurant_id={rid}")
        except Exception:
            pass
    presence_items = [i for i in checklist if i.get("kind") == "presence"]
    setup_items    = [i for i in checklist if i.get("kind") == "setup"]
    _presence_done = sum(1 for item in presence_items if item["done"])
    _setup_done    = sum(1 for item in setup_items if item["done"])
    presence_score = round(_presence_done / len(presence_items) * 100) if presence_items else 0
    setup_done, setup_total = _setup_done, len(setup_items)
    # Kept so an older client still decodes something sane; it is the
    # presence figure now, not the blended one.
    gbp_score = presence_score

    # Social posting cadence — deliberately NOT a checklist item / part of
    # gbp_score (this isn't a Google Business Profile field, it's marketing
    # activity within this app), returned as its own field so the roadmap's
    # "Post consistently on social" card can auto-complete instead of
    # always showing not-done. marketing_content_log only proves content
    # was drafted through this app, not confirmed-posted to a platform —
    # an imperfect signal, but a real, live one rather than none at all.
    # Same table-creation pattern mobile_api.py's own home-KPI query uses
    _conn = get_conn()
    social_posts_30d = _conn.execute(
        "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','-30 days')",
        (rid,)
    ).fetchone()[0] or 0
    _conn.close()

    # Denominator is the queries that came back, not the ones we sent: a
    # throttled query used to drag the score down and then be written into
    # ai_visibility_runs, where it became a "declining visibility" data point
    # the owner reads as real.
    ai_score = round((appeared_count / len(discovery)) * 100) if discovery else None
    # With no city nothing can be matched (_mentions_this_restaurant needs
    # one), so a 0 here would be ours, not the restaurant's (MOD-INT-4). No
    # score, and — below — no history row.
    if not city:
        ai_score = None

    # A point estimate from a handful of non-deterministic queries is not a
    # measurement, and drawing it as one is how ordinary model variance
    # became a "declining visibility" trend line and an SMS. The interval
    # is the Wilson score at 90%, which is the standard way to put bounds
    # on a proportion from a small sample and degrades sensibly at 0 and
    # 100 where a naive interval does not.
    ai_score_low = ai_score_high = None
    if discovery and ai_score is not None:
        import math as _math
        _n = len(discovery)
        _p = appeared_count / _n
        _z = 1.645  # 90%
        _d = 1 + _z * _z / _n
        _c = (_p + _z * _z / (2 * _n)) / _d
        _m = (_z * _math.sqrt(_p * (1 - _p) / _n + _z * _z / (4 * _n * _n))) / _d
        ai_score_low = max(0, round((_c - _m) * 100))
        ai_score_high = min(100, round((_c + _m) * 100))

    _run_id = None
    try:
        from models import record_ai_visibility_run, record_ai_visibility_queries
        # A partial run is not a measurement. Show it, don't record it.
        if ai_score is not None and len(answered) == len(queries):
            _run_id = record_ai_visibility_run(
                rid, ai_score, gbp_score,
                answered=len(discovery), appeared=appeared_count,
                city_basis=f"{city_source}:{_norm(city)}")
            # What was asked, what came back, and what grounded it. The runs
            # table held a score and nothing else, so a change could never be
            # explained — while the drop alert told the owner to open Intel
            # and see which questions changed.
            record_ai_visibility_queries(_run_id, rid, query_results)
    except Exception as _he:
        print(f"[aivis] history write failed for rid={rid}: {_he}")

    _payload = {
        "ok": True,
        # Which system was asked, and with what model. A score from one
        # vendor was being presented as "AI search" generally.
        "platform": AIVIS_PLATFORM,
        "model": AIVIS_MODEL,
        "restaurant_name": name,
        "neighborhood": neighborhood,
        "queries": query_results,
        "appeared_count": appeared_count,
        "total_queries": len(queries),
        # answered_queries is what ai_score is actually out of. When it is
        # below total_queries the run is partial: show the score as an
        # estimate, not a measurement, and say why.
        "answered_queries": len(discovery),
        "partial": len(answered) < len(queries),
        # Branded recall, kept separate from discovery. None when no branded
        # question was asked or answered.
        "branded_score": branded_score,
        "branded_queries": len(branded),
        # Which competitors surfaced in the same answers, and in how many.
        "competitor_appearances": competitor_appearances,
        # No city on the profile means two locations of the same brand are
        # indistinguishable in an answer, so appearance cannot be judged at
        # all. Surface that rather than silently scoring 0.
        "location_known": bool(city),
        # Where the city in those queries came from. "profile" means it is a
        # free-text field an owner typed, not Google's own address for this
        # listing, and the appearance match is only as good as that string.
        "city": city_full,
        "city_source": city_source,
        "ai_score": ai_score,
        # The honest bounds on ai_score for this sample size. Present the
        # range, not the point.
        "ai_score_low": ai_score_low,
        "ai_score_high": ai_score_high,
        "gbp_score": gbp_score,
        # gbp_score's two halves, split apart — see the comment at their
        # computation. presence_score is the only one that describes the
        # restaurant rather than this product's own configuration.
        "presence_score": presence_score,
        "setup_done": setup_done,
        "setup_total": setup_total,
        # Which kind of claim each number is, so a client can stop rendering
        # a measurement, an estimate and a configuration count identically.
        # Same convention the Reviews module already ships.
        "claim_kinds": {
            "ai_score": "measured" if (answered and len(answered) == len(queries)) else "partial",
            "ai_score_low": "estimate",
            "ai_score_high": "estimate",
            "presence_score": "measured",
            "branded_score": "measured",
            "competitor_appearances": "measured",
            "setup_done": "configuration",
        },
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
    }
    # Only a COMPLETE run is worth caching. Caching a partial one would pin a
    # Perplexity outage in place for six hours and make it look like the
    # restaurant's real standing.
    if not _payload["partial"]:
        _aivis_cache[rid] = (datetime.utcnow(), dict(_payload))
    if _run_id:
        try:
            import json as _json_av
            from models import attach_ai_visibility_payload
            attach_ai_visibility_payload(_run_id, _json_av.dumps(_payload, default=str))
        except Exception as _pe:
            print(f"[aivis] payload store failed for rid={rid}: {_pe}")
    return _payload, 200


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
    # The signing secret is the owner's; a teammate sees that a webhook
    # exists and its health, not the key that forges its payloads (SEC-24).
    from permissions import is_principal
    return jsonify(ok=True, webhook={
        "url":                  wh["url"],
        "secret":               wh["secret"] if is_principal(current_user) else None,
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
    from permissions import principal_only
    denied = principal_only(current_user, "the webhook")
    if denied:
        return denied
    from webhooks import reactivate_webhook
    reactivate_webhook(current_user["restaurant_id"])
    return jsonify(ok=True)

@client_bp.route("/api/webhook", methods=["POST"])
@login_required
def webhook_save(current_user):
    # The webhook receives every review and alert, signed with a secret this
    # response hands back — an owner's decision, not any teammate's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the webhook")
    if denied:
        return denied
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
        return jsonify(ok=False, error=_safe_err(e))
    return jsonify(ok=True, secret=secret)

@client_bp.route("/api/webhook", methods=["DELETE"])
@login_required
def webhook_delete(current_user):
    from permissions import principal_only
    denied = principal_only(current_user, "the webhook")
    if denied:
        return denied
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
    """Web twin — the one body is mobile_api.mobile_add_guest_contact."""
    return _m("mobile_add_guest_contact")(current_user)

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
    """Web twin — the one body is mobile_api.mobile_guest_campaign_draft."""
    return _m("mobile_guest_campaign_draft")(current_user)

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _track_campaign_outcome(rid, data, result, user_id):
    """A campaign aimed at a slow weekday starts an outcome tracker on that
    weekday's sales — only once it has actually SENT. Recording on the Ask
    confirmation instead would track a campaign that quiet hours or an empty
    segment then refused. Best-effort: tracking never fails the send."""
    day = (data.get("target_day") or "").strip().capitalize()
    if day not in _WEEKDAYS or not (result or {}).get("ok"):
        return
    try:
        import outcomes
        from datetime import date as _d
        outcomes.record(rid, "slow_day_campaign", f"campaign:{day}:{_d.today().isoformat()}",
                        f"Guest text to lift {day}s", f"weekday_sales:{day}", user_id=user_id)
    except Exception as e:
        import ops
        ops.capture(e, job="campaign_outcome", context=f"restaurant_id={rid}")


@client_bp.route("/api/guest-campaign/send", methods=["POST"])
@login_required
def guest_campaign_send(current_user):
    """Web twin — the one body is mobile_api.mobile_guest_campaign_send."""
    return _m("mobile_guest_campaign_send")(current_user)


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
    from guest_links import sign_join
    join_url = request.url_root.rstrip("/") + f"/join/{sign_join(rid)}"
    img = qrcode.make(join_url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    download = request.args.get("download") == "1"
    return send_file(buf, mimetype="image/png", as_attachment=download,
                      download_name="guest-text-club-qr.png" if download else None)


# ── Public, unauthenticated: media, short links, newsletter unsubscribe ────
# All three are fetched by someone who cannot log in — Meta's servers pulling
# an image, a guest tapping a link in a text, a mail client honouring
# List-Unsubscribe — so each is guarded by an unguessable token instead, the
# same trade the staff schedule share links already make.

@client_bp.route("/m/<token>.jpg")
def marketing_media_file(token):
    """The photo Instagram and Google fetch at publish time."""
    from marketing_media import get_image
    found = get_image(token)
    if not found:
        return "Not found", 404
    data, mime = found
    import io
    resp = send_file(io.BytesIO(data), mimetype=mime or "image/jpeg")
    # Meta re-fetches on retries; a long cache keeps a scheduled post from
    # hammering the database on every attempt.
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@client_bp.route("/g/<token>")
def marketing_link_redirect(token):
    """Counts the tap, then sends them where they were going."""
    from marketing_links import resolve
    target = resolve(token)
    if not target:
        return render_template("staff_schedule_invalid.html"), 404
    return redirect(target, code=302)


@client_bp.route("/e/<token>", methods=["GET", "POST"])
def guest_newsletter_unsubscribe(token):
    """Per-guest, and it works without signing in — CAN-SPAM requires the link
    to work for someone who has no account here and never will."""
    from guest_email import unsubscribe
    name = unsubscribe(token)
    if not name:
        return render_template("unsubscribed.html", ok=False, restaurant_name=""), 404
    return render_template("unsubscribed.html", ok=True, restaurant_name=name)


# ── Public guest opt-in page — no login, printed on a table tent / QR code ──
# Also module-gated: if a client's Marketing module is later removed, their
# old join link/QR code (already printed, already in the wild) must stop
# accepting new signups rather than keep working for free.

@client_bp.route("/join/<token>")
def guest_optin_page(token):
    from guest_links import verify_join
    restaurant_id = verify_join(token)
    if not restaurant_id:
        return "Not found", 404
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    if not restaurant.module_marketing:
        return "This text club isn't active right now.", 404
    return render_template("guest_optin.html", restaurant_name=restaurant.name)

@client_bp.route("/api/public/guest-optin/<token>", methods=["POST"])
def guest_optin_submit(token):
    from guest_links import verify_join
    restaurant_id = verify_join(token)
    if not restaurant_id:
        return jsonify(ok=False, error="Not found"), 404
    from ai_utils import ai_rate_limited
    ip = request.remote_addr or "unknown"   # ProxyFix-vouched, never the client's own header (SEC-3)
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
    contact_id = add_guest_contact_public_optin(restaurant_id, phone, name=name)

    # Email is optional and separately consented — ticking the SMS box is not
    # agreement to a newsletter, same principle the SMS side is built on. An
    # address given without the box is stored but never mailed.
    from guest_email import valid_email, set_guest_email
    email = valid_email(data.get("email"))
    if email and contact_id:
        set_guest_email(contact_id, restaurant_id, email,
                        consent=bool(data.get("email_consent")))
    return jsonify(ok=True)


# ── Email history + preview send (web parity) ───────────────────────────────

@client_bp.route("/api/email-history")
@login_required
def email_history(current_user):
    """What Cavnar has actually sent on this restaurant's behalf, and
    whether it landed. Until email_log carried a real status this could
    only ever have said "sent"."""
    from models import get_email_log_for_client
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    return jsonify(ok=True, emails=get_email_log_for_client(current_user["restaurant_id"], limit=limit))


@client_bp.route("/api/send-test-digest", methods=["POST"])
@login_required
def send_test_digest_web(current_user):
    """Web twin of mobile_api's /account/send-test-digest — same build and
    render, sent to whoever is logged in."""
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    to_email = current_user.get("email") or restaurant.owner_email
    if not to_email:
        return jsonify(ok=False, error="No email on file for your account."), 400
    try:
        from reporter import build_report_from_db, render_html
        from emails import deliver, _from_email
        report = build_report_from_db(rid, restaurant.name, days=7)
        from permissions import has_permission as _hp_dg, LOSS_VIEW as _lv_dg
        html = render_html(report, restaurant.name, owner_name=restaurant.owner_name, restaurant_id=rid,
                           owner_view=_hp_dg(current_user, _lv_dg))
        result = deliver({
            "from": f"Cavnar AI <{_from_email()}>",
            "to": [to_email],
            "subject": f"[Preview] Your weekly review digest — {restaurant.name}",
            "html": _html_doc(html),
        }, restaurant_id=rid, email_type="digest_preview")
        if not result.ok:
            return jsonify(ok=False, error="Couldn't send the preview — try again in a moment."), 502
        return jsonify(ok=True, email=to_email)
    except Exception as e:
        import ops
        ops.capture(e, job="send_test_digest_web", context=f"restaurant_id={rid}")
        return jsonify(ok=False, error="Couldn't build the preview digest."), 500


# ── Public marketing unsubscribe ────────────────────────────────────────────
# Deliberately public and login-free: the person who wants out is reading an
# email, not signed into a dashboard. Only ever sets the marketing flag —
# security mail (2FA, password resets) is unaffected, and there is no way to
# reach any other setting from this token.

@client_bp.route("/u/<token>", methods=["GET", "POST"])
def marketing_unsubscribe(token):
    from models import verify_unsubscribe_token
    rid = verify_unsubscribe_token(token)
    if not rid:
        return render_template("unsubscribed.html", ok=False, restaurant_name=""), 404
    restaurant = get_restaurant(rid)
    if not restaurant:
        return render_template("unsubscribed.html", ok=False, restaurant_name=""), 404

    # RFC 8058 one-click: a POST from the mail client unsubscribes directly.
    # A GET shows the same confirmation, so a human clicking the link in the
    # footer gets a page rather than a silent no-op.
    update_restaurant(rid, {"marketing_emails_opt_out": 1})
    return render_template("unsubscribed.html", ok=True,
                           restaurant_name=restaurant.name or "")


def _do_switch_location(current_user, target_id, token):
    from permissions import LOCATION_SWITCH, has_permission
    if not has_permission(current_user, LOCATION_SWITCH):
        return {"ok": False, "error": "Not an owner account"}, 403
    if not target_id:
        return {"ok": False, "error": "Missing restaurant_id"}, 400
    # Validate target is in same group as base restaurant
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return {"ok": False, "error": "No location group configured"}, 400
    group = get_location_group(base.location_group, owner_email=base.owner_email)
    valid_ids = [r["id"] for r in group]
    if target_id not in valid_ids:
        return {"ok": False, "error": "Location not in your group"}, 403
    from auth import switch_active_restaurant
    switch_active_restaurant(token, target_id)
    try:
        # This login's cached Homes only — not every tenant's (MOD-HOME-3).
        import home_brief; home_brief.invalidate_user(current_user.get("id"))
    except Exception:
        pass
    target = get_restaurant(target_id)
    return {"ok": True, "restaurant_name": target.name, "restaurant_id": target_id}, 200


def _do_group_locations(current_user):
    from permissions import LOCATION_SWITCH, has_permission
    if not has_permission(current_user, LOCATION_SWITCH):
        return {"ok": True, "locations": []}, 200
    from models import get_restaurant, get_location_group
    base = get_restaurant(current_user["base_restaurant_id"])
    if not base or not base.location_group:
        return {"ok": True, "locations": []}, 200
    group = get_location_group(base.location_group, owner_email=base.owner_email)
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
    "3star":            "3★ review received",
    "5star":            "5★ review received",
    "any_review":       "New review",
    "health":           "Health/safety mention",
    "edit_downgrade":   "A guest lowered their review",
    "resp_approved":    "Your reply went out",
    "neg_spike":        "Negative review spike",
    "negative_trend":   "Rating declining trend",
    "no_response":      "Unresponded review (48h)",
    "unresponded":      "Unresponded review (48h)",
    "rating_threshold": "Rating below threshold",
    "labor_over":       "Labor % over target",
    "coverage":         "Someone hasn't clocked in",
    "food_waste":       "Food waste flagged",
    "critical_low":     "Running out before delivery",
    "price_spike":      "Ingredient price climbing",
    "ai_visibility_drop": "AI visibility dropped",
    "login":            "New sign-in",
    "staff_signin":     "Staff portal sign-in",
    "issue":            "An issue was opened",
    "issue_escalated":  "An issue was escalated",
    "outcome_achieved": "A change you made paid off",
    "demand_opportunity": "A quiet night worth filling",
    "morning_brief":    "Morning brief",
    "daily_briefing":   "Your day, in one place",
    "intraday_pulse":   "Today vs a typical day",
    "closing_summary":  "How tonight went",
    "weekly_review":    "Your week",
    "monthly_review":   "Your month",
    "schedule_drafted": "Next week's schedule drafted",
    "order_send_pending": "Supplier order going out",
    "order_send_voided": "Supplier order not sent",
}

# Which module a notification's "view" action should open. The keys are the
# web tab ids (?tab=), and iOS DeepLinkRouter maps the same strings.
#
# Nine types that actually fire had no entry here and no label either, so
# .get(type, "reviews") sent a food-cost alert to Reviews while .get(type,
# type) printed the raw column value — an owner's notification list read
# "ai_visibility_drop" and "critical_low".
_NOTIFICATION_MODULE = {
    "1star": "reviews", "2star": "reviews", "3star": "reviews", "5star": "reviews",
    "any_review": "reviews", "health": "reviews", "edit_downgrade": "reviews",
    "resp_approved": "reviews", "neg_spike": "reviews", "no_response": "reviews",
    "unresponded": "reviews", "negative_trend": "reviews", "rating_threshold": "reviews",
    "labor_over": "labor", "schedule_drafted": "labor", "coverage": "labor",
    "food_waste": "inventory", "critical_low": "inventory", "price_spike": "inventory",
    "order_send_pending": "inventory", "order_send_voided": "inventory",
    "ai_visibility_drop": "competitor",
    "demand_opportunity": "marketing",
    # Cross-module reads that arrive with their own question, so they open
    # the assistant rather than guessing a module (iOS does the same).
    "morning_brief": "ask", "daily_briefing": "ask", "intraday_pulse": "ask",
    "closing_summary": "ask", "weekly_review": "ask", "monthly_review": "ask",
    "outcome_achieved": "ask",
    "issue": "account", "issue_escalated": "account",
    # Not a product module — the web dashboard's bell reads this field
    # directly; iOS's DeepLinkRouter has its own "login" special-case.
    "login": "account", "staff_signin": "account",
}

# Which module permission a row needs before it is shown. A teammate whose
# role cannot open Labor or Food Cost was still shown "Labor % over target"
# and "Food waste flagged" in the bell — the role scoping applied to Ask and
# Home never reached the notification list.
_NOTIFICATION_MODULE_KEY = {
    "reviews": "reviews", "labor": "labor", "inventory": "inventory",
    "marketing": "marketing", "competitor": "intel",
}


def _do_get_notifications(restaurant_id, viewer=None, limit=40):
    """The notification history, newest first, scoped to what this login may
    see and carrying the priority both clients rank by."""
    try:
        import push as _push
        conn = get_conn()
        rows = conn.execute(
            """SELECT alert_type, review_id, fired_at, priority FROM alert_log
               WHERE restaurant_id=?
               ORDER BY fired_at DESC, id DESC LIMIT ?""",
            (restaurant_id, int(limit))
        ).fetchall()
        seen_at = None
        if viewer and viewer.get("id"):
            from models import notifications_seen_at
            seen_at = notifications_seen_at(viewer["id"], restaurant_id)
        conn.close()
        items = []
        for r in rows:
            module = _NOTIFICATION_MODULE.get(r["alert_type"], "reviews")
            if not _sees(viewer, module):
                continue
            priority = r["priority"]
            if priority is None:
                priority = _push.priority_of(r["alert_type"])
            items.append({
                "type": r["alert_type"],
                "label": _NOTIFICATION_LABELS.get(r["alert_type"],
                                                  r["alert_type"].replace("_", " ").capitalize()),
                "fired_at": r["fired_at"],
                "review_id": r["review_id"],
                "module": module,
                "priority": priority,
                "urgent": priority <= _push.P1_ACT_NOW,
                "unread": bool(seen_at is None or (r["fired_at"] or "") > seen_at),
            })
        return {"ok": True, "notifications": items}, 200
    except Exception as e:
        print(f"[notifications] load failed for rid={restaurant_id}: {e}")
        return {"ok": False, "notifications": [], "error": "Couldn't load notifications right now."}, 200


def _sees(viewer, module):
    """Whether this login may see a row about `module`. No viewer means an
    internal/admin caller, which sees everything."""
    if not viewer or module not in _NOTIFICATION_MODULE_KEY:
        return True
    try:
        from permissions import has_permission, MODULE_VIEW_PERMISSIONS
        need = MODULE_VIEW_PERMISSIONS.get(_NOTIFICATION_MODULE_KEY[module])
        return has_permission(viewer, need) if need else True
    except Exception:
        return True


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
    payload, status = _do_get_notifications(current_user["restaurant_id"], viewer=current_user)
    if payload.get("ok"):
        try:
            from models import mark_notifications_seen
            mark_notifications_seen(current_user["id"], current_user["restaurant_id"])
        except Exception:
            pass
    return jsonify(**payload), status


@client_bp.route("/api/account/send-test-push", methods=["POST"])
@login_required
def send_test_push_route(current_user):
    """Web twin — the one body is mobile_api.mobile_send_test_push."""
    return _m("mobile_send_test_push")(current_user)


@client_bp.route("/api/notifications/opened", methods=["POST"])
@login_required
def mark_notification_opened(current_user):
    """Web twin — the one body is mobile_api.mobile_mark_notification_opened."""
    return _m("mobile_mark_notification_opened")(current_user)


@client_bp.route("/api/notifications/engagement")
@login_required
def notifications_engagement(current_user):
    """Types this restaurant gets a lot of and never opens — the raw material
    for one sentence in Account, not an automatic change."""
    import notify
    from client_api import _NOTIFICATION_LABELS as _labels
    rows = notify.engagement_report(current_user["restaurant_id"])
    for row in rows:
        row["label"] = _labels.get(row["alert_type"], row["alert_type"])
    return jsonify(ok=True, suggestions=rows)


@client_bp.route("/api/notifications/unread-count")
@login_required
def get_notifications_unread_count(current_user):
    """Per-LOGIN, so a co-owner opening the bell no longer clears their
    partner's badge — and it reads through models.unread_notification_count,
    which compares timestamps in one format. The web bell kept its own
    localStorage mark, so the two clients never agreed either."""
    from models import unread_notification_count
    return jsonify(ok=True, count=unread_notification_count(
        current_user["id"], current_user["restaurant_id"]))


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Ryan seed (module-level — runs under Gunicorn AND direct python) ─────────




# ── Staff-facing schedule page ─────────────────────────────────────────────────

@client_bp.route("/s/<token>")
def staff_schedule_page(token):
    """One employee's own shifts, no login. Deliberately public: kitchen and
    floor staff don't have dashboard accounts, and requiring one is exactly
    why schedules end up as a photo of a printout in a group chat.

    The token is the whole authorisation — long, random, per employee per
    schedule (models.create_schedule_share) — and it only ever exposes that
    one person's shifts, never the full roster, wages, or anything else.
    An unknown token 404s rather than saying whether it ever existed.
    """
    from models import get_schedule_share, mark_schedule_share_viewed
    from labor import employee_shifts_from_csv

    share = get_schedule_share(token)
    if not share:
        # A branded page, not the bare string this used to return — the one
        # link in any Cavnar AI email that could land a member of staff on
        # something that looked broken. Its OWN template, not the expired
        # one: saying "expired" would confirm the token had once been real,
        # which is precisely what this branch is careful not to reveal.
        return render_template("staff_schedule_invalid.html"), 404
    if share.get("expired"):
        # 410 Gone, not 404: the link was real, it has simply aged out. Says
        # so plainly so someone who no longer works here isn't left guessing,
        # and doesn't leak any shift data.
        return render_template("staff_schedule_expired.html",
                               restaurant_name=share.get("restaurant_name") or ""), 410

    from models import get_staff_availability
    import json as _json_av

    shifts = employee_shifts_from_csv(share.get("schedule_csv") or "", share["employee_name"])
    mark_schedule_share_viewed(token)

    # Whatever they last told us — days and note — so the form comes back
    # pre-filled rather than making them re-enter it every week, and a
    # re-save cannot silently blank the note (CLIENT-11).
    unavailable, saved_note = [], ""
    for row in get_staff_availability(share["restaurant_id"]):
        if (row.get("employee_name") or "").strip().lower() == share["employee_name"].strip().lower():
            try:
                unavailable = _json_av.loads(row.get("unavailable_days") or "[]")
            except Exception:
                unavailable = []
            saved_note = row.get("notes") or ""
            break
    from time_utils import mdy as _mdy

    # The availability form below is a plain HTML POST, not a fetch, so it
    # can't use the dashboard's fetch wrapper to supply the CSRF header —
    # it echoes the same csrf_js cookie back as a hidden field instead
    # (csrf.py's _token_from_request accepts that form fallback). On a
    # first visit the cookie hasn't been issued yet (ensure_csrf_cookie
    # runs after_request), so mint it here and set it on this response,
    # otherwise the very first submission a staff member makes would be
    # rejected. Exempting the route was the alternative and is strictly
    # worse: the token is what stops a third-party page from posting
    # availability on someone's behalf.
    import secrets as _secrets_csrf
    from csrf import CSRF_COOKIE as _CSRF_COOKIE

    csrf_token = request.cookies.get(_CSRF_COOKIE) or _secrets_csrf.token_urlsafe(32)
    response = make_response(render_template(
        "staff_schedule.html",
        restaurant_name=share.get("restaurant_name") or "",
        employee_name=share["employee_name"],
        week_start=_mdy(share.get("week_start")),
        week_end=_mdy(share.get("week_end")),
        shifts=[dict(s, date_label=_mdy(s.get("date"))) for s in shifts],
        total_hours=round(sum(s["hours"] for s in shifts), 1),
        token=token,
        days=DAY_NAMES,
        unavailable_days=unavailable,
        note=saved_note,
        saved=request.args.get("saved") == "1",
        all_days_blocked=request.args.get("error") == "all_days",
        csrf_token=csrf_token,
    ))
    if not request.cookies.get(_CSRF_COOKIE):
        response.set_cookie(_CSRF_COOKIE, csrf_token, max_age=30 * 24 * 3600,
                            httponly=False, secure=config.on_railway(),
                            samesite="Lax")
    return response


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@client_bp.route("/s/<token>/availability", methods=["POST"])
def staff_availability_submit(token):
    """Staff tell the schedule when they can't work, from the same link.

    This closes a real loop: staff_availability is already a HARD
    constraint in schedule generation (see labor.py's EMPLOYEE AVAILABILITY
    block), so what someone submits here genuinely shapes next week's
    schedule instead of going to a manager to be re-typed.

    Authorised by the same per-person token as the page itself, and it can
    only ever write that one person's row — the employee name comes from
    the token, never from the form, so a submitted name can't be forged.
    """
    from models import get_schedule_share, save_staff_availability, get_staff_availability
    from ai_utils import ai_rate_limited

    share = get_schedule_share(token)
    if not share:
        return "This link isn't valid. Ask your manager for a new one.", 404
    if share.get("expired"):
        # An expired link is read-only-gone in both directions — it must not
        # keep writing availability that would shape next week's schedule.
        return render_template("staff_schedule_expired.html",
                               restaurant_name=share.get("restaurant_name") or ""), 410

    ip = request.remote_addr or "unknown"   # ProxyFix-vouched, never the client's own header (SEC-3)
    if ai_rate_limited(f"staffavail:{ip}", max_calls=20, window_secs=300):
        return "Too many updates just now — try again in a few minutes.", 429

    # The same rule the portal applies (CLIENT-11): 7 of 7 blocked is
    # refused, and available_days is the complement, never a stale list.
    from staff_routes import availability_from_submission
    available, blocked, note, err = availability_from_submission(
        request.form.getlist("unavailable"), request.form.get("note"))
    if err:
        return redirect(f"/s/{token}?error=all_days")

    # The page pre-fills the note and says so with note_prefilled, so there a
    # blank field means "clear it". A form without it (a tab opened before the
    # field was pre-filled) never showed the note, so a blank keeps it.
    if note is None and not request.form.get("note_prefilled"):
        for row in get_staff_availability(share["restaurant_id"]):
            if (row.get("employee_name") or "").strip().lower() == share["employee_name"].strip().lower():
                note = row.get("notes") or None
                break

    save_staff_availability(share["restaurant_id"], share["employee_name"],
                            available, blocked, note)
    return redirect(f"/s/{token}?saved=1")


# ── Shared account settings (web + iOS) ────────────────────────────────────────
#
# These five settings were built iOS-first and had no web equivalent, which
# left web-only users unable to see or change behaviour that was running for
# them regardless — the auto-approve rule in particular publishes review
# replies on their behalf from a scheduled job. The handlers live here (the
# import direction is mobile_api -> client_api) so both surfaces run the
# same code and can't drift apart again.

def log_account_event(restaurant_id, event_type, current_user=None, detail=None):
    """Account activity log (Account -> Security -> Account activity).
    Shared so a change made on the web is recorded identically to one made
    in the app — mobile_api._log_account_event delegates here."""
    try:
        from models import log_event
        data = {"detail": detail}
        if current_user:
            data["actor"] = current_user.get("username")
        log_event(restaurant_id, event_type, data)
    except Exception:
        pass


def _auto_approve_trust_safe(rid):
    try:
        from models import auto_approve_trust
        return {str(k): v for k, v in auto_approve_trust(rid).items()}
    except Exception:
        return {}


def _do_auto_approve(rid, data, current_user=None):
    """The one rule: drafted 5-star responses get approved (and posted, when
    Google is connected) without waiting — capped per day, with a kill
    switch. Runs inside the daily fetch (scheduler.auto_approve_five_stars).
    Owner-only: it publishes replies under the brand with nobody reading
    them first (SEC-24)."""
    from permissions import is_principal
    if current_user is not None and not is_principal(current_user):
        return {"ok": False, "owner_only": True,
                "error": "Only the account owner can change auto-approve."}, 403
    cap = (data or {}).get("daily_cap", 5)
    try:
        cap = max(1, min(50, int(cap)))
    except Exception:
        cap = 5
    enabled = bool((data or {}).get("enabled"))
    paused = bool((data or {}).get("paused"))
    include_4star = bool((data or {}).get("include_4star"))
    earned = bool((data or {}).get("earned"))
    update_restaurant(rid, {
        "auto_approve_5star": int(enabled),
        "auto_approve_4star": int(enabled and include_4star),
        "auto_approve_earned": int(enabled and earned),
        "auto_approve_daily_cap": cap,
        "auto_approve_paused": int(paused),
    })
    log_account_event(rid, "auto_approve_changed", current_user,
                      detail=("on" if enabled else "off") + (" incl. 4-star" if enabled and include_4star else "")
                             + (", earned bands" if enabled and earned else "")
                             + (", paused" if paused else "") + f", cap {cap}/day")
    return {"ok": True}, 200


_ACCOUNT_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _do_account_hours(rid, data, current_user=None):
    """Open/close time per day plus closure dates. close_times_json already
    drove schedule generation (shift_end hard cap); open_times_json too."""
    import json as _json_h

    def _clean_times(raw):
        out = {}
        for day in _ACCOUNT_DAYS:
            v = (raw or {}).get(day)
            if isinstance(v, str) and v.strip():
                out[day] = v.strip()[:12]
        return _json_h.dumps(out) if out else None

    closures = (data or {}).get("closures") or []
    if not isinstance(closures, list):
        closures = []
    closures = sorted({str(c).strip()[:10] for c in closures if str(c).strip()})[:60]
    update_restaurant(rid, {
        "open_times_json": _clean_times((data or {}).get("open")),
        "close_times_json": _clean_times((data or {}).get("close")),
        "skip_holidays": ",".join(closures) or None,
    })
    log_account_event(rid, "hours_changed", current_user)
    return {"ok": True}, 200


def _do_data_retention(rid, data, current_user=None):
    """0 = keep everything; otherwise reviews older than N months are
    soft-deleted by the nightly job (models.purge_expired_reviews).
    Owner-only: a shorter window deletes review history (SEC-24)."""
    from permissions import is_principal
    if current_user is not None and not is_principal(current_user):
        return {"ok": False, "owner_only": True,
                "error": "Only the account owner can change how long reviews are kept."}, 403
    try:
        months = int((data or {}).get("months", 0))
    except Exception:
        months = 0
    if months not in (0, 6, 12, 24, 36):
        return {"ok": False, "error": "Choose keep everything, or 6, 12, 24 or 36 months."}, 400
    update_restaurant(rid, {"data_retention_months": months})
    log_account_event(rid, "data_retention_changed", current_user,
                      detail=f"{months} months" if months else "keep everything")
    return {"ok": True}, 200


def _do_marketing_opt_out(rid, data, current_user=None):
    """Gates only the promotional onboarding drip at its scheduler.py call
    sites — security/transactional email (2FA, login notify,
    password/email-changed, welcome) is never affected, and neither is the
    monthly business review, which has its own switch below. Until the ROI
    audit this also silenced the monthly review, so an owner declining
    promotional mail lost the one email that reports what their changes were
    measured to do."""
    opted_out = bool((data or {}).get("opted_out"))
    update_restaurant(rid, {"marketing_emails_opt_out": int(opted_out)})
    log_account_event(rid, "marketing_emails_changed", current_user,
                      detail="off" if opted_out else "on")
    return {"ok": True}, 200


def _do_monthly_review_pref(rid, data, current_user=None):
    """The monthly business review on or off. Separate from marketing mail:
    this is a service report on a paid account."""
    on = bool((data or {}).get("enabled"))
    update_restaurant(rid, {"monthly_review_enabled": int(on)})
    log_account_event(rid, "monthly_review_changed", current_user,
                      detail="on" if on else "off")
    return {"ok": True}, 200


def _do_brand_voice(rid, data, current_user=None):
    """The three freeform fields that steer every piece of generated copy.

    These were editable on iOS (Account -> Profile) and admin-only on web, so
    the dashboard's Marketing tab offered "Update brand voice" as a mailto to
    Will for something the same client could already change themselves on
    their phone. Same three fields, same sanitising and same length caps as
    mobile_api.mobile_update_profile — deliberately NOT name/neighborhood/
    vibe/known_for, which feed string matching in competitor lookups and stay
    admin-set (see that route's docstring)."""
    import re as _re_bv

    def _clean(value, max_len):
        if value is None:
            return None
        value = _re_bv.sub(r"<[^>]+>", "", str(value))
        value = _re_bv.sub(r"(?i)javascript\s*:", "", value)
        return value[:max_len].strip() or None

    update_restaurant(rid, {
        "voice_notes": _clean((data or {}).get("voice_notes"), 1000),
        "never_say": _clean((data or {}).get("never_say"), 1000),
        "menu_notes": _clean((data or {}).get("menu_notes"), 2000),
        # The phone's profile sheet has these two as well; one brand voice,
        # same fields on both.
        "sign_off_name": _clean((data or {}).get("sign_off_name"), 80),
    })
    log_account_event(rid, "brand_voice_changed", current_user)
    return {"ok": True}, 200


def _do_login_notify(rid, data, current_user=None):
    enabled = bool((data or {}).get("enabled"))
    update_restaurant(rid, {"login_notify": int(enabled)})
    log_account_event(rid, "login_notify_changed", current_user,
                      detail="on" if enabled else "off")
    return {"ok": True}, 200


def _account_settings_payload(rid):
    """Everything the web Account panel needs to render these five settings."""
    import json as _json_s
    r = get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Restaurant not found"}, 404

    def _times(raw):
        try:
            return _json_s.loads(raw) if raw else {}
        except Exception:
            return {}

    closures = [c for c in (getattr(r, "skip_holidays", "") or "").split(",") if c.strip()]
    return {
        "ok": True,
        "auto_approve": {
            "enabled": bool(getattr(r, "auto_approve_5star", 0)),
            "include_4star": bool(getattr(r, "auto_approve_4star", 0)),
            "earned": bool(getattr(r, "auto_approve_earned", 0)),
            "daily_cap": int(getattr(r, "auto_approve_daily_cap", 5) or 5),
            "paused": bool(getattr(r, "auto_approve_paused", 0)),
            # The owner's own record per star band — what "earned" reads.
            "trust": _auto_approve_trust_safe(rid),
        },
        "hours": {
            "open": _times(getattr(r, "open_times_json", None)),
            "close": _times(getattr(r, "close_times_json", None)),
            "closures": closures,
        },
        "data_retention_months": int(getattr(r, "data_retention_months", 0) or 0),
        "marketing_emails_opt_out": bool(getattr(r, "marketing_emails_opt_out", 0)),
        "monthly_review_enabled": bool(getattr(r, "monthly_review_enabled", 1)),
        "login_notify": bool(getattr(r, "login_notify", 0)),
    }, 200


@client_bp.route("/api/account-settings")
@login_required
def get_account_settings(current_user):
    payload, status = _account_settings_payload(current_user["restaurant_id"])
    return jsonify(**payload), status


@client_bp.route("/api/brand-voice", methods=["GET", "POST"])
@login_required
def brand_voice(current_user):
    rid = current_user["restaurant_id"]
    if request.method == "GET":
        r = get_restaurant(rid)
        return jsonify(ok=True,
                       voice_notes=getattr(r, "voice_notes", "") or "",
                       never_say=getattr(r, "never_say", "") or "",
                       menu_notes=getattr(r, "menu_notes", "") or "",
                       sign_off_name=getattr(r, "sign_off_name", "") or "")
    payload, status = _do_brand_voice(rid, request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/auto-approve", methods=["POST"])
@login_required
def save_auto_approve(current_user):
    payload, status = _do_auto_approve(current_user["restaurant_id"],
                                       request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/hours", methods=["POST"])
@login_required
def save_account_hours(current_user):
    payload, status = _do_account_hours(current_user["restaurant_id"],
                                        request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/data-retention", methods=["POST"])
@login_required
def save_data_retention(current_user):
    payload, status = _do_data_retention(current_user["restaurant_id"],
                                         request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/marketing-opt-out", methods=["POST"])
@login_required
def save_marketing_opt_out(current_user):
    payload, status = _do_marketing_opt_out(current_user["restaurant_id"],
                                            request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/monthly-review", methods=["POST"])
@login_required
def save_monthly_review_pref(current_user):
    payload, status = _do_monthly_review_pref(current_user["restaurant_id"],
                                              request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account-settings/login-notify", methods=["POST"])
@login_required
def save_login_notify(current_user):
    payload, status = _do_login_notify(current_user["restaurant_id"],
                                       request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


# ── Web parity: supplier orders, menu margins, schedule publishing ─────────────
# Mobile-only until now (mobile_api.py) — same underlying models/inventory/
# labor functions, just a session-authed route instead of a bearer-authed one.

@client_bp.route("/api/food-cost/ingredient-supplier", methods=["POST"])
@login_required
def set_ingredient_supplier(current_user):
    """Web twin — the one body is mobile_api.mobile_set_ingredient_supplier."""
    return _m("mobile_set_ingredient_supplier")(current_user)


_ORDER_SEND_COOLDOWN = 60  # seconds between sends to one supplier
_order_send_last = {}
_order_send_lock = threading.Lock()


def _order_send_allowed(restaurant_id, supplier_emails=None) -> bool:
    """One send per supplier per cooldown — the double-click guard. In
    process, and deliberately not the real re-send guard: that is the
    durable claim on the PO row (models.record_purchase_order, DATA-15),
    which survives a restart and does not expire after a minute.

    Keyed per (restaurant, supplier address), so sending supplier B's order
    right after supplier A's is not refused (MOD-FC-11). The check and the
    set happen under one lock, so two request threads cannot both be
    allowed. Callers claim it only once the send has passed validation —
    see _release_order_send for handing it back when nothing went out."""
    import time as _time_po
    keys = [(restaurant_id, e) for e in sorted(set(supplier_emails))] if supplier_emails else [restaurant_id]
    with _order_send_lock:
        now = _time_po.monotonic()
        for key in keys:
            last = _order_send_last.get(key)
            if last is not None and (now - last) < _ORDER_SEND_COOLDOWN:
                return False
        for key in keys:
            _order_send_last[key] = now
    return True


def _release_order_send(restaurant_id, supplier_emails):
    """Give a cooldown back when nothing reached that supplier, so the fixed
    retry is not refused for a minute."""
    with _order_send_lock:
        for e in supplier_emails or ():
            _order_send_last.pop((restaurant_id, e), None)


@client_bp.route("/api/food-cost/order-draft")
@login_required
def food_cost_order_draft(current_user):
    """Web twin — the one body is mobile_api.mobile_food_cost_order_draft."""
    return _m("mobile_food_cost_order_draft")(current_user)


def _send_supplier_orders(rid, restaurant, groups, actor, resend=False):
    """Send one purchase order per supplier group. Shared by the route and
    delayed.py (trusted-supplier send). Returns (sent, failed).

    A group whose exact draft is already on an open PO is not sent again
    (DATA-15): it comes back in `failed` with already_sent=True and the
    existing number, unless `resend` — the owner's explicit "send it again"."""
    actor = actor or {}
    from models import record_purchase_order, DuplicatePurchaseOrder
    sent, failed = [], []
    for group in groups:
        # The PO row is written BEFORE the email, and carries the number: an
        # order that reached a supplier with no record of it is the worse of
        # the two failure modes by a distance.
        try:
            po_number = record_purchase_order(
                rid, group.get("supplier_name") or "", group["supplier_email"],
                group["items"], group.get("total_cost") or 0,
                draft_hash=group.get("draft_hash"), allow_duplicate=bool(resend))
        except DuplicatePurchaseOrder as dup:
            from time_utils import mdy as _mdy
            failed.append({"supplier_email": group["supplier_email"], "already_sent": True,
                           "po_number": dup.po_number,
                           "error": f"This order already went to {group.get('supplier_name') or group['supplier_email']} "
                                    f"as {dup.po_number} on {_mdy(dup.sent_at)}."})
            continue
        except Exception as e:
            failed.append({"supplier_email": group["supplier_email"], "error": _safe_err(e)})
            continue
        try:
            import outcomes as _oc
            _oc.observe(rid, "supplier_order_sent", detail=group.get("supplier_name") or None,
                        user_id=actor.get("id"))
        except Exception as _oe:
            import ops as _ops_o
            _ops_o.capture(_oe, job="observe_order", context=f"restaurant_id={rid}")
        try:
            from emails import send_supplier_order_email
            send_supplier_order_email(
                to_email=group["supplier_email"],
                supplier_name=group.get("supplier_name") or "",
                restaurant_name=restaurant.name,
                po_number=po_number,
                items=group["items"],
                total_cost=group.get("total_cost") or 0,
                reply_to=restaurant.owner_email or None,
            )
        except Exception as e:
            from models import void_purchase_order as _void_po
            _void_po(rid, po_number)
            failed.append({"supplier_email": group["supplier_email"], "error": _safe_err(e)})
            continue

        from models import log_email as _log_email
        _log_email(rid, "supplier_order", group["supplier_email"],
                   f"Order {po_number} — {restaurant.name}")
        # Per order, with the supplier, the number and the total — the audit
        # line used to read "2 orders" and nothing else.
        log_account_event(rid, "supplier_order_sent", actor,
                          detail=f"{po_number} to {group['supplier_email']} — "
                                 f"{len(group['items'])} items, ${group.get('total_cost') or 0:,.2f}")
        sent.append({"po_number": po_number, "supplier_email": group["supplier_email"],
                     "supplier_name": group.get("supplier_name") or "",
                     "item_count": len(group["items"]), "total_cost": group.get("total_cost") or 0})

    return sent, failed


@client_bp.route("/api/food-cost/send-order", methods=["POST"])
@login_required
def send_supplier_order(current_user):
    """Web twin — the one body is _send_order_request, which the phone's
    /mobile/api/food-cost/send-order calls too."""
    return _send_order_request(current_user)


def _send_order_request(current_user):
    """Email the suggested order to each supplier and record a PO per
    supplier. `supplier_email` in the body sends just that supplier; `resend`
    is the explicit "send it again" past the already-sent guard.

    One body for web and phone. The phone had its own copy, which never read
    send_delay_minutes (so it skipped the owner's undo window — MOD-FC-9 /
    DATA-41), returned raw exception text and skipped outcomes.observe.

    Order of refusals: nothing to order (400), then the draft the client
    reviewed (409 — it is REQUIRED, MOD-FC-8), then already on an open PO
    (409), and only then the double-click cooldown (429), so a refused send
    never spends the cooldown and blocks its own fixed retry (MOD-FC-11)."""
    from inventory import build_supplier_orders
    from models import open_purchase_order

    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404

    data = request.get_json(silent=True) or {}
    only = (data.get("supplier_email") or "").strip().lower()
    resend = bool(data.get("resend"))

    try:
        draft = build_supplier_orders(rid)
    except Exception as e:
        import ops as _ops_d
        _ops_d.capture(e, job="build_supplier_orders", context=f"restaurant_id={rid}")
        return jsonify(ok=False, error="Couldn't build the order — try again in a moment."), 500

    groups = draft.get("groups") or []
    if only:
        groups = [g for g in groups if (g.get("supplier_email") or "").lower() == only]
    if not groups:
        return jsonify(ok=False, error="Nothing to order — no items with a supplier assigned."), 400

    # Send what the owner approved. The draft is rebuilt here rather than
    # stored, so the hash of what they reviewed has to come back with the
    # send: it was optional, neither client sent it, and the quantities
    # emailed could differ from the ones on screen (MOD-FC-8). Sending one
    # supplier accepts that supplier's own hash as well as the whole draft's.
    expected = (data.get("draft_hash") or "").strip()
    accepted = {draft.get("draft_hash")}
    if only and len(groups) == 1 and groups[0].get("draft_hash"):
        accepted.add(groups[0]["draft_hash"])
    accepted.discard(None)
    if not expected or expected not in accepted:
        return jsonify(ok=False, stale=True, draft_hash=draft.get("draft_hash"),
                       error=("The order changed since you reviewed it — take another look before sending."
                              if expected else "Review the order before sending it.")), 409

    emails_ = sorted({(g.get("supplier_email") or "").lower() for g in groups})
    if not _order_send_allowed(rid, emails_):
        return jsonify(ok=False, error="An order was just sent — give it a moment before sending again."), 429

    # This exact order already went and has not been received: a retry after
    # the cooldown, or after a deploy wiped it, used to send it again
    # (DATA-15). record_purchase_order re-checks inside its write lock; this
    # is the early answer, and hands the cooldown back.
    if not resend:
        dups = [(g, open_purchase_order(rid, g["supplier_email"], g.get("draft_hash")))
                for g in groups if g.get("draft_hash")]
        dups = [(g, po) for g, po in dups if po]
        if dups:
            _release_order_send(rid, sorted({g["supplier_email"].lower() for g, _ in dups}))
        if dups and len(dups) == len(groups):
            from time_utils import mdy as _mdy
            g, po = dups[0]
            return jsonify(ok=False, already_sent=True, po_number=po["po_number"],
                           error=f"This order already went to {g.get('supplier_name') or g['supplier_email']} "
                                 f"as {po['po_number']} on {_mdy(po['sent_at'])}. Send it again?"), 409
        if dups:
            skip = {id(g) for g, _ in dups}
            groups = [g for g in groups if id(g) not in skip]

    sent, failed = [], []
    delay = int(getattr(restaurant, "send_delay_minutes", 0) or 0)
    if delay > 0:
        # The owner asked for a window: the order is queued, shown in the
        # feed with Undo, and sent by the scheduler unless cancelled. The
        # payload carries this supplier's own hash, so another supplier's
        # count changing in the window does not void it (MOD-FC-10).
        import delayed
        queued = []
        for group in groups:
            row = delayed.schedule(rid, "order_send",
                                   {"supplier_email": group["supplier_email"],
                                    "draft_hash": group.get("draft_hash") or draft.get("draft_hash"),
                                    "resend": resend},
                                   delay, actor=current_user,
                                   label=f"Sending the {group.get('supplier_name') or group['supplier_email']} order "
                                         f"(${float(group.get('total_cost') or 0):,.0f}, {len(group.get('items') or [])} items)")
            queued.append({"action_id": row["id"], "execute_at": row["execute_at"], "supplier_email": group["supplier_email"]})
        return jsonify(ok=True, queued=queued, sent=[], failed=[], undo_minutes=delay)
    _s, _f = _send_supplier_orders(rid, restaurant, groups, current_user, resend=resend)
    sent.extend(_s); failed.extend(_f)
    _release_order_send(rid, sorted({f["supplier_email"].lower() for f in failed
                                     if not f.get("already_sent")} - {s["supplier_email"].lower() for s in sent}))
    if not sent:
        if failed and all(f.get("already_sent") for f in failed):
            return jsonify(ok=False, already_sent=True, sent=[], failed=failed,
                           error=failed[0]["error"] + " Send it again?"), 409
        # Was a 200 with ok=False, so any client branching on HTTP status read
        # a total failure to send as a success.
        return jsonify(ok=False, sent=[], failed=failed,
                       error="Couldn't send the order — check the supplier addresses."), 502
    return jsonify(ok=True, sent=sent, failed=failed, error=None)


@client_bp.route("/api/food-cost/purchase-orders")
@login_required
def food_cost_purchase_orders(current_user):
    from models import get_purchase_orders
    status = request.args.get("status") or None
    return jsonify(ok=True, orders=get_purchase_orders(current_user["restaurant_id"], status=status))


@client_bp.route("/api/food-cost/purchase-orders/<int:po_id>/received", methods=["POST"])
@login_required
def receive_purchase_order(current_user, po_id):
    from models import mark_purchase_order_received
    if not mark_purchase_order_received(current_user["restaurant_id"], po_id):
        return jsonify(ok=False, error="That order is already received, or isn't yours."), 404
    return jsonify(ok=True)


@client_bp.route("/api/food-cost/cogs")
@login_required
def food_cost_cogs(current_user):
    """Web twin — the one body is mobile_api.mobile_food_cost_cogs."""
    return _m("mobile_food_cost_cogs")(current_user)


@client_bp.route("/api/food-cost/cfo")
@login_required
def food_cost_cfo(current_user):
    """Web twin — the one body is mobile_api.mobile_food_cost_cfo."""
    return _m("mobile_food_cost_cfo")(current_user)


@client_bp.route("/api/food-cost/waste-sources")
@login_required
def food_cost_waste_sources(current_user):
    """Web twin — the one body is mobile_api.mobile_waste_sources."""
    return _m("mobile_waste_sources")(current_user)


@client_bp.route("/api/food-cost/recipe-coverage")
@login_required
def food_cost_recipe_coverage(current_user):
    """Web twin — the one body is mobile_api.mobile_recipe_coverage."""
    return _m("mobile_recipe_coverage")(current_user)


@client_bp.route("/api/food-cost/menu-profitability")
@login_required
def food_cost_menu_profitability(current_user):
    """Web twin — the one body is mobile_api.mobile_menu_profitability."""
    return _m("mobile_menu_profitability")(current_user)


def track_reprice(rid, user_id=None):
    """Repricing a dish is a deliberate commitment to move food cost, so it
    starts measuring itself — the same shape as _track_campaign_outcome, and
    only AFTER the write actually landed.

    Keyed on the MONTH, so changing six prices in a week is one tracker
    ("the prices you changed in September") rather than six. Six trackers on
    one metric would all read the same movement and report it six times.
    Best-effort: measurement never fails a price change.
    """
    try:
        import outcomes
        from datetime import date as _d
        month = _d.today().strftime("%Y-%m")
        outcomes.record(rid, "reprice", f"reprice:{month}",
                        f"Menu prices changed in {_d.today().strftime('%B')}",
                        "food_cost_pct", user_id=user_id)
    except Exception as e:
        import ops
        ops.capture(e, job="reprice_outcome", context=f"restaurant_id={rid}")


@client_bp.route("/api/food-cost/menu-item-price", methods=["POST"])
@login_required
def set_menu_item_price(current_user):
    """Web twin — the one body is mobile_api.mobile_set_menu_item_price."""
    return _m("mobile_set_menu_item_price")(current_user)


@client_bp.route("/api/labor/staff-contacts")
@login_required
def get_staff_contacts_api(current_user):
    """Web twin — the one body is mobile_api.mobile_get_staff_contacts."""
    return _m("mobile_get_staff_contacts")(current_user)


@client_bp.route("/api/labor/staff-contacts", methods=["POST"])
@login_required
def set_staff_contact_api(current_user):
    """Web twin — the one body is mobile_api.mobile_set_staff_contact."""
    return _m("mobile_set_staff_contact")(current_user)


def publish_blockers(restaurant_id, schedule_id=None):
    """Why this schedule should not go to staff unread: rows the engine
    flagged (hard rule breaches, names it could not vouch for), and a week
    the quality engine judged weak or could not judge. Empty means clear."""
    from models import _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        if schedule_id:
            row = conn.execute("SELECT id, schedule_csv, review_json, quality_json, hours_scheduled, hours_budget "
                               "FROM schedule_history WHERE id=? AND restaurant_id=?", (int(schedule_id), restaurant_id)).fetchone()
        else:
            row = conn.execute("SELECT id, schedule_csv, review_json, quality_json, hours_scheduled, hours_budget "
                               "FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    out = []
    flagged = sum(1 for line in (row["schedule_csv"] or "").split("\n") if "NEEDS REVIEW" in line)
    if flagged:
        out.append(f"{flagged} shift{'s' if flagged != 1 else ''} marked NEEDS REVIEW")
    try:
        review = json.loads(row["review_json"] or "null") or {}
    except Exception:
        review = {}
    for line in (review.get("lines") or [])[:6]:
        if line.startswith("⚠") and "over the ceiling" not in line:   # the hours check below says it once
            out.append(line.lstrip("⚠ ").strip())
    # The labor budget is a ceiling. A week the model wrote past it is not
    # trimmed (a silently thinner week is worse) — it is named here, so the
    # owner sends it knowing, or takes hours out first.
    try:
        hs, hb = float(row["hours_scheduled"] or 0), float(row["hours_budget"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        hs = hb = 0.0
    if hb > 0 and hs > hb * 1.02:
        out.append(f"{hs:,.0f}h scheduled against a {hb:,.0f}h budget — {hs - hb:,.0f}h over the ceiling")
    try:
        quality = json.loads(row["quality_json"] or "null") or {}
    except Exception:
        quality = {}
    if quality.get("checked"):
        if quality.get("band") == "weak":
            out.append(f"Shift Quality {quality.get('score')}/100 — a weak week")
        if (quality.get("confidence") or {}).get("level") == "low":
            out.append("The quality engine had too little to judge this week on")
        below = quality.get("below_profile") or []
        if below:
            out.append(f"{len(below)} shift{'s' if len(below) != 1 else ''} below the bar set for {'it' if len(below) == 1 else 'them'}")
    return out


def _publish_schedule(restaurant_id, schedule_id=None, actor=None, acknowledge=False):
    """Shared by the route, the mobile twin and delayed.py (auto-publish).
    Returns (payload, http_status). `actor` is the user dict acting, or
    delayed.AUTOMATION_ACTOR.

    A schedule with blockers (publish_blockers) is refused with
    needs_ack=True until the caller says acknowledge — a human reading the
    list, never automation. Publishing stamps published_at, which is what
    the staff portal and the payroll-week hours check read."""
    actor = actor or {}
    from models import get_staff_contacts, create_schedule_share, get_schedule_share_status
    from labor import employees_in_schedule, employee_shifts_from_csv

    rid = restaurant_id
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    conn = get_conn()
    try:
        if schedule_id:
            row = conn.execute(
                "SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                (int(schedule_id), rid)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, week_start, week_end, schedule_csv FROM schedule_history "
                "WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    except (TypeError, ValueError):
        return {"ok": False, "error": "Which schedule?"}, 400
    finally:
        conn.close()

    if not row or not (row["schedule_csv"] or "").strip():
        return {"ok": False, "error": "Generate a schedule first — there's nothing to send yet."}, 400

    blockers = publish_blockers(rid, row["id"])
    if blockers and not acknowledge:
        return {"ok": False, "needs_ack": True, "blockers": blockers, "schedule_id": row["id"],
                "error": "This week has things to look at before it goes to staff."}, 409

    schedule_id = row["id"]
    week_label = row["week_start"] or ""
    if row["week_end"]:
        week_label = f"{row['week_start']} – {row['week_end']}"

    # Claim the week before anything goes out. Nothing stopped a second
    # publish (a double tap, a second device, the 11am auto-publish after a
    # 10am manual one) from emailing every member of staff again (SCHED-29,
    # DATA-12). A claim older than 10 minutes belongs to a publish that died.
    from models import _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        got = conn.execute(
            "UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=? AND restaurant_id=? "
            "AND published_at IS NULL AND (publishing_at IS NULL OR publishing_at < datetime('now','-10 minutes'))",
            (schedule_id, rid)).rowcount
        conn.commit()
        state = conn.execute("SELECT published_at FROM schedule_history WHERE id=?", (schedule_id,)).fetchone()
    finally:
        conn.close()
    if not got:
        if state and state["published_at"]:
            return dict(ok=True, already_published=True, schedule_id=schedule_id, week_label=week_label,
                        sent=[], unreachable=[], failed=[],
                        status=get_schedule_share_status(rid, schedule_id),
                        error=None), 200
        return {"ok": False, "in_progress": True, "schedule_id": schedule_id,
                "error": "This week is being sent to staff right now."}, 409

    contacts = {c["employee_name"].lower(): c for c in get_staff_contacts(rid)}
    base_url = config.base_url()

    # The owner is acting. Measure what it does to labor % over the next
    # window, whether or not they ever pressed Track (outcomes.observe).
    try:
        import outcomes as _oc
        _oc.observe(rid, "schedule_published", detail=f"week of {row['week_start']}",
                    user_id=actor.get("id"))
    except Exception as _oe:
        import ops as _ops_o
        _ops_o.capture(_oe, job="observe_schedule", context=f"restaurant_id={rid}")

    sent, unreachable, failed, failed_tokens = [], [], [], []
    for name in employees_in_schedule(row["schedule_csv"]):
        contact = contacts.get(name.lower()) or {}
        email = (contact.get("email") or "").strip()
        if not email:
            unreachable.append({"employee_name": name, "reason": "no email address on file"})
            continue

        token = create_schedule_share(rid, schedule_id, name, sent_to=email)
        link = f"{base_url}/s/{token}"
        shifts = employee_shifts_from_csv(row["schedule_csv"], name)
        try:
            from emails import send_staff_schedule_email
            send_staff_schedule_email(
                to_email=email, employee_name=name, restaurant_name=restaurant.name,
                week_label=week_label, link=link, shifts=shifts,
                reply_to=restaurant.owner_email or None)
        except Exception as e:
            failed.append({"employee_name": name, "error": str(e)})
            failed_tokens.append(token)
            continue

        try:
            from models import log_email as _log_email
            _log_email(rid, "staff_schedule", email, f"Your schedule — {week_label}")
        except Exception:
            pass
        sent.append({"employee_name": name, "sent_to": email, "shifts": len(shifts)})

    actor_name = (actor.get("username") or actor.get("email") or "automation") if isinstance(actor, dict) else "automation"
    if failed and not sent:
        # Every email that was tried failed (Resend down): nothing is
        # published. The share rows made for those emails are removed so the
        # week is not half-published, and the claim is released for a retry.
        conn = get_conn()
        try:
            for t in failed_tokens:
                conn.execute("DELETE FROM schedule_shares WHERE token=? AND schedule_id=?", (t, schedule_id))
            conn.execute("UPDATE schedule_history SET publishing_at=NULL WHERE id=?", (schedule_id,))
            conn.commit()
        finally:
            conn.close()
        return dict(ok=False, schedule_id=schedule_id, week_label=week_label, sent=[], unreachable=unreachable,
                    failed=failed, acknowledged=bool(blockers),
                    status=get_schedule_share_status(rid, schedule_id),
                    error="The emails could not be sent, so nothing was published. Try again in a few minutes."), 200

    # Published — to the staff portal always, and by email to everyone with
    # an address. A restaurant whose staff use only the portal used to never
    # publish at all, because the stamp waited on a successful email (SCHED-9).
    log_account_event(rid, "schedule_published", actor,
                      detail=(f"{len(sent)} to staff" if sent else "to the staff portal")
                      + (" — acknowledged blockers" if blockers else ""))
    try:
        conn = get_conn()
        try:
            conn.execute("UPDATE schedule_history SET published_at=datetime('now'), published_by=?, publishing_at=NULL "
                         "WHERE id=? AND restaurant_id=?", (actor_name, schedule_id, rid))
            # One live version of a week: an older published copy is retired,
            # or both fed outcomes, fairness, hours and claims (SCHED-10).
            conn.execute("UPDATE schedule_history SET superseded_by=? WHERE restaurant_id=? AND week_start=? "
                         "AND id<>? AND published_at IS NOT NULL AND superseded_by IS NULL",
                         (schedule_id, rid, row["week_start"], schedule_id))
            conn.commit()
        finally:
            conn.close()
        import schedule_versions as _sv
        _sv.append(rid, schedule_id, "published", row["schedule_csv"], saved_by=actor_name)
    except Exception as _px:
        _ops.capture(_px, job="schedule_publish_stamp", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    note = None
    if not sent:
        note = ("Published to the staff portal. Nobody has an email address on file, so no emails went out."
                if not failed else None)
    return dict(ok=True, schedule_id=schedule_id, week_label=week_label,
                sent=sent, unreachable=unreachable, failed=failed,
                acknowledged=bool(blockers), portal_only=not sent, note=note,
                status=get_schedule_share_status(rid, schedule_id),
                error=None), 200


@client_bp.route("/api/labor/publish-schedule", methods=["POST"])
@login_required
def publish_schedule_api(current_user):
    return _publish_schedule_request(current_user)


def _publish_schedule_request(current_user):
    """The one publish body for web and phone. The phone called
    _publish_schedule directly and never read send_delay_minutes, so a week
    published from it skipped the owner's undo window (DATA-41)."""
    from permissions import has_permission, SCHEDULE_PUBLISH
    if not (current_user.get("is_admin") or has_permission(current_user, SCHEDULE_PUBLISH)):
        return jsonify(ok=False, error="Your login can draft a schedule but not send it to staff."), 403
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    delay = int(getattr(restaurant, "send_delay_minutes", 0) or 0) if restaurant else 0
    if delay > 0:
        # The owner asked for a window before anything reaches staff.
        conn = get_conn()
        try:
            if data.get("schedule_id"):
                row = conn.execute("SELECT id, week_start FROM schedule_history WHERE id=? AND restaurant_id=?",
                                   (int(data["schedule_id"]), rid)).fetchone()
            else:
                row = conn.execute("SELECT id, week_start FROM schedule_history WHERE restaurant_id=? "
                                   "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Which schedule?"), 400
        finally:
            conn.close()
        if not row:
            return jsonify(ok=False, error="Generate a schedule first — there's nothing to send yet."), 400
        blockers = publish_blockers(rid, row["id"])
        if blockers and not data.get("acknowledge"):
            return jsonify(ok=False, needs_ack=True, blockers=blockers, schedule_id=row["id"],
                           error="This week has things to look at before it goes to staff."), 409
        import delayed
        act = delayed.schedule(rid, "schedule_publish",
                               {"schedule_id": row["id"], "acknowledge": bool(data.get("acknowledge"))},
                               delay, actor=current_user,
                               label=f"Publishing the week of {row['week_start']} to staff")
        return jsonify(ok=True, queued=True, action_id=act["id"], execute_at=act["execute_at"],
                       undo_minutes=delay, sent=[], unreachable=[], failed=[])
    try:
        out, status = _publish_schedule(rid, data.get("schedule_id"), current_user,
                                        acknowledge=bool(data.get("acknowledge")))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Which schedule?"), 400
    return jsonify(**out), status


@client_bp.route("/api/labor/schedule-share-status")
@login_required
def schedule_share_status_api(current_user):
    """Web twin — the one body is mobile_api.mobile_schedule_share_status."""
    return _m("mobile_schedule_share_status")(current_user)



# ── iOS parity: the web halves of everything the phone had first ──────────
# One pattern for all of them: the mobile handler is the implementation
# (request.get_json / jsonify work the same under a cookie session), and the
# web route is its @login_required twin calling the undecorated function via
# functools.wraps' __wrapped__. mobile_api imports this module, so the import
# is deferred to call time.

def _m(name):
    import mobile_api as _mob
    fn = getattr(_mob, name)
    return getattr(fn, "__wrapped__", fn)


@client_bp.route("/api/home/brief")
@login_required
def home_brief_api(current_user):
    """The web Home screen in one payload — see home_brief.py. Deterministic,
    cached 60s per restaurant, no AI call on load. ?fresh=1 recomputes."""
    import home_brief
    payload, status = home_brief.build_home_brief(current_user, fresh=request.args.get("fresh") == "1")
    resp = jsonify(**payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp, status


@client_bp.route("/api/home/brief/group")
@login_required
def home_brief_group_api(current_user):
    """Every location in the owner's group, side by side — see home_brief.build_group_brief."""
    import home_brief
    payload, status = home_brief.build_group_brief(current_user, fresh=request.args.get("fresh") == "1")
    resp = jsonify(**payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp, status


@client_bp.route("/api/home/dismiss", methods=["POST"])
@login_required
def home_dismiss_api(current_user):
    """Hide a recommendation for two weeks (or restore it with undo=true)."""
    import home_brief
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify(ok=False, error="Missing key"), 400
    if data.get("undo"):
        return jsonify(**home_brief.undismiss(current_user["restaurant_id"], key))
    kind = (data.get("kind") or "recommendation")[:40]
    out = home_brief.dismiss(current_user["restaurant_id"], key, kind=kind, user_id=current_user.get("id"),
                             reason=data.get("reason"), title=data.get("title"))
    # "Done" on a recommendation that names a metric is an owner saying
    # they acted. That is exactly what Track this records, so record it:
    # source "observed", baseline now, re-measured when the window closes.
    # Nothing is invented — a recommendation with no honest metric records
    # nothing, the same rule the Track button follows.
    if out.get("ok") and kind == "done" and data.get("metric") and data.get("title"):
        try:
            import outcomes
            if outcomes.known_metric(data["metric"]):
                o = outcomes.record(current_user["restaurant_id"], "observed", key,
                                    str(data["title"])[:200], data["metric"],
                                    user_id=current_user.get("id"))
                out["outcome"] = {"id": o.get("id"), "evaluate_on": o.get("evaluate_on")}
        except Exception as e:
            import ops
            ops.capture(e, job="home_dismiss_done", context=f"key={key}")
    return jsonify(**out)


@client_bp.route("/api/home")
@login_required
def home_summary_api(current_user):
    """Home's receipts, value history and needs-attention deck — the same
    payload the phone renders, for the parts of web Home that were static."""
    import mobile_api as _mob
    payload, status = _mob._do_mobile_home(current_user)
    return jsonify(**payload), status


@client_bp.route("/api/account/profile", methods=["POST"])
@login_required
def account_profile_update(current_user):
    return _m("mobile_update_profile")(current_user)


@client_bp.route("/api/account/team")
@login_required
def account_team(current_user):
    return _m("mobile_get_team")(current_user)


@client_bp.route("/api/account/team/invite", methods=["POST"])
@login_required
def account_team_invite(current_user):
    return _m("mobile_invite_team_member")(current_user)


@client_bp.route("/api/account/team/<int:user_id>/revoke", methods=["POST"])
@login_required
def account_team_revoke(current_user, user_id):
    return _m("mobile_revoke_team_member")(current_user, user_id)


@client_bp.route("/api/account/export-data", methods=["POST"])
@login_required
def account_export_data(current_user):
    return _m("mobile_export_data")(current_user)


@client_bp.route("/api/account/login-history")
@login_required
def account_login_history(current_user):
    return _m("mobile_login_history")(current_user)


@client_bp.route("/api/account/activity")
@login_required
def account_activity(current_user):
    return _m("mobile_account_activity")(current_user)


@client_bp.route("/api/account/recovery-email", methods=["POST"])
@login_required
def account_recovery_email(current_user):
    return _m("mobile_set_recovery_email")(current_user)


@client_bp.route("/api/account/recovery-email/verify", methods=["POST"])
@login_required
def account_recovery_email_verify(current_user):
    return _m("mobile_verify_recovery_email")(current_user)


@client_bp.route("/api/account/recovery-email/remove", methods=["POST"])
@login_required
def account_recovery_email_remove(current_user):
    return _m("mobile_remove_recovery_email")(current_user)


@client_bp.route("/api/account/report-bug", methods=["POST"])
@login_required
def account_report_bug(current_user):
    return _m("mobile_report_bug")(current_user)


@client_bp.route("/api/account/2fa/send-test", methods=["POST"])
@login_required
def account_2fa_send_test(current_user):
    return _m("mobile_send_2fa_test")(current_user)


@client_bp.route("/api/account/2fa/verify", methods=["POST"])
@login_required
def account_2fa_verify(current_user):
    return _m("mobile_verify_2fa_setup")(current_user)


@client_bp.route("/api/account/2fa/disable", methods=["POST"])
@login_required
def account_2fa_disable(current_user):
    return _m("mobile_disable_2fa")(current_user)


@client_bp.route("/api/account/2fa/backup-codes")
@login_required
def account_backup_codes(current_user):
    return _m("mobile_backup_codes_status")(current_user)


@client_bp.route("/api/account/2fa/backup-codes", methods=["POST"])
@login_required
def account_backup_codes_regenerate(current_user):
    return _m("mobile_regenerate_backup_codes")(current_user)


@client_bp.route("/api/account/2fa/trusted-devices")
@login_required
def account_trusted_devices(current_user):
    return _m("mobile_trusted_devices")(current_user)


@client_bp.route("/api/account/2fa/trusted-devices/<int:device_id>/revoke", methods=["POST"])
@login_required
def account_trusted_device_revoke(current_user, device_id):
    return _m("mobile_revoke_trusted_device")(current_user, device_id)


@client_bp.route("/api/account/2fa/trusted-devices/revoke-all", methods=["POST"])
@login_required
def account_trusted_devices_revoke_all(current_user):
    return _m("mobile_revoke_all_trusted_devices")(current_user)


@client_bp.route("/api/account/security-summary")
@login_required
def account_security_summary(current_user):
    """The numbers behind the Security checkup — same inputs the phone
    scores (see AccountSecurityCheckupView)."""
    from models import count_unused_backup_codes
    from auth import get_trusted_devices, get_sessions_for_user
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    try:
        sessions = len(get_sessions_for_user(current_user["id"]))
    except Exception:
        sessions = 1
    return jsonify(ok=True,
                   two_fa_enabled=bool(r and r.two_fa_enabled),
                   two_fa_method=(getattr(r, "two_fa_method", None) or "email") if r else "email",
                   backup_codes_remaining=count_unused_backup_codes(rid),
                   trusted_devices=len(get_trusted_devices(rid)),
                   login_notify=bool(r and getattr(r, "login_notify", 0)),
                   recovery_email=current_user.get("recovery_email"),
                   password_strength=current_user.get("password_strength"),
                   password_changed_at=current_user.get("password_changed_at"),
                   active_sessions=sessions)


@client_bp.route("/api/intel/search-places")
@login_required
def intel_search_places(current_user):
    return _m("mobile_search_places")(current_user)


@client_bp.route("/api/intel/add-competitor", methods=["POST"])
@login_required
def intel_add_competitor(current_user):
    return _m("mobile_add_competitor")(current_user)


@client_bp.route("/api/intel/remove-competitor", methods=["POST"])
@login_required
def intel_remove_competitor(current_user):
    return _m("mobile_remove_competitor")(current_user)


@client_bp.route("/api/ai-visibility/history")
@login_required
def ai_visibility_history(current_user):
    return _m("mobile_ai_visibility_history")(current_user)


@client_bp.route("/api/labor/team")
@login_required
def labor_team(current_user):
    return _m("mobile_labor_team")(current_user)


@client_bp.route("/api/labor/team/rating", methods=["POST"])
@login_required
def labor_team_rating(current_user):
    return _m("mobile_set_rating")(current_user)


@client_bp.route("/api/labor/team/add", methods=["POST"])
@login_required
def labor_team_add(current_user):
    return _m("mobile_add_team_member")(current_user)


@client_bp.route("/api/labor/team/remove", methods=["POST"])
@login_required
def labor_team_remove(current_user):
    return _m("mobile_remove_team_member")(current_user)


@client_bp.route("/api/labor/team/thresholds", methods=["POST"])
@login_required
def labor_team_thresholds(current_user):
    return _m("mobile_set_thresholds")(current_user)


@client_bp.route("/api/account/team/<int:user_id>/can-manage", methods=["POST"])
@login_required
def account_team_can_manage(current_user, user_id):
    return _m("mobile_set_can_manage_team")(current_user, user_id)


@client_bp.route("/api/account/team/<int:user_id>/role", methods=["POST"])
@login_required
def account_team_role(current_user, user_id):
    return _m("mobile_set_team_role")(current_user, user_id)


@client_bp.route("/api/account/team/<int:user_id>/access", methods=["POST"])
@login_required
def account_team_access(current_user, user_id):
    return _m("mobile_set_team_access")(current_user, user_id)


@client_bp.route("/api/account/staff")
@login_required
def account_staff_list(current_user):
    return _m("mobile_list_staff")(current_user)


@client_bp.route("/api/account/staff", methods=["POST"])
@login_required
def account_staff_create(current_user):
    return _m("mobile_create_staff")(current_user)


@client_bp.route("/api/account/staff/<int:membership_id>", methods=["PATCH", "POST"])
@login_required
def account_staff_update(current_user, membership_id):
    """Promote, rename, retitle or reactivate one staff account."""
    return _m("mobile_update_staff")(current_user, membership_id)


@client_bp.route("/api/account/staff/<int:membership_id>/pin", methods=["POST"])
@login_required
def account_staff_pin(current_user, membership_id):
    return _m("mobile_reset_staff_pin")(current_user, membership_id)


@client_bp.route("/api/account/staff/<int:membership_id>/unlock", methods=["POST"])
@login_required
def account_staff_unlock(current_user, membership_id):
    return _m("mobile_unlock_staff")(current_user, membership_id)


@client_bp.route("/api/account/staff/<int:membership_id>/unlink", methods=["POST"])
@login_required
def account_staff_unlink(current_user, membership_id):
    """Take a claimed name back from whoever claimed it."""
    return _m("mobile_unlink_staff")(current_user, membership_id)


@client_bp.route("/api/account/staff/<int:membership_id>/deactivate", methods=["POST"])
@login_required
def account_staff_deactivate(current_user, membership_id):
    return _m("mobile_deactivate_staff")(current_user, membership_id)


@client_bp.route("/api/account/staff/portal-link/rotate", methods=["POST"])
@login_required
def account_staff_rotate_link(current_user):
    return _m("mobile_rotate_portal_link")(current_user)


@client_bp.route("/api/team/inbox")
@login_required
def team_inbox(current_user):
    return _m("mobile_team_inbox")(current_user)


@client_bp.route("/api/team/messages/<int:other_id>")
@login_required
def team_thread(current_user, other_id):
    return _m("mobile_team_thread")(other_id, current_user)


@client_bp.route("/api/team/messages", methods=["POST"])
@login_required
def team_send_message(current_user):
    return _m("mobile_send_team_message")(current_user)


@client_bp.route("/api/tasks")
@login_required
def tasks_today(current_user):
    return _m("mobile_get_tasks")(current_user)


@client_bp.route("/api/tasks/complete", methods=["POST"])
@login_required
def tasks_complete(current_user):
    return _m("mobile_set_task_complete")(current_user)


@client_bp.route("/api/tasks/templates", methods=["POST"])
@login_required
def tasks_add_template(current_user):
    return _m("mobile_add_task_template")(current_user)


@client_bp.route("/api/tasks/templates/remove", methods=["POST"])
@login_required
def tasks_remove_template(current_user):
    return _m("mobile_remove_task_template")(current_user)


@client_bp.route("/api/labor/schedule/score", methods=["POST"])
@login_required
def labor_schedule_score(current_user):
    return _m("mobile_score_schedule")(current_user)


@client_bp.route("/api/labor/profiles")
@login_required
def labor_profiles(current_user):
    return _m("mobile_shift_profiles")(current_user)


@client_bp.route("/api/labor/profiles", methods=["POST"])
@login_required
def labor_save_profile(current_user):
    return _m("mobile_save_shift_profile")(current_user)


@client_bp.route("/api/labor/profiles/delete", methods=["POST"])
@login_required
def labor_delete_profile(current_user):
    return _m("mobile_delete_shift_profile")(current_user)


@client_bp.route("/api/labor/quality-weights", methods=["POST"])
@login_required
def labor_quality_weights(current_user):
    return _m("mobile_save_quality_weights")(current_user)


@client_bp.route("/api/labor/schedule/replacements", methods=["POST"])
@login_required
def labor_schedule_replacements(current_user):
    return _m("mobile_schedule_replacements")(current_user)


@client_bp.route("/api/labor/capability-changes")
@login_required
def labor_capability_changes(current_user):
    return _m("mobile_capability_changes")(current_user)


@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)


@client_bp.route("/api/intel/movement")
@login_required
def intel_movement(current_user):
    return _m("mobile_intel_movement")(current_user)


@client_bp.route("/api/labor/schedule-history")
@login_required
def labor_schedule_history(current_user):
    return _m("mobile_schedule_history")(current_user)


@client_bp.route("/api/labor/schedule-history/<int:history_id>")
@login_required
def labor_schedule_history_detail(current_user, history_id):
    return _m("mobile_schedule_history_detail")(history_id, current_user)


@client_bp.route("/api/labor/schedule-history/<int:history_id>", methods=["DELETE"])
@login_required
def labor_schedule_history_delete(current_user, history_id):
    return _m("mobile_schedule_history_delete")(history_id, current_user)
