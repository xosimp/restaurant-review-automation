"""review_signals.py — what the owner and the guests tell the review layer
that it did not capture (memory audit 9/29/26, workstream M6: "uncaptured").

  owner re-tags     A review's category, sentiment, severity or dish was
                    whatever the analyser wrote: a mis-tagged review could not
                    be fixed, so nothing learned from it, and "the carbonara"
                    and "carbonara pasta" landed in separate clusters. Now the
                    owner (or a manager) can re-tag a review (retag): the
                    review row takes the correction, the correction is kept
                    (review_retags, kept), a later re-analysis keeps it
                    (overlay), and the analyser reads the restaurant's recent
                    corrections as examples — never an admin's or a view-as
                    one — with the restaurant's own menu as the dish
                    vocabulary (menu_dishes / map_dishes).
  review requests   were counted and never matched to the review they asked
                    for. Now each request is matched to a later review by the
                    guest's name, unambiguous or not at all
                    (match_review_requests, at review ingest), and the
                    request stats carry a conversion over requests whose
                    window has closed.
"""
import json
import re
from datetime import datetime, timedelta

RETAG_FIELDS = ("categories", "sentiment", "severity", "dishes")
RETAG_EXAMPLES = 4
RETAG_EXAMPLE_CHARS = 220
MENU_VOCAB_MAX = 60
REQUEST_WINDOW_DAYS = 14         # a review this long after the request can be its answer
REQUEST_MATCH_LOOKBACK_DAYS = 45  # requests still being matched
CONVERSION_MIN_REQUESTS = 10     # closed-window requests before a conversion % is said


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── the menu as the dish vocabulary ─────────────────────────────────────────

def menu_dishes(restaurant_id, db_path=None) -> list:
    """The restaurant's active menu dishes by name (at most MENU_VOCAB_MAX).
    Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT name FROM menu_items WHERE restaurant_id=? AND is_active=1 "
                                "AND COALESCE(kind, 'dish')='dish' AND TRIM(COALESCE(name, '')) != '' "
                                "ORDER BY name COLLATE NOCASE LIMIT ?", (restaurant_id, MENU_VOCAB_MAX)).fetchall()
        finally:
            conn.close()
        return [str(r["name"]).strip() for r in rows]
    except Exception:
        return []


def _words(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if w in ("the", "a", "an", "our", "their", "my", "of", "and", "with") or len(w) < 2:
            continue
        if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def map_dishes(dishes, menu) -> list:
    """Each dish the guest named, as the menu names it when exactly one menu
    dish matches by whole words (every word of one inside the other) —
    lowercased like every stored dish; otherwise as the guest wrote it. Two
    candidates is no match: "pasta" never becomes one of three pastas.
    Deterministic, deduplicated, order kept."""
    named = [(m, _words(m)) for m in (menu or []) if _words(m)]
    out = []
    for d in dishes or []:
        said = _words(d)
        hits = [m for m, w in named if said and (said <= w or w <= said)]
        name = hits[0].lower() if len(hits) == 1 else str(d)
        if name not in out:
            out.append(name)
    return out


# ── owner re-tags ───────────────────────────────────────────────────────────

def _clean(field, value):
    """The re-tag value in the analyser's own vocabulary, or raise
    ValueError naming what is wrong."""
    import analyser
    if field == "categories":
        vals = [str(v).strip().lower() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
        bad = [v for v in vals if v not in analyser.CATEGORIES]
        if bad or not vals:
            raise ValueError("Pick one to three of: " + ", ".join(analyser.category_label(c)
                                                                 for c in analyser.CATEGORIES) + ".")
        return list(dict.fromkeys(vals))[:3]
    if field == "sentiment":
        v = str(value or "").strip().lower()
        if v not in analyser.SENTIMENTS:
            raise ValueError("A review reads positive, neutral or negative.")
        return v
    if field == "severity":
        v = str(value or "").strip().lower()
        if v not in analyser.SEVERITIES:
            raise ValueError("Pick one of: " + ", ".join(analyser.SEVERITY_LABELS[x] for x in analyser.SEVERITIES)
                             + ".")
        return v
    if field == "dishes":
        vals = value if isinstance(value, list) else [value]
        out = [n for n in (analyser._clean_dish(v) for v in vals) if n]
        return list(dict.fromkeys(out))[:3]
    raise ValueError(f"{field} can't be re-tagged")


def retag(restaurant_id, review_id, changes, user=None, db_path=None) -> dict:
    """Correct a review's tags: {"categories"?, "sentiment"?, "severity"?,
    "dishes"?}. The review row takes the correction and each changed field
    is kept in review_retags with who made it. {"ok", "changed", "review"}
    or {"ok": False, "error"}."""
    changes = {k: v for k, v in (changes or {}).items() if k in RETAG_FIELDS}
    if not changes:
        return {"ok": False, "error": "Say what to change: the topics, how it reads, how serious it is, or the "
                                      "dishes it names."}
    try:
        clean = {k: _clean(k, v) for k, v in changes.items()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    from permissions import answer_authority
    u = user or {}
    authority = answer_authority(u) if u else None
    uid = u.get("acting_admin_id") or u.get("id")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT categories, sentiment, severity, entities FROM reviews WHERE id=? AND "
                           "restaurant_id=? AND deleted_at IS NULL", (review_id, restaurant_id)).fetchone()
        if not row:
            return {"ok": False, "error": "Review not found."}
        try:
            ents = json.loads(row["entities"] or "{}") or {}
        except (TypeError, ValueError):
            ents = {}
        try:
            before_cats = json.loads(row["categories"] or "[]") or []
        except (TypeError, ValueError):
            before_cats = []
        before = {"categories": before_cats, "sentiment": row["sentiment"], "severity": row["severity"],
                  "dishes": list(ents.get("dishes") or [])}
        changed = [k for k, v in clean.items() if v != before.get(k)]
        if not changed:
            return {"ok": True, "changed": [], "review": {"id": review_id, **before}}
        after = dict(before, **{k: clean[k] for k in changed})
        if "dishes" in changed:
            ents["dishes"] = after["dishes"]
            if not ents["dishes"]:
                ents.pop("dishes")
        conn.execute("UPDATE reviews SET categories=?, sentiment=?, severity=?, entities=? WHERE id=? AND "
                     "restaurant_id=?", (json.dumps(after["categories"]), after["sentiment"], after["severity"],
                                         json.dumps(ents) if ents else None, review_id, restaurant_id))
        for k in changed:
            conn.execute("INSERT INTO review_retags (restaurant_id, review_id, field, before_json, after_json, "
                         "user_id, authority) VALUES (?,?,?,?,?,?,?)",
                         (restaurant_id, review_id, k, json.dumps(before.get(k)), json.dumps(after[k]), uid,
                          authority))
        conn.commit()
    finally:
        conn.close()
    try:
        import home_brief
        home_brief.invalidate(int(restaurant_id))
    except Exception:
        pass
    return {"ok": True, "changed": changed, "review": {"id": review_id, **after}}


def overlay(restaurant_id, review_id, result, db_path=None) -> dict:
    """An analysis result with the owner's corrections to this review laid
    over it: a re-analysis never undoes a re-tag. Mutates and returns
    `result`. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT field, after_json FROM review_retags WHERE restaurant_id=? AND review_id=? "
                                "ORDER BY id", (restaurant_id, review_id)).fetchall()
        finally:
            conn.close()
    except Exception:
        return result
    for r in rows:
        try:
            v = json.loads(r["after_json"])
        except (TypeError, ValueError):
            continue
        if r["field"] == "dishes":
            ents = dict(result.get("entities") or {})
            if v:
                ents["dishes"] = v
            else:
                ents.pop("dishes", None)
            result["entities"] = ents or None
        elif r["field"] in ("categories", "sentiment", "severity"):
            result[r["field"]] = v
    return result


def retag_examples(restaurant_id, db_path=None, rating=None) -> list:
    """The restaurant's recent re-tagged reviews as analyser examples:
    [{"text", "rating", "fields": {field: value}, "by": "owner" | "manager"}].
    Never an admin's or a view-as correction, never a removed review, and
    nothing for a restaurant that does not learn for itself
    (models.learns_for_itself). With `rating` (the review being analysed),
    the corrections of reviews in its star band come first, then the rest,
    newest first within each (memory re-audit 9/29/26, PROMPTS-12: the four
    newest, whatever they were about). `by` names whose correction it is — a
    manager's re-tag is never labelled the owner's. Never raises."""
    try:
        import models
        if not models.learns_for_itself(restaurant_id):
            return []
        conn = get_conn(db_path)
        try:
            band = models.reply_band(rating) if rating is not None else None
            band_first = (f"CASE WHEN r.rating IN ({','.join(str(int(x)) for x in band)}) THEN 0 ELSE 1 END, "
                          if band else "")
            rows = conn.execute(
                "SELECT t.review_id, t.field, t.after_json, t.authority, r.text, r.rating FROM review_retags t "
                "JOIN reviews r ON r.id=t.review_id AND r.restaurant_id=t.restaurant_id "
                "WHERE t.restaurant_id=? AND COALESCE(t.authority, '') != 'admin' AND r.deleted_at IS NULL "
                f"ORDER BY {band_first}t.id DESC LIMIT 40", (restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out, by = [], {}
    for r in rows:
        if r["review_id"] not in by:
            if len(by) >= RETAG_EXAMPLES:
                continue
            by[r["review_id"]] = {"text": str(r["text"] or "")[:RETAG_EXAMPLE_CHARS], "rating": r["rating"],
                                  "fields": {}, "by": "manager" if r["authority"] == "delegate" else "owner"}
            out.append(by[r["review_id"]])
        fields = by[r["review_id"]]["fields"]
        if r["field"] not in fields:                 # the newest correction of each field
            try:
                fields[r["field"]] = json.loads(r["after_json"])
            except (TypeError, ValueError):
                continue
    return out


def analyser_block(restaurant_id, db_path=None, rating=None) -> str:
    """The analyser prompt's restaurant block: the restaurant's corrections
    as examples (the review text fenced; those in the analysed review's star
    band first — `rating`), each labelled by whose it is, and the menu as the
    dish vocabulary. "" when there is neither."""
    from ai_guard import wrap_untrusted
    parts = []
    ex = retag_examples(restaurant_id, db_path=db_path, rating=rating)
    if ex:
        lines = []
        for e in ex:
            tags = "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in e["fields"].items())
            whose = "A manager's tags" if e.get("by") == "manager" else "The owner's tags"
            lines.append(f"{e['rating']}-star review:\n{wrap_untrusted(e['text'])}\n{whose}: {tags}")
        parts.append("THIS RESTAURANT'S CORRECTIONS — the owner (or a manager, where it says so) re-tagged these "
                     "reviews by hand. Tag a review like one of them the way they did:\n" + "\n".join(lines))
    menu = menu_dishes(restaurant_id, db_path=db_path)
    if menu:
        parts.append("This restaurant's menu (a dish a guest names is often one of these — still write `dishes` "
                     "as the guest wrote them): " + ", ".join(menu))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# ── review requests matched to the reviews they asked for ───────────────────

def _first_and_initial(name):
    words = [w for w in re.findall(r"[A-Za-z]+", str(name or ""))]
    if not words:
        return None, None
    return words[0].lower(), (words[1][0].lower() if len(words) > 1 else None)


def _same_guest(request_name, author):
    f1, i1 = _first_and_initial(request_name)
    f2, i2 = _first_and_initial(author)
    if not f1 or not f2 or f1 != f2 or len(f1) < 2:
        return False
    return not (i1 and i2 and i1 != i2)


def match_review_requests(restaurant_id, db_path=None, now=None) -> int:
    """Match review requests sent in the last REQUEST_MATCH_LOOKBACK_DAYS to a
    review posted within REQUEST_WINDOW_DAYS after, by the guest's first
    name (and last initial where both have one). Only a one-to-one match is
    kept: a request two reviews could answer, or a review two requests could
    have asked for, is left unmatched. Writes review_requests.review_id /
    matched_at. Returns how many were matched. Never raises."""
    now = now or datetime.utcnow()
    since = (now - timedelta(days=REQUEST_MATCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
        try:
            reqs = [dict(r) for r in conn.execute(
                "SELECT id, customer_name, sent_at FROM review_requests WHERE restaurant_id=? AND review_id IS NULL "
                "AND sent_at >= ? AND TRIM(COALESCE(customer_name, '')) != ''", (restaurant_id, since)).fetchall()]
            if not reqs:
                return 0
            taken = {r["review_id"] for r in conn.execute(
                "SELECT review_id FROM review_requests WHERE restaurant_id=? AND review_id IS NOT NULL",
                (restaurant_id,)).fetchall()}
            revs = [dict(r) for r in conn.execute(
                "SELECT id, author, COALESCE(NULLIF(review_date, ''), fetched_at) AS at FROM reviews "
                "WHERE restaurant_id=? AND deleted_at IS NULL AND COALESCE(NULLIF(review_date, ''), fetched_at) >= ?",
                (restaurant_id, since)).fetchall() if r["id"] not in taken]
            for v in revs:
                at = str(v["at"] or "")[:19].replace("T", " ")
                v["at"] = at + " 23:59:59" if len(at) == 10 else at      # a date alone: that day, any time
            cands = {}
            for q in reqs:
                sent = str(q["sent_at"] or "")[:19].replace("T", " ")
                end = (datetime.fromisoformat(sent[:19]) + timedelta(days=REQUEST_WINDOW_DAYS)).strftime(
                    "%Y-%m-%d %H:%M:%S") if sent else ""
                cands[q["id"]] = [v["id"] for v in revs
                                  if sent <= v["at"] <= end
                                  and _same_guest(q["customer_name"], v["author"])]
            per_review = {}
            for qid, vs in cands.items():
                for v in vs:
                    per_review.setdefault(v, []).append(qid)
            n = 0
            for qid, vs in cands.items():
                if len(vs) == 1 and len(per_review.get(vs[0], [])) == 1:
                    n += conn.execute("UPDATE review_requests SET review_id=?, matched_at=datetime('now') "
                                      "WHERE id=? AND review_id IS NULL", (vs[0], qid)).rowcount
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception as e:
        print(f"[review_signals] review requests not matched for {restaurant_id}: {e}")
        return 0


def request_conversion(restaurant_id, db_path=None, now=None) -> dict:
    """{"asked", "reviewed", "pct", "window_days", "basis"} over requests
    whose REQUEST_WINDOW_DAYS have passed — pct None below
    CONVERSION_MIN_REQUESTS. Never raises."""
    now = now or datetime.utcnow()
    closed = (now - timedelta(days=REQUEST_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    out = {"asked": 0, "reviewed": 0, "pct": None, "window_days": REQUEST_WINDOW_DAYS,
           "basis": (f"a request counts once its {REQUEST_WINDOW_DAYS} days have passed; a review is matched to it "
                     "by the guest's name, only when exactly one fits")}
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT COUNT(*) AS asked, SUM(CASE WHEN review_id IS NOT NULL THEN 1 ELSE 0 END) AS got "
                               "FROM review_requests WHERE restaurant_id=? AND sent_at <= ?",
                               (restaurant_id, closed)).fetchone()
        finally:
            conn.close()
    except Exception:
        return out
    asked, got = int(row["asked"] or 0), int(row["got"] or 0)
    out.update(asked=asked, reviewed=got)
    if asked >= CONVERSION_MIN_REQUESTS:
        out["pct"] = round(got / asked * 100, 1)
    return out
