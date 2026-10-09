"""
reply_keywords.py — the phrases a restaurant wants worked into its review
replies for local search (Danny at Simple EJ's, 10/9/26: "add some logic to
the replies for me to plug certain keywords we're trying to move for SEO").

Google rewards a reply that reads as an answer to the guest, not a stuffed
one, so the rules are narrow:

  * at most ONE phrase per reply, and only where it reads naturally — the
    prompt offers one phrase and says to leave it out if it doesn't fit;
  * never in a reply to an unhappy guest (1-2 stars, or anything urgent): a
    keyword in an apology reads as marketing;
  * the phrase that matches what the guest wrote leads ("brunch in St.
    Charles" when the review is about brunch); otherwise the one used least
    lately, so the same words don't open reply after reply;
  * a draft that works in more than one, or one where none belongs, is
    written again, told why (drafter._attempt).

Stored as restaurants.reply_keywords (JSON list). Anyone with the Reviews
module may edit it (a manager runs the replies at Simple EJ's); each change
is in the change log. Deterministic; no model call here.
"""
import json
import re

from models import DB_PATH

MAX_KEYWORDS = 20
MAX_LEN = 60
RECENT_DRAFTS = 40          # the window "used least lately" reads
NEGATIVE_MAX_STARS = 2


def clean(raw) -> list:
    """A list of distinct phrases (case-insensitive), trimmed, each at most
    MAX_LEN characters, at most MAX_KEYWORDS. Accepts a list or a string of
    lines or commas."""
    if isinstance(raw, str):
        items = re.split(r"[\n,;]+", raw)
    else:
        items = list(raw or [])
    out, seen = [], set()
    for x in items:
        k = " ".join(str(x or "").split())[:MAX_LEN].strip(" .\"'")
        if len(k) < 2 or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def of(restaurant) -> list:
    raw = getattr(restaurant, "reply_keywords", None) if restaurant is not None else None
    try:
        return clean(json.loads(raw)) if raw else []
    except (TypeError, ValueError):
        return clean(raw)


def _rx(k):
    return re.compile(r"(?<![\w])" + re.escape(k.lower()) + r"(?![\w])")


def found_in(text, keywords) -> list:
    """The keywords a reply contains (each once), in list order."""
    low = str(text or "").lower()
    return [k for k in keywords or [] if _rx(k).search(low)]


def occurrences(text, keywords) -> int:
    low = str(text or "").lower()
    return sum(len(_rx(k).findall(low)) for k in keywords or [])


def allowed(rating, urgency=None, sentiment=None) -> bool:
    """Whether a reply may carry a keyword at all."""
    try:
        r = int(rating or 0)
    except (TypeError, ValueError):
        r = 0
    if str(urgency or "").lower() == "high" or str(sentiment or "").lower() == "negative":
        return False
    return r > NEGATIVE_MAX_STARS


def _words(s):
    return {w for w in re.findall(r"[a-z']+", str(s or "").lower()) if len(w) >= 4}


def pick(restaurant_id, keywords, rating, text, urgency=None, sentiment=None, db_path=DB_PATH):
    """The one phrase to offer this reply, or None. A phrase sharing a word
    with the review leads; ties and the rest go to the one used least in
    the restaurant's last RECENT_DRAFTS replies."""
    if not keywords or not allowed(rating, urgency, sentiment):
        return None
    uses = {k: 0 for k in keywords}
    try:
        from models import get_conn
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT draft_response FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                "AND draft_response IS NOT NULL AND draft_response != '' ORDER BY id DESC LIMIT ?",
                (restaurant_id, RECENT_DRAFTS)).fetchall()
        finally:
            conn.close()
        for (d,) in rows:
            for k in found_in(d, keywords):
                uses[k] += 1
    except Exception:
        pass
    said = _words(text)
    order = sorted(keywords, key=lambda k: (0 if (_words(k) & said) else 1, uses.get(k, 0), keywords.index(k)))
    return order[0]


def prompt_note(keyword) -> str:
    if not keyword:
        return ""
    return (f"\nSEARCH PHRASE: if — and only if — it reads naturally, work the phrase \"{keyword}\" into the reply "
            "once, in the restaurant's voice (for example while thanking them or inviting them back). Never force "
            "it, never quote it, never add any other search phrase. If it doesn't fit, leave it out.")


def check(draft, keywords, rating, urgency=None, sentiment=None) -> str:
    """"" when the draft keeps to the rules, else the reason (phrased to
    follow "it ..." in the drafter's retry note)."""
    if not keywords:
        return ""
    n = occurrences(draft, keywords)
    if n and not allowed(rating, urgency, sentiment):
        return "worked a search phrase into a reply to an unhappy guest — leave every search phrase out"
    if n > 1:
        return "worked in more than one search phrase — use at most one, once"
    return ""


def usage(restaurant_id, keywords, days=90, db_path=DB_PATH) -> dict:
    """{phrase: replies posted in the last `days` days that carry it}."""
    out = {k: 0 for k in keywords or []}
    if not out:
        return out
    from models import get_conn
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT draft_response FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            "AND response_status='posted' AND draft_response IS NOT NULL "
            "AND COALESCE(posted_at, approved_at) >= datetime('now', ?)", (restaurant_id, f"-{int(days)} days")).fetchall()
    finally:
        conn.close()
    for (d,) in rows:
        for k in found_in(d, keywords):
            out[k] += 1
    return out


def save(restaurant_id, raw, user=None, db_path=DB_PATH) -> list:
    """Store the list (clean) and log the change; returns what was stored."""
    from models import get_restaurant, update_restaurant
    before = of(get_restaurant(restaurant_id, db_path=db_path))
    kws = clean(raw)
    update_restaurant(restaurant_id, {"reply_keywords": json.dumps(kws) if kws else None}, db_path=db_path)
    try:
        import change_log
        kw = {"user": user} if user else {}
        change_log.record(restaurant_id, "reviews", "reply_keywords", before, kws, subject="Reply search phrases", **kw)
    except Exception:
        pass
    return kws


def api(restaurant_id, method, data=None, user=None, db_path=DB_PATH) -> dict:
    """The one body of GET/POST /api/reviews/keywords and its mobile twin:
    {"ok", "keywords": [{"phrase", "used"}], "max"}. A POST sends the whole
    list (`keywords`) or one change (`add` / `remove`), so an add or a remove
    saves on its own and never overwrites a list someone else just changed."""
    from models import get_restaurant
    kws = of(get_restaurant(restaurant_id, db_path=db_path))
    if method == "POST":
        data = data or {}
        if "keywords" in data:
            kws = save(restaurant_id, data.get("keywords"), user=user, db_path=db_path)
        elif data.get("add"):
            kws = save(restaurant_id, kws + clean([data["add"]]), user=user, db_path=db_path)
        elif data.get("remove"):
            gone = str(data["remove"]).strip().lower()
            kws = save(restaurant_id, [k for k in kws if k.lower() != gone], user=user, db_path=db_path)
    used = usage(restaurant_id, kws, db_path=db_path)
    return {"ok": True, "keywords": [{"phrase": k, "used": used.get(k, 0)} for k in kws], "max": MAX_KEYWORDS}
