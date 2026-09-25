"""
invoices.py — a supplier invoice photo in, ingredient costs updated.

Food cost is only as right as the ingredient costs under it, and those were
typed in by hand once and then went stale: the reprice suggestions, the price
watch and every plate cost read unit_cost, and nothing kept it current. The
invoice is where the real price arrives every week, so this reads it.

THREE STEPS, AND THE OWNER CONFIRMS THE MIDDLE ONE.

  1. extract  — a model reads the photo or PDF into line items. It reads; it
                does not decide anything.
  2. propose  — Python matches each line to an ingredient and works out the
                new unit cost. Lines that don't add up, don't match exactly
                one ingredient, or whose units don't line up get NO proposal:
                they are shown, unselected, for the owner to settle.
  3. apply    — only the lines the owner confirmed are written, and the old
                cost is recorded next to the new one so the change can be
                read back (and undone by hand).

Prices only. Quantities received are NOT posted to stock: a misread "4" vs
"40" on a photo would silently corrupt the inventory ledger, and a receiving
entry has its own path (inventory_ledger.record_receiving) with its own
checks.
"""
import hashlib
import json
import logging
import math
from ai_utils import model_for
import re

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): the
    bound copy kept whatever models.get_conn was when this module was first
    imported — in a test run, a previous test's database."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

log = logging.getLogger(__name__)

# Reading prices off a photo is the one place in this product where a misread
# digit goes straight into every plate cost, so this uses the most capable
# model rather than the house default; invoices are a weekly, low-volume call.
MODEL = model_for("invoices")

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PDF_TYPE = "application/pdf"
# Both under the app's global 5 MB MAX_CONTENT_LENGTH (hosted_dashboard.py),
# with room for the multipart envelope; an image over this is also over the
# API's own 5 MB per-image ceiling. Phones should downscale before sending.
MAX_IMAGE_BYTES = int(4.5 * 1024 * 1024)
MAX_PDF_BYTES = int(4.5 * 1024 * 1024)

# A line "adds up" when quantity × unit price is within this of the printed
# line total (rounding on the invoice itself is usually a cent or two).
LINE_TOLERANCE_PCT = 2.0
LINE_TOLERANCE_ABS = 0.05
# A proposed cost this far from the current one is shown but not preselected:
# it is far more often a unit mismatch (case price vs per-lb) than a real
# overnight doubling.
BIG_CHANGE_PCT = 40.0

_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"],
                         "description": "YYYY-MM-DD if printed, else null"},
        "invoice_total": {"type": ["number", "null"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "line_total": {"type": ["number", "null"]},
                },
                "required": ["description", "quantity", "unit", "unit_price", "line_total"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["supplier", "invoice_date", "invoice_total", "lines"],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a restaurant supplier invoice. Transcribe every product line exactly as "
    "printed: the item description, the quantity shipped, the unit it is sold in (case, "
    "lb, each, gal ...), the unit price and the extended line total. Use null for any "
    "value that is not printed or not legible — never estimate or calculate a missing "
    "number. Skip non-product lines (fuel surcharge, deposits, tax, subtotal). Also give "
    "the supplier name, the invoice date, and the invoice total if printed."
)


class InvoiceError(ValueError):
    """A reason the invoice can't be read, phrased for the owner."""


# ── 1. extract ────────────────────────────────────────────────────────────────

def _content_block(data, media_type):
    import base64
    b64 = base64.standard_b64encode(data).decode("ascii")
    if media_type == PDF_TYPE:
        return {"type": "document",
                "source": {"type": "base64", "media_type": PDF_TYPE, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def check_upload(data, media_type):
    """Raise InvoiceError for anything the model shouldn't be sent."""
    if not data:
        raise InvoiceError("The file was empty.")
    if media_type not in IMAGE_TYPES and media_type != PDF_TYPE:
        raise InvoiceError("Send a photo (JPEG, PNG, WebP) or a PDF of the invoice.")
    limit = MAX_PDF_BYTES if media_type == PDF_TYPE else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise InvoiceError(f"That file is over {limit / (1024 * 1024):.1f} MB — "
                           f"try a smaller photo or a single-invoice PDF.")


def extract(restaurant_id, data, media_type, client=None):
    """The model's transcription of the invoice, as a dict matching _SCHEMA."""
    from ai_utils import create_with_retry, get_client
    check_upload(data, media_type)
    client = client or get_client()
    import data_health
    msg = create_with_retry(
        client, restaurant_id=restaurant_id, action="invoice_extract",
        # Rests on no data source: invoice OCR the owner confirms line by line.
        readiness=data_health.NOT_APPLICABLE,
        model=MODEL, max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": [_content_block(data, media_type),
                                               {"type": "text", "text": _PROMPT}]}])
    if getattr(msg, "stop_reason", None) == "refusal":
        raise InvoiceError("The invoice couldn't be read. Try a clearer photo.")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise InvoiceError("That invoice has more lines than can be read in one go — "
                           "photograph it a page at a time.")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        raise InvoiceError("The invoice couldn't be read. Try a clearer photo.")
    if not out.get("lines"):
        raise InvoiceError("No product lines were found on that invoice.")
    return out


# ── 2. propose ────────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z]+")
_UNIT_ALIASES = {
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb", "#": "lb",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "ea": "each", "each": "each", "ct": "each", "count": "each", "pc": "each", "pcs": "each",
    "cs": "case", "case": "case", "cases": "case", "bx": "case", "box": "case",
    "gal": "gal", "gallon": "gal", "gallons": "gal",
    "qt": "qt", "quart": "qt", "doz": "dozen", "dozen": "dozen",
    "kg": "kg", "g": "g", "l": "l", "liter": "l", "litre": "l",
}


def _unit(u):
    u = (u or "").strip().lower().rstrip(".")
    return _UNIT_ALIASES.get(u, u or None)


# Only true filler is dropped. A word the owner put in their own ingredient
# name ("Mozzarella Fresh", "Butter Unsalted") is what tells two products
# apart, so it must appear on the invoice line — stripping descriptive words
# priced fresh mozzarella off a whole-milk mozzarella line.
_NAME_FILLER = {"the", "and", "with"}


def _tokens(s, drop=_NAME_FILLER):
    return {w for w in _WORD.findall((s or "").lower()) if len(w) >= 3 and w not in drop}


def match_ingredient(description, ingredients):
    """The one ingredient this line is, or None.

    Every significant word of the ingredient's name must appear in the line
    ("Chicken Breast" matches "CHICKEN BREAST BNLS 4/10#"); a shared word is
    not enough, because "chicken" alone would price the thighs off the
    breast line. When several qualify, the most specific (most words) wins —
    only if it is the unique most specific one.
    """
    desc = _tokens(description, drop=_NAME_FILLER)
    hits = []
    for ing in ingredients:
        name = _tokens(ing["name"], drop=_NAME_FILLER)
        if name and name <= desc:
            hits.append((len(name), ing))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[0])
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        return None
    return hits[0][1]


def _adds_up(line):
    q, p, t = line.get("quantity"), line.get("unit_price"), line.get("line_total")
    if q is None or p is None or t is None:
        return None          # can't check — not the same as failing
    diff = abs(q * p - t)
    return diff <= LINE_TOLERANCE_ABS or (t and diff / abs(t) * 100 <= LINE_TOLERANCE_PCT)


def propose(restaurant_id, extracted, db_path=DB_PATH):
    """Attach a match and a proposed unit cost to every line, or the reason
    there isn't one. Nothing is written."""
    conn = get_conn(db_path)
    try:
        ingredients = [dict(r) for r in conn.execute(
            "SELECT id, name, unit, unit_cost, case_size FROM ingredients "
            "WHERE restaurant_id=? AND is_active=1", (restaurant_id,)).fetchall()]
    finally:
        conn.close()

    lines = []
    for i, raw in enumerate(extracted.get("lines") or []):
        line = {"index": i, "description": raw.get("description") or "",
                "quantity": raw.get("quantity"), "unit": raw.get("unit"),
                "unit_price": raw.get("unit_price"), "line_total": raw.get("line_total"),
                "ingredient_id": None, "ingredient_name": None, "current_cost": None,
                "proposed_cost": None, "change_pct": None, "selected": False, "note": None,
                "verified": False}
        ok = _adds_up(raw)
        ing = match_ingredient(line["description"], ingredients)
        if ing:
            line.update(ingredient_id=ing["id"], ingredient_name=ing["name"],
                        current_cost=ing["unit_cost"])
        if ok is False:
            line["note"] = "quantity × price doesn't match the line total — check the photo"
        elif line["unit_price"] is None or line["unit_price"] <= 0:
            line["note"] = "no unit price read"
        elif not ing:
            line["note"] = "no single matching ingredient — pick one to update it"
        else:
            inv_unit, ing_unit = _unit(line["unit"]), _unit(ing["unit"])
            case_size = float(ing.get("case_size") or 1)
            cost = None
            if inv_unit and ing_unit and inv_unit == ing_unit:
                cost = line["unit_price"]
            elif inv_unit == "case" and case_size > 1:
                cost = line["unit_price"] / case_size
            if cost is None:
                line["note"] = (f"invoice is priced per {line['unit'] or 'unknown unit'}, the "
                                f"ingredient is costed per {ing['unit'] or 'unknown unit'} — "
                                f"enter the cost yourself")
            else:
                cost = round(cost, 4)
                line["proposed_cost"] = cost
                cur = ing["unit_cost"] or 0
                if cur > 0:
                    line["change_pct"] = round((cost - cur) / cur * 100, 1)
                big = line["change_pct"] is not None and abs(line["change_pct"]) >= BIG_CHANGE_PCT
                if big:
                    line["note"] = "a large change — usually a unit mix-up; check before applying"
                # Preselected only when it plainly adds up (or can't be
                # checked), matched exactly one ingredient, units agree, and
                # the move is plausible.
                line["selected"] = not big and cost != cur
                # Whether the reading passed BOTH checks this code has: the
                # line's own arithmetic ran and agreed, and there was a
                # current cost to compare the move against. A preselected
                # line an owner is looking at can rest on less; one applied
                # with nobody looking cannot — a $189 read for $1.89 on an
                # ingredient with no cost yet, or a line whose quantity or
                # total was not read, went straight into plate costs (AI-19).
                line["verified"] = bool(ok is True and cur > 0 and not big)
        lines.append(line)

    totals = [l["line_total"] for l in lines if l["line_total"] is not None]
    inv_total = extracted.get("invoice_total")
    total_check = None
    if inv_total and totals:
        # Surcharges and tax are skipped on purpose, so the lines can
        # legitimately sum to a little under the printed total — only a sum
        # OVER it, or far under, suggests a misread.
        s = round(sum(totals), 2)
        total_check = {"lines_sum": s, "invoice_total": inv_total,
                       "plausible": s <= inv_total + LINE_TOLERANCE_ABS and s >= inv_total * 0.8}
    return {"supplier": extracted.get("supplier"), "invoice_date": extracted.get("invoice_date"),
            "lines": lines, "total_check": total_check,
            "ingredients": [{"id": x["id"], "name": x["name"], "unit": x["unit"]}
                            for x in ingredients]}


def scan(restaurant_id, data, media_type, user_id=None, db_path=DB_PATH, client=None):
    """Extract + propose + store as a pending import. The same file sent twice
    returns the stored import rather than paying to read it again."""
    check_upload(data, media_type)
    sha = hashlib.sha256(data).hexdigest()
    conn = get_conn(db_path)
    try:
        prior = conn.execute(
            "SELECT id FROM invoice_imports WHERE restaurant_id=? AND image_sha=?",
            (restaurant_id, sha)).fetchone()
    finally:
        conn.close()
    if prior:
        out = get_import(restaurant_id, prior["id"], db_path=db_path)
        out["duplicate"] = True
        return out
    proposal = propose(restaurant_id, extract(restaurant_id, data, media_type, client=client),
                       db_path=db_path)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, image_sha, "
            "lines_json, created_by) VALUES (?,?,?,?,?,?)",
            (restaurant_id, proposal["supplier"], proposal["invoice_date"], sha,
             json.dumps({"lines": proposal["lines"], "total_check": proposal["total_check"]}),
             user_id))
        conn.commit()
        proposal["id"] = cur.lastrowid
    finally:
        conn.close()
    proposal["duplicate"] = False
    proposal["applied_at"] = None
    return proposal


def mark_applied(out, applied, auto_applied):
    """Tag each line of a proposal with whether it went in, and say whether
    the import still waits for the owner: the rule applied some lines on
    its own and no person has acted on the rest yet (M-4). Web and iOS show
    the remaining lines with an apply button while `awaiting_owner`."""
    applied = [a for a in (applied or []) if isinstance(a, dict)]
    done = {a.get("index") for a in applied}
    for ln in out.get("lines") or []:
        ln["applied"] = ln.get("index") in done
        if ln["applied"]:
            ln["selected"] = False
    owner_acted = any(a.get("by") == "owner" for a in applied)
    out["auto_applied_count"] = sum(1 for a in applied if a.get("by") == "rule") if auto_applied else 0
    out["awaiting_owner"] = bool(auto_applied and not owner_acted
                                 and any(not ln.get("applied") for ln in (out.get("lines") or [])))
    return out


def get_import(restaurant_id, import_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM invoice_imports WHERE id=? AND restaurant_id=?",
                           (import_id, restaurant_id)).fetchone()
        ingredients = [dict(r) for r in conn.execute(
            "SELECT id, name, unit FROM ingredients WHERE restaurant_id=? AND is_active=1 "
            "ORDER BY name", (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    if not row:
        return None
    body = json.loads(row["lines_json"] or "{}")
    applied = json.loads(row["applied_json"]) if row["applied_json"] else None
    out = {"id": row["id"], "supplier": row["supplier"], "invoice_date": row["invoice_date"],
           "lines": body.get("lines", []), "total_check": body.get("total_check"),
           "applied": applied,
           "applied_at": row["applied_at"], "created_at": row["created_at"],
           "ingredients": ingredients}
    auto = row["auto_applied"] if "auto_applied" in row.keys() else 0
    return mark_applied(out, applied, bool(auto))


def list_imports(restaurant_id, limit=20, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM invoice_imports WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        applied = json.loads(r["applied_json"]) if r["applied_json"] else []
        try:
            lines = (json.loads(r["lines_json"] or "{}") or {}).get("lines") or []
        except Exception:
            lines = []
        # `pending` is what the owner still owes this invoice: never applied,
        # or applied by the trusted-supplier rule with lines left for a person
        # (mark_applied's awaiting_owner). Pending imports lived only in page
        # memory, so after a reload Home kept asking about an invoice no
        # screen could open (friction audit U2-2).
        tagged = mark_applied({"lines": [dict(ln) for ln in lines]}, applied,
                              bool(r["auto_applied"]) if "auto_applied" in r.keys() else False)
        waiting = sum(1 for ln in tagged["lines"] if not ln.get("applied"))
        out.append({"id": r["id"], "supplier": r["supplier"], "invoice_date": r["invoice_date"],
                    "created_at": r["created_at"], "applied_at": r["applied_at"],
                    "updated": len(applied),
                    "lines": len(lines), "waiting": waiting if (not r["applied_at"] or tagged["awaiting_owner"]) else 0,
                    "pending": (not r["applied_at"]) or bool(tagged["awaiting_owner"])})
    return out


def checked_selections(invoice) -> list:
    """The selections apply() takes for every line of a loaded import that
    Cavnar preselected and has not been applied: matched to one ingredient,
    with a proposed cost. What "apply the checked lines" means when it is
    not a person ticking boxes (Ask's apply_invoice_lines)."""
    out = []
    for ln in (invoice or {}).get("lines") or []:
        if ln.get("applied") or not ln.get("selected"):
            continue
        if not ln.get("ingredient_id") or not ln.get("proposed_cost"):
            continue
        out.append({"index": ln.get("index"), "ingredient_id": ln["ingredient_id"],
                    "unit_cost": ln["proposed_cost"]})
    return out


def pending_imports(restaurant_id, db_path=DB_PATH):
    """The scanned invoices still waiting on the owner, newest first - what
    Home's "N scanned invoices not applied" counts and Food Cost lists."""
    return [i for i in list_imports(restaurant_id, db_path=db_path)
            if i.get("pending", not i.get("applied_at"))]


# ── 3. apply ──────────────────────────────────────────────────────────────────

def apply(restaurant_id, import_id, selections, user_id=None, db_path=DB_PATH, auto=False):
    """Write the confirmed costs. selections: [{"index", "ingredient_id",
    "unit_cost"}] — the owner's choices, which may differ from the proposal
    (a different ingredient, a corrected cost). Returns {"ok", "updated"}.

    Each LINE applies once: a second tap, or a second device, must not
    write the same line twice on top of a correction made in between. An
    import is not closed by its first apply, though. The trusted-supplier
    rule (ordering.auto_apply_if_trusted, auto=True) applies only the
    verified lines, and the flagged ones it skipped still wait for a person;
    they used to answer "already been applied" (409) because the rule had
    claimed the whole import (M-4). A later apply writes only lines not
    already in applied_json.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT applied_at, applied_json FROM invoice_imports WHERE id=? AND restaurant_id=?",
                           (import_id, restaurant_id)).fetchone()
        if not row:
            return {"ok": False, "error": "Invoice not found."}
        prior_json = row["applied_json"]
        prior = []
        if row["applied_at"]:
            try:
                prior = [p for p in (json.loads(prior_json or "[]") or []) if isinstance(p, dict)]
            except Exception:
                prior = []
        done_idx = {p.get("index") for p in prior}
        applied = []
        for s in selections or []:
            if row["applied_at"] and s.get("index") in done_idx:
                continue          # this line already went in

            try:
                ing_id = int(s["ingredient_id"])
                cost = round(float(s["unit_cost"]), 4)
            except (KeyError, TypeError, ValueError):
                continue
            # NaN fails every comparison, so it slipped past `cost <= 0 or
            # cost > 100000`, was stored as NULL, and broke analysis (MOD-FC-6).
            if not math.isfinite(cost) or cost <= 0 or cost > 100000:
                continue
            ing = conn.execute("SELECT name, unit_cost FROM ingredients WHERE id=? AND "
                               "restaurant_id=? AND is_active=1", (ing_id, restaurant_id)).fetchone()
            if not ing:
                continue          # not this restaurant's — never write it
            conn.execute("UPDATE ingredients SET unit_cost=?, updated_at=datetime('now') "
                         "WHERE id=? AND restaurant_id=?", (cost, ing_id, restaurant_id))
            applied.append({"index": s.get("index"), "ingredient_id": ing_id,
                            "name": ing["name"], "old_cost": ing["unit_cost"], "new_cost": cost,
                            "by": "rule" if auto else "owner"})
        if row["applied_at"] and not applied:
            conn.rollback()
            return {"ok": False, "error": "This invoice has already been applied."}
        # Claim the import in the same transaction as the writes, guarded on
        # the state this apply read (applied_at NULL, or the same applied
        # lines), so two concurrent applies can't both win.
        if row["applied_at"]:
            cur = conn.execute("UPDATE invoice_imports SET applied_json=? "
                               "WHERE id=? AND restaurant_id=? AND applied_at IS NOT NULL "
                               "AND COALESCE(applied_json,'') = COALESCE(?,'')",
                               (json.dumps(prior + applied), import_id, restaurant_id, prior_json))
        else:
            cur = conn.execute("UPDATE invoice_imports SET applied_json=?, applied_at=datetime('now'), "
                               "auto_applied=? WHERE id=? AND restaurant_id=? AND applied_at IS NULL",
                               (json.dumps(applied), 1 if auto else 0, import_id, restaurant_id))
        if cur.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "This invoice has already been applied."}
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "updated": applied}
