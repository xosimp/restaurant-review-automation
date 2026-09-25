"""Restaurant types and the owner-confirmed restaurant profile — what a
restaurant IS, which decides who it may be compared with.

Two layers (Benchmarking audit 9/24/26, #7, #8, #20, BM2-1/2/11):

* The PROFILE the owner confirms in Account (web + iOS) or an admin sets:
  service model (counter / full-service / bar-led / daytime), concept (a
  TAXONOMY value), bar-led, ownership (independent / franchise /
  corporate) and the year it opened, stamped `profile_source='set'` and
  `profile_confirmed_at`. Only a confirmed profile builds a peer
  partition (`partition_key`) — the hard split every peer band is read
  from.
* An INFERENCE from the name, vibe and menu notes (and the restaurant's own
  Google types and price level, which competitor.py now keeps). It checks
  FORMAT before cuisine — a tavern whose menu mentions flatbread pizza is a
  bar, not a pizzeria — reads the name before the menu text, and carries a
  confidence. It only ever pre-fills the "we think you're X — is that
  right?" prompt: a guessed type never joins a peer group, never counts
  toward a floor, and never produces a dollar figure from a published
  source.

`category_for` keeps its (category, source) shape for every existing
caller: the confirmed concept first, then the admin/owner `category`, then
the guess labelled 'inferred'.
"""
import json
import re
from types import SimpleNamespace

TAXONOMY = (
    "sports_bar", "bar", "fine_dining", "steakhouse", "pizza", "italian", "mexican",
    "breakfast", "coffee_shop", "fast_casual", "cafe", "bakery", "seafood", "sushi",
    "asian", "bbq", "burgers", "brewery", "wine_bar", "family", "other",
)

LABELS = {
    "sports_bar": "Sports bars", "bar": "Bars", "fine_dining": "Fine dining", "steakhouse": "Steakhouses",
    "pizza": "Pizza", "italian": "Italian", "mexican": "Mexican", "breakfast": "Breakfast & brunch",
    "coffee_shop": "Coffee shops", "fast_casual": "Fast casual", "cafe": "Cafés", "bakery": "Bakeries",
    "seafood": "Seafood", "sushi": "Sushi", "asian": "Asian", "bbq": "BBQ", "burgers": "Burgers",
    "brewery": "Breweries", "wine_bar": "Wine bars", "family": "Family restaurants", "other": "Other",
}

# A single restaurant, for the "we think you're X" prompt.
SINGULAR = {
    "sports_bar": "a sports bar", "bar": "a bar", "fine_dining": "a fine-dining restaurant",
    "steakhouse": "a steakhouse", "pizza": "a pizzeria", "italian": "an Italian restaurant",
    "mexican": "a Mexican restaurant", "breakfast": "a breakfast and brunch spot", "coffee_shop": "a coffee shop",
    "fast_casual": "a fast-casual restaurant", "cafe": "a café", "bakery": "a bakery", "seafood": "a seafood restaurant",
    "sushi": "a sushi restaurant", "asian": "an Asian restaurant", "bbq": "a BBQ restaurant", "burgers": "a burger spot",
    "brewery": "a brewery", "wine_bar": "a wine bar", "family": "a family restaurant", "other": "a restaurant",
}

# ── the profile's closed vocabularies ─────────────────────────────────────
SERVICE_MODELS = ("counter", "full_service", "bar_led", "daytime")
SERVICE_MODEL_LABELS = {"counter": "Counter service", "full_service": "Full service", "bar_led": "Bar-led",
                        "daytime": "Daytime (café, bakery, breakfast)"}
_SERVICE_MODEL_PEERS = {"counter": "counter-service restaurants", "full_service": "full-service restaurants",
                        "bar_led": "bar-led restaurants", "daytime": "daytime restaurants (café, bakery, breakfast)"}
_SERVICE_MODEL_SINGULAR = {"counter": "a counter-service restaurant", "full_service": "a full-service restaurant",
                           "bar_led": "a bar-led restaurant", "daytime": "a daytime café, bakery or breakfast spot"}
OWNERSHIP = ("independent", "franchise", "corporate")
OWNERSHIP_LABELS = {"independent": "Independent", "franchise": "Franchise", "corporate": "Corporate / chain-owned"}

# Menu family (food cost and waste only): what the plate is built on moves
# food cost % a lot — "steak-heavy menus run higher, pizza far lower".
MENU_FAMILY = {
    "steakhouse": "protein", "seafood": "protein", "sushi": "protein", "bbq": "protein", "burgers": "protein",
    "fine_dining": "protein",
    "pizza": "starch", "italian": "starch", "bakery": "starch", "breakfast": "starch", "mexican": "starch",
    "coffee_shop": "starch", "cafe": "starch",
}
MENU_FAMILY_LABELS = {"protein": "a protein-led menu", "starch": "a starch-led menu", "mixed": "a mixed menu"}

# Service model a concept implies, where it is unambiguous — only ever a
# suggestion for the confirmation prompt, never a partition by itself.
_CONCEPT_SERVICE = {
    "sports_bar": "bar_led", "bar": "bar_led", "brewery": "bar_led", "wine_bar": "bar_led",
    "fine_dining": "full_service", "steakhouse": "full_service", "italian": "full_service", "seafood": "full_service",
    "family": "full_service",
    "coffee_shop": "daytime", "cafe": "daytime", "bakery": "daytime", "breakfast": "daytime",
    "fast_casual": "counter",
}

# ── inference: FORMAT first, then cuisine (BM2-2) ─────────────────────────
# A format word decides the kind of business; a dish word only the menu.
_FORMAT_RULES = (
    ("sports_bar", r"sports? bar|sports grill|game day"),
    ("brewery", r"brew(ery|ing|pub)|taproom"),
    ("wine_bar", r"wine bar|enoteca"),
    ("bar", r"\bbar\b|\bpub\b|tavern|saloon|lounge|cocktail|\bbar ?& ?grill\b"),
    ("fine_dining", r"fine dining|tasting menu|michelin|prix fixe|chef's table"),
    ("steakhouse", r"steak ?house|chop ?house|prime rib"),
    ("coffee_shop", r"coffee|espresso|roaster|cafe ?bar"),
    ("bakery", r"bakery|patisserie|boulangerie|donut|doughnut"),
    ("fast_casual", r"fast casual|counter service|quick service|order at the counter|drive.?thru"),
)
_CUISINE_RULES = (
    ("sushi", r"sushi|omakase|izakaya|ramen"),
    ("bbq", r"\bbbq\b|barbecue|smokehouse|brisket"),
    ("mexican", r"mexican|taqueria|tacos?\b|cantina|burrito"),
    ("pizza", r"pizz(a|eria)|neapolitan"),
    ("italian", r"italian|trattoria|osteria|ristorante|pasta"),
    ("seafood", r"seafood|oyster|fish house|crab|lobster"),
    ("breakfast", r"breakfast|brunch|pancake|diner|waffle"),
    ("burgers", r"burger"),
    ("asian", r"thai|chinese|vietnamese|pho\b|korean|indian|dim sum|noodle"),
    ("fast_casual", r"\bbowls?\b|grab ?(and|&|n) ?go"),
    ("family", r"family|grill|kitchen|eatery|bistro"),
)
_RULES = _FORMAT_RULES + _CUISINE_RULES

# Google's own types for the listing (competitor.py keeps them now).
_GOOGLE_SERVICE = (("bar", "bar_led"), ("night_club", "bar_led"), ("cafe", "daytime"), ("bakery", "daytime"),
                   ("meal_takeaway", "counter"), ("meal_delivery", "counter"))


def _text(restaurant, fields):
    return " ".join(str(getattr(restaurant, k, "") or "") for k in fields).lower()


def _first(text, rules):
    for cat, rx in rules:
        if re.search(rx, text):
            return cat
    return None


def infer(restaurant) -> str | None:
    """The concept Cavnar would guess: format words before cuisine words,
    and the name (with the vibe) before the menu text."""
    return (infer_detail(restaurant) or {}).get("concept")


def google_types(restaurant) -> list:
    raw = getattr(restaurant, "google_types", None)
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [str(t) for t in v] if isinstance(v, (list, tuple)) else []
    except Exception:
        return []


def infer_detail(restaurant) -> dict | None:
    """{concept, service_model, confidence (0..1), cues[]} — what Cavnar
    would guess, and how sure it is: one cue alone is a weak guess; a name
    cue that agrees with Google's own listing is a strong one; cues that
    disagree lower it. None when nothing matched."""
    if restaurant is None:
        return None
    name = _text(restaurant, ("name", "vibe"))
    menu = _text(restaurant, ("menu_notes", "brand_voice"))
    cues = []
    concept = None
    for label, text in (("name", name), ("menu", menu)):
        if not text.strip():
            continue
        c = _first(text, _FORMAT_RULES) or _first(text, _CUISINE_RULES)
        if c:
            cues.append(f"{label}: {LABELS[c].lower()}")
            if concept is None:
                concept = c
    gtypes = google_types(restaurant)
    g_service = next((sm for t, sm in _GOOGLE_SERVICE if t in gtypes), None)
    price = getattr(restaurant, "google_price_level", None)
    service = _CONCEPT_SERVICE.get(concept or "")
    conf = 0.0
    if concept:
        conf = 0.45 if cues and cues[0].startswith("name") else 0.3
        if len(cues) > 1 and all(c.split(": ")[1] == LABELS[concept].lower() for c in cues):
            conf += 0.15
    if g_service:
        cues.append(f"Google listing: {SERVICE_MODEL_LABELS[g_service].lower()}")
        if service is None:
            service = g_service
            conf = max(conf, 0.35)
        elif service == g_service:
            conf += 0.3
        else:
            conf -= 0.2
    if price is not None:
        try:
            p = int(price)
            if p >= 3 and service in (None, "full_service"):
                cues.append("Google price level: upscale")
                service = service or "full_service"
                conf += 0.1 if service == "full_service" else 0.0
            elif p <= 1 and service == "full_service":
                cues.append("Google price level: inexpensive")
                conf -= 0.15
        except (TypeError, ValueError):
            pass
    if not concept and not service:
        return None
    return {"concept": concept, "service_model": service, "confidence": round(max(0.05, min(0.9, conf)), 2),
            "cues": cues}


def _explicit(v, vocab):
    v = (v or "").strip().lower() if isinstance(v, str) else v
    return v if v in vocab else None


def category_for(restaurant):
    """(category, source) — source is 'set' | 'inferred' | None. The
    confirmed profile's concept first, then the owner's or admin's
    `category`, then the guess."""
    if getattr(restaurant, "profile_source", None) == "set":
        concept = _explicit(getattr(restaurant, "concept", None), TAXONOMY)
        if concept:
            return concept, "set"
    explicit = _explicit(getattr(restaurant, "category", None), TAXONOMY)
    if explicit:
        return explicit, "set"
    guess = infer(restaurant)
    return (guess, "inferred") if guess else (None, None)


def label(category) -> str:
    """A type's label. No type means the comparison is platform-wide, and
    it is named that way — never "Restaurants like yours" (NS4 H4)."""
    return LABELS.get(category or "", "All restaurants on Cavnar")


def valid(category) -> bool:
    return (category or "").strip().lower() in TAXONOMY


# ── the profile ───────────────────────────────────────────────────────────

def _as_bool(v):
    if v in (None, ""):
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def menu_family(concept) -> str:
    return MENU_FAMILY.get(concept or "", "mixed")


def profile_for(restaurant) -> dict:
    """The restaurant's profile as the engine reads it:
    {service_model, concept, bar_led, ownership, opened_year, source,
     confirmed, confirmed_at, menu_family, guess}. `confirmed` is True only
    for an owner/admin-set service model — the one thing a peer partition
    may be built from. `guess` is infer_detail() for the prompt."""
    if isinstance(restaurant, dict):
        restaurant = SimpleNamespace(**restaurant)
    src = getattr(restaurant, "profile_source", None)
    sm = _explicit(getattr(restaurant, "service_model", None), SERVICE_MODELS)
    concept = _explicit(getattr(restaurant, "concept", None), TAXONOMY)
    if concept is None:
        cat, csrc = category_for(restaurant)
        concept = cat if csrc == "set" else None
    bar = _as_bool(getattr(restaurant, "bar_led", None))
    if sm == "bar_led":
        bar = True
    try:
        opened = int(getattr(restaurant, "opened_year", None)) if getattr(restaurant, "opened_year", None) else None
    except (TypeError, ValueError):
        opened = None
    confirmed = src == "set" and sm is not None
    guess = infer_detail(restaurant)
    return {"service_model": sm, "concept": concept, "bar_led": bool(bar) if bar is not None else None,
            "ownership": _explicit(getattr(restaurant, "ownership", None), OWNERSHIP), "opened_year": opened,
            "source": src if src in ("set", "inferred") else None, "confirmed": confirmed,
            "confirmed_at": getattr(restaurant, "profile_confirmed_at", None),
            "menu_family": menu_family(concept) if concept else None, "guess": guess}


# Which hard split a metric family needs (BM2 §4, "Recommended peer
# definition"): service model for every format metric; × bar-led for labor
# and food cost; × menu family for food cost and waste.
FAMILIES = ("format", "labor", "food")


def partition_key(profile, family="format", volume_band=None) -> str | None:
    """The peer partition a CONFIRMED profile belongs to for one metric
    family, or None (unconfirmed, or food with no concept to read a menu
    family from). `volume_band` narrows a staffing ratio to restaurants of
    the same sales band once one is measured."""
    if not profile or not profile.get("confirmed") or not profile.get("service_model"):
        return None
    key = f"sm:{profile['service_model']}"
    if family in ("labor", "food", "staff"):
        if profile.get("bar_led") and profile["service_model"] != "bar_led":
            key += "|bar"
    if family == "food":
        if not profile.get("concept"):
            return None
        key += f"|{profile.get('menu_family') or menu_family(profile['concept'])}"
    if family == "staff" and volume_band is not None:
        key += f"|v{int(volume_band)}"
    return key


def partition_label(key) -> str:
    """"Full-service, bar-led restaurants with a protein-led menu on
    Cavnar" — the group a partition band was read from, in words."""
    parts = str(key or "")[3:].split("|")
    sm = parts[0]
    base = _SERVICE_MODEL_PEERS.get(sm, "restaurants")
    extras = []
    if "bar" in parts[1:]:
        base = base.replace(" restaurants", ", bar-led restaurants") if sm != "bar_led" else base
    fam = next((p for p in parts[1:] if p in MENU_FAMILY_LABELS), None)
    if fam:
        extras.append(f"with {MENU_FAMILY_LABELS[fam]}")
    vb = next((p for p in parts[1:] if p.startswith("v") and p[1:].isdigit()), None)
    if vb:
        extras.append("of similar sales volume")
    s = base[0].upper() + base[1:]
    return " ".join([s] + extras) + " on Cavnar"


def is_partition(key) -> bool:
    return str(key or "").startswith("sm:")


def suggestion(restaurant) -> dict | None:
    """The "we think you're X — is that right?" prompt for an unconfirmed
    profile: {text, service_model, concept, confidence_pct, cues}, or None
    when the profile is confirmed or nothing can be guessed."""
    p = profile_for(restaurant)
    if p["confirmed"]:
        return None
    g = p.get("guess") or {}
    concept, sm = g.get("concept"), g.get("service_model")
    if not concept and not sm:
        return None
    what = SINGULAR.get(concept) if concept else _SERVICE_MODEL_SINGULAR.get(sm)
    return {"text": f"We think you're {what} — is that right?", "service_model": sm, "concept": concept,
            "confidence_pct": int(round((g.get("confidence") or 0) * 100)), "cues": g.get("cues") or []}


def clean_profile(data) -> tuple[dict, str | None]:
    """(updates, error) for a profile save from Account or admin — closed
    vocabularies only. A save IS the owner's confirmation: it stamps
    profile_source='set' and profile_confirmed_at."""
    data = data or {}
    out = {}
    sm = (data.get("service_model") or "").strip().lower()
    if sm not in SERVICE_MODELS:
        return {}, "Choose how the restaurant serves: counter, full service, bar-led or daytime."
    out["service_model"] = sm
    if "concept" in data:
        c = (data.get("concept") or "").strip().lower()
        if c and c not in TAXONOMY:
            return {}, "Unknown concept."
        out["concept"] = c or None
    if "bar_led" in data:
        b = _as_bool(data.get("bar_led"))
        out["bar_led"] = None if b is None else int(b)
    if sm == "bar_led":
        out["bar_led"] = 1
    if "ownership" in data:
        o = (data.get("ownership") or "").strip().lower()
        if o and o not in OWNERSHIP:
            return {}, "Ownership is independent, franchise or corporate."
        out["ownership"] = o or None
    if "opened_year" in data:
        raw = data.get("opened_year")
        if raw in (None, ""):
            out["opened_year"] = None
        else:
            try:
                y = int(raw)
            except (TypeError, ValueError):
                return {}, "The year it opened is a four-digit year."
            from datetime import date
            if y < 1800 or y > date.today().year:
                return {}, "The year it opened is a four-digit year, not in the future."
            out["opened_year"] = y
    out["profile_source"] = "set"
    from datetime import datetime
    out["profile_confirmed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    return out, None


def profile_payload(restaurant) -> dict:
    """The Account block's read: the profile, its choices and — while it is
    unconfirmed — the suggestion. Owner-facing, so it carries no other
    restaurant's anything."""
    p = profile_for(restaurant)
    return {"service_model": p["service_model"], "concept": p["concept"], "bar_led": p["bar_led"],
            "ownership": p["ownership"], "opened_year": p["opened_year"], "confirmed": p["confirmed"],
            "confirmed_at": p["confirmed_at"], "source": p["source"],
            "suggestion": suggestion(restaurant),
            "choices": {"service_model": [{"value": k, "label": SERVICE_MODEL_LABELS[k]} for k in SERVICE_MODELS],
                        "concept": [{"value": k, "label": LABELS[k]} for k in TAXONOMY],
                        "ownership": [{"value": k, "label": OWNERSHIP_LABELS[k]} for k in OWNERSHIP]}}
