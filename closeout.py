"""
closeout.py — the 60 seconds at the end of the night that the data can't see.

Every number this product reads about a service arrives the next morning:
the POS syncs at 3am, reviews land days later, labor is settled after the
pay period. So the one account of what actually happened tonight is the
person who was standing in it, and nothing asked them.

Four quick questions, none of them typing-heavy, all optional:

  * what went well            (the thing worth repeating)
  * what went wrong           (the thing worth fixing)
  * what you ran out of       (86s — tomorrow's order, in tonight's words)
  * who didn't make it        (callouts — tomorrow's schedule problem)

and, for the nightly Daily Sales Report (dsr/block_closeout.py), six more,
also optional: equipment, VIP guests, maintenance, shift notes, general
notes, and `influence` — Erik's "Influence/Result" column, the closer's own
read of why the day went the way it did.

It is a HANDOFF, not a report: it is written by whoever closed and read by
whoever opens, and it appears in the next morning's brief. Nothing here is
scored or ranked, and nothing rewrites it — an owner reading their GM's own
sentence is the point. The one model that reads it is the Daily Sales
Report narrative (dsr/narrative.py), and only as fenced, untrusted data: it
may inform the night's read, never be quoted as a figure or be the basis of
an action.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

# The four quick lines first, then the six the Daily Sales Report adds.
# Order is the order every surface shows them in.
FIELDS = ("went_well", "went_wrong", "eighty_sixed", "callouts",
          "equipment", "vip_guests", "maintenance", "shift_notes", "general_notes", "influence")
DSR_FIELDS = FIELDS[4:]
# What each field is called wherever it is shown (web, iOS, the DSR).
LABELS = {
    "went_well": "What went well",
    "went_wrong": "What went wrong",
    "eighty_sixed": "What we ran out of",
    "callouts": "Who didn't make it",
    "equipment": "Equipment",
    "vip_guests": "VIP guests",
    "maintenance": "Maintenance",
    "shift_notes": "Shift notes",
    "general_notes": "General notes",
    "influence": "Why the day went how it did",
}
MAX_LEN = 1000


def business_date_for(restaurant, now_local=None):
    """The service a close-out belongs to. A close-out filed at 1am is about
    yesterday's service — restaurants close after midnight, and filing it
    under the new date would put it in the wrong morning's brief."""
    from time_utils import restaurant_now, business_date
    local = now_local or restaurant_now(restaurant, naive=True)
    return business_date(restaurant, local)


def save(restaurant_id, fields, user_id=None, submitted_by=None, business_date=None,
         db_path=DB_PATH, restaurant=None):
    """Write tonight's close-out. Re-filing the same night replaces it —
    a manager correcting what they typed is not a second close-out.

    Replaces the fields it is SENT; a field the request leaves out keeps
    what was filed. Web and the current app always send all ten (an empty
    string clears one), but an app build from before the DSR fields knows
    only the first four, and re-filing from it must not wipe the six a
    manager wrote from the web."""
    from models import get_restaurant
    restaurant = restaurant or get_restaurant(restaurant_id)
    day = (business_date or business_date_for(restaurant)).isoformat() \
        if not isinstance(business_date, str) else business_date
    fields = fields if isinstance(fields, dict) else {}
    values = {k: (str(fields.get(k) or "").strip()[:MAX_LEN] or None) for k in FIELDS if k in fields}
    existing = get(restaurant_id, day, db_path=db_path) or {}
    merged = {k: (values[k] if k in values else existing.get(k)) for k in FIELDS}
    if not any(merged.values()):
        raise ValueError("Write at least one line — otherwise there is nothing to hand over.")
    cols = ", ".join(FIELDS)
    marks = ",".join("?" for _ in FIELDS)
    sent = [k for k in FIELDS if k in values]
    updates = "".join(f"{k}=excluded.{k}, " for k in sent)
    conn = get_conn(db_path)
    try:
        conn.execute(
            f"INSERT INTO close_outs (restaurant_id, business_date, submitted_by, user_id, {cols}) "
            f"VALUES (?,?,?,?,{marks}) "
            "ON CONFLICT(restaurant_id, business_date) DO UPDATE SET "
            "submitted_by=excluded.submitted_by, user_id=excluded.user_id, "
            f"{updates}created_at=datetime('now')",
            (restaurant_id, day, submitted_by, user_id, *(merged[k] for k in FIELDS)))
        conn.commit()
    finally:
        conn.close()
    try:
        _act_on(restaurant_id, day, values, restaurant, db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="closeout_actions", context=f"restaurant_id={restaurant_id}")
    return get(restaurant_id, day, db_path=db_path)


def _act_on(restaurant_id, day, values, restaurant, db_path=DB_PATH):
    """What the close-out changes, beyond tomorrow's brief.

    An 86 is a count of zero: the ingredient it names is recounted to 0
    tonight (inventory_ledger.record_recount, source 'closeout'), which is
    exactly what puts it at the top of tomorrow's order draft. A callout is
    tomorrow's staffing gap: it opens a routed issue carrying who could
    cover (labor_replacements), the same answer the coverage check gives.
    Names are matched, never invented — a dish with no ingredient of that
    name changes nothing."""
    eighty = (values.get("eighty_sixed") or "").strip()
    if eighty and getattr(restaurant, "module_inventory", 0):
        import inventory_ledger
        conn = get_conn(db_path)
        try:
            ings = [dict(r) for r in conn.execute(
                "SELECT id, name FROM ingredients WHERE restaurant_id=? AND is_active=1", (restaurant_id,)).fetchall()]
        finally:
            conn.close()
        for phrase in _phrases(eighty):
            hit = _match_ingredient(phrase, ings)
            if hit:
                inventory_ledger.record_recount(restaurant_id, hit["id"], 0.0, event_date=day,
                                                source="closeout", note=f"86'd at close: {phrase}")
                # Ran out in service: was it ever flagged as running low
                # first? If not, a missed detection (ROI #44).
                try:
                    import rec_ledger
                    rec_ledger.note_problem(restaurant_id, "closeout",
                                            rec_ledger.rec_key("stock_low", hit["name"]), module="food",
                                            detail=f"86'd at close: {phrase}", db_path=db_path)
                except Exception as e:
                    print(f"[closeout] missed-detection note failed for {restaurant_id}: {e}")
    callouts = (values.get("callouts") or "").strip()
    if callouts and getattr(restaurant, "module_labor", 0):
        import issues, labor_replacements
        from datetime import date as _d, timedelta as _td
        tomorrow = _d.fromisoformat(day) + _td(days=1)
        fits = ""
        try:
            first = _phrases(callouts)[0] if _phrases(callouts) else ""
            fits = labor_replacements.sentence(labor_replacements.for_gap(
                restaurant_id, None, tomorrow.strftime("%A"), exclude={first}, db_path=db_path,
                on_date=tomorrow.isoformat()))
        except Exception:
            pass
        issues.create_issue(restaurant_id, "callout", f"Callout at close: {callouts[:70]}",
                            detail=f"From tonight's close-out: {callouts}." + fits,
                            severity="normal", source_key=f"callout:{day}", db_path=db_path)


def _phrases(text):
    import re
    parts = re.split(r"[,;\n]+|\band\b", text or "")
    return [p.strip(" .") for p in parts if p and p.strip(" .")]


def _match_ingredient(phrase, ingredients):
    """The one ingredient whose name the phrase contains (or that contains
    the phrase). Two candidates is no match — an 86 that says "chicken"
    must not zero both chicken thighs and chicken stock."""
    low = phrase.lower()
    hits = [i for i in ingredients if i["name"] and (i["name"].lower() in low or low in i["name"].lower())]
    return hits[0] if len(hits) == 1 else None


def get(restaurant_id, business_date, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM close_outs WHERE restaurant_id=? AND business_date=?",
                           (restaurant_id, str(business_date))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def latest(restaurant_id, before=None, db_path=DB_PATH):
    """The most recent close-out on or before `before` (default today)."""
    before = str(before or date.today())
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM close_outs WHERE restaurant_id=? AND business_date<=? "
                           "ORDER BY business_date DESC LIMIT 1",
                           (restaurant_id, before)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def summarise(entry) -> str:
    """One line for the morning brief, in the closer's own words."""
    if not entry:
        return ""
    who = entry.get("submitted_by") or "Last night"
    bits = []
    if entry.get("went_wrong"):
        bits.append(entry["went_wrong"])
    if entry.get("eighty_sixed"):
        bits.append(f"86'd: {entry['eighty_sixed']}")
    if entry.get("callouts"):
        bits.append(f"callouts: {entry['callouts']}")
    # What the opener walks into: a broken fryer or a leak is this
    # morning's problem, not just the report's.
    if entry.get("equipment"):
        bits.append(f"equipment: {entry['equipment']}")
    if entry.get("maintenance"):
        bits.append(f"maintenance: {entry['maintenance']}")
    if not bits and entry.get("went_well"):
        bits.append(entry["went_well"])
    return f"{who} at close — " + " · ".join(bits) if bits else ""
