"""
demand_drivers.py — what moves ONE daypart and ONE role on a date, measured
here: each date's own daypart sales, an event's lift per daypart and per
role, and the weather beyond rain — per daypart, by temperature band and
snow, and for the patio (scheduler audit 10/10/26 LOGIC-9, LEARNING-11,
LEARNING-20, LEARNING-21).

The schedule used to move every shift of a date by ONE ratio: a 7pm game
lifted the lunch crew, a rainy-lunch forecast cut dinner, a Cubs night (+22%,
mostly bar) added cooks and dishwashers the kitchen did not need, and only
rain was ever learned. Everything here is measured from this restaurant's
own nights, behind the same floors event_memory uses (EFFECT_MIN_N nights,
EFFECT_FLOOR_PCT), and says how many nights it rests on:

  date_part_sales()     {date: {"morning", "night", "source"}} — each date's
                        OWN split, from its tickets (the POS archive), else
                        its intraday captures, else its nightly report's
                        hourly split. One source per date, never two added.
                        The schedule's outcome record splits a day by it
                        (schedule_intel.record_outcomes, LEARNING-11).
  label_part_lifts()    a label's lift per daypart over its measured nights
                        against the same weekday's ordinary nights.
  role_event_lifts()    a label's lift per role: the people each role ran on
                        its nights against that role on ordinary same
                        weekdays, shrunk toward the night's sales lift (the
                        "every role alike" prior) by ROLE_SHRINK nights.
  weather_effects()     per daypart: rain, snow, cold and heat against a dry
                        mild same-weekday daypart, with an interval.
  patio_effects()       the patio room's own sales on wet / cold dayparts
                        against dry mild ones (pos_tickets room names).
  forecast_parts()      the NWS day and night periods already fetched
                        (weather.py) as each date's lunch and dinner
                        conditions — the forecast side of the above.

Reads only. Never raises from a public function (an empty answer means
"nothing measured": the date's ratio stands for every daypart and role).
"""
import json
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

PARTS = ("morning", "night")
BASELINE_WEEKS = 8
EFFECT_MIN_N = 3                 # event_memory.EFFECT_MIN_N
EFFECT_FLOOR_PCT = 5.0           # event_memory.EFFECT_FLOOR_PCT
ROLE_SHRINK = 3.0                # nights of a role's own record that weigh as much as the prior
ROLE_MAX_NIGHTS = 8              # the newest label nights a role lift reads
WEATHER_DAYS = 400
COLD_F = 40.0
HOT_F = 85.0
MILD = (55.0, 80.0)
RAIN_PCT = 60                    # schedule_economics.RAIN_FORECAST_PCT
WEATHER_SHRINK = 4.0             # nights of a weather bucket that weigh as much as the cohort prior
_SNOW_WORDS = ("snow", "blizzard", "sleet", "flurr", "wintry", "ice pellets", "freezing")
_WET_WORDS = ("rain", "shower", "thunder", "drizzle", "storm")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _median(vals):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


def _wd(iso) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%A")
    except ValueError:
        return ""


# ── each date's own daypart sales (LEARNING-11) ───────────────────────────

def split_hours(hours: dict) -> tuple:
    """(morning, night) sales from {business hour: net}: an hour before the
    restaurant's lunch/dinner boundary is lunch's, an hour after midnight
    (24+) the night's tail (schedule_time.daypart_split)."""
    import schedule_time as _stime
    split = _stime.daypart_split()
    m = n = 0.0
    for h, v in (hours or {}).items():
        try:
            h, v = int(h), float(v or 0)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if h < 24 and h * 60 < split:
            m += v
        else:
            n += v
    return m, n


def date_part_sales(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    """{iso date: {"morning", "night", "source"}} for [start, end): each
    date's own split, from the first source that holds it (tickets, then
    intraday captures, then the nightly report's hourly split)."""
    try:
        conn = get_conn(db_path)
        try:
            return date_part_sales_conn(conn, restaurant_id, start, end)
        finally:
            conn.close()
    except Exception as e:
        print(f"[demand_drivers] daypart sales unreadable for {restaurant_id}: {e}")
        return {}


def date_part_sales_conn(conn, restaurant_id, start, end) -> dict:
    """date_part_sales on an open connection (a caller mid-transaction)."""
    import schedule_economics as _econ
    sources = (("tickets", _econ._ticket_days(conn, restaurant_id, _iso(start), _iso(end))),
               ("intraday", _econ._intraday_days(conn, restaurant_id, _iso(start), _iso(end))),
               ("dsr", _econ._dsr_days(conn, restaurant_id, _iso(start), _iso(end))))
    out = {}
    for name, by_date in sources:
        for d, hours in (by_date or {}).items():
            if d in out:
                continue
            m, n = split_hours(hours)
            if m + n > 0:
                out[d] = {"morning": round(m, 2), "night": round(n, 2), "source": name}
    return out


def _part_baseline(series: dict, d: str, part: str, skip=frozenset()):
    """The median of `part`'s sales on d's weekday over the BASELINE_WEEKS
    before d (dates in `skip` left out), and how many it rests on."""
    try:
        day = date.fromisoformat(d)
    except ValueError:
        return None, 0
    vals = []
    for k in range(1, BASELINE_WEEKS + 1):
        x = (day - timedelta(weeks=k)).isoformat()
        if x in skip:
            continue
        v = (series.get(x) or {}).get(part)
        if v and v > 0:
            vals.append(v)
    return (_median(vals), len(vals)) if len(vals) >= 3 else (None, len(vals))


def _summ(ratios) -> dict | None:
    """{"lift_pct", "n", "lo", "hi", "applies"} over per-night ratios; an
    80% interval of the median by order statistics (sign test), None
    below two nights."""
    rs = sorted(r for r in ratios if r and r > 0)
    if len(rs) < 2:
        return None
    med = _median(rs)
    n = len(rs)
    # Order-statistic interval for the median (~80%): k = n/2 - 0.64*sqrt(n).
    k = max(0, int(math.floor(n / 2.0 - 0.64 * math.sqrt(n))))
    lo, hi = rs[k], rs[n - 1 - k]
    pct = (med - 1.0) * 100.0
    return {"lift_pct": round(pct, 1), "n": n, "lo": round((lo - 1.0) * 100.0, 1),
            "hi": round((hi - 1.0) * 100.0, 1),
            "applies": n >= EFFECT_MIN_N and abs(pct) >= EFFECT_FLOOR_PCT}


def label_part_lifts(restaurant_id, dates, db_path=DB_PATH, series: dict = None, skip=frozenset()) -> dict:
    """{part: summary} — a label's lift on each daypart over its nights
    (`dates`, ISO) against the same weekday's ordinary nights (`skip`: the
    other flagged nights, left out of the baseline)."""
    ds = sorted({_iso(d) for d in dates or ()})
    if not ds:
        return {}
    try:
        if series is None:
            first = date.fromisoformat(ds[0]) - timedelta(weeks=BASELINE_WEEKS)
            last = date.fromisoformat(ds[-1]) + timedelta(days=1)
            series = date_part_sales(restaurant_id, first, last, db_path=db_path)
        skip = set(skip) | set(ds)
        out = {}
        for part in PARTS:
            ratios = []
            for d in ds:
                v = (series.get(d) or {}).get(part)
                base, _n = _part_baseline(series, d, part, skip)
                if v and base:
                    ratios.append(v / base)
            s = _summ(ratios)
            if s:
                out[part] = s
        return out
    except Exception as e:
        print(f"[demand_drivers] label daypart lifts unavailable for {restaurant_id}: {e}")
        return {}


# ── an event's lift per role (LEARNING-20) ────────────────────────────────

def _role_counts(rows) -> dict:
    """{iso: {role lower: distinct people}} from shift rows."""
    out = {}
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        who = " ".join(str(r.get("employee") or "").lower().split())
        role = " ".join(str(r.get("role") or "").lower().split())
        if d and who and role:
            out.setdefault(d, {}).setdefault(role, set()).add(who)
    return {d: {r: len(p) for r, p in roles.items()} for d, roles in out.items()}


def role_event_lifts(restaurant_id, label, sales_lift_pct=None, db_path=DB_PATH, rows=None,
                     family_prior: dict = None) -> dict:
    """{role lower: {"ratio", "raw", "n", "prior"}} — how many of each role a
    label's nights ran against that role on ordinary same weekdays (the
    BASELINE_WEEKS before each), over the label's newest ROLE_MAX_NIGHTS
    measured nights with punches; each shrunk toward its prior by
    ROLE_SHRINK nights: the night's sales lift (`sales_lift_pct`, every
    role alike) times how the role's family moves on event nights at
    similar restaurants (`family_prior` {family: factor} —
    intelligence.schedule_priors.role_lift_prior, LEARNING-27; read here
    when None). {} when nothing is measured. The by-role heads are what the
    managers ran (event_memory._staffing_on) — read against the same
    managers' ordinary nights, so a habit common to both cancels."""
    try:
        import event_memory as _em
        eff = _em.measured_effect(restaurant_id, label, db_path=None if db_path == DB_PATH else db_path)
        if not eff:
            return {}
        nights = sorted(eff.get("dates") or [])[-ROLE_MAX_NIGHTS:]
        if not nights:
            return {}
        prior = 1.0 + float(sales_lift_pct if sales_lift_pct is not None else eff.get("median_lift_pct") or 0) / 100.0
        if rows is None:
            import shift_facts
            first = date.fromisoformat(nights[0]) - timedelta(weeks=BASELINE_WEEKS)
            rows = shift_facts.rows(restaurant_id, since=first.isoformat(), until=nights[-1],
                                    db_path=None if db_path == DB_PATH else db_path)
        counts = _role_counts(rows)
        flagged = set(nights)
        ratios = {}
        for d in nights:
            here = counts.get(d)
            if not here:
                continue
            day = date.fromisoformat(d)
            base_days = [(day - timedelta(weeks=k)).isoformat() for k in range(1, BASELINE_WEEKS + 1)]
            base_days = [x for x in base_days if x not in flagged and x in counts]
            if len(base_days) < 3:
                continue
            for role in set(here) | {r for x in base_days for r in counts[x]}:
                base = _median([counts[x].get(role, 0) for x in base_days])
                if base and base > 0:
                    ratios.setdefault(role, []).append(here.get(role, 0) / float(base))
        if family_prior is None:
            try:
                from intelligence import schedule_priors as _sp
                family_prior = _sp.role_lift_prior(restaurant_id, db_path=db_path) or {}
            except Exception:
                family_prior = {}
        from intelligence.staffing import family_of
        out = {}
        for role, rs in ratios.items():
            raw = _median(rs)
            n = len(rs)
            pr = prior * float((family_prior or {}).get(family_of(role), 1.0))
            shrunk = (n * raw + ROLE_SHRINK * pr) / (n + ROLE_SHRINK)
            out[role] = {"ratio": round(shrunk, 3), "raw": round(raw, 3), "n": n, "prior": round(pr, 3)}
        return out
    except Exception as e:
        print(f"[demand_drivers] role lifts unavailable for {restaurant_id} {label}: {e}")
        return {}


# ── the weather beyond rain (LEARNING-21) ─────────────────────────────────

def conditions_of(row: dict, part: str) -> set:
    """The weather buckets one observed (weather_daily) or forecast row puts
    `part` in: rain, snow, cold, hot — or {"mild"} for a dry mild one, set()
    when nothing about it is known. The row's daypart fields (lunch_*/dinner_*)
    when present; else the day's own figures for both."""
    if not row:
        return set()
    p = "lunch" if part == "morning" else "dinner"
    wet = row.get(f"{p}_wet")
    if wet is None:
        wet = row.get("rain")
    temp = row.get(f"{p}_temp_f")
    if temp is None:
        temp = row.get("high_f") if part == "morning" else (row.get("low_f") if row.get("low_f") is not None
                                                             else row.get("high_f"))
    snow = row.get("snow")
    out = set()
    if snow:
        out.add("snow")
    elif wet:
        out.add("rain")
    try:
        t = float(temp) if temp is not None else None
    except (TypeError, ValueError):
        t = None
    if t is not None:
        if t < COLD_F:
            out.add("cold")
        elif t >= HOT_F:
            out.add("hot")
    if not out and wet is not None and t is not None and MILD[0] <= t < MILD[1]:
        return {"mild"}
    return out


def _observed(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM weather_daily WHERE restaurant_id=? AND date>=? AND date<?",
                            (restaurant_id, _iso(start), _iso(end))).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {str(r["date"])[:10]: dict(r) for r in rows}


def weather_effects(restaurant_id, db_path=DB_PATH, today=None, series=None, observed=None,
                    prior: dict = None) -> dict:
    """{part: {condition: summary + "prior", "shrunk_pct"}} — each weather
    bucket's lift on a daypart against the same weekday's DRY MILD dayparts
    of the BASELINE_WEEKS before (a ratio per night, summarised as
    label_part_lifts does). `prior` ({part: {condition: lift_pct}}, the
    cohort's — intelligence.schedule_priors) shrinks a bucket with few
    nights toward it (WEATHER_SHRINK); with no prior the bucket's own
    figure stands only past EFFECT_MIN_N nights."""
    try:
        today = today or date.today()
        start = today - timedelta(days=WEATHER_DAYS)
        if observed is None:
            observed = _observed(restaurant_id, start - timedelta(weeks=BASELINE_WEEKS), today, db_path=db_path)
        if not observed:
            return {}
        if series is None:
            series = date_part_sales(restaurant_id, start - timedelta(weeks=BASELINE_WEEKS), today, db_path=db_path)
        if not series:
            return {}
        mild = {part: {d for d, w in observed.items() if conditions_of(w, part) == {"mild"}} for part in PARTS}
        out = {}
        for part in PARTS:
            buckets = {}
            for d, w in observed.items():
                if d < start.isoformat():
                    continue
                conds = conditions_of(w, part) - {"mild"}
                v = (series.get(d) or {}).get(part)
                if not conds or not v:
                    continue
                day = date.fromisoformat(d)
                base_vals = []
                for k in range(1, BASELINE_WEEKS + 1):
                    x = (day - timedelta(weeks=k)).isoformat()
                    bv = (series.get(x) or {}).get(part)
                    if x in mild[part] and bv:
                        base_vals.append(bv)
                if len(base_vals) < 2:
                    continue
                base = _median(base_vals)
                for c in conds:
                    buckets.setdefault(c, []).append(v / base)
            for c, ratios in buckets.items():
                s = _summ(ratios)
                if not s:
                    continue
                pr = ((prior or {}).get(part) or {}).get(c)
                if pr is not None:
                    s["prior"] = pr
                    s["shrunk_pct"] = round((s["n"] * s["lift_pct"] + WEATHER_SHRINK * float(pr))
                                            / (s["n"] + WEATHER_SHRINK), 1)
                    s["applies"] = abs(s["shrunk_pct"]) >= EFFECT_FLOOR_PCT
                else:
                    s["shrunk_pct"] = s["lift_pct"]
                out.setdefault(part, {})[c] = s
        return out
    except Exception as e:
        print(f"[demand_drivers] weather effects unavailable for {restaurant_id}: {e}")
        return {}


def patio_effects(restaurant_id, patio_roles, db_path=DB_PATH, today=None, observed=None) -> dict:
    """{part: {condition: summary}} — the patio room's own sales on wet,
    snowy, cold or hot dayparts against dry mild same-weekday ones: how far
    the patio's crew moves, read from the tickets' room names (a room whose
    name carries "patio" or one of `patio_roles`' words). {} when the
    archive has no such room."""
    words = {"patio"} | {w for r in (patio_roles or ()) for w in str(r).lower().split() if len(w) > 3}
    if not patio_roles:
        return {}
    try:
        today = today or date.today()
        start = today - timedelta(days=WEATHER_DAYS)
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT business_date, substr(replace(opened_at, ' ', 'T'), 1, 13) AS hk, room_name, "
                "SUM(net_sales) AS net FROM pos_tickets WHERE restaurant_id=? AND business_date>=? AND "
                "business_date<? AND COALESCE(cancelled,0)=0 AND room_name IS NOT NULL "
                "GROUP BY business_date, hk, room_name",
                (restaurant_id, (start - timedelta(weeks=BASELINE_WEEKS)).isoformat(), today.isoformat())).fetchall()
        finally:
            conn.close()
        hours = {}
        for r in rows:
            if not any(w in str(r["room_name"] or "").lower() for w in words):
                continue
            try:
                hk = str(r["hk"])
                bd = str(r["business_date"])[:10]
                h = int(hk[11:13]) + 24 * max(0, (date.fromisoformat(hk[:10]) - date.fromisoformat(bd)).days)
            except (TypeError, ValueError):
                continue
            day = hours.setdefault(bd, {})
            day[h] = day.get(h, 0.0) + float(r["net"] or 0)
        if not hours:
            return {}
        series = {}
        for d, hs in hours.items():
            m, n = split_hours(hs)
            series[d] = {"morning": m, "night": n}
        if observed is None:
            observed = _observed(restaurant_id, start - timedelta(weeks=BASELINE_WEEKS), today, db_path=db_path)
        return weather_effects(restaurant_id, db_path=db_path, today=today, series=series, observed=observed)
    except Exception as e:
        print(f"[demand_drivers] patio effects unavailable for {restaurant_id}: {e}")
        return {}


def forecast_parts(weather_rows) -> dict:
    """{iso date: {"morning": row, "night": row}} from the NWS rows the
    schedule already holds (weather.get_forecast_for_week /
    cached_forecast_for_week): the daytime period is lunch's, the night
    period dinner's (`night_precip_pct`, `low_f`, `night_short_forecast`
    when the row carries them, else the day's own). Each part row has
    {"rain", "snow", "high_f"/"low_f"} for conditions_of; a stale row is
    left out."""
    out = {}
    for w in weather_rows or []:
        try:
            if w.get("stale"):
                continue
            d = str(w.get("date"))[:10]
            day_text = str(w.get("short_forecast") or "").lower()
            night_text = str(w.get("night_short_forecast") or day_text).lower()
            day_p = w.get("precip_pct")
            night_p = w.get("night_precip_pct", day_p)
            def _wet(p, text):
                try:
                    if p is not None and int(p) >= RAIN_PCT:
                        return 1
                except (TypeError, ValueError):
                    pass
                return 1 if (p is None and any(x in text for x in _WET_WORDS)) else 0
            def _snow(text):
                return 1 if any(x in text for x in _SNOW_WORDS) else 0
            out[d] = {
                "morning": {"rain": _wet(day_p, day_text), "snow": _snow(day_text) and _wet(day_p, day_text),
                            "high_f": w.get("high_f"), "precip_pct": day_p},
                "night": {"rain": _wet(night_p, night_text),
                          "snow": _snow(night_text) and _wet(night_p, night_text),
                          "high_f": w.get("low_f") if w.get("low_f") is not None else w.get("high_f"),
                          "low_f": w.get("low_f"), "precip_pct": night_p},
            }
        except Exception:
            continue
    return out


WEATHER_WORDS = {"rain": "rain", "snow": "snow", "cold": "a cold (under 40°F)", "hot": "a hot (85°F+)"}


def weather_factor(parts_forecast: dict, effects: dict, part: str) -> tuple:
    """(factor, [{"cond", "pct", "n"}], [conditions]) for one daypart of one
    date: each forecast condition with a measured effect that applies,
    multiplied (rain and snow are one bucket each; cold/hot one), bounded
    to 0.5-1.6. weather_words() says them."""
    row = (parts_forecast or {}).get(part)
    conds = conditions_of(row, part) - {"mild"} if row else set()
    f, items, used = 1.0, [], []
    for c in sorted(conds):
        e = ((effects or {}).get(part) or {}).get(c)
        if not e or not e.get("applies"):
            continue
        pct = float(e.get("shrunk_pct", e.get("lift_pct")) or 0)
        f *= 1.0 + pct / 100.0
        used.append(c)
        items.append({"cond": c, "pct": pct, "n": e["n"]})
    return max(0.5, min(1.6, f)), items, used


def weather_words(items, part=None) -> list:
    """The owner's words for weather_factor's items — on one meal (`part`)
    or, with None, the whole day (both meals alike)."""
    meal = {"morning": "lunch", "night": "dinner"}.get(part)
    out = []
    for it in items or []:
        pct = float(it["pct"])
        what = WEATHER_WORDS.get(it["cond"], it["cond"])
        where = f" {meal}" if meal else " day"
        out.append(f"{what}{where} forecast: {meal + 's' if meal else 'days'} like it here ran {abs(pct):.0f}% "
                   f"{'below' if pct < 0 else 'above'} a dry mild one (measured {it['n']} times)")
    return out


def event_parts(restaurant_id, iso, db_path=DB_PATH) -> set:
    """The dayparts a date's catalog games start in (kickoff on the
    restaurant's clock, an hour before it counting as the game's), or
    set() when no game on the date has a kickoff on file."""
    try:
        conn = get_conn(db_path)
        try:
            refs = [r["ref"] for r in conn.execute(
                "SELECT ref FROM demand_signals WHERE restaurant_id=? AND date=? AND kind='event' AND ref LIKE "
                "'event:%'", (restaurant_id, iso)).fetchall() if r["ref"]]
        finally:
            conn.close()
        if not refs:
            return set()
        from event_intel import store as _store, engine as _engine
        import schedule_time as _stime
        tz = _engine.restaurant_clock(restaurant_id)
        out = set()
        for ref in refs:
            try:
                e = _store.event_by_id(int(str(ref).split(":", 1)[1]))
            except (ValueError, TypeError):
                continue
            lk = _engine.local_kickoff(e, tz) if e else None
            if not lk or not lk[1]:
                continue
            m = int(lk[1][:2]) * 60 + int(lk[1][3:5]) - 60
            out.add("morning" if m < _stime.daypart_split() else "night")
        return out
    except Exception as e:
        print(f"[demand_drivers] event dayparts unavailable for {restaurant_id} {iso}: {e}")
        return set()


def headcount_json(raw) -> dict:
    try:
        return json.loads(raw or "{}") or {}
    except (TypeError, ValueError):
        return {}
