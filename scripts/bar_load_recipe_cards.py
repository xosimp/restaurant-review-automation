"""scripts/bar_load_recipe_cards.py — load a bar's recipe cards as SUGGESTED
cocktail specs (bar.variance.load_recipe_cards, barfix-1015 W6b F2). Run by
hand on production (railway ssh), dry run first:

    python3 scripts/bar_load_recipe_cards.py --rid 5            # dry run: lists what it would suggest
    python3 scripts/bar_load_recipe_cards.py --rid 5 --apply    # writes the suggestions

Each alcoholic ingredient measured in ounces becomes a `bar_drink_specs`
row with status `suggested`, on the POS button the card names, against the
bought product its name matches best (or none). Nothing counts in bought vs
rung until the account holder confirms it. Idempotent: a row the account
holder confirmed or changed is never touched. Defaults to Simple EJ's cards
(docs/clients/simple-ejs/bar_recipes.json); `--path` for another bar's.

Sends nothing, calls no model, reads no provider. Prints JSON.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rid", type=int, required=True)
    ap.add_argument("--path", default=None, help="the recipe-card JSON (default: Simple EJ's)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a = ap.parse_args(argv)
    import bar.variance as bv
    out = bv.load_recipe_cards(a.rid, path=a.path, apply=a.apply)
    out["applied"] = bool(a.apply)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
