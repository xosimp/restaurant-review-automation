"""Restaurant types — the cohorts a restaurant is compared against.

The owner's or admin's own choice (`restaurants.category`) wins. When it
is unset the category is inferred from the restaurant's name, vibe and
menu notes and labelled `inferred`, so a surface can say "we think you're
a pizzeria" rather than assert it. An inference that finds nothing is
`None`, and the restaurant compares only platform-wide.
"""
import re

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

# Keyword → category, checked in order (first hit wins); each entry is a
# regex over the lower-cased name + vibe + menu notes.
_RULES = (
    ("sports_bar", r"sports? bar|sports grill|taproom & grill|game day"),
    ("steakhouse", r"steak ?house|chop ?house|prime rib"),
    ("fine_dining", r"fine dining|tasting menu|michelin|prix fixe|chef's table"),
    ("pizza", r"pizz(a|eria)|wood.?fired|neapolitan"),
    ("sushi", r"sushi|omakase|izakaya|ramen"),
    ("mexican", r"mexican|taqueria|tacos?|cantina|burrito"),
    ("italian", r"italian|trattoria|osteria|ristorante|pasta"),
    ("bbq", r"\bbbq\b|barbecue|smokehouse|brisket"),
    ("breakfast", r"breakfast|brunch|pancake|diner|waffle"),
    ("coffee_shop", r"coffee|espresso|roaster|cafe ?bar"),
    ("bakery", r"bakery|patisserie|boulangerie|donut|doughnut"),
    ("seafood", r"seafood|oyster|fish house|crab|lobster"),
    ("brewery", r"brew(ery|ing|pub)|taproom"),
    ("wine_bar", r"wine bar|enoteca"),
    ("burgers", r"burger"),
    ("bar", r"\bbar\b|pub\b|tavern|saloon|lounge|cocktail"),
    ("asian", r"thai|chinese|vietnamese|pho\b|korean|indian|dim sum|noodle"),
    ("fast_casual", r"fast casual|counter service|bowls?|quick service|grab"),
    ("family", r"family|grill|kitchen|eatery|bistro"),
)


def infer(restaurant) -> str | None:
    text = " ".join(str(getattr(restaurant, k, "") or "") for k in ("name", "vibe", "menu_notes", "brand_voice")).lower()
    if not text.strip():
        return None
    for cat, rx in _RULES:
        if re.search(rx, text):
            return cat
    return None


def category_for(restaurant):
    """(category, source) — source is 'set' | 'inferred' | None."""
    explicit = (getattr(restaurant, "category", None) or "").strip().lower()
    if explicit in TAXONOMY:
        return explicit, "set"
    guess = infer(restaurant)
    return (guess, "inferred") if guess else (None, None)


def label(category) -> str:
    return LABELS.get(category or "", "Restaurants like yours")


def valid(category) -> bool:
    return (category or "").strip().lower() in TAXONOMY
