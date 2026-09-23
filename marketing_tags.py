"""What a post is about — a dish, an occasion, a kind — so its result can
be read against the thing it promoted.

A post used to be a free-text `topic`. "Margherita for the Bears game"
could not be linked to the Margherita's own sales, or compared with other
game-day posts, because nothing knew it was either. Every content-log row
now carries `menu_item_id`, `occasion` and `post_kind`, inferred from the
topic and body against the restaurant's own menu and a small occasion
vocabulary, and correctable by the owner. Inference only ever names a
menu item the restaurant actually has.
"""
import re

from models import get_conn, DB_PATH

OCCASIONS = {
    "game_day": r"\b(game ?day|kick ?off|bears|packers|cubs|white sox|bulls|blackhawks|nfl|nba|mlb|nhl|playoff|super ?bowl|"
                r"world series|march madness|tailgate|watch party|big game|the game)\b(?!-)",
    "holiday": r"\b(thanksgiving|christmas|xmas|halloween|valentine|mother'?s day|father'?s day|new year|4th of july|"
               r"fourth of july|labor day|memorial day|st\.? ?patrick|easter|cinco de mayo|hanukkah|juneteenth)\b",
    "event": r"\b(live music|trivia|karaoke|brunch launch|patio (opening|season)|anniversary|grand opening|party|"
             r"tasting|wine dinner|pop.?up|dj|band|open mic)\b",
    "offer": r"\b(happy hour|special(s)?|% ?off|percent off|deal|bogo|two for one|2 for 1|free|discount|promo|"
             r"half.?price|\$\d+ )\b",
    "weekend": r"\b(weekend|friday|saturday|sunday|fri|sat|sun)\b",
    "weather": r"\b(patio|rainy|rain|snow|sunny|heat ?wave|cold|first warm|last warm|al fresco)\b",
}
_OCCASION_ORDER = ("game_day", "holiday", "event", "offer", "weather", "weekend")
KINDS = ("dish", "offer", "event", "general")
OCCASION_LABELS = {"game_day": "game day", "holiday": "holiday", "event": "event", "offer": "offer",
                   "weekend": "weekend", "weather": "weather"}


def _menu(restaurant_id, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, name FROM menu_items WHERE restaurant_id=? AND is_active=1 AND name IS NOT NULL",
                            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [(r["id"], r["name"]) for r in rows if (r["name"] or "").strip()]


def match_menu_item(text, menu):
    """The longest menu item name that appears whole in `text`, or None."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    best = None
    for mid, name in menu:
        n = re.sub(r"\s+", " ", name.lower().strip())
        if len(n) < 3:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t):
            if best is None or len(n) > len(best[1]):
                best = (mid, name)
    return best


# Menu words that contain an occasion word without being one (MOD-MKT-18):
# "gluten free" is not an offer, "cold brew" is not the weather. Taken out
# before the occasion patterns run; "game-changer" is handled by the
# game_day pattern refusing a trailing hyphen.
_NOT_OCCASIONS = re.compile(
    r"\b(gluten|dairy|nut|peanut|soy|egg|sugar|fat|lactose|grain|meat|cage|cruelty|caffeine|"
    r"alcohol|msg|carb|allergen|guilt|hassle|stress|hands|smoke|seed[- ]oil)[- ]free\b"
    r"|\bice[- ]cold\b"
    r"|\bcold[- ](brew(ed|s)?|beers?|drinks?|cuts?|press(ed)?|foam|plates?|noodles|sandwich(es)?|"
    r"smoked|soba|sesame|tea|coffee|pints?|ones?)\b")


def occasion_of(text):
    t = _NOT_OCCASIONS.sub(" ", (text or "").lower())
    for occ in _OCCASION_ORDER:
        if re.search(OCCASIONS[occ], t):
            return occ
    return None


def infer(restaurant_id, topic, body=None, db_path=DB_PATH) -> dict:
    text = f"{topic or ''} {body or ''}"
    item = match_menu_item(text, _menu(restaurant_id, db_path))
    occ = occasion_of(text)
    kind = "dish" if item else ("offer" if occ == "offer" else ("event" if occ in ("event", "holiday", "game_day") else "general"))
    return {"menu_item_id": item[0] if item else None, "menu_item_name": item[1] if item else None,
            "occasion": occ, "post_kind": kind}


def tag_row(content_log_id, restaurant_id, topic=None, body=None, overrides: dict = None, clear_item=False,
            db_path=DB_PATH) -> dict:
    """Write tags onto one content-log row. `overrides` (from the owner or
    the caller) win over inference; a None override falls through, except
    `clear_item`, which is the owner saying "this is not about a dish"."""
    tags = infer(restaurant_id, topic, body, db_path=db_path)
    for k in ("menu_item_id", "occasion", "post_kind"):
        if overrides and overrides.get(k) is not None:
            tags[k] = overrides[k]
    if clear_item:
        tags["menu_item_id"], tags["menu_item_name"] = None, None
        if not (overrides or {}).get("post_kind"):
            tags["post_kind"] = "offer" if tags["occasion"] == "offer" else ("event" if tags["occasion"] in ("event", "holiday", "game_day") else "general")
    if tags.get("menu_item_id"):
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT name FROM menu_items WHERE id=? AND restaurant_id=?",
                               (tags["menu_item_id"], restaurant_id)).fetchone()
        finally:
            conn.close()
        if not row:          # an override naming another restaurant's item, or none
            tags["menu_item_id"], tags["menu_item_name"] = None, None
        else:
            tags["menu_item_name"] = row["name"]
            if tags["post_kind"] == "general":
                tags["post_kind"] = "dish"
    if tags["post_kind"] not in KINDS:
        tags["post_kind"] = "general"
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE marketing_content_log SET menu_item_id=?, occasion=?, post_kind=? WHERE id=? AND restaurant_id=?",
                     (tags["menu_item_id"], tags["occasion"], tags["post_kind"], content_log_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return tags


def backfill(restaurant_id, limit=200, db_path=DB_PATH) -> int:
    """Tag rows written before tags existed. Idempotent: only rows with no
    post_kind. Called lazily by the attribution summary."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, topic FROM marketing_content_log WHERE restaurant_id=? AND post_kind IS NULL "
                            "ORDER BY id DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
    finally:
        conn.close()
    for r in rows:
        tag_row(r["id"], restaurant_id, r["topic"], db_path=db_path)
    return len(rows)


def label(tags: dict) -> str:
    bits = []
    if tags.get("menu_item_name"):
        bits.append(tags["menu_item_name"])
    if tags.get("occasion"):
        bits.append(OCCASION_LABELS.get(tags["occasion"], tags["occasion"]))
    if not bits and tags.get("post_kind"):
        bits.append(tags["post_kind"])
    return " · ".join(bits)
