"""
dashboard.py — Unified 4-tab demo dashboard
Run:  python3 dashboard.py
Open: http://localhost:8080
"""
import os, json
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request

app = Flask(__name__)

def _labor():
    from labor import load_shifts, analyse_shifts, get_claude_insights as li
    return load_shifts, analyse_shifts, li

def _inventory():
    from inventory import load_inventory, analyse_inventory, get_claude_insights as ii
    return load_inventory, analyse_inventory, ii

def _marketing():
    from marketing import generate_content, get_content_calendar_ideas, CONTENT_TYPES
    return generate_content, get_content_calendar_ideas, CONTENT_TYPES

def get_review_stats():
    from models import get_conn
    conn = get_conn()
    total   = conn.execute("SELECT COUNT(*) FROM reviews WHERE processed=1").fetchone()[0]
    pos     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='positive'").fetchone()[0]
    neg     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='negative'").fetchone()[0]
    neu     = conn.execute("SELECT COUNT(*) FROM reviews WHERE sentiment='neutral'").fetchone()[0]
    urgent  = conn.execute("SELECT COUNT(*) FROM reviews WHERE urgency='high' AND response_status NOT IN ('posted','skipped')").fetchone()[0]
    avg_row = conn.execute("SELECT AVG(rating) FROM reviews WHERE processed=1").fetchone()[0]
    drafted = conn.execute("SELECT COUNT(*) FROM reviews WHERE response_status='drafted'").fetchone()[0]
    conn.close()
    return dict(total=total, positive=pos, negative=neg, neutral=neu,
                urgent=urgent, avg_rating=round(avg_row or 0, 1), awaiting_approval=drafted)

def get_reviews_data(filter_by="all", search=""):
    from models import get_conn
    conn = get_conn()
    where = ["processed=1"]
    if filter_by == "urgent":   where.append("urgency='high'")
    elif filter_by in ("positive","neutral","negative"): where.append(f"sentiment='{filter_by}'")
    elif filter_by == "pending": where.append("response_status='drafted'")
    if search:
        s = search.replace("'","''")
        where.append(f"(author LIKE '%{s}%' OR text LIKE '%{s}%')")
    rows = conn.execute(f"""SELECT * FROM reviews WHERE {' AND '.join(where)}
        ORDER BY CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
        CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,
        fetched_at DESC""").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"] or "[]")
        result.append(d)
    return result

TEMPLATE = open(os.path.join(os.path.dirname(__file__), "dashboard_template.html")).read()

@app.template_filter("format_num")
def format_num(value):
    try: return f"{float(value):,.0f}"
    except Exception: return value

@app.route("/")
def index():
    from models import DB_PATH
    if not os.path.exists(DB_PATH):
        return "<h2 style='font-family:sans-serif;padding:40px'>Run <code>python3 main.py --demo</code> first.</h2>"
    load_shifts, analyse_shifts, _ = _labor()
    load_inv, analyse_inv, _       = _inventory()
    _, _, CONTENT_TYPES             = _marketing()
    rfilter = request.args.get("filter", "all")
    rsearch = request.args.get("search", "")
    rstats  = get_review_stats()
    reviews = get_reviews_data(rfilter, rsearch)
    labor   = analyse_shifts(load_shifts())
    inv     = analyse_inv(load_inv())
    now     = datetime.now().strftime("%b %d, %Y %I:%M %p")
    return render_template_string(TEMPLATE,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES, now=now)

@app.route("/approve/<int:rid>", methods=["POST"])
def approve(rid):
    from models import approve_response
    approve_response(rid)
    return jsonify(ok=True)

@app.route("/skip/<int:rid>", methods=["POST"])
def skip(rid):
    from models import get_conn
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@app.route("/api/labor-insight")
def labor_insight_api():
    load_shifts, analyse_shifts, get_insights = _labor()
    insight = get_insights(analyse_shifts(load_shifts()))
    return jsonify(insight=insight)

@app.route("/api/inv-insight")
def inv_insight_api():
    load_inv, analyse_inv, get_insights = _inventory()
    insight = get_insights(analyse_inv(load_inv()))
    return jsonify(insight=insight)

@app.route("/api/generate-content", methods=["POST"])
def gen_content():
    data = request.get_json()
    gen, _, _ = _marketing()
    content = gen(data.get("type","instagram_post"), data.get("topic",""))
    return jsonify(content=content)

@app.route("/api/content-calendar")
def content_calendar():
    _, get_ideas, _ = _marketing()
    return jsonify(ideas=get_ideas())

if __name__ == "__main__":
    from models import DB_PATH
    if not os.path.exists(DB_PATH):
        print("No database found. Run `python3 main.py --demo` first.")
    else:
        print("\n  Dashboard → http://localhost:8080\n")
        app.run(host="0.0.0.0", port=8080, debug=False)
