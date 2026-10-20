"""dsr/settings.py — the Daily Sales Report's settings, validated once.

The owner's settings route (strategy_routes._do_dsr_settings_set) and the
admin console's DSR calendar action (client_setup.apply_dsr_settings; scale
audit 10/19/26 ONB-7: the per-client DSR config was loaded on a production
shell) take the same body and refuse the same things with the same
sentences. validate(body, r) -> (fields for update_restaurant, None) or
(None, the sentence to show).
"""
import json
from datetime import date

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _dsr_day(raw):
    try:
        return date.fromisoformat(str(raw or "")[:10]) if len(str(raw or "")) == 10 else None
    except ValueError:
        return None


def validate(body, r):
    """Any of fiscal_week_start_dow (0=Mon … 6=Sun, or null), fiscal_year_start
    (YYYY-MM-DD, or "" to clear), fiscal_period_scheme ("4x13" | "445" | "454" |
    "544" | the period lengths, comma-separated), fiscal_years (the years that
    differ, [{start, lengths}], or [] / null to clear), dsr_enabled, dsr_notify,
    dsr_gross_basis ("items" | "all"), dsr_deadline_hour (0–11, local),
    dsr_late_night_hour (18–23, or null), dsr_budget_mode ("day" | "week").
    `r` is the restaurant as it stands (the week checks read it)."""
    from dsr import fiscal
    from time_utils import mdy
    if not isinstance(body, dict):
        return None, "Send the settings as a JSON object."
    fields = {}
    if "fiscal_week_start_dow" in body:
        v = body.get("fiscal_week_start_dow")
        if v is None or v == "":
            fields["fiscal_week_start_dow"] = None
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = -1
            if isinstance(body.get("fiscal_week_start_dow"), bool) or not 0 <= v <= 6:
                return None, "Pick a day for your week to start."
            fields["fiscal_week_start_dow"] = v
    if "fiscal_year_start" in body:
        raw = body.get("fiscal_year_start")
        if raw in (None, ""):
            fields["fiscal_year_start"] = None
        else:
            d = _dsr_day(raw)
            if d is None:
                return None, "The year start must be a date."
            fields["fiscal_year_start"] = d.isoformat()
    if "fiscal_period_scheme" in body:
        raw = body.get("fiscal_period_scheme")
        if isinstance(raw, list) and all(isinstance(n, int) and not isinstance(n, bool) for n in raw):
            raw = ",".join(str(n) for n in raw)
        scheme = raw.replace(" ", "") if isinstance(raw, str) else ""
        if not fiscal.valid_scheme(scheme):
            return None, ("Pick 13 four-week periods, 4-4-5, 4-5-4 or 5-4-4, or list your "
                          "periods' lengths in weeks (12 or 13 of 4 or 5, totalling 52 or 53).")
        fields["fiscal_period_scheme"] = scheme
    if "fiscal_years" in body:
        raw = body.get("fiscal_years")
        if raw is not None and not isinstance(raw, list):
            return None, "List each year as a start date and its period lengths."
        rows, err = fiscal.parse_years(raw)
        if err:
            return None, err
        fields["fiscal_years_json"] = (json.dumps([{"start": s.isoformat(), "lengths": n} for s, n in rows])
                                       if rows else None)
    if "dsr_enabled" in body:
        fields["dsr_enabled"] = 1 if body.get("dsr_enabled") else 0
    if "dsr_notify" in body:
        fields["dsr_notify"] = 1 if body.get("dsr_notify") else 0
    if "dsr_gross_basis" in body:
        from dsr.block_sales import GROSS_BASES
        # A list or an object is unhashable: `in GROSS_BASES` raised and the
        # route answered 500 (re-audit D16). Only a known string is a basis.
        basis = body.get("dsr_gross_basis")
        if not isinstance(basis, str) or basis not in GROSS_BASES:
            return None, "Gross is either items only or everything rung (items, tax and voids)."
        fields["dsr_gross_basis"] = basis
    if "dsr_deadline_hour" in body:
        raw = body.get("dsr_deadline_hour")
        try:
            h = -1 if isinstance(raw, bool) else int(raw)
        except (TypeError, ValueError):
            h = -1
        if not 0 <= h <= 11:
            return None, "Pick an hour between midnight and 11am."
        fields["dsr_deadline_hour"] = h
    if "dsr_late_night_hour" in body:
        # Checks opened from this hour on are "Late night" (dsr.block_service);
        # null keeps the POS's own Lunch/Dinner tags.
        raw = body.get("dsr_late_night_hour")
        if raw is None or raw == "":
            fields["dsr_late_night_hour"] = None
        else:
            try:
                h = -1 if isinstance(raw, bool) else int(raw)
            except (TypeError, ValueError):
                h = -1
            if not 18 <= h <= 23:
                return None, "Late night starts between 6pm and 11pm."
            fields["dsr_late_night_hour"] = h
    if "dsr_budget_mode" in body:
        # How the owner budgets (10/9/26): a figure per night, or one for
        # the week split by the restaurant's own weekday mix.
        if body.get("dsr_budget_mode") not in ("day", "week"):
            return None, "Budget by day or by week."
        fields["dsr_budget_mode"] = body["dsr_budget_mode"]
    if not fields:
        return None, "Nothing to change."
    # Periods are whole weeks: a year start that isn't the week's first day
    # would split a week across two periods.
    dow = fields["fiscal_week_start_dow"] if "fiscal_week_start_dow" in fields else getattr(r, "fiscal_week_start_dow", None)
    ys = fields["fiscal_year_start"] if "fiscal_year_start" in fields else getattr(r, "fiscal_year_start", None)
    want = 0 if dow is None else int(dow)
    if ys:
        if date.fromisoformat(str(ys)[:10]).weekday() != want:
            return None, f"Period 1 has to start on a {_WEEKDAYS[want]}, the first day of your week."
    listed = (fiscal.parse_years(fields["fiscal_years_json"])[0] if "fiscal_years_json" in fields
              else fiscal.listed_years(r))
    for start, _lengths in listed:
        if start.weekday() != want:
            return None, (f"The year starting {mdy(start)} has to start on a "
                          f"{_WEEKDAYS[want]}, the first day of your week.")
    return fields, None
