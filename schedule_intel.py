"""
schedule_intel.py — what the schedule learns from its own record.

Everything here is per-restaurant, read from first-party rows, and
rendered as facts the prompt and the quality engine can use. Nothing calls
a model; nothing crosses a tenant.

  record_outcomes       what each PUBLISHED week actually did, by date and
                        daypart: hours, sales, labor %, issues, review rating
  outcome_block         "last time this pattern ran" for the prompt
  fairness_ledger       weekends, closes and holidays per person over 8 weeks
  behaviour_preferences what people keep dropping and claiming
  mentoring             shifts worked beside a closer in a role that is not
                        their own — who could hold a station
  chemistry_suggestions pairs whose shared dayparts ran clean — suggested,
                        never applied
  recommendation events an accept/dismiss ledger, and the kinds nobody takes
  learned-pattern dismissals — the owner's say over what the draft learns
"""
import json
import re
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports). A
    bound copy — and a db_path default bound to DB_PATH — sent a test's
    patched models.get_conn to the real database. The module's own DB_PATH
    default means "whatever models uses now"."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

OUTCOME_WEEKS = 12
LEDGER_WEEKS = 8
MENTOR_SHIFTS_TO_HOLD = 8
SUGGEST_MIN_SHARED = 6
SUGGEST_MIN_CLEAN = 0.8
SUPPRESS_AFTER_SHOWN = 10


def _hours(r):
    try:
        return float(r.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _daypart(r):
    from schedule_rules import daypart_of
    return daypart_of(r.get("shift_start", ""))


# ── outcomes per published week ───────────────────────────────────────────

def record_outcomes(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """For every published week that has ended, one row per date and
    daypart: scheduled hours (from the published CSV), sales and labor %
    (labor_daily_history, split by the intraday morning share), coverage
    and no-show issues on that date, and the mean review rating dated that
    day. Idempotent: rows are keyed by (history_id, date, daypart)."""
    from schedule_versions import rows_from_csv
    today = today or date.today()
    conn = get_conn(db_path)
    written = 0
    try:
        weeks = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
            "AND week_end < ? ORDER BY id DESC LIMIT ?", (restaurant_id, today.isoformat(), OUTCOME_WEEKS)).fetchall()
        if not weeks:
            return {"written": 0}
        share = _morning_share(conn, restaurant_id)
        # Issues are stamped in UTC; the schedule's dates and dayparts are
        # the restaurant's own. A 7pm Central no-show is 00:00 UTC the next
        # day: matched on the UTC date it landed on the wrong day and was
        # counted against both dayparts.
        try:
            from models import get_restaurant as _gr
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI((getattr(_gr(restaurant_id), "timezone", None) or "America/Chicago"))
        except Exception:
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI("America/Chicago")
        issue_at = {}
        try:
            for (created,) in conn.execute(
                    "SELECT created_at FROM ops_issues WHERE restaurant_id=? AND kind IN ('coverage','no_show') "
                    "AND created_at >= date(?, '-2 days')",
                    (restaurant_id, min(w["week_start"] for w in weeks if w["week_start"]))).fetchall():
                try:
                    utc = datetime.fromisoformat(str(created).replace("Z", "")).replace(tzinfo=_ZI("UTC"))
                except ValueError:
                    continue
                local = utc.astimezone(_tz)
                key = (local.strftime("%Y-%m-%d"), "morning" if local.hour < 15 else "night")
                issue_at[key] = issue_at.get(key, 0) + 1
        except Exception as _ix:
            print(f"[outcomes] issues unavailable for restaurant {restaurant_id}: {_ix}")
        for w in weeks:
            rows = rows_from_csv(w["schedule_csv"])
            by = {}
            for r in rows:
                part = _daypart(r)
                if part == "unknown" or not r.get("date"):
                    continue
                e = by.setdefault((r["date"], part), {"hours": 0.0, "people": set()})
                e["hours"] += _hours(r)
                e["people"].add(r["employee"])
            for (d, part), e in by.items():
                day = conn.execute("SELECT sales, labor_pct, day_of_week FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                                   (restaurant_id, d)).fetchone()
                sales = None
                if day and day["sales"]:
                    try:
                        wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
                    except ValueError:
                        wd = day["day_of_week"]
                    s = share.get(wd, 0.4)
                    sales = round(float(day["sales"]) * (s if part == "morning" else 1 - s), 0)
                issues = issue_at.get((d, part), 0)
                # A review carries a date, not a time: it can't be split
                # between lunch and dinner, so it is recorded once, on the
                # daypart that carried the day's most hours — not on both.
                main_part = max((p2 for (d2, p2) in by if d2 == d), key=lambda p2: by[(d, p2)]["hours"])
                rating, n_reviews = None, 0
                if part == main_part:
                    try:
                        rv = conn.execute("SELECT AVG(rating), COUNT(*) FROM reviews WHERE restaurant_id=? AND substr(review_date,1,10)=?",
                                          (restaurant_id, d)).fetchone()
                        rating, n_reviews = (round(float(rv[0]), 2) if rv and rv[0] else None), (rv[1] if rv else 0)
                    except Exception as _rx:
                        print(f"[outcomes] reviews unavailable for restaurant {restaurant_id}: {_rx}")
                conn.execute(
                    "INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, sales, labor_pct, issues, "
                    "review_rating, reviews) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(history_id, date, daypart) DO UPDATE SET "
                    "hours=excluded.hours, people=excluded.people, sales=excluded.sales, labor_pct=excluded.labor_pct, "
                    "issues=excluded.issues, review_rating=excluded.review_rating, reviews=excluded.reviews, recorded_at=datetime('now')",
                    (restaurant_id, w["id"], d, part, round(e["hours"], 1), len(e["people"]), sales,
                     (day["labor_pct"] if day else None), issues, rating, n_reviews))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written}


def _morning_share(conn, restaurant_id) -> dict:
    """{weekday: share of the day's sales taken by 3pm}, from ≥3 captured days."""
    try:
        rows = conn.execute("SELECT weekday, business_date, captured_hour, net_sales FROM pos_intraday WHERE restaurant_id=? "
                            "AND business_date >= date('now','-84 days') ORDER BY business_date, captured_hour", (restaurant_id,)).fetchall()
    except Exception:
        return {}
    by = {}
    for r in rows:
        by.setdefault((r["weekday"], r["business_date"]), []).append((int(r["captured_hour"]), float(r["net_sales"] or 0)))
    tmp = {}
    for (wd, _), caps in by.items():
        caps.sort()
        total = caps[-1][1]
        at3 = max((s for h, s in caps if h <= 15), default=None)
        if total > 0 and at3 is not None:
            tmp.setdefault(wd, []).append(min(1.0, at3 / total))
    return {wd: sorted(v)[len(v) // 2] for wd, v in tmp.items() if len(v) >= 3}


def outcomes_by_daypart(restaurant_id, db_path=DB_PATH) -> dict:
    """{weekday: {daypart: {weeks, avg_hours, avg_sales, splh, issues, troubled, rating}}}
    over the recorded weeks."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, daypart, hours, sales, issues, review_rating FROM schedule_outcomes WHERE restaurant_id=? "
                            "ORDER BY date DESC LIMIT 400", (restaurant_id,)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    acc = {}
    for r in rows:
        try:
            wd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        e = acc.setdefault(wd, {}).setdefault(r["daypart"], {"weeks": 0, "hours": 0.0, "sales": 0.0, "sales_n": 0, "issues": 0, "ratings": []})
        e["weeks"] += 1
        e["hours"] += float(r["hours"] or 0)
        if r["sales"] is not None:
            e["sales"] += float(r["sales"]); e["sales_n"] += 1
        e["issues"] += int(r["issues"] or 0)
        if r["review_rating"] is not None:
            e["ratings"].append(float(r["review_rating"]))
    out = {}
    for wd, parts in acc.items():
        for part, e in parts.items():
            if e["weeks"] < 2:
                continue
            avg_h = e["hours"] / e["weeks"]
            avg_s = (e["sales"] / e["sales_n"]) if e["sales_n"] else None
            out.setdefault(wd, {})[part] = {
                "weeks": e["weeks"], "avg_hours": round(avg_h, 1), "avg_sales": round(avg_s, 0) if avg_s else None,
                "splh": round(avg_s / avg_h, 0) if (avg_s and avg_h) else None,
                "issues": e["issues"], "troubled": e["issues"] >= max(2, e["weeks"] // 2),
                "rating": round(sum(e["ratings"]) / len(e["ratings"]), 2) if e["ratings"] else None,
            }
    return out


def outcome_block(outcomes: dict, week_days: list) -> str:
    """The prompt's "last time this pattern ran" facts."""
    if not outcomes:
        return ""
    lines = []
    for wd in week_days or []:
        parts = outcomes.get(wd) or {}
        for part in ("morning", "night"):
            e = parts.get(part)
            if not e:
                continue
            bits = [f"about {e['avg_hours']:g}h scheduled"]
            if e.get("splh"):
                bits.append(f"${e['splh']:,.0f} of sales per labor hour")
            if e["issues"]:
                bits.append(f"{e['issues']} coverage or no-show issue{'s' if e['issues'] != 1 else ''} in {e['weeks']} weeks")
            if e.get("rating") is not None:
                bits.append(f"reviews averaged {e['rating']:g}★")
            tag = " — a daypart that has gone wrong before; do not thin it" if e["troubled"] else ""
            lines.append(f"  {wd} {'lunch/day' if part == 'morning' else 'dinner/night'}: " + ", ".join(bits) + tag)
    if not lines:
        return ""
    return ("\n\nWHAT PUBLISHED WEEKS ACTUALLY DID (this restaurant's own record, by daypart — the pattern that ran, "
            "and how it went):\n" + "\n".join(lines))


# ── fairness ledger ────────────────────────────────────────────────────────

def fairness_ledger(restaurant_id, weeks: int = LEDGER_WEEKS, db_path=DB_PATH, today=None) -> dict:
    """{name: {"weekend": n, "closing": n, "holiday": n, "weeks": w}} from
    published weeks — the rotation memory a seven-day window cannot hold."""
    from schedule_versions import rows_from_csv
    from schedule_economics import _holiday_dates
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT week_start, week_end, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                            "AND week_start >= ? ORDER BY week_start DESC LIMIT ?",
                            (restaurant_id, (today - timedelta(weeks=weeks)).isoformat(), weeks)).fetchall()
        close_times = {}
        try:
            from models import get_close_times
            close_times = get_close_times(restaurant_id, db_path) or {}
        except Exception:
            pass
    finally:
        conn.close()
    if not rows:
        return {}
    from schedule_rules import parse_minutes
    # Both ends of every week: one starting Dec 28 holds New Year's Day of
    # the next year, which the week_start year alone never looked up (SCHED-33).
    holidays = {}
    years = set()
    for r in rows:
        for k in ("week_start", "week_end"):
            try:
                years.add(int((r[k] or "")[:4]))
            except (TypeError, ValueError):
                pass
    for y in years:
        holidays.update(_holiday_dates(y))
    ledger = {}
    for w in rows:
        for r in rows_from_csv(w["schedule_csv"]):
            n = r["employee"]
            e = ledger.setdefault(n, {"weekend": 0, "closing": 0, "holiday": 0, "shifts": 0})
            e["shifts"] += 1
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d")
            except ValueError:
                continue
            # Friday to Sunday, the weekend the scorer's fairness and the
            # prompt count (shift_quality._WEEKEND) — this ledger counted only
            # Saturday and Sunday, so the two disagreed about who had them.
            if d.weekday() >= 4:
                e["weekend"] += 1
            if r["date"] in holidays:
                e["holiday"] += 1
            close = parse_minutes(close_times.get(d.strftime("%A"), ""))
            end = parse_minutes(r.get("shift_end", ""))
            if close is not None and end is not None and end >= close - 30:
                e["closing"] += 1
            elif close is None and end is not None and end >= 22 * 60:
                e["closing"] += 1
    for e in ledger.values():
        e["weeks"] = len(rows)
    return ledger


def ledger_block(ledger: dict) -> str:
    if not ledger:
        return ""
    active = {n: e for n, e in ledger.items() if e["shifts"] >= 3}
    if len(active) < 3:
        return ""
    wk = next(iter(active.values()))["weeks"]
    top_w = sorted(active.items(), key=lambda kv: -kv[1]["weekend"])[:3]
    low_w = sorted(active.items(), key=lambda kv: kv[1]["weekend"])[:3]
    top_c = sorted(active.items(), key=lambda kv: -kv[1]["closing"])[:3]
    lines = [f"  Most weekend shifts in the last {wk} published weeks: " + ", ".join(f"{n} ({e['weekend']})" for n, e in top_w),
             "  Fewest: " + ", ".join(f"{n} ({e['weekend']})" for n, e in low_w),
             "  Most closes: " + ", ".join(f"{n} ({e['closing']})" for n, e in top_c)]
    hol = [(n, e["holiday"]) for n, e in active.items() if e["holiday"]]
    if hol:
        lines.append("  Holidays worked: " + ", ".join(f"{n} ({h})" for n, h in sorted(hol, key=lambda x: -x[1])[:5]))
    return ("\n\nROTATION LEDGER (who has carried the weekends, closes and holidays lately — spread the next ones "
            "toward the people at the bottom of each list where the rules allow):\n" + "\n".join(lines))


# ── behaviour-learned preferences ─────────────────────────────────────────

def behaviour_preferences(restaurant_id, weeks: int = 12, db_path=DB_PATH) -> dict:
    """{name: {"avoids": ["Sunday night", …], "prefers": [...], "drops": n, "claims": n}}
    from drop requests and claims. Two of the same is a pattern."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name, replacement_name, date, shift_start, status, kind FROM shift_change_requests "
                            "WHERE restaurant_id=? AND created_at >= date('now', ?)", (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    from schedule_rules import daypart_of
    tally = {}
    for r in rows:
        try:
            wd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        slot = f"{wd} {'night' if daypart_of(r['shift_start'] or '') == 'night' else 'day'}"
        # A drop the manager DENIED is not a preference the next draft should
        # honour — reading it as "avoids" overrode that decision (SCHED-40).
        # Only a drop that was let go (open, covered) or a swap that went
        # through says the person does not want that slot.
        if (r["kind"] or "drop") in ("drop", "swap") and r["status"] in ("open", "covered"):
            t = tally.setdefault(r["employee_name"], {"avoid": {}, "prefer": {}, "drops": 0, "claims": 0})
            t["avoid"][slot] = t["avoid"].get(slot, 0) + 1
            t["drops"] += 1
        if r["replacement_name"] and r["status"] == "covered":
            t = tally.setdefault(r["replacement_name"], {"avoid": {}, "prefer": {}, "drops": 0, "claims": 0})
            t["prefer"][slot] = t["prefer"].get(slot, 0) + 1
            t["claims"] += 1
    out = {}
    for n, t in tally.items():
        avoids = sorted(s for s, c in t["avoid"].items() if c >= 2)
        prefers = sorted(s for s, c in t["prefer"].items() if c >= 2)
        if avoids or prefers:
            out[n] = {"avoids": avoids, "prefers": prefers, "drops": t["drops"], "claims": t["claims"]}
    return out


def preferences_block(learned: dict, stated: dict) -> str:
    """Stated preferences (staff_settings.preferred_dayparts / desired_hours)
    and learned ones, as soft signals."""
    lines = []
    for n, p in sorted((stated or {}).items()):
        bits = []
        if p.get("preferred_dayparts"):
            bits.append("prefers " + "/".join(p["preferred_dayparts"]))
        if p.get("desired_hours"):
            bits.append(f"would like about {float(p['desired_hours']):g}h a week")
        if bits:
            lines.append(f"  {n}: " + "; ".join(bits))
    for n, p in sorted((learned or {}).items()):
        bits = []
        if p["avoids"]:
            bits.append("keeps asking to drop " + ", ".join(p["avoids"]))
        if p["prefers"]:
            bits.append("keeps picking up " + ", ".join(p["prefers"]))
        lines.append(f"  {n}: " + "; ".join(bits))
    if not lines:
        return ""
    return ("\n\nWHAT STAFF WANT (stated, and learned from what they drop and claim — soft: honour it where the "
            "rules and coverage allow, never over them):\n" + "\n".join(lines))


# ── mentoring / succession ─────────────────────────────────────────────────

def mentoring(restaurant_id, db_path=DB_PATH) -> dict:
    """{name: {role: shifts beside a closer}} — shifts a person worked in a
    role that is not their usual one, on the same date and daypart as
    somebody authorised to close. At MENTOR_SHIFTS_TO_HOLD they could hold
    the station."""
    from models import _cached_shifts, get_leader_flags
    try:
        closers = {n.lower() for n, v in (get_leader_flags(restaurant_id, db_path) or {}).items() if v}
        shifts = _cached_shifts(restaurant_id)
    except Exception:
        return {}
    if not closers or not shifts:
        return {}
    usual = {}
    for s in shifts:
        n = (s.get("employee") or "").strip()
        role = (s.get("role") or "").strip()
        if n and role:
            usual.setdefault(n, {})[role] = usual.get(n, {}).get(role, 0) + 1
    usual = {n: max(r.items(), key=lambda kv: kv[1])[0] for n, r in usual.items()}
    by_slot = {}
    for s in shifts:
        by_slot.setdefault((s.get("date"), _daypart(s)), []).append(s)
    out = {}
    for (_d, _p), group in by_slot.items():
        has_closer = any((g.get("employee") or "").strip().lower() in closers for g in group)
        if not has_closer:
            continue
        for g in group:
            n, role = (g.get("employee") or "").strip(), (g.get("role") or "").strip()
            if not n or not role or usual.get(n) == role or n.lower() in closers:
                continue
            out.setdefault(n, {})[role] = out.get(n, {}).get(role, 0) + 1
    return out


def could_hold(mentored: dict) -> dict:
    """{name: [roles]} they have been mentored in enough times."""
    return {n: [r for r, c in roles.items() if c >= MENTOR_SHIFTS_TO_HOLD] for n, roles in (mentored or {}).items()
            if any(c >= MENTOR_SHIFTS_TO_HOLD for c in roles.values())}


# ── was anyone watching? ──────────────────────────────────────────────────
#
# "Clean" here means no coverage or no-show issue was opened. That is only
# evidence when something could have opened one. Nothing creates "no_show"
# issues at all, and a "coverage" issue is opened only by
# strategy_jobs.run_coverage_check, which runs only when ALL of these hold:
#
#   1. the Labor module is on;
#   2. issue routing names a manager (issues.get_routing has "manager");
#   3. the connected POS has a live clock-in feed (its provider module
#      exposes fetch_clock_ins_today — Toast does; RPOWER, month-at-a-time,
#      does not);
#   4. the restaurant was open and the POS was read during service THAT DAY.
#
# 1-3 are read from the current configuration. 4 is per date: a pos_intraday
# reading exists for it (run_intraday_capture takes one each open hour from
# the same live POS, on the same open-hours rule as the coverage check).
# Without all four, "8 of 8 shared dayparts ran without an issue", an
# accepted recommendation "improved", and auto-publish's "ran clean" weeks
# were all true of every restaurant by default (re-audit A-19) — so those
# reads are withheld for any date nobody was watching.

def coverage_check_possible(restaurant_id, db_path=DB_PATH) -> bool:
    """Conditions 1-3 above: whether run_coverage_check can open a coverage
    issue for this restaurant at all."""
    try:
        from models import get_restaurant
        import issues, pos
        r = get_restaurant(restaurant_id, db_path)
        if not r or not getattr(r, "module_labor", 0):
            return False
        if "manager" not in issues.get_routing(restaurant_id, db_path):
            return False
        _name, mod = pos.connected_provider(restaurant_id)
        return bool(mod) and getattr(mod, "fetch_clock_ins_today", None) is not None
    except Exception as e:
        print(f"[schedule_intel] coverage_check_possible failed for {restaurant_id}: {e}")
        return False


def watched_dates(restaurant_id, start, end, db_path=DB_PATH) -> set:
    """ISO dates in [start, end] on which a clean night means something:
    coverage_check_possible, and a live POS reading taken that day (4)."""
    if not coverage_check_possible(restaurant_id, db_path):
        return set()
    conn = get_conn(db_path)
    try:
        return {r["business_date"] for r in conn.execute(
            "SELECT DISTINCT business_date FROM pos_intraday WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()}
    except Exception as e:
        print(f"[schedule_intel] watched_dates failed for {restaurant_id}: {e}")
        return set()
    finally:
        conn.close()


# ── chemistry suggestions ─────────────────────────────────────────────────

def chemistry_suggestions(restaurant_id, db_path=DB_PATH) -> list:
    """Pairs who shared at least SUGGEST_MIN_SHARED recorded dayparts of
    which SUGGEST_MIN_CLEAN ran without an issue, and are not already a
    pair. Returned as suggestions with their evidence; nothing is written."""
    from schedule_versions import rows_from_csv
    conn = get_conn(db_path)
    try:
        outs = conn.execute("SELECT history_id, date, daypart, issues FROM schedule_outcomes WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        csvs = {r["id"]: r["schedule_csv"] for r in conn.execute(
            "SELECT id, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id)", (restaurant_id,)).fetchall()}
        existing = {frozenset((r["employee_a"].lower(), r["employee_b"].lower())) for r in conn.execute(
            "SELECT employee_a, employee_b FROM staff_pairs WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
    except Exception:
        return []
    finally:
        conn.close()
    if not outs:
        return []
    # Only nights somebody was watching can count as "ran without an issue"
    # (watched_dates, A-19). None watched, no suggestion.
    dates = sorted(str(o["date"]) for o in outs if o["date"])
    seen = watched_dates(restaurant_id, dates[0], dates[-1], db_path) if dates else set()
    outs = [o for o in outs if str(o["date"]) in seen]
    if not outs:
        return []
    people_by_slot = {}
    for hid, csv_text in csvs.items():
        for r in rows_from_csv(csv_text):
            people_by_slot.setdefault((hid, r["date"], _daypart(r)), set()).add(r["employee"])
    shared, clean = {}, {}
    for o in outs:
        people = sorted(people_by_slot.get((o["history_id"], o["date"], o["daypart"]), set()))
        ok = int(o["issues"] or 0) == 0
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                k = frozenset((people[i].lower(), people[j].lower()))
                shared[k] = shared.get(k, 0) + 1
                clean[k] = clean.get(k, 0) + (1 if ok else 0)
    out = []
    for k, n in shared.items():
        if n < SUGGEST_MIN_SHARED or k in existing:
            continue
        rate = clean[k] / n
        if rate >= SUGGEST_MIN_CLEAN:
            a, b = sorted(k)
            out.append({"a": a.title(), "b": b.title(), "kind": "prefer", "shared": n, "clean_rate": round(rate, 2),
                        "evidence": f"{clean[k]} of {n} shared dayparts ran without a coverage or no-show issue"})
    out.sort(key=lambda x: (-x["shared"], -x["clean_rate"]))
    for x in out:
        x["rec_key"] = pair_rec_key(x["a"], x["b"])
    return out[:8]


def pair_rec_key(a, b) -> str:
    import rec_ledger as _rl
    return _rl.rec_key("suggested_pair", "|".join(sorted((str(a).strip().lower(), str(b).strip().lower()))))


def chemistry_suggestions_shown(restaurant_id, surface="labor", user_id=None, db_path=DB_PATH) -> list:
    """chemistry_suggestions as an owner sees them: without the pairs they
    said "Ignore" to (rec_ledger, on any device), and logged as shown."""
    import rec_ledger as _rl
    quiet = _rl.silenced_keys(restaurant_id, db_path=db_path)
    out = [x for x in chemistry_suggestions(restaurant_id, db_path=db_path) if x["rec_key"] not in quiet]
    _rl.present_many(restaurant_id, [dict(key=x["rec_key"], module="schedule", kind="suggested_pair",
                                          title=f"Pair {x['a']} with {x['b']}", evidence_sources=["schedule"])
                                     for x in out], surface, user_id=user_id, db_path=db_path)
    return out


# ── recommendation ledger ──────────────────────────────────────────────────

def record_recommendation(restaurant_id, kind: str, key: str, action: str, actor=None, db_path=DB_PATH) -> None:
    """action: shown | accepted | dismissed | restored (the owner asked for a
    suppressed kind back)."""
    if action not in ("shown", "accepted", "dismissed", "restored") or not kind:
        return
    conn = get_conn(db_path)
    try:
        # A showing is one per recommendation per day: a manager saving the
        # same week ten times in one sitting was ten "shown, never taken"
        # and switched the advice off for good (SCHED-26), and every rescore
        # grew the table (SCHED-27).
        if action == "shown" and conn.execute(
                "SELECT 1 FROM schedule_recommendation_events WHERE restaurant_id=? AND kind=? AND key=? "
                "AND action='shown' AND created_at >= date('now')",
                (restaurant_id, str(kind)[:60], str(key or "")[:200])).fetchone():
            return
        conn.execute("INSERT INTO schedule_recommendation_events (restaurant_id, kind, key, action, actor) VALUES (?,?,?,?,?)",
                     (restaurant_id, str(kind)[:60], str(key or "")[:200], action, (actor or "")[:120] or None))
        conn.commit()
    finally:
        conn.close()


_REC_WHEN = re.compile(r"\bon (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (morning|night|lunch|dinner)\b")


def measure_accepted_recommendations(restaurant_id, db_path=DB_PATH, today=None) -> int:
    """For accepted schedule recommendations about one night ("Fill the gap
    on Friday night…", "Move somebody … onto Saturday night"), read what that
    night recorded once its published week is over (schedule_outcomes): no
    coverage or no-show issue is "improved", any is "worsened". Recorded as
    the recommendation's outcome in rec_ledger. Idempotent."""
    import rec_ledger as _rl
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        acc = conn.execute("SELECT kind, key, created_at FROM schedule_recommendation_events WHERE restaurant_id=? "
                           "AND action='accepted' AND created_at >= datetime('now', '-60 days')", (restaurant_id,)).fetchall()
        out = conn.execute("SELECT o.date, o.daypart, o.issues, h.week_start, h.week_end FROM schedule_outcomes o "
                           "JOIN schedule_history h ON h.id=o.history_id WHERE o.restaurant_id=? AND h.week_end < ?",
                           (restaurant_id, today.isoformat())).fetchall()
    except Exception as e:
        print(f"[schedule_intel] measure_accepted_recommendations unavailable: {e}")
        return 0
    finally:
        conn.close()
    n = 0
    for a in acc:
        m = _REC_WHEN.search(a["key"] or "")
        if not m:
            continue
        day, part = m.group(1), {"lunch": "morning", "dinner": "night"}.get(m.group(2), m.group(2))
        accepted_on = str(a["created_at"])[:10]
        match = [o for o in out if o["daypart"] == part and o["date"] >= accepted_on
                 and datetime.strptime(o["date"], "%Y-%m-%d").strftime("%A") == day]
        if not match:
            continue
        o = sorted(match, key=lambda x: x["date"])[0]
        # An issue on the night is evidence either way; a night with none is
        # "improved" only if the coverage check was watching it (A-19).
        if not (o["issues"] or 0) and o["date"] not in watched_dates(restaurant_id, o["date"], o["date"], db_path):
            continue
        verdict = "improved" if not (o["issues"] or 0) else "worsened"
        if _rl.record(restaurant_id, schedule_rec_key(a["kind"], a["key"]), "outcome",
                      meta={"verdict": verdict, "date": o["date"], "issues": o["issues"] or 0},
                      source_ref=f"night:{o['date']}:{part}", db_path=db_path):
            n += 1
    return n


def schedule_rec_key(kind, text) -> str:
    """The rec_ledger key of one Shift Quality recommendation."""
    import rec_ledger as _rl
    return _rl.rec_key("schedule_" + (kind or "other"), (text or "")[:120])


def suppressed_kinds(restaurant_id, db_path=DB_PATH) -> set:
    """Recommendation kinds this owner has plainly declined: shown at least
    SUPPRESS_AFTER_SHOWN times and never accepted, or dismissed "not for us"
    at least twice and never accepted — counted since the owner last asked
    for the kind back. Kinds about whether a shift is safe to run
    (shift_quality.PROTECTED_REC_KINDS) are never suppressed.

    "Not for us" used to change nothing (only shown and accepted counted),
    and a suppressed kind could never come back: its recommendations were
    neither shown nor stored, so neither a button nor an edit could accept
    one."""
    from shift_quality import PROTECTED_REC_KINDS
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT e.kind, COUNT(DISTINCT CASE WHEN e.action='shown' THEN e.key || '|' || date(e.created_at) END) AS shown, "
            "SUM(e.action='accepted') AS acc, SUM(e.action='dismissed') AS dis FROM schedule_recommendation_events e "
            "WHERE e.restaurant_id=? AND e.created_at > COALESCE((SELECT MAX(r.created_at) FROM schedule_recommendation_events r "
            "  WHERE r.restaurant_id=e.restaurant_id AND r.kind=e.kind AND r.action='restored'), '') "
            "GROUP BY e.kind", (restaurant_id,)).fetchall()
    except Exception as e:
        print(f"[schedule_intel] suppressed_kinds failed: {e}")
        return set()
    finally:
        conn.close()
    out = set()
    for r in rows:
        if r["kind"] in PROTECTED_REC_KINDS or (r["acc"] or 0):
            continue
        if (r["shown"] or 0) >= SUPPRESS_AFTER_SHOWN or (r["dis"] or 0) >= 2:
            out.add(r["kind"])
    return out


# ── learned-pattern dismissals ────────────────────────────────────────────

def pattern_key(p: dict) -> str:
    """kind|employee|day|daypart — and, for the kinds that are about a role
    rather than a person (retimes, headcount, role changes, leader swaps),
    the role, the role it was changed from and the time, so two roles on one
    night are dismissed separately. The original moved_off / moved_on keys
    carry none of those and read exactly as they always have."""
    key = f"{p.get('kind')}|{(p.get('employee') or '').lower()}|{p.get('day')}|{p.get('daypart')}"
    extra = [str(p.get(f) or "").strip().lower() for f in ("role", "was_role", "time")]
    if any(extra):
        key += "|" + "|".join(extra)
    return key


def dismissed_patterns(restaurant_id, db_path=DB_PATH) -> set:
    conn = get_conn(db_path)
    try:
        return {r["key"] for r in conn.execute("SELECT key FROM schedule_pattern_dismissals WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def dismiss_pattern(restaurant_id, key: str, actor=None, db_path=DB_PATH) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO schedule_pattern_dismissals (restaurant_id, key, dismissed_by) VALUES (?,?,?)",
                     (restaurant_id, str(key)[:200], (actor or "")[:120] or None))
        conn.commit()
    finally:
        conn.close()


def restore_pattern(restaurant_id, key: str, db_path=DB_PATH) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM schedule_pattern_dismissals WHERE restaurant_id=? AND key=?", (restaurant_id, str(key)[:200]))
        conn.commit()
    finally:
        conn.close()


def init_schedule_intel(db_path: str = DB_PATH):
    """Tables for the outcome record, the recommendation ledger and the
    owner's pattern dismissals — created at boot like every other table."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_outcomes (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        date           TEXT    NOT NULL,
        daypart        TEXT    NOT NULL,
        hours          REAL,
        people         INTEGER,
        sales          REAL,
        labor_pct      REAL,
        issues         INTEGER NOT NULL DEFAULT 0,
        review_rating  REAL,
        reviews        INTEGER NOT NULL DEFAULT 0,
        recorded_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(history_id, date, daypart)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_outcomes_rest ON schedule_outcomes(restaurant_id, date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_recommendation_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        kind           TEXT    NOT NULL,
        key            TEXT,
        action         TEXT    NOT NULL,          -- shown | accepted | dismissed
        actor          TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rec_events ON schedule_recommendation_events(restaurant_id, kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_rec_events_created "
                 "ON schedule_recommendation_events(created_at)")          # ops.prune_ledgers (DATA-40)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_pattern_dismissals (
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        key            TEXT    NOT NULL,
        dismissed_by   TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_first_seen (
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name  TEXT    NOT NULL,
        first_seen     TEXT    NOT NULL,
        shifts_seen    INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, employee_name)
    )""")
    conn.commit()
    conn.close()


def remember_tenure(restaurant_id, shifts: list, db_path=DB_PATH) -> None:
    """Keep the earliest date and the most shifts ever seen for each name,
    so tenure survives the rolling upload window."""
    if not shifts:
        return
    first, count = {}, {}
    for s in shifts:
        n = (s.get("employee") or "").strip()
        d = (s.get("date") or "")[:10]
        if not n or len(d) != 10:
            continue
        count[n] = count.get(n, 0) + 1
        if n not in first or d < first[n]:
            first[n] = d
    conn = get_conn(db_path)
    try:
        for n in count:
            conn.execute(
                "INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen) VALUES (?,?,?,?) "
                "ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen), "
                "shifts_seen=MAX(shifts_seen, excluded.shifts_seen), updated_at=datetime('now')",
                (restaurant_id, n, first[n], count[n]))
        conn.commit()
    except Exception as e:
        import ops
        ops.capture(e, job="remember_tenure", context=f"restaurant_id={restaurant_id}")
    finally:
        conn.close()


def tenure(restaurant_id, from_csv: dict, db_path=DB_PATH) -> dict:
    """{name: shifts} — the larger of what the current upload shows and what
    has been remembered; a person seen since March keeps their tenure when
    the upload window rolls past March."""
    out = dict(from_csv or {})
    conn = get_conn(db_path)
    try:
        for r in conn.execute("SELECT employee_name, shifts_seen, first_seen FROM staff_first_seen WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            n = r["employee_name"]
            seen = int(r["shifts_seen"] or 0)
            # At least as many shifts as were ever counted for them — never
            # a guess upward from a hire date.
            out[n] = max(out.get(n, 0), seen)
    except Exception:
        pass
    finally:
        conn.close()
    return out


def history_weeks(restaurant_id) -> int:
    """How many weeks the shift history spans — an 'unusual day' claim needs a few."""
    from models import _cached_shifts
    try:
        ds = sorted({(s.get("date") or "")[:10] for s in _cached_shifts(restaurant_id) if s.get("date")})
        if len(ds) < 2:
            return 0
        a, b = datetime.strptime(ds[0], "%Y-%m-%d"), datetime.strptime(ds[-1], "%Y-%m-%d")
        return max(0, (b - a).days // 7)
    except Exception:
        return 0
