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
    "influence": "Influence — what was going on (a Cubs or Bears game, the World Cup, an event)",
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
    # Only what this filing CHANGED is acted on. Every re-save sends all ten
    # fields, so fixing a typo in the shift notes re-zeroed the 86'd item
    # over the night's receiving and re-opened its miss (F2-8). An 86 acts
    # on the items it newly names; a callout on a changed line.
    acts = {}
    if "eighty_sixed" in values:
        before = {p.lower() for p in _phrases(existing.get("eighty_sixed") or "")}
        new_items = [p for p in _phrases(values.get("eighty_sixed") or "") if p.lower() not in before]
        if new_items:
            acts["eighty_sixed"] = ", ".join(new_items)
    if "callouts" in values and (values.get("callouts") or None) != (existing.get("callouts") or None):
        acts["callouts"] = values.get("callouts")
    try:
        _act_on(restaurant_id, day, acts, restaurant, db_path=db_path)
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
                # It ran out in service: the gap between what the ledger
                # expected and zero is usage the recipes missed, not waste.
                # Recorded as waste it inflated inferred waste on every 86
                # (verified 9/24/26 — record_recount wrote the full gap as
                # an 'inferred' waste event; CA1 F20, fix I8).
                inventory_ledger.record_recount(restaurant_id, hit["id"], 0.0, event_date=day,
                                                source="closeout", note=f"86'd at close: {phrase}",
                                                infer_waste=False)
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


_FILLER = {"86", "86d", "86ed", "eighty", "sixed", "out", "of", "the", "ran", "run", "we", "no", "all",
           "our", "for", "tonight", "today", "sold", "is", "are", "was", "were", "on", "at", "in", "and"}


def _words(text):
    """Whole words, lowercased, a plural's trailing s taken off ("eggs" and
    "egg" are one word; "eggplant" is another)."""
    import re
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _FILLER or len(w) < 2:
            continue
        if w.endswith("oes") and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def _match_ingredient(phrase, ingredients):
    """The one ingredient the phrase names, by WHOLE words: every word of
    the ingredient's name is in the phrase ("86 chicken breast"), or else
    every word of the phrase is in the name ("chicken" -> a lone "Chicken
    Thighs"). Two candidates is no match — an 86 that says "chicken" must
    not zero both chicken thighs and chicken stock. Substrings matched
    "egg" to Eggplant and "oil" to Boiled Peanuts (F2-8)."""
    said = _words(phrase)
    if not said:
        return None
    named = [(i, _words(i["name"])) for i in ingredients if i.get("name")]
    whole = [(i, w) for i, w in named if w and w <= said]
    if whole:
        # Several names fully said: the most specific one, if it is unique.
        top = max(len(w) for _i, w in whole)
        best = [i for i, w in whole if len(w) == top]
        return best[0] if len(best) == 1 else None
    part = [i for i, w in named if w and said <= w]
    return part[0] if len(part) == 1 else None


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


def suggestions(restaurant_id, business_date, db_path=DB_PATH) -> dict:
    """What the system already knows about tonight, offered as editable
    prefill for two lines (Friction audit #40): {"callouts": str|None,
    "influence": str|None, "sources": {field: sentence}}.

    callouts  — tonight's "hasn't clocked in" issues still open: the
                coverage check (intraday.coverage_gaps) opened one per person
                on the published schedule who never arrived. A late arrival
                closes theirs, so it is not suggested.
    influence — tonight's dated events and reservations (demand_signals) and
                the holiday, if tonight is one.

    A suggestion is never saved on its own: the closer sees it in the box and
    keeps, edits or clears it."""
    import staff_settings as _ss
    day = business_date if isinstance(business_date, str) else business_date.isoformat()
    out = {"callouts": None, "influence": None, "sources": {}}
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute("SELECT source_key FROM ops_issues WHERE restaurant_id=? AND kind='coverage' "
                                "AND status!='resolved' AND source_key LIKE ? ORDER BY id",
                                (restaurant_id, f"coverage:{day}:%")).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    keys = [r["source_key"].split(":", 2)[2] for r in rows if (r["source_key"] or "").count(":") >= 2]
    if keys:
        try:
            names = {_ss.name_key(e["name"]): e["name"]
                     for e in _ss.roster(restaurant_id, db_path=db_path, include_inactive=True)}
        except Exception:
            names = {}
        who = []
        for k in keys:
            n = names.get(k) or " ".join(w.capitalize() for w in k.split())
            if n not in who:
                who.append(n)
        out["callouts"] = ", ".join(who)
        out["sources"]["callouts"] = "Scheduled tonight and never clocked in"
    bits = []
    try:
        import demand_signals
        for s in demand_signals.upcoming(restaurant_id, day, day, db_path=db_path):
            label = (s.get("label") or "").strip()
            if label:
                bits.append(label + (f" ({s['covers']} covers booked)" if s.get("covers") else ""))
    except Exception:
        pass
    try:
        import demand
        from datetime import datetime
        for h in demand.upcoming_holidays(restaurant_id, now=datetime.fromisoformat(day), days=0,
                                          db_path=db_path) or []:
            if h.get("date") == day and h.get("name") and h["name"] not in bits:
                bits.append(h["name"])
    except Exception:
        pass
    if bits:
        out["influence"] = "; ".join(bits)
        out["sources"]["influence"] = "From tonight's events, reservations and holidays on file"
    return out
