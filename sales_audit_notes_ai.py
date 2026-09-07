"""sales_audit_notes_ai.py — the audit's notes reader.

Every section of the audit has notes boxes (audit notes that may go in the
report, internal sales notes that never do) plus free-text answers. The
savings engine is deterministic and only reads typed answers, so anything
Will learns in conversation — "we actually count the bar weekly", "labor
runs high because the GM is on the floor", "he already uses 7shifts" —
would otherwise never touch the numbers.

read_notes() sends every note plus the current answers and computed
results to Claude and gets back three structured things, each tied to a
specific note:

  insights     — what a note changes about a category: raise or lower the
                 confidence of a sized opportunity, or plain context.
                 Tagged with the note's source (audit / internal) and
                 whether it is safe to show the owner.
  suggestions  — a concrete answer a note supports ("he said pour cost is
                 22%" → bar_pour_cost = 22). Never applied silently: the
                 tool shows them with an Apply button and the engine
                 recomputes from the typed answer like any other.
  caveats      — things a note says that the numbers cannot honour yet.

The engine applies insights (sales_audit_engine.apply_notes). Results
carry a fingerprint of the notes that were read, so the tool can tell
when notes have changed since and the read is stale.
"""
import hashlib
import json
import os
from datetime import datetime, timezone

from sales_audit_schema import SECTIONS

MODEL = "claude-sonnet-5"
MAX_NOTE_CHARS = 6000
CATEGORIES = ("labor", "food", "bar", "reviews", "marketing", "waitlist", "operations", "technology")
EFFECTS = ("raise_confidence", "lower_confidence", "context")
SECTION_TO_CATEGORY = {"labor": "labor", "food": "food", "bar": "bar", "reviews": "reviews", "marketing": "marketing",
                       "waitlist": "waitlist", "operations": "operations", "technology": "technology"}


def _questions():
    out = {}
    for sec in SECTIONS:
        for q in sec.get("questions") or []:
            out[q["id"]] = dict(q, section=sec["key"])
    return out


QUESTIONS = _questions()


def collect_notes(audit):
    """Every piece of free text typed during the audit, attributed."""
    notes = audit.get("notes") or {}
    answers = audit.get("answers") or {}
    out = []
    for sec in SECTIONS:
        n = notes.get(sec["key"]) or {}
        if isinstance(n, dict):
            if (n.get("audit") or "").strip():
                out.append({"section": sec["key"], "source": "audit", "text": n["audit"].strip()[:MAX_NOTE_CHARS],
                            "in_report": bool(n.get("audit_in_report"))})
            if (n.get("internal") or "").strip():
                out.append({"section": sec["key"], "source": "internal", "text": n["internal"].strip()[:MAX_NOTE_CHARS], "in_report": False})
        for q in sec.get("questions") or []:
            if q.get("type") == "textarea" and isinstance(answers.get(q["id"]), str) and answers[q["id"]].strip():
                out.append({"section": sec["key"], "source": "answer", "question": q["id"], "label": q["label"],
                            "text": answers[q["id"]].strip()[:MAX_NOTE_CHARS], "in_report": False})
    g = notes.get("_global") or {}
    if isinstance(g, dict) and (g.get("internal") or "").strip():
        out.append({"section": "_global", "source": "internal", "text": g["internal"].strip()[:MAX_NOTE_CHARS], "in_report": False})
    return out


def notes_fingerprint(audit):
    """Stable hash of everything the reader would see. Answers that are not
    free text are excluded on purpose: changing a number does not make a
    note-read stale, changing a note does."""
    items = [(n["section"], n["source"], n.get("question") or "", n["text"]) for n in collect_notes(audit)]
    return hashlib.sha1(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _answers_for_prompt(answers):
    rows = []
    for qid, q in QUESTIONS.items():
        v = answers.get(qid)
        if v in (None, "", []):
            continue
        if q.get("type") in ("textarea", "text", "tools"):
            continue
        rows.append("%s (%s): %s" % (qid, q["label"], json.dumps(v) if not isinstance(v, str) else v))
    unknown = answers.get("_unknown") or []
    if unknown:
        rows.append("Marked unknown by the owner: " + ", ".join(unknown))
    return "\n".join(rows) or "(no numeric answers yet)"


def _catalog_for_prompt():
    rows = []
    for qid, q in QUESTIONS.items():
        if q.get("type") in ("textarea", "text", "tools"):
            continue
        extra = ""
        if q.get("type") == "choice":
            extra = " options: " + " | ".join(q.get("options") or [])
        elif q.get("type") == "yesno":
            extra = " options: yes | no"
        elif q.get("type") == "rating":
            extra = " 1-5"
        elif q.get("type") == "percent":
            extra = " percent"
        elif q.get("type") == "currency":
            extra = " dollars"
        rows.append("%s [%s] %s%s" % (qid, q["section"], q["label"], extra))
    return "\n".join(rows)


def _results_for_prompt(results):
    rows = []
    for k in CATEGORIES:
        c = (results.get("categories") or {}).get(k) or {}
        if c.get("status") == "ok":
            rows.append("%s: sized $%s–$%s/yr, confidence %s. %s" % (k, "{:,.0f}".format(c.get("low") or 0), "{:,.0f}".format(c.get("high") or 0), c.get("confidence"), c.get("current_state") or ""))
        elif c.get("status") == "none":
            rows.append("%s: performing well, nothing counted. %s" % (k, c.get("current_state") or ""))
        else:
            rows.append("%s: insufficient data. Missing: %s" % (k, "; ".join(c.get("missing") or [])[:200]))
    return "\n".join(rows)


SYSTEM = """You are the notes reader inside Cavnar AI's in-person restaurant audit. Will Cavnar sits with an owner, types answers into a structured audit, and writes free-form notes per section. A deterministic engine sizes savings from the typed answers only. Your job is to read every note and say, precisely and conservatively, what the notes change.

Rules:
- Never invent a number. A suggestion must quote the note that states the fact, and the value must be exactly what the note says.
- Only suggest answers for questions in the catalog, with values valid for that question. When a note states a concrete figure for a catalog question — including one that differs from the typed value (a fresher count, a correction, a number the owner gave later) — suggest it and say which note it came from; Will decides whether to apply it. Do not suggest a value identical to what is typed. Seasonal or ranged figures become context, not suggestions, unless the note also gives a single current figure.
- Effects: "lower_confidence" when a note shows a sized opportunity is less real than the numbers imply (a reason the gap exists that the product will not fix, a number the owner guessed, a one-off period). "raise_confidence" when a note corroborates a sized gap with a concrete fact. "context" for everything else worth carrying into the results. Do not attach an effect to a category that has insufficient data — suggest the missing answer instead if the note contains it.
- Ignore small talk, rapport notes, and anything with no bearing on the operation.
- report_safe is true only for insights an owner could read in their own report: derived from audit notes, factual, no sales strategy, no opinions about the owner, no pricing or negotiation tactics. Anything from an internal note is never report_safe.
- Keep each text under 35 words, plain English, no jargon.
- Output JSON only, no prose, no code fence."""


def _prompt(audit, results, notes):
    name = audit.get("restaurant_name") or "the restaurant"
    owner = audit.get("owner_name") or "the owner"
    note_lines = []
    for i, n in enumerate(notes):
        tag = "%s / %s" % (n["section"], n["source"] + (" answer '%s'" % n.get("label") if n["source"] == "answer" else ""))
        note_lines.append("[%d] (%s) %s" % (i, tag, n["text"]))
    return (
        "Restaurant: %s. Owner: %s.\n\n" % (name, owner)
        + "CURRENT TYPED ANSWERS\n" + _answers_for_prompt(audit.get("answers") or {}) + "\n\n"
        + "CURRENT ENGINE RESULTS\n" + _results_for_prompt(results) + "\n\n"
        + "NOTES (numbered)\n" + "\n".join(note_lines) + "\n\n"
        + "QUESTION CATALOG (id [section] label)\n" + _catalog_for_prompt() + "\n\n"
        + 'Return exactly: {"insights":[{"note":<index>,"category":<one of %s or null>,"effect":<one of %s>,"text":"...","report_safe":true|false}],'
          '"suggestions":[{"note":<index>,"id":"<question id>","value":"<value>","reason":"..."}],"caveats":["..."]}'
        % ("/".join(CATEGORIES), "/".join(EFFECTS))
    )


def _call_claude(prompt):
    """Isolated so tests replace it. Raises on any failure."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    import anthropic
    from ai_utils import create_with_retry, extract_text
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = create_with_retry(client, model=MODEL, max_tokens=1800, system=SYSTEM,
                            messages=[{"role": "user", "content": prompt}], action="audit_notes_read")
    return extract_text(msg)


def _parse(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("notes reader returned no JSON")
    return json.loads(raw[start:end + 1])


def _valid_value(q, value):
    """Coerce a suggested value into what the tool would have stored for
    that question, or None if the note cannot support it."""
    t = q.get("type")
    s = str(value).strip()
    if not s:
        return None
    if t in ("currency", "percent", "integer", "decimal"):
        try:
            n = float(s.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return None
        if n < 0 and not q.get("allow_negative"):
            return None
        if t == "percent" and n > (q.get("max") if q.get("max") is not None else 100):
            return None
        if q.get("max") is not None and n > q["max"]:
            return None
        if q.get("min") is not None and n < q["min"]:
            return None
        return str(int(n)) if t == "integer" or n == int(n) else str(n)
    if t == "yesno":
        s = s.lower()
        return s if s in ("yes", "no") else None
    if t == "rating":
        return s if s in ("1", "2", "3", "4", "5") else None
    if t == "choice":
        opts = q.get("options") or []
        for o in opts:
            if o.lower() == s.lower():
                return o
        return s if q.get("allow_other") else None
    if t == "multi":
        return None
    if t == "date":
        return s if len(s) == 10 else None
    return s


def _sanitize(parsed, notes, answers):
    insights, suggestions, caveats = [], [], []
    for ins in parsed.get("insights") or []:
        if not isinstance(ins, dict):
            continue
        try:
            n = notes[int(ins.get("note"))]
        except (TypeError, ValueError, IndexError):
            continue
        text = str(ins.get("text") or "").strip()
        if not text:
            continue
        cat = ins.get("category") if ins.get("category") in CATEGORIES else SECTION_TO_CATEGORY.get(n["section"])
        eff = ins.get("effect") if ins.get("effect") in EFFECTS else "context"
        safe = bool(ins.get("report_safe")) and n["source"] == "audit"
        insights.append({"note": int(ins.get("note")), "section": n["section"], "source": n["source"], "category": cat,
                         "effect": eff, "text": text[:240], "report_safe": safe, "in_report": safe and n.get("in_report", False)})
    for sg in parsed.get("suggestions") or []:
        if not isinstance(sg, dict):
            continue
        q = QUESTIONS.get(str(sg.get("id") or ""))
        if not q:
            continue
        v = _valid_value(q, sg.get("value"))
        if v is None:
            continue
        cur = answers.get(q["id"])
        if cur not in (None, "") and str(cur).strip().lower() == v.lower():
            continue
        try:
            n = notes[int(sg.get("note"))]
        except (TypeError, ValueError, IndexError):
            n = {"section": q["section"], "source": "note"}
        suggestions.append({"note": sg.get("note"), "id": q["id"], "label": q["label"], "section": q["section"], "value": v,
                            "current": cur if cur not in (None, "") else None, "reason": str(sg.get("reason") or "").strip()[:200],
                            "source": n["source"]})
    for c in parsed.get("caveats") or []:
        if isinstance(c, str) and c.strip():
            caveats.append(c.strip()[:240])
    return insights, suggestions[:12], caveats[:8]


def read_notes(audit, results):
    """Run the reader. Returns the record to store on the audit. Raises if
    the model cannot be reached or returns nothing usable; the caller
    decides whether that blocks anything (it never blocks generating)."""
    notes = collect_notes(audit)
    fp = notes_fingerprint(audit)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not notes:
        return {"fingerprint": fp, "read_at": now, "model": None, "insights": [], "suggestions": [], "caveats": [], "notes_seen": 0}
    raw = _call_claude(_prompt(audit, results, notes))
    parsed = _parse(raw)
    insights, suggestions, caveats = _sanitize(parsed, notes, audit.get("answers") or {})
    return {"fingerprint": fp, "read_at": now, "model": MODEL, "insights": insights, "suggestions": suggestions,
            "caveats": caveats, "notes_seen": len(notes)}
