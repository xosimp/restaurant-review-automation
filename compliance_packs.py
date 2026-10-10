"""
compliance_packs.py — jurisdiction rule packs for the schedule rules.

The rules dict (schedule_rules.DEFAULTS) had the right knobs — daily
overtime, meal break, minors, notice — and nothing to set them to. A pack
is a set of starting values for one jurisdiction, applied UNDER the
owner's own settings: anything the owner has typed wins, and everything a
pack sets is shown with a note saying where it came from and that it
should be checked with counsel for the restaurant's exact situation.

These are widely published baseline figures, not legal advice, and they
are deliberately conservative where rules vary by employer size or city.
"""

PACKS = {
    "CA": {
        "label": "California",
        "rules": {"daily_ot_hours": 8, "meal_break_after_hours": 5, "min_rest_hours": 10, "notice_days": None},
        # No California law requires rest between shifts: the 10h is a
        # starting value, the restaurant's policy (TIER_POLICY).
        "law": (),
        "notes": ["Daily overtime after 8 hours and double time after 12 (the engine flags daily OT; double time is not modelled).",
                  "A meal period is due before the end of the fifth hour.",
                  "A split shift may owe a split-shift premium — this engine does not compute it.",
                  "Minors: school-day limits differ from the generic minor rule; check the work permit."],
    },
    "NY": {
        "label": "New York",
        "rules": {"meal_break_after_hours": 6, "min_rest_hours": 10},
        # New York City's Fair Workweek (fast food): a clopen needs the
        # worker's written consent and a premium.
        "law": ("min_rest_hours",),
        "notes": ["A meal period is due on a shift over six hours that spans the midday meal.",
                  "Spread-of-hours pay may apply to a day spanning more than ten hours — not computed here.",
                  "New York City fast-food employers: predictive scheduling with 14 days' notice (set notice days if that is you — Cavnar AI then holds a week that would reach staff with less notice)."],
    },
    "OR": {
        "label": "Oregon",
        "rules": {"notice_days": 14, "min_rest_hours": 10, "meal_break_after_hours": 6},
        "law": ("min_rest_hours",),
        "notes": ["Predictive scheduling (large retail, hospitality and food service employers): 14 days' notice and a 10-hour rest between shifts unless the employee consents.",
                  "Meal and rest periods per the state rule."],
    },
    "WA": {
        "label": "Washington",
        "rules": {"meal_break_after_hours": 5, "min_rest_hours": 10},
        "law": ("min_rest_hours",),
        "notes": ["Seattle secure scheduling: 14 days' notice and a 10-hour rest for covered employers (set notice days if that is you — Cavnar AI then holds a week that would reach staff with less notice).",
                  "A meal period is due on a shift over five hours."],
    },
    "IL": {
        "label": "Illinois",
        # One Day Rest in Seven Act: at least 24 hours off in every calendar
        # week (six days in a row at most), and Chicago's Fair Workweek
        # rest between shifts.
        "rules": {"meal_break_after_hours": 7.5, "min_rest_hours": 10, "max_consecutive_days": 6},
        "law": ("min_rest_hours", "max_consecutive_days"),
        "notes": ["Chicago Fair Workweek: 14 days' notice for covered employers (set notice days if that is you — Cavnar AI then holds a week that would reach staff with less notice).",
                  "The 10-hour rest between shifts is the Chicago ordinance's starting value, not an Illinois state rule.",
                  "Predictability pay for late changes is not modelled — Cavnar AI warns when a published week changes inside the notice window.",
                  "One day of rest in seven applies to most employees."],
    },
    "MA": {
        "label": "Massachusetts",
        "rules": {"meal_break_after_hours": 6},
        "law": (),
        "notes": ["A 30-minute meal break on a shift over six hours."],
    },
}


# Which way is stricter for a value a pack makes law: more rest, fewer days
# in a row.
LAW_DIRECTION = {"min_rest_hours": 1, "max_consecutive_days": -1}


def available() -> list:
    return [{"code": k, "label": v["label"]} for k, v in sorted(PACKS.items(), key=lambda kv: kv[1]["label"])]


def apply(compliance: dict, jurisdiction: str, owner_set: dict = None) -> dict:
    """Merge a pack under the owner's own values. Returns the merged rules
    with `_pack` describing what came from the pack."""
    code = (jurisdiction or "").strip().upper()
    pack = PACKS.get(code)
    merged = dict(compliance or {})
    if not pack:
        merged.pop("_pack", None)
        return merged
    owner_set = owner_set or {}
    applied = {}
    for k, v in pack["rules"].items():
        if k in owner_set and owner_set[k] not in (None, ""):
            continue
        if v is not None:
            merged[k] = v
            applied[k] = v
    # Which values in force are the jurisdiction's law (schedule_rules.
    # law_backed, schedule audit 10/10/26 LOGIC-1): the pack's own, or a
    # value the owner typed that is at least as strict. A law outranks the
    # manager rule; the restaurant's own policy limits rank below it.
    law = []
    for k in pack.get("law") or ():
        v, mine = pack["rules"].get(k), merged.get(k)
        if v is None or mine in (None, ""):
            continue
        try:
            strict = float(mine) * LAW_DIRECTION.get(k, 1) >= float(v) * LAW_DIRECTION.get(k, 1)
        except (TypeError, ValueError):
            strict = False
        if strict:
            law.append(k)
    # "starting values", never "rules that apply": a pack is a place to
    # start, checked with counsel for the restaurant's own situation (NS5 L13).
    merged["_pack"] = {"code": code, "label": pack["label"], "applied": applied, "notes": list(pack["notes"]),
                       "law": law,
                       "wording": f"Starting values from the {pack['label']} pack — check with counsel."}
    return merged
